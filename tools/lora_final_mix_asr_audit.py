"""ASR-audit a completed episode's final mixed audio against target text.

This differs from tests/quality/dub_asr_validation.py: it transcribes windows
from the final episode media (for example final.mp4), not the per-segment TTS
source files. The comparison target is the final spoken Chinese text
(`meta.tts_spoken_text` when present, otherwise `target_text`).
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


WEIRD_ASR_RE = re.compile(
    r"\u7d22\u9a6c\u91cc|"  # Somali
    r"\u9501\u739b\u4e3d|"
    r"\u82cf\u9a6c\u91cc|"
    r"\u9501\u9a6c\u91cc|"
    r"\u9501\u9a6c\u529b|"
    r"\u7d22\u739b\u4e3d|"
    r"\u9a6c\u91cc\u5148\u4ee4|"
    r"\u5148\u4ee4"  # shilling
)
MIXED_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:S\d+|[A-Za-z]+[A-Za-z0-9]*\d+[A-Za-z0-9]*|[A-Za-z]{2,})(?![A-Za-z0-9])"
)
COMPARE_DROP_RE = re.compile(r"[\s\W_]+", re.UNICODE)

DEFAULT_LOW_SIMILARITY = 0.80
DEFAULT_VERY_LOW_SIMILARITY = 0.60
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


def load_episode_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    sql = f"""
        select
          e.id as episode_id,
          e.name as episode_name,
          e.duration_ms as episode_duration_ms,
          e.output_rel_path as episode_output_relpath,
          e.chapters_manifest_rel_path,
          e.episode_judge_score,
          e.episode_judge_meta,
          j.id as job_id,
          j.chapter_ordinal,
          j.chapter_start_ms,
          j.chapter_end_ms,
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


def expected_text_for_segment(row: dict[str, Any]) -> tuple[str, str]:
    meta = parse_jsonish(row.get("meta"))
    spoken = str(meta.get("tts_spoken_text") or "").strip()
    if spoken:
        return spoken, "tts_spoken_text"
    return str(row.get("target_text") or ""), "target_text"


def segment_abs_start(row: dict[str, Any]) -> int:
    chapter_start = row.get("asr_chapter_start_ms")
    if chapter_start is None:
        chapter_start = row.get("chapter_start_ms")
    return int(chapter_start or 0) + int(row.get("start_ms") or 0)


def segment_abs_end(row: dict[str, Any]) -> int:
    chapter_start = row.get("asr_chapter_start_ms")
    if chapter_start is None:
        chapter_start = row.get("chapter_start_ms")
    return int(chapter_start or 0) + int(row.get("end_ms") or 0)


def sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (int(row.get("chapter_ordinal") or 0), int(row.get("segment_ordinal") or 0)))


def window_for_row(
    row: dict[str, Any],
    next_row: dict[str, Any] | None,
    *,
    episode_duration_ms: int | None,
    pre_pad_ms: int,
    post_pad_ms: int,
    next_gap_ms: int,
    min_window_ms: int,
) -> dict[str, Any]:
    abs_start = segment_abs_start(row)
    abs_end = segment_abs_end(row)
    tts_duration_ms = int(row.get("tts_duration_ms") or 0)
    planned_end = max(abs_end, abs_start + tts_duration_ms if tts_duration_ms > 0 else abs_end)
    window_start = max(0, abs_start - pre_pad_ms)
    raw_window_end = planned_end + post_pad_ms

    next_abs_start = segment_abs_start(next_row) if next_row else None
    bounded_by_next_segment = False
    if next_abs_start is not None and next_abs_start > window_start:
        hard_next_end = max(window_start + 1, next_abs_start - next_gap_ms)
        if raw_window_end > hard_next_end:
            raw_window_end = hard_next_end
            bounded_by_next_segment = True
    if episode_duration_ms:
        raw_window_end = min(raw_window_end, int(episode_duration_ms))
    window_end = max(window_start + min_window_ms, raw_window_end)
    if next_abs_start is not None and next_abs_start > window_start:
        window_end = min(window_end, max(window_start + 1, next_abs_start - next_gap_ms))
    if episode_duration_ms:
        window_end = min(window_end, int(episode_duration_ms))
    if window_end <= window_start:
        window_end = window_start + max(1, min_window_ms)
    return {
        "absolute_start_ms": abs_start,
        "absolute_end_ms": abs_end,
        "window_start_ms": int(window_start),
        "window_end_ms": int(window_end),
        "window_duration_ms": int(window_end - window_start),
        "next_segment_start_ms": next_abs_start,
        "bounded_by_next_segment": bounded_by_next_segment,
    }


def build_manifest(
    rows: list[dict[str, Any]],
    *,
    audio_relpath_override: str | None = None,
    chapter_start_overrides_ms: dict[int, int] | None = None,
    chapter_timing_source: str = "db",
    pre_pad_ms: int = 0,
    post_pad_ms: int = 250,
    next_gap_ms: int = 50,
    min_window_ms: int = 500,
) -> list[dict[str, Any]]:
    ordered = []
    overrides = chapter_start_overrides_ms or {}
    for row in sort_rows(rows):
        row_copy = dict(row)
        ordinal = int(row_copy.get("chapter_ordinal") or 0)
        if ordinal in overrides:
            row_copy["asr_chapter_start_ms"] = int(overrides[ordinal])
        ordered.append(row_copy)
    if not ordered:
        return []
    final_relpath = str(audio_relpath_override or ordered[0].get("episode_output_relpath") or "").strip()
    episode_duration_ms = ordered[0].get("episode_duration_ms")
    episode_duration = int(episode_duration_ms) if episode_duration_ms else None
    manifest: list[dict[str, Any]] = []
    for index, row in enumerate(ordered):
        expected, expected_source = expected_text_for_segment(row)
        window = window_for_row(
            row,
            ordered[index + 1] if index + 1 < len(ordered) else None,
            episode_duration_ms=episode_duration,
            pre_pad_ms=pre_pad_ms,
            post_pad_ms=post_pad_ms,
            next_gap_ms=next_gap_ms,
            min_window_ms=min_window_ms,
        )
        manifest.append(
            {
                "kind": "final_mix_segment_window",
                "label": f"c{int(row['chapter_ordinal']):02d}_s{int(row['segment_ordinal']):04d}_seg{int(row['segment_id'])}",
                "episode_id": row["episode_id"],
                "episode_name": row.get("episode_name") or "",
                "job_id": row["job_id"],
                "segment_id": row["segment_id"],
                "chapter_ordinal": row["chapter_ordinal"],
                "segment_ordinal": row["segment_ordinal"],
                "audio_relpath": final_relpath,
                "source_language": "zh",
                "start_ms": window["window_start_ms"],
                "end_ms": window["window_end_ms"],
                "expected_text": expected,
                "expected_source": expected_source,
                "target_text": row.get("target_text") or "",
                "source_text": row.get("source_text") or "",
                "segment_start_ms": row.get("start_ms"),
                "segment_end_ms": row.get("end_ms"),
                "db_chapter_start_ms": row.get("chapter_start_ms"),
                "asr_chapter_start_ms": row.get("asr_chapter_start_ms", row.get("chapter_start_ms")),
                "chapter_timing_source": chapter_timing_source,
                "original_duration_ms": row.get("original_duration_ms"),
                "tts_duration_ms": row.get("tts_duration_ms"),
                **window,
            }
        )
    return manifest


def write_asr_batch_script(output_dir: Path) -> Path:
    script = output_dir / "final_mix_asr_batch.py"
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
                with urllib.request.urlopen(request, timeout=360) as response:
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
                    raise SystemExit("usage: final_mix_asr_batch.py <manifest.json> <output.json>")
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
                    print(
                        f"[{index}/{len(manifest)}] {item.get('label')} "
                        f"{item.get('start_ms')}..{item.get('end_ms')} ok={result['ok']}",
                        flush=True,
                    )

            if __name__ == "__main__":
                main()
            """
        ).lstrip(),
        encoding="utf-8",
    )
    return script


def data_relpath(data_root: Path, host_path: Path) -> str:
    return host_path.resolve().relative_to(data_root.resolve()).as_posix()


def extract_episode_audio_wav(args: argparse.Namespace, rows: list[dict[str, Any]], output_dir: Path) -> str:
    source_relpath = str(rows[0].get("episode_output_relpath") or "").strip()
    if not source_relpath:
        raise SystemExit("episode_output_relpath is empty; cannot extract final audio")
    source_path = args.data_root / source_relpath
    if not source_path.exists():
        raise SystemExit(f"final media does not exist: {source_path}")
    out_path = output_dir / "final_mix_audio.wav"
    run(
        [
            args.ffmpeg,
            "-y",
            "-i",
            str(source_path),
            "-vn",
            "-ar",
            "24000",
            "-ac",
            "1",
            str(out_path),
        ],
        cwd=args.repo_root,
        timeout=args.extract_timeout_sec,
    )
    return data_relpath(args.data_root, out_path)


def probe_duration_ms(args: argparse.Namespace, relpath: str, *, timing: str) -> int:
    path = args.data_root / relpath
    if not path.exists():
        raise SystemExit(f"chapter media does not exist: {path}")
    cmd = [args.ffprobe, "-v", "error"]
    if timing == "actual-audio":
        cmd.extend(["-select_streams", "a:0", "-show_entries", "stream=duration"])
    else:
        cmd.extend(["-show_entries", "format=duration"])
    cmd.extend(["-of", "default=nw=1:nk=1", str(path)])
    output = run(cmd, cwd=args.repo_root, timeout=120)
    for line in output.splitlines():
        line = line.strip()
        if not line or line.upper() == "N/A":
            continue
        try:
            return int(round(float(line) * 1000.0))
        except ValueError:
            continue
    if timing == "actual-audio":
        return probe_duration_ms(args, relpath, timing="actual-format")
    raise SystemExit(f"could not probe duration for {path}")


def chapter_start_overrides(args: argparse.Namespace, rows: list[dict[str, Any]]) -> dict[int, int]:
    if args.chapter_timing == "db":
        return {}
    chapters_path: Path | None = args.chapters_json
    if chapters_path is None:
        rel = str(rows[0].get("chapters_manifest_rel_path") or "").strip()
        if rel:
            chapters_path = args.data_root / rel
        else:
            chapters_path = args.data_root / "episodes" / str(args.episode_id) / "chapters.json"
    if not chapters_path.is_absolute():
        chapters_path = (args.repo_root / chapters_path).resolve()
    if not chapters_path.exists():
        raise SystemExit(f"chapters json does not exist: {chapters_path}")
    payload = json.loads(chapters_path.read_text(encoding="utf-8"))
    chapters = payload.get("chapters") or []
    actual_start = 0
    overrides: dict[int, int] = {}
    for chapter in sorted(chapters, key=lambda item: int(item.get("ordinal") or 0)):
        ordinal = int(chapter.get("ordinal") or 0)
        relpath = str(chapter.get("output_rel_path") or "").strip()
        if not ordinal or not relpath:
            continue
        overrides[ordinal] = actual_start
        actual_start += probe_duration_ms(args, relpath, timing=args.chapter_timing)
    return overrides


def write_asr_chunk_retry_script(output_dir: Path) -> Path:
    script = output_dir / "final_mix_asr_chunk_retry.py"
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
                with urllib.request.urlopen(request, timeout=360) as response:
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
                    raise SystemExit("usage: final_mix_asr_chunk_retry.py <manifest.json> <output.json>")
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
    manifest_path = output_dir / "final_mix_asr_manifest.json"
    raw_path = output_dir / "final_mix_asr_results_raw.json"
    remaining_raw = output_dir / "final_mix_asr_results_remaining_raw.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    existing: list[dict[str, Any]] = []
    if args.resume and raw_path.exists() and raw_path.stat().st_size:
        existing = json.loads(raw_path.read_text(encoding="utf-8"))
    if args.resume and remaining_raw.exists() and remaining_raw.stat().st_size:
        existing.extend(json.loads(remaining_raw.read_text(encoding="utf-8")))
    by_label: dict[str, dict[str, Any]] = {}
    for row in existing:
        label = str(row.get("label") or "")
        if label:
            by_label[label] = row
    existing = list(by_label.values())
    done = {str(row.get("label")) for row in existing if row.get("label")}
    remaining = [item for item in manifest if str(item.get("label")) not in done]
    if remaining:
        remaining_manifest = output_dir / "final_mix_asr_manifest_remaining.json"
        remaining_manifest.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        if remaining_raw.exists():
            remaining_raw.unlink()
        write_asr_batch_script(output_dir)
        try:
            run(
                [
                    "docker",
                    "exec",
                    args.ml_container,
                    "python3",
                    data_container_path(args.data_root, output_dir / "final_mix_asr_batch.py"),
                    data_container_path(args.data_root, remaining_manifest),
                    data_container_path(args.data_root, remaining_raw),
                ],
                cwd=args.repo_root,
                timeout=args.asr_timeout_sec,
            )
        finally:
            if remaining_raw.exists() and remaining_raw.stat().st_size:
                for row in json.loads(remaining_raw.read_text(encoding="utf-8")):
                    label = str(row.get("label") or "")
                    if label:
                        by_label[label] = row
                existing = list(by_label.values())
                existing.sort(key=lambda row: (int(row["chapter_ordinal"]), int(row["segment_ordinal"])))
                raw_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")

    existing.sort(key=lambda row: (int(row["chapter_ordinal"]), int(row["segment_ordinal"])))
    raw_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return existing


def run_asr_chunk_retry(
    args: argparse.Namespace,
    retry_items: list[dict[str, Any]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    if not retry_items:
        return []
    retry_manifest = output_dir / "final_mix_asr_chunk_retry_manifest.json"
    retry_raw = output_dir / "final_mix_asr_chunk_retry_results.json"
    retry_manifest.write_text(json.dumps(retry_items, ensure_ascii=False, indent=2), encoding="utf-8")
    write_asr_chunk_retry_script(output_dir)
    run(
        [
            "docker",
            "exec",
            args.ml_container,
            "python3",
            data_container_path(args.data_root, output_dir / "final_mix_asr_chunk_retry.py"),
            data_container_path(args.data_root, retry_manifest),
            data_container_path(args.data_root, retry_raw),
        ],
        cwd=args.repo_root,
        timeout=args.asr_timeout_sec,
    )
    return json.loads(retry_raw.read_text(encoding="utf-8")) if retry_raw.exists() else []


def normalize_for_compare(text: str) -> str:
    return COMPARE_DROP_RE.sub("", text or "").lower()


def similarity(expected: str, actual: str) -> float:
    exp = normalize_for_compare(expected)
    got = normalize_for_compare(actual)
    if not exp and not got:
        return 1.0
    if not exp or not got:
        return 0.0
    return round(SequenceMatcher(None, exp, got).ratio(), 4)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    frac = pos - lower
    return round(ordered[lower] * (1.0 - frac) + ordered[upper] * frac, 4)


def analyze_results(
    *,
    manifest: list[dict[str, Any]],
    asr_results: list[dict[str, Any]],
    low_similarity: float = DEFAULT_LOW_SIMILARITY,
    very_low_similarity: float = DEFAULT_VERY_LOW_SIMILARITY,
) -> list[dict[str, Any]]:
    manifest_by_label = {str(row.get("label")): row for row in manifest}
    analyzed: list[dict[str, Any]] = []
    for result in asr_results:
        source = manifest_by_label.get(str(result.get("label")), result)
        expected = str(result.get("expected_text") or source.get("expected_text") or "")
        actual = str(result.get("asr_text") or "")
        sim = similarity(expected, actual)
        flags: list[str] = []
        weird = sorted(set(WEIRD_ASR_RE.findall(actual)))
        mixed = sorted(set(MIXED_TOKEN_RE.findall(expected)))
        if not result.get("ok", False):
            flags.append("asr_failed")
        if not actual.strip():
            flags.append("empty_asr")
        if weird:
            flags.append("weird_asr_terms:" + ",".join(weird))
        if sim < very_low_similarity:
            flags.append(f"very_low_similarity:{sim}")
        elif sim < low_similarity:
            flags.append(f"low_similarity:{sim}")
        if mixed:
            flags.append("expected_mixed_tokens:" + ",".join(mixed[:12]))
        if result.get("bounded_by_next_segment"):
            flags.append("window_bounded_by_next_segment")
        exp_len = len(normalize_for_compare(expected))
        analyzed.append(
            {
                **source,
                **result,
                "expected_text": expected,
                "asr_text": actual,
                "similarity": sim,
                "expected_norm_len": exp_len,
                "flags": flags,
            }
        )
    return analyzed


def retry_low_similarity_with_chunked_asr(
    *,
    args: argparse.Namespace,
    manifest: list[dict[str, Any]],
    asr_results: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    analyzed = analyze_results(
        manifest=manifest,
        asr_results=asr_results,
        low_similarity=args.low_similarity,
        very_low_similarity=args.very_low_similarity,
    )
    retry_items: list[dict[str, Any]] = []
    for row in analyzed:
        duration_ms = int(row.get("end_ms") or 0) - int(row.get("start_ms") or 0)
        is_low = any(flag.startswith("low_similarity") or flag.startswith("very_low_similarity") for flag in row["flags"])
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
    chunk_by_label = {str(row.get("label")): row for row in chunk_results}
    improved: list[dict[str, Any]] = []
    updated_results: list[dict[str, Any]] = []
    for result in asr_results:
        chunk = chunk_by_label.get(str(result.get("label")))
        if not chunk:
            updated_results.append(result)
            continue
        expected = str(chunk.get("expected_text") or result.get("expected_text") or "")
        old_sim = similarity(expected, str(result.get("asr_text") or ""))
        new_sim = similarity(expected, str(chunk.get("asr_text") or ""))
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
                    "segment_id": result.get("segment_id"),
                    "old_similarity": old_sim,
                    "new_similarity": new_sim,
                }
            )
        else:
            updated_results.append(result)
    return updated_results, improved


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sims = [float(row["similarity"]) for row in rows if row.get("ok")]
    weighted_den = sum(int(row.get("expected_norm_len") or 0) for row in rows if row.get("ok"))
    weighted_num = sum(float(row["similarity"]) * int(row.get("expected_norm_len") or 0) for row in rows if row.get("ok"))
    expected_full = "\n".join(str(row.get("expected_text") or "") for row in rows)
    asr_full = "\n".join(str(row.get("asr_text") or "") for row in rows)

    def count_flag(prefix: str) -> int:
        return sum(1 for row in rows if any(str(flag).startswith(prefix) for flag in row.get("flags") or []))

    by_chapter: dict[str, dict[str, Any]] = {}
    for row in rows:
        chapter = f"c{int(row['chapter_ordinal']):02d}"
        bucket = by_chapter.setdefault(chapter, {"count": 0, "sum_similarity": 0.0, "low": 0, "very_low": 0})
        bucket["count"] += 1
        bucket["sum_similarity"] += float(row["similarity"])
        if any(flag.startswith("low_similarity") for flag in row["flags"]):
            bucket["low"] += 1
        if any(flag.startswith("very_low_similarity") for flag in row["flags"]):
            bucket["very_low"] += 1
    for bucket in by_chapter.values():
        bucket["mean_similarity"] = round(bucket["sum_similarity"] / bucket["count"], 4) if bucket["count"] else None
        del bucket["sum_similarity"]

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "total_segments": len(rows),
        "ok_segments": sum(1 for row in rows if row.get("ok")),
        "mean_similarity": round(statistics.fmean(sims), 4) if sims else None,
        "weighted_similarity": round(weighted_num / weighted_den, 4) if weighted_den else None,
        "full_transcript_similarity": similarity(expected_full, asr_full),
        "min_similarity": min(sims) if sims else None,
        "p10_similarity": percentile(sims, 0.10),
        "low_similarity_count": count_flag("low_similarity"),
        "very_low_similarity_count": count_flag("very_low_similarity"),
        "empty_asr_count": count_flag("empty_asr"),
        "asr_failed_count": count_flag("asr_failed"),
        "weird_asr_count": count_flag("weird_asr_terms"),
        "mixed_token_segment_count": count_flag("expected_mixed_tokens"),
        "bounded_window_count": count_flag("window_bounded_by_next_segment"),
        "total_window_ms": sum(int(row.get("window_duration_ms") or 0) for row in rows),
        "by_chapter": by_chapter,
    }


def gate_summary(summary: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    checks = [
        ("ok_segments", summary["ok_segments"], summary["total_segments"], ">="),
        ("mean_similarity", summary["mean_similarity"], args.min_mean_similarity, ">="),
        ("weighted_similarity", summary["weighted_similarity"], args.min_weighted_similarity, ">="),
        ("full_transcript_similarity", summary["full_transcript_similarity"], args.min_full_transcript_similarity, ">="),
        ("empty_asr_count", summary["empty_asr_count"], 0, "<="),
        ("asr_failed_count", summary["asr_failed_count"], 0, "<="),
        ("weird_asr_count", summary["weird_asr_count"], 0, "<="),
        ("very_low_similarity_count", summary["very_low_similarity_count"], args.max_very_low_similarity, "<="),
        ("low_similarity_count", summary["low_similarity_count"], args.max_low_similarity, "<="),
    ]
    out = []
    passed = True
    for metric, actual, threshold, op in checks:
        if actual is None:
            ok = False
        elif op == ">=":
            ok = float(actual) >= float(threshold)
        else:
            ok = float(actual) <= float(threshold)
        passed = passed and ok
        out.append({"metric": metric, "actual": actual, "op": op, "threshold": threshold, "pass": ok})
    return {"pass": passed, "checks": out}


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": row.get("label"),
        "segment_id": row.get("segment_id"),
        "chapter_ordinal": row.get("chapter_ordinal"),
        "segment_ordinal": row.get("segment_ordinal"),
        "similarity": row.get("similarity"),
        "flags": row.get("flags"),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "expected_text": str(row.get("expected_text") or "")[:500],
        "asr_text": str(row.get("asr_text") or "")[:500],
        "source_text": str(row.get("source_text") or "")[:300],
    }


def write_markdown(
    summary: dict[str, Any],
    gate: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    episode: dict[str, Any],
    chunked_asr_improvements: list[dict[str, Any]],
) -> str:
    lines = [
        "# Final-Mix ASR Audit",
        "",
        "## Scope",
        "",
        f"- episode_id: {episode.get('episode_id')}",
        f"- episode_name: {episode.get('episode_name')}",
        f"- final_audio_relpath: {episode.get('episode_output_relpath')}",
        f"- asr_audio_relpath: {episode.get('asr_audio_relpath')}",
        f"- asr_audio_source: {episode.get('asr_audio_source')}",
        f"- chapter_timing_source: {episode.get('chapter_timing_source')}",
        "- comparison_target: meta.tts_spoken_text, falling back to target_text",
        "- audio_source: final mixed episode media segmented by absolute episode time windows",
        "",
        "## Summary",
        "",
    ]
    for key in [
        "total_segments",
        "ok_segments",
        "mean_similarity",
        "weighted_similarity",
        "full_transcript_similarity",
        "min_similarity",
        "p10_similarity",
        "low_similarity_count",
        "very_low_similarity_count",
        "empty_asr_count",
        "asr_failed_count",
        "weird_asr_count",
        "bounded_window_count",
    ]:
        lines.append(f"- {key}: {summary.get(key)}")
    lines.append(f"- gate: {'PASS' if gate['pass'] else 'FAIL'}")
    lines.extend(["", "## Gate Checks", ""])
    for check in gate["checks"]:
        lines.append(
            f"- {check['metric']}: {check['actual']} {check['op']} {check['threshold']} -> "
            f"{'PASS' if check['pass'] else 'FAIL'}"
        )
    lines.extend(["", "## Chapter Breakdown", ""])
    for chapter, bucket in sorted(summary["by_chapter"].items()):
        lines.append(
            f"- {chapter}: n={bucket['count']}, mean_similarity={bucket['mean_similarity']}, "
            f"low={bucket['low']}, very_low={bucket['very_low']}"
        )
    if chunked_asr_improvements:
        lines.extend(["", "## Chunked ASR Retry", ""])
        for item in chunked_asr_improvements:
            lines.append(
                f"- `{item['label']}` segment={item['segment_id']}: "
                f"{item['old_similarity']} -> {item['new_similarity']}"
            )
    flagged = [row for row in rows if row.get("flags")]
    lines.extend(["", "## Flagged Rows", ""])
    if not flagged:
        lines.append("- none")
    for row in sorted(flagged, key=lambda r: (float(r["similarity"]), int(r["segment_id"])))[:60]:
        lines.append(
            f"- `{row['label']}` sim={row['similarity']} flags={','.join(row['flags'])}\n"
            f"  expected: {str(row.get('expected_text', ''))[:240]}\n"
            f"  asr: {str(row.get('asr_text', ''))[:240]}"
        )
    lines.extend(["", "## Worst Similarity", ""])
    for row in sorted(rows, key=lambda r: float(r["similarity"]))[:30]:
        lines.append(
            f"- `{row['label']}` sim={row['similarity']} flags={','.join(row['flags'])}\n"
            f"  expected: {str(row.get('expected_text', ''))[:240]}\n"
            f"  asr: {str(row.get('asr_text', ''))[:240]}"
        )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--data-root", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--postgres-container", default="holodub-postgres-1")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--run-asr", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--asr-timeout-sec", type=int, default=10800)
    parser.add_argument("--extract-timeout-sec", type=int, default=1200)
    parser.add_argument("--extract-audio-wav", action="store_true")
    parser.add_argument("--audio-relpath-override", default="")
    parser.add_argument("--chapters-json", type=Path)
    parser.add_argument("--chapter-timing", choices=["db", "actual-audio", "actual-format"], default="db")
    parser.add_argument("--disable-chunked-asr-retry", action="store_true")
    parser.add_argument("--chunk-asr-min-ms", type=int, default=DEFAULT_CHUNK_ASR_MIN_MS)
    parser.add_argument("--chunk-asr-window-ms", type=int, default=DEFAULT_CHUNK_ASR_WINDOW_MS)
    parser.add_argument("--pre-pad-ms", type=int, default=0)
    parser.add_argument("--post-pad-ms", type=int, default=250)
    parser.add_argument("--next-gap-ms", type=int, default=50)
    parser.add_argument("--min-window-ms", type=int, default=500)
    parser.add_argument("--low-similarity", type=float, default=DEFAULT_LOW_SIMILARITY)
    parser.add_argument("--very-low-similarity", type=float, default=DEFAULT_VERY_LOW_SIMILARITY)
    parser.add_argument("--min-mean-similarity", type=float, default=0.86)
    parser.add_argument("--min-weighted-similarity", type=float, default=0.88)
    parser.add_argument("--min-full-transcript-similarity", type=float, default=0.90)
    parser.add_argument("--max-low-similarity", type=int, default=12)
    parser.add_argument("--max-very-low-similarity", type=int, default=0)
    parser.add_argument("--fail-on-gate", action="store_true")
    args = parser.parse_args(argv)
    args.repo_root = args.repo_root.resolve()
    if not args.data_root.is_absolute():
        args.data_root = (args.repo_root / args.data_root).resolve()
    if args.output_dir is None:
        args.output_dir = args.data_root / "validation" / f"episode{args.episode_id}_final_mix_asr"
    elif not args.output_dir.is_absolute():
        args.output_dir = (args.repo_root / args.output_dir).resolve()
    if args.chapters_json and not args.chapters_json.is_absolute():
        args.chapters_json = (args.repo_root / args.chapters_json).resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = load_episode_rows(args)
    if not rows:
        raise SystemExit(f"episode {args.episode_id} returned no segments")
    audio_relpath_override = str(args.audio_relpath_override or "").strip() or None
    if args.extract_audio_wav:
        audio_relpath_override = extract_episode_audio_wav(args, rows, args.output_dir)
    chapter_overrides = chapter_start_overrides(args, rows)
    manifest = build_manifest(
        rows,
        audio_relpath_override=audio_relpath_override,
        chapter_start_overrides_ms=chapter_overrides,
        chapter_timing_source=args.chapter_timing,
        pre_pad_ms=args.pre_pad_ms,
        post_pad_ms=args.post_pad_ms,
        next_gap_ms=args.next_gap_ms,
        min_window_ms=args.min_window_ms,
    )
    if not manifest:
        raise SystemExit(f"episode {args.episode_id} produced an empty final-mix ASR manifest")
    episode = {
        "episode_id": rows[0].get("episode_id"),
        "episode_name": rows[0].get("episode_name"),
        "episode_duration_ms": rows[0].get("episode_duration_ms"),
        "episode_output_relpath": rows[0].get("episode_output_relpath"),
        "asr_audio_relpath": manifest[0].get("audio_relpath") if manifest else "",
        "asr_audio_source": (
            "extracted_wav"
            if args.extract_audio_wav
            else "audio_relpath_override"
            if args.audio_relpath_override
            else "episode_output_relpath"
        ),
        "chapter_timing_source": args.chapter_timing,
        "chapter_start_overrides_ms": chapter_overrides,
    }
    (args.output_dir / "episode_segments.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output_dir / "final_mix_asr_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.run_asr:
        asr_results = run_asr_batch(args, manifest, args.output_dir)
    else:
        raw_path = args.output_dir / "final_mix_asr_results_raw.json"
        if not raw_path.exists():
            raise SystemExit("--run-asr was not set and final_mix_asr_results_raw.json does not exist")
        asr_results = json.loads(raw_path.read_text(encoding="utf-8"))

    chunked_asr_improvements: list[dict[str, Any]] = []
    if args.run_asr and not args.disable_chunked_asr_retry:
        asr_results, chunked_asr_improvements = retry_low_similarity_with_chunked_asr(
            args=args,
            manifest=manifest,
            asr_results=asr_results,
            output_dir=args.output_dir,
        )
        (args.output_dir / "final_mix_asr_results_raw.json").write_text(
            json.dumps(asr_results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    analyzed = analyze_results(
        manifest=manifest,
        asr_results=asr_results,
        low_similarity=args.low_similarity,
        very_low_similarity=args.very_low_similarity,
    )
    summary = summarize(analyzed)
    gate = gate_summary(summary, args)
    payload = {
        "schema_version": "holodub.final_mix_asr_audit.v1",
        "episode": episode,
        "window_config": {
            "pre_pad_ms": args.pre_pad_ms,
            "post_pad_ms": args.post_pad_ms,
            "next_gap_ms": args.next_gap_ms,
            "min_window_ms": args.min_window_ms,
            "chapter_timing": args.chapter_timing,
        },
        "thresholds": {
            "low_similarity": args.low_similarity,
            "very_low_similarity": args.very_low_similarity,
            "min_mean_similarity": args.min_mean_similarity,
            "min_weighted_similarity": args.min_weighted_similarity,
            "min_full_transcript_similarity": args.min_full_transcript_similarity,
            "max_low_similarity": args.max_low_similarity,
            "max_very_low_similarity": args.max_very_low_similarity,
        },
        "summary": summary,
        "gate": gate,
        "chunked_asr_improvements": chunked_asr_improvements,
        "flagged_rows": [compact_row(row) for row in analyzed if row.get("flags")],
        "worst_rows": [compact_row(row) for row in sorted(analyzed, key=lambda r: float(r["similarity"]))[:50]],
        "rows": analyzed,
    }
    (args.output_dir / "final_mix_asr_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (args.output_dir / "final_mix_asr_report.md").write_text(
        write_markdown(summary, gate, analyzed, episode=episode, chunked_asr_improvements=chunked_asr_improvements),
        encoding="utf-8",
    )
    print(json.dumps({"summary": summary, "gate": gate}, ensure_ascii=False, indent=2))
    if args.fail_on_gate and not gate["pass"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
