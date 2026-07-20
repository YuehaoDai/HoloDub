"""Post-dub ASR validation for completed HoloDub episodes.

The command is read-only with respect to the application database. It writes
reports under the requested output directory and, when --run-asr is set,
invokes the local ML container to transcribe generated TTS segment audio.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import textwrap
import wave
from array import array
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


WEIRD_ASR_RE = re.compile(
    r"\u7d22\u9a6c\u91cc|"  # Somali
    r"\u9501\u739b\u4e3d|"  # suo ma li variant
    r"\u82cf\u9a6c\u91cc|"  # su ma li variant
    r"\u9501\u9a6c\u91cc|"  # suo ma li variant
    r"\u9501\u9a6c\u529b|"  # suo ma li variant
    r"\u7d22\u739b\u4e3d|"  # suo ma li variant
    r"\u9a6c\u91cc\u5148\u4ee4|"  # Mali shilling
    r"\u5148\u4ee4"  # shilling
)
PUNCT_RE = re.compile(r"[\s,.\!?;:，。！？、；：（）()《》“”\"'`~\-]+")
MIXED_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:S\d+|[A-Za-z]+[A-Za-z0-9]*\d+[A-Za-z0-9]*|[A-Za-z]{2,})(?![A-Za-z0-9])")

DEFAULT_LOW_SIMILARITY = 0.72
DEFAULT_CLIPPED_FRACTION = 0.0001
DEFAULT_CHUNK_ASR_MIN_MS = 20_000
DEFAULT_CHUNK_ASR_WINDOW_MS = 8_000


def run(cmd: list[str], *, cwd: Path, timeout: int) -> str:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    return completed.stdout.strip()


def query_json(
    *,
    repo_root: Path,
    postgres_container: str,
    postgres_user: str,
    postgres_db: str,
    sql: str,
) -> list[dict[str, Any]]:
    wrapped = f"select coalesce(json_agg(row_to_json(t)), '[]'::json) from ({sql}) t;"
    output = run(
        [
            "docker",
            "exec",
            postgres_container,
            "psql",
            "-U",
            postgres_user,
            "-d",
            postgres_db,
            "-t",
            "-A",
            "-c",
            wrapped,
        ],
        cwd=repo_root,
        timeout=120,
    )
    return json.loads(output or "[]")


def load_episode_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    sql = f"""
        select
          e.id as episode_id,
          e.name as episode_name,
          e.output_rel_path as episode_output_relpath,
          e.episode_judge_score,
          e.episode_judge_meta,
          j.id as job_id,
          j.chapter_ordinal,
          j.chapter_judge_score,
          j.chapter_judge_meta,
          s.id as segment_id,
          s.ordinal as segment_ordinal,
          s.start_ms,
          s.end_ms,
          s.original_duration_ms,
          s.source_text,
          s.target_text,
          s.tts_audio_rel_path,
          s.tts_duration_ms,
          s.status,
          s.meta,
          s.judge_score,
          s.judge_meta
        from episodes e
        join jobs j on j.episode_id = e.id
        join segments s on s.job_id = j.id
        where e.id = {int(args.episode_id)}
        order by j.chapter_ordinal asc, s.ordinal asc
    """
    return query_json(
        repo_root=args.repo_root,
        postgres_container=args.postgres_container,
        postgres_user=args.postgres_user,
        postgres_db=args.postgres_db,
        sql=sql,
    )


def parse_jsonish(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def expected_text_for_segment(row: dict[str, Any]) -> tuple[str, str]:
    meta = parse_jsonish(row.get("meta"))
    spoken = str(meta.get("tts_spoken_text") or "").strip()
    if spoken:
        return spoken, "tts_spoken_text"
    return str(row.get("target_text") or ""), "target_text"


def build_manifest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    for row in rows:
        relpath = row.get("tts_audio_rel_path") or ""
        if not relpath:
            continue
        expected, expected_source = expected_text_for_segment(row)
        manifest.append(
            {
                "kind": "tts_segment_full",
                "label": f"c{int(row['chapter_ordinal']):02d}_s{int(row['segment_ordinal']):04d}",
                "episode_id": row["episode_id"],
                "job_id": row["job_id"],
                "segment_id": row["segment_id"],
                "chapter_ordinal": row["chapter_ordinal"],
                "segment_ordinal": row["segment_ordinal"],
                "audio_relpath": relpath,
                "source_language": "zh",
                "start_ms": 0,
                "end_ms": int(row.get("tts_duration_ms") or row.get("original_duration_ms") or 1000),
                "expected_text": expected,
                "expected_source": expected_source,
                "target_text": row.get("target_text") or "",
                "source_text": row.get("source_text") or "",
                "judge_score": row.get("judge_score"),
                "judge_meta": row.get("judge_meta"),
                "meta": row.get("meta") or {},
            }
        )
    return manifest


def write_asr_batch_script(output_dir: Path) -> Path:
    script = output_dir / "asr_batch.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time
            import urllib.error
            import urllib.request

            ASR_URL = "http://127.0.0.1:8000/asr/transcribe_segment"

            def transcribe(item):
                payload = {
                    "audio_relpath": item["audio_relpath"],
                    "source_language": item.get("source_language", "zh"),
                    "start_ms": int(item["start_ms"]),
                    "end_ms": int(item["end_ms"]),
                }
                request = urllib.request.Request(
                    ASR_URL,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                started = time.time()
                with urllib.request.urlopen(request, timeout=300) as response:
                    body = json.loads(response.read().decode("utf-8"))
                return {
                    **item,
                    "ok": True,
                    "elapsed_sec": round(time.time() - started, 3),
                    "asr_text": body.get("text", ""),
                    "diagnostics": body.get("diagnostics", []),
                }

            def main():
                if len(sys.argv) != 3:
                    raise SystemExit("usage: asr_batch.py <manifest.json> <output.json>")
                with open(sys.argv[1], "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                results = []
                for index, item in enumerate(manifest, 1):
                    try:
                        result = transcribe(item)
                    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
                        result = {
                            **item,
                            "ok": False,
                            "elapsed_sec": 0,
                            "asr_text": "",
                            "diagnostics": [],
                            "error": str(exc),
                        }
                    results.append(result)
                    with open(sys.argv[2], "w", encoding="utf-8") as handle:
                        json.dump(results, handle, ensure_ascii=False, indent=2)
                    print(f"[{index}/{len(manifest)}] {item.get('label')}: ok={result['ok']}", flush=True)

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


def write_asr_chunk_retry_script(output_dir: Path) -> Path:
    script = output_dir / "asr_chunk_retry.py"
    script.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time
            import urllib.error
            import urllib.request

            ASR_URL = "http://127.0.0.1:8000/asr/transcribe_segment"

            def transcribe_window(item, start_ms, end_ms):
                payload = {
                    "audio_relpath": item["audio_relpath"],
                    "source_language": item.get("source_language", "zh"),
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                }
                request = urllib.request.Request(
                    ASR_URL,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=300) as response:
                    return json.loads(response.read().decode("utf-8"))

            def transcribe_chunked(item):
                started = time.time()
                window_ms = int(item.get("chunk_window_ms") or 8000)
                start_ms = int(item.get("start_ms", 0))
                end_ms = int(item["end_ms"])
                chunks = []
                diagnostics = []
                ok = True
                current = start_ms
                while current < end_ms:
                    next_end = min(end_ms, current + window_ms)
                    try:
                        body = transcribe_window(item, current, next_end)
                        text = body.get("text", "")
                        if text.strip():
                            chunks.append(text.strip())
                        diagnostics.extend(body.get("diagnostics", []))
                    except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as exc:
                        ok = False
                        diagnostics.append(f"chunk_asr_failed:{current}-{next_end}:{exc}")
                    current = next_end
                return {
                    **item,
                    "ok": ok,
                    "elapsed_sec": round(time.time() - started, 3),
                    "asr_text": " ".join(chunks),
                    "diagnostics": [
                        f"chunked_asr_retry window_ms={window_ms} chunks={len(chunks)}",
                        *diagnostics,
                    ],
                }

            def main():
                if len(sys.argv) != 3:
                    raise SystemExit("usage: asr_chunk_retry.py <manifest.json> <output.json>")
                with open(sys.argv[1], "r", encoding="utf-8") as handle:
                    manifest = json.load(handle)
                results = []
                for index, item in enumerate(manifest, 1):
                    result = transcribe_chunked(item)
                    results.append(result)
                    with open(sys.argv[2], "w", encoding="utf-8") as handle:
                        json.dump(results, handle, ensure_ascii=False, indent=2)
                    print(f"[chunk {index}/{len(manifest)}] {item.get('label')}: ok={result['ok']}", flush=True)

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


def data_container_path(data_root: Path, host_path: Path) -> str:
    rel = host_path.resolve().relative_to(data_root.resolve())
    return "/data/" + rel.as_posix()


def run_asr_batch(args: argparse.Namespace, manifest: list[dict[str, Any]], output_dir: Path) -> list[dict[str, Any]]:
    manifest_path = output_dir / "asr_manifest.json"
    raw_path = output_dir / "asr_results_raw.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    existing: list[dict[str, Any]] = []
    if raw_path.exists() and raw_path.stat().st_size:
        existing = json.loads(raw_path.read_text(encoding="utf-8"))
    elif (output_dir / "asr_results_remaining_raw.json").exists():
        partial_path = output_dir / "asr_results_remaining_raw.json"
        if partial_path.stat().st_size:
            existing = json.loads(partial_path.read_text(encoding="utf-8"))
    done = {row.get("label") for row in existing}
    remaining = [item for item in manifest if item.get("label") not in done]
    if remaining:
        remaining_manifest = output_dir / "asr_manifest_remaining.json"
        remaining_raw = output_dir / "asr_results_remaining_raw.json"
        remaining_manifest.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        write_asr_batch_script(output_dir)
        run(
            [
                "docker",
                "exec",
                args.ml_container,
                "python3",
                data_container_path(args.data_root, output_dir / "asr_batch.py"),
                data_container_path(args.data_root, remaining_manifest),
                data_container_path(args.data_root, remaining_raw),
            ],
            cwd=args.repo_root,
            timeout=args.asr_timeout_sec,
        )
        existing.extend(json.loads(remaining_raw.read_text(encoding="utf-8")))
        existing.sort(key=lambda row: (int(row["chapter_ordinal"]), int(row["segment_ordinal"])))
        raw_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else []


def run_asr_chunk_retry(
    args: argparse.Namespace,
    retry_items: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    if not retry_items:
        return []
    retry_manifest = output_dir / "asr_chunk_retry_manifest.json"
    retry_raw = output_dir / "asr_chunk_retry_results.json"
    retry_manifest.write_text(json.dumps(retry_items, ensure_ascii=False, indent=2), encoding="utf-8")
    write_asr_chunk_retry_script(output_dir)
    run(
        [
            "docker",
            "exec",
            args.ml_container,
            "python3",
            data_container_path(args.data_root, output_dir / "asr_chunk_retry.py"),
            data_container_path(args.data_root, retry_manifest),
            data_container_path(args.data_root, retry_raw),
        ],
        cwd=args.repo_root,
        timeout=args.asr_timeout_sec,
    )
    return json.loads(retry_raw.read_text(encoding="utf-8")) if retry_raw.exists() else []


def normalize_for_compare(text: str) -> str:
    return PUNCT_RE.sub("", text or "").lower()


def similarity(expected: str, actual: str) -> float:
    exp = normalize_for_compare(expected)
    got = normalize_for_compare(actual)
    if not exp and not got:
        return 1.0
    if not exp or not got:
        return 0.0
    return round(SequenceMatcher(None, exp, got).ratio(), 4)


def wav_metrics(data_root: Path, relpath: str) -> dict[str, Any]:
    path = data_root / relpath
    metrics: dict[str, Any] = {"relpath": relpath, "exists": path.exists()}
    if not path.exists():
        return metrics
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            frames = wav.getnframes()
            raw = wav.readframes(frames)
    except wave.Error as exc:
        metrics["error"] = str(exc)
        return metrics
    metrics.update(
        {
            "channels": channels,
            "sample_rate": sample_rate,
            "sample_width": sample_width,
            "duration_sec": round(frames / sample_rate, 3) if sample_rate else 0,
        }
    )
    if sample_width != 2 or not raw:
        return metrics
    samples = array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return metrics
    sample_count = len(samples)
    sum_sq = sum(float(s) * float(s) for s in samples)
    rms = math.sqrt(sum_sq / sample_count) / 32768.0
    peak = max(abs(s) for s in samples) / 32768.0
    clipped = sum(1 for s in samples if abs(s) >= 32760)
    metrics.update(
        {
            "rms": round(rms, 6),
            "rms_dbfs": round(20 * math.log10(max(rms, 1e-9)), 2),
            "peak": round(peak, 6),
            "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2),
            "clipped_samples": clipped,
            "clipped_fraction": round(clipped / sample_count, 8),
        }
    )
    return metrics


def judge_verdict(row: dict[str, Any]) -> str:
    meta = parse_jsonish(row.get("judge_meta"))
    verdict = meta.get("verdict")
    return str(verdict or "")


def analyze_results(
    *,
    rows: list[dict[str, Any]],
    asr_results: list[dict[str, Any]],
    data_root: Path,
    low_similarity: float = DEFAULT_LOW_SIMILARITY,
    clipped_fraction: float = DEFAULT_CLIPPED_FRACTION,
) -> list[dict[str, Any]]:
    row_by_segment = {int(row["segment_id"]): row for row in rows}
    analyzed: list[dict[str, Any]] = []
    for result in asr_results:
        segment_id = int(result["segment_id"])
        row = row_by_segment.get(segment_id, {})
        expected = result.get("expected_text") or expected_text_for_segment(row)[0]
        actual = result.get("asr_text") or ""
        sim = similarity(expected, actual)
        meta = parse_jsonish(row.get("meta"))
        audio = wav_metrics(data_root, result.get("audio_relpath") or "")
        flags: list[str] = []
        weird = sorted(set(WEIRD_ASR_RE.findall(actual)))
        mixed = sorted(set(MIXED_TOKEN_RE.findall(expected)))
        if not result.get("ok", False):
            flags.append("asr_failed")
        if not actual.strip():
            flags.append("empty_asr")
        if weird:
            flags.append("weird_asr_terms:" + ",".join(weird))
        if sim < low_similarity:
            flags.append(f"low_similarity:{sim}")
        if mixed:
            flags.append("expected_mixed_tokens:" + ",".join(mixed[:12]))
        if not audio.get("exists"):
            flags.append("missing_tts_audio")
        if float(audio.get("clipped_fraction") or 0) >= clipped_fraction:
            flags.append(f"audio_clipping:{audio.get('clipped_fraction')}")
        verdict = judge_verdict(row)
        judge_score = row.get("judge_score")
        if verdict == "retry":
            flags.append("judge_retry")
        voice_source = str(meta.get("voice_config_source") or "")
        if voice_source == "global_default_fallback":
            flags.append("global_default_voice")
        analyzed.append(
            {
                **result,
                "expected_text": expected,
                "similarity": sim,
                "flags": flags,
                "audio_metrics": audio,
                "voice_config_source": voice_source,
                "judge_verdict": verdict,
                "judge_score": judge_score,
            }
        )
    return analyzed


def retry_low_similarity_with_chunked_asr(
    *,
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
    asr_results: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    analyzed = analyze_results(rows=rows, asr_results=asr_results, data_root=args.data_root)
    retry_items: list[dict[str, Any]] = []
    for row in analyzed:
        duration_ms = int(row.get("end_ms") or 0) - int(row.get("start_ms") or 0)
        is_low = any(flag.startswith("low_similarity") for flag in row["flags"])
        if is_low and duration_ms >= args.chunk_asr_min_ms:
            retry_items.append(
                {
                    **row,
                    "chunk_window_ms": args.chunk_asr_window_ms,
                    "full_asr_text": row.get("asr_text") or "",
                    "full_similarity": row.get("similarity"),
                }
            )
    if not retry_items:
        return asr_results, []

    chunk_results = run_asr_chunk_retry(args, retry_items, output_dir)
    chunk_by_segment = {int(row["segment_id"]): row for row in chunk_results}
    improved: list[dict[str, Any]] = []
    updated_results: list[dict[str, Any]] = []
    for result in asr_results:
        segment_id = int(result["segment_id"])
        chunk = chunk_by_segment.get(segment_id)
        if not chunk:
            updated_results.append(result)
            continue
        expected = chunk.get("expected_text") or result.get("expected_text") or ""
        old_sim = similarity(expected, result.get("asr_text") or "")
        new_sim = similarity(expected, chunk.get("asr_text") or "")
        if chunk.get("ok", False) and new_sim > old_sim:
            replacement = {
                **result,
                "full_asr_text": result.get("asr_text") or "",
                "full_similarity": old_sim,
                "asr_text": chunk.get("asr_text") or "",
                "elapsed_sec": chunk.get("elapsed_sec", result.get("elapsed_sec", 0)),
                "diagnostics": [
                    *(result.get("diagnostics") or []),
                    *(chunk.get("diagnostics") or []),
                ],
            }
            updated_results.append(replacement)
            improved.append(
                {
                    "label": result.get("label"),
                    "segment_id": segment_id,
                    "old_similarity": old_sim,
                    "new_similarity": new_sim,
                }
            )
        else:
            updated_results.append(result)
    return updated_results, improved


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sims = [float(row["similarity"]) for row in rows if row.get("ok")]
    by_chapter: dict[str, dict[str, Any]] = {}
    for row in rows:
        chapter = f"c{int(row['chapter_ordinal']):02d}"
        bucket = by_chapter.setdefault(chapter, {"count": 0, "sum_similarity": 0.0, "low": 0, "weird": 0})
        bucket["count"] += 1
        bucket["sum_similarity"] += float(row["similarity"])
        if any(flag.startswith("low_similarity") for flag in row["flags"]):
            bucket["low"] += 1
        if any(flag.startswith("weird_asr_terms") for flag in row["flags"]):
            bucket["weird"] += 1
    for bucket in by_chapter.values():
        bucket["mean_similarity"] = round(bucket["sum_similarity"] / bucket["count"], 4) if bucket["count"] else None
        del bucket["sum_similarity"]

    def count_flag(prefix: str) -> int:
        return sum(1 for row in rows if any(flag.startswith(prefix) for flag in row["flags"]))

    voice_source_counts: dict[str, int] = {}
    for row in rows:
        source = row.get("voice_config_source") or ""
        if source:
            voice_source_counts[source] = voice_source_counts.get(source, 0) + 1
    voice_sources = sorted(voice_source_counts)
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "total_segments": len(rows),
        "ok_segments": sum(1 for row in rows if row.get("ok")),
        "mean_similarity": round(statistics.fmean(sims), 4) if sims else None,
        "min_similarity": min(sims) if sims else None,
        "low_similarity_count": count_flag("low_similarity"),
        "very_low_similarity_count": sum(1 for row in rows if float(row["similarity"]) < 0.5),
        "empty_asr_count": count_flag("empty_asr"),
        "weird_asr_count": count_flag("weird_asr_terms"),
        "mixed_token_segment_count": count_flag("expected_mixed_tokens"),
        "audio_missing_count": count_flag("missing_tts_audio"),
        "audio_clipped_count": count_flag("audio_clipping"),
        "judge_retry_count": count_flag("judge_retry"),
        "global_default_voice_count": count_flag("global_default_voice"),
        "voice_config_sources": voice_sources,
        "voice_config_source_counts": voice_source_counts,
        "by_chapter": by_chapter,
    }


def gate_summary(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks = [
        ("weird_asr_count", summary["weird_asr_count"], 0),
        ("empty_asr_count", summary["empty_asr_count"], 0),
        ("audio_missing_count", summary["audio_missing_count"], 0),
        ("global_default_voice_count", summary["global_default_voice_count"], 0),
        ("low_similarity_count", summary["low_similarity_count"], args.max_low_similarity),
        ("judge_retry_count", summary["judge_retry_count"], 0),
    ]
    results = []
    passed = True
    for name, actual, maximum in checks:
        ok = actual <= maximum
        passed = passed and ok
        results.append({"metric": name, "actual": actual, "max": maximum, "pass": ok})
    return {"pass": passed, "checks": results}


def write_markdown(
    summary: dict[str, Any],
    gate: dict[str, Any],
    rows: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
    chunked_asr_improvements: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = ["# Post-Dub ASR Validation", ""]
    lines.append("## Summary")
    lines.append("")
    for key in [
        "total_segments",
        "ok_segments",
        "mean_similarity",
        "min_similarity",
        "low_similarity_count",
        "empty_asr_count",
        "weird_asr_count",
        "audio_missing_count",
        "audio_clipped_count",
        "judge_retry_count",
        "global_default_voice_count",
    ]:
        lines.append(f"- {key}: {summary[key]}")
    if summary.get("voice_config_source_counts"):
        lines.append(f"- voice_config_source_counts: {summary['voice_config_source_counts']}")
    lines.append(f"- gate: {'PASS' if gate['pass'] else 'FAIL'}")
    lines.append("")
    if baseline:
        lines.append("## Baseline Delta")
        lines.append("")
        base_summary = baseline.get("summary", {})
        for key in ["mean_similarity", "low_similarity_count", "empty_asr_count", "weird_asr_count", "audio_clipped_count"]:
            lines.append(f"- {key}: baseline={base_summary.get(key)} current={summary.get(key)}")
        lines.append("")
    lines.append("## Chapter Breakdown")
    lines.append("")
    for chapter, bucket in sorted(summary["by_chapter"].items()):
        lines.append(
            f"- {chapter}: n={bucket['count']}, mean_similarity={bucket['mean_similarity']}, "
            f"low={bucket['low']}, weird={bucket['weird']}"
        )
    lines.append("")
    lines.append("## Gate Checks")
    lines.append("")
    for check in gate["checks"]:
        lines.append(f"- {check['metric']}: {check['actual']} <= {check['max']} -> {'PASS' if check['pass'] else 'FAIL'}")
    lines.append("")
    if chunked_asr_improvements:
        lines.append("## Chunked ASR Retry")
        lines.append("")
        for item in chunked_asr_improvements:
            lines.append(
                f"- `{item['label']}` segment={item['segment_id']}: "
                f"{item['old_similarity']} -> {item['new_similarity']}"
            )
        lines.append("")
    lines.append("## Worst Similarity")
    lines.append("")
    for row in sorted(rows, key=lambda r: float(r["similarity"]))[:30]:
        lines.append(
            f"- `{row['label']}` sim={row['similarity']} flags={','.join(row['flags'])}\n"
            f"  expected: {str(row.get('expected_text', ''))[:220]}\n"
            f"  asr: {str(row.get('asr_text', ''))[:220]}"
        )
    lines.append("")
    lines.append("## Weird ASR Terms")
    lines.append("")
    weird_rows = [row for row in rows if any(flag.startswith("weird_asr_terms") for flag in row["flags"])]
    if not weird_rows:
        lines.append("- none")
    for row in weird_rows:
        lines.append(
            f"- `{row['label']}` sim={row['similarity']} flags={','.join(row['flags'])}\n"
            f"  expected: {str(row.get('expected_text', ''))[:220]}\n"
            f"  asr: {str(row.get('asr_text', ''))[:220]}"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--postgres-container", default="holodub-postgres-1")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    parser.add_argument("--run-asr", action="store_true")
    parser.add_argument("--asr-timeout-sec", type=int, default=7200)
    parser.add_argument("--disable-chunked-asr-retry", action="store_true")
    parser.add_argument("--chunk-asr-min-ms", type=int, default=DEFAULT_CHUNK_ASR_MIN_MS)
    parser.add_argument("--chunk-asr-window-ms", type=int, default=DEFAULT_CHUNK_ASR_WINDOW_MS)
    parser.add_argument("--baseline-json", type=Path)
    parser.add_argument("--max-low-similarity", type=int, default=2)
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    if not args.data_root.is_absolute():
        args.data_root = (args.repo_root / args.data_root).resolve()
    if args.output_dir is None:
        args.output_dir = args.data_root / "validation" / f"episode{args.episode_id}"
    elif not args.output_dir.is_absolute():
        args.output_dir = (args.repo_root / args.output_dir).resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = load_episode_rows(args)
    if not rows:
        raise SystemExit(f"episode {args.episode_id} returned no segments")
    manifest = build_manifest(rows)
    (args.output_dir / "episode_segments.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "asr_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.run_asr:
        asr_results = run_asr_batch(args, manifest, args.output_dir)
    else:
        raw_path = args.output_dir / "asr_results_raw.json"
        if not raw_path.exists():
            raise SystemExit("--run-asr was not set and asr_results_raw.json does not exist")
        asr_results = json.loads(raw_path.read_text(encoding="utf-8"))
    chunked_asr_improvements: list[dict[str, Any]] = []
    if args.run_asr and not args.disable_chunked_asr_retry:
        asr_results, chunked_asr_improvements = retry_low_similarity_with_chunked_asr(
            args=args,
            rows=rows,
            asr_results=asr_results,
            output_dir=args.output_dir,
        )
        (args.output_dir / "asr_results_raw.json").write_text(
            json.dumps(asr_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    analyzed = analyze_results(rows=rows, asr_results=asr_results, data_root=args.data_root)
    summary = summarize(analyzed)
    gate = gate_summary(summary, args)
    baseline = json.loads(args.baseline_json.read_text(encoding="utf-8")) if args.baseline_json else None
    payload = {
        "summary": summary,
        "gate": gate,
        "rows": analyzed,
        "chunked_asr_improvements": chunked_asr_improvements,
    }
    (args.output_dir / "dub_asr_validation.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "dub_asr_validation.md").write_text(
        write_markdown(summary, gate, analyzed, baseline, chunked_asr_improvements),
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary, "gate": gate}, ensure_ascii=False, indent=2))
    if args.fail_on_gate and not gate["pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
