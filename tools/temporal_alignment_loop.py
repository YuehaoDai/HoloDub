#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import difflib
import json
import math
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import temporal_plan_experiment as exp


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "validation" / "temporal_alignment_loop"


@dataclass
class Gates:
    p95_abs_drift_ms: int = 700
    max_abs_drift_ms: int = 1500
    max_over_2s_count: int = 0
    min_mean_asr_similarity: float = 0.90
    min_asr_similarity: float = 0.72
    max_empty_asr_count: int = 0
    max_unnatural_rate_count: int = 0
    max_fit_tempo_delta: float = 0.12
    max_adjacent_rate_jump: float = 0.28
    max_missing_asr_count: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Closed-loop temporal/prosody experiment scorer for HoloDub."
    )
    parser.add_argument("--episode-id", type=int, default=146)
    parser.add_argument("--reuse-run", type=Path, default=None)
    parser.add_argument("--run-tts", action="store_true")
    parser.add_argument("--run-asr", action="store_true")
    parser.add_argument("--segment-ids", default="")
    parser.add_argument("--per-bucket", type=int, default=3)
    parser.add_argument("--top-drift", type=int, default=6)
    parser.add_argument("--max-samples", type=int, default=12)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument(
        "--variants",
        default="old,temporal,old_fit,temporal_fit",
    )
    parser.add_argument("--llm-compact", action="store_true")
    parser.add_argument("--cps-mode", choices=("default", "episode"), default="episode")
    parser.add_argument("--temporal-min-target-sec", type=float, default=15.0)
    parser.add_argument("--fit-min-tempo", type=float, default=0.85)
    parser.add_argument("--fit-max-tempo", type=float, default=1.15)
    parser.add_argument("--fit-filter", choices=("rubberband", "atempo"), default="rubberband")
    parser.add_argument("--chunk-asr-min-ms", type=int, default=20_000)
    parser.add_argument("--chunk-asr-window-ms", type=int, default=8_000)
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    parser.add_argument("--pg-container", default="holodub-postgres-1")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    if args.reuse_run:
        run_dir = args.reuse_run
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        rows = load_jsonl(run_dir / "results.jsonl")
        episode_id = int(manifest.get("episode_id") or args.episode_id)
    else:
        run_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        episode_id = args.episode_id
        rows, manifest = run_experiment(args, run_dir)

    target_texts = load_target_texts(args.pg_container, [int(r["segment_id"]) for r in rows])

    if args.run_asr:
        rows = attach_asr(
            args.ml_container,
            rows,
            target_texts,
            chunk_min_ms=args.chunk_asr_min_ms,
            chunk_window_ms=args.chunk_asr_window_ms,
        )
        write_jsonl(run_dir / "results.asr.jsonl", rows)
    elif any("asr_similarity" in r for r in rows):
        pass
    else:
        asr_path = run_dir / "results.asr.jsonl"
        if asr_path.exists():
            rows = load_jsonl(asr_path)

    selected, scored_rows = select_candidates(rows, manifest, target_texts)
    summary = summarize_selection(selected, scored_rows, Gates())
    advice = recommend_next_actions(summary, selected, scored_rows)

    report = {
        "episode_id": episode_id,
        "run_dir": str(run_dir),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "gates": Gates().__dict__,
        "summary": summary,
        "advice": advice,
        "selected": selected,
    }
    (run_dir / "scorecard.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_jsonl(run_dir / "scored_candidates.jsonl", scored_rows)
    (run_dir / "summary.md").write_text(render_markdown(report), encoding="utf-8")

    print_summary(report)
    return 0


def run_experiment(args: argparse.Namespace, run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    segments = exp.load_episode_segments(args.pg_container, args.episode_id)
    if not segments:
        raise RuntimeError(f"no synthesized segments found for episode {args.episode_id}")
    cps = 4.0
    if args.cps_mode == "episode":
        cps = exp.estimate_episode_chars_per_sec(segments) or cps
    if args.segment_ids:
        wanted = {int(x.strip()) for x in args.segment_ids.split(",") if x.strip()}
        by_id = {s.segment_id: s for s in segments}
        missing = sorted(wanted - set(by_id))
        if missing:
            raise RuntimeError(f"missing segment ids: {missing}")
        samples = [by_id[sid] for sid in sorted(wanted)]
    else:
        samples = exp.choose_samples(
            segments,
            per_bucket=args.per_bucket,
            top_drift=args.top_drift,
            max_samples=args.max_samples,
        )
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    manifest = {
        "episode_id": args.episode_id,
        "sample_count": len(samples),
        "repeats": args.repeats,
        "variants": variants,
        "chars_per_sec": cps,
        "temporal_min_target_sec": args.temporal_min_target_sec,
        "fit_min_tempo": args.fit_min_tempo,
        "fit_max_tempo": args.fit_max_tempo,
        "fit_filter": args.fit_filter,
        "llm_compact": args.llm_compact,
        "samples": [exp.sample_summary(s, cps) for s in samples],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    exp.print_sample_table(samples, cps)

    rows: list[dict[str, Any]] = []
    if not args.run_tts:
        (run_dir / "results.jsonl").write_text("", encoding="utf-8")
        return rows, manifest

    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        compact_cache: dict[str, dict[str, Any]] = {}
        for sample in samples:
            for repeat in range(args.repeats):
                base_cache: dict[str, dict[str, Any]] = {}
                for variant in variants:
                    base_variant = variant.replace("_fit", "")
                    started = time.time()
                    if variant.endswith("_fit") and base_variant in base_cache:
                        cached = base_cache[base_variant]
                        response = dict(cached.get("response") or {})
                        error = str(cached.get("error") or "")
                        payload = dict(cached.get("payload") or {})
                    else:
                        payload = exp.build_tts_payload(
                            sample,
                            base_variant,
                            repeat,
                            run_dir.name,
                            cps,
                            temporal_min_target_sec=args.temporal_min_target_sec,
                            text_override=exp.text_for_variant(
                                sample,
                                base_variant,
                                cps,
                                llm_compact=args.llm_compact,
                                compact_cache=compact_cache,
                            ),
                        )
                        try:
                            response = exp.call_tts(args.ml_container, payload)
                            error = ""
                        except Exception as exc:  # noqa: BLE001
                            response = {}
                            error = repr(exc)
                        if not error:
                            base_cache[base_variant] = {
                                "response": dict(response),
                                "error": error,
                                "payload": dict(payload),
                            }
                    elapsed = time.time() - started
                    actual_ms = int(response.get("actual_duration_ms") or 0)
                    fit_info: dict[str, Any] = {}
                    if not error and variant.endswith("_fit"):
                        fit_info = exp.apply_duration_fit(
                            args.ml_container,
                            response.get("audio_relpath", ""),
                            sample.target_ms,
                            actual_ms,
                            min_tempo=args.fit_min_tempo,
                            max_tempo=args.fit_max_tempo,
                            fit_filter=args.fit_filter,
                        )
                        if fit_info.get("fit_error"):
                            error = str(fit_info["fit_error"])
                        elif fit_info.get("fit_applied"):
                            actual_ms = int(fit_info.get("fit_actual_ms") or actual_ms)
                        fit_info["fit_source_variant"] = base_variant
                    row = {
                        "segment_id": sample.segment_id,
                        "job_id": sample.job_id,
                        "ordinal": sample.ordinal,
                        "variant": variant,
                        "repeat": repeat,
                        "target_ms": sample.target_ms,
                        "baseline_tts_ms": sample.baseline_tts_ms,
                        "baseline_drift_ms": sample.baseline_tts_ms - sample.target_ms,
                        "actual_ms": actual_ms,
                        "drift_ms": actual_ms - sample.target_ms if actual_ms else None,
                        "abs_drift_ms": abs(actual_ms - sample.target_ms) if actual_ms else None,
                        "elapsed_sec": round(elapsed, 3),
                        "audio_relpath": response.get("audio_relpath", ""),
                        "expected_text": payload.get("text", ""),
                        "expected_chars": exp.speakable_len(payload.get("text", "")),
                        "text_source": exp.text_source_for_variant(sample, base_variant),
                        **fit_info,
                        "diagnostics": response.get("diagnostics", []),
                        "error": error,
                    }
                    rows.append(row)
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    exp.print_result(row)
    if compact_cache:
        manifest["compact_cache"] = compact_cache
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return rows, manifest


def attach_asr(
    ml_container: str,
    rows: list[dict[str, Any]],
    target_texts: dict[int, str],
    *,
    chunk_min_ms: int,
    chunk_window_ms: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        if row.get("error"):
            out.append(row)
            continue
        audio = row.get("fit_audio_relpath") or row.get("audio_relpath")
        actual_ms = int(row.get("actual_ms") or row.get("target_ms") or 0)
        if not audio or actual_ms <= 0:
            row["asr_text"] = ""
            row["asr_similarity"] = 0.0
            row["asr_error"] = "missing audio"
            out.append(row)
            continue
        print(f"ASR seg={row['segment_id']} {row['variant']} {audio}", flush=True)
        try:
            hyp = call_asr(ml_container, audio, actual_ms)
            row["asr_error"] = ""
        except Exception as exc:  # noqa: BLE001
            hyp = ""
            row["asr_error"] = repr(exc)
        target = str(row.get("expected_text") or target_texts.get(int(row["segment_id"]), ""))
        full_similarity = similarity(target, hyp)
        row["asr_text"] = hyp
        row["asr_similarity"] = full_similarity
        if full_similarity < Gates.min_asr_similarity and actual_ms >= chunk_min_ms:
            print(
                f"ASR chunk retry seg={row['segment_id']} {row['variant']} "
                f"sim={full_similarity:.3f} window={chunk_window_ms}ms",
                flush=True,
            )
            try:
                chunk_hyp, chunk_count = call_asr_chunked(
                    ml_container,
                    audio,
                    actual_ms,
                    chunk_window_ms=chunk_window_ms,
                )
                chunk_similarity = similarity(target, chunk_hyp)
                row["chunk_asr_text"] = chunk_hyp
                row["chunk_asr_similarity"] = chunk_similarity
                row["chunk_asr_windows"] = chunk_count
                if chunk_similarity > full_similarity:
                    row["full_asr_text"] = hyp
                    row["full_asr_similarity"] = full_similarity
                    row["asr_text"] = chunk_hyp
                    row["asr_similarity"] = chunk_similarity
                    row["asr_mode"] = "chunked"
                else:
                    row["asr_mode"] = "full"
            except Exception as exc:  # noqa: BLE001
                row["chunk_asr_error"] = repr(exc)
        out.append(row)
    return out


def call_asr(container: str, audio_relpath: str, end_ms: int, start_ms: int = 0) -> str:
    payload = {
        "audio_relpath": audio_relpath,
        "source_language": "zh",
        "start_ms": max(0, int(start_ms)),
        "end_ms": max(500, int(end_ms)),
    }
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode()
    code = (
        "import base64,json,sys,urllib.request;"
        "p=json.loads(base64.b64decode(sys.argv[1]).decode('utf-8'));"
        "data=json.dumps(p,ensure_ascii=False).encode('utf-8');"
        "req=urllib.request.Request('http://127.0.0.1:8000/asr/transcribe_segment',"
        "data=data,headers={'Content-Type':'application/json'},method='POST');"
        "print(urllib.request.urlopen(req,timeout=1800).read().decode('utf-8'))"
    )
    proc = subprocess.run(
        ["docker", "exec", container, "python", "-c", code, encoded],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=1900,
    )
    return json.loads(proc.stdout).get("text", "")


def call_asr_chunked(
    container: str,
    audio_relpath: str,
    end_ms: int,
    *,
    chunk_window_ms: int,
) -> tuple[str, int]:
    chunks: list[str] = []
    current = 0
    window = max(1000, int(chunk_window_ms))
    while current < end_ms:
        next_end = min(end_ms, current + window)
        text = call_asr(container, audio_relpath, next_end, start_ms=current)
        if text.strip():
            chunks.append(text.strip())
        current = next_end
    return " ".join(chunks).strip(), math.ceil(max(0, end_ms) / window)


def select_candidates(
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
    target_texts: dict[int, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    sample_meta = {int(s["segment_id"]): s for s in manifest.get("samples", [])}
    scored: list[dict[str, Any]] = []
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        row = dict(row)
        sid = int(row["segment_id"])
        target = str(row.get("expected_text") or target_texts.get(sid, ""))
        features = candidate_features(row, sample_meta.get(sid, {}), target)
        row.update(features)
        row["candidate_score"] = candidate_score(row)
        scored.append(row)
        grouped.setdefault(sid, []).append(row)

    selected: list[dict[str, Any]] = []
    for sid, candidates in grouped.items():
        candidates.sort(key=lambda r: r["candidate_score"], reverse=True)
        best = dict(candidates[0])
        best["selection_rank"] = 1
        selected.append(best)
    selected.sort(key=lambda r: (int(r.get("job_id", 0)), int(r.get("ordinal", 0)), int(r["segment_id"])))
    attach_rate_jump(selected)
    return selected, scored


def candidate_features(row: dict[str, Any], meta: dict[str, Any], target_text: str) -> dict[str, Any]:
    target_ms = int(row.get("target_ms") or 0)
    actual_ms = int(row.get("actual_ms") or 0)
    chars = int(row.get("expected_chars") or meta.get("chars") or exp.speakable_len(target_text))
    cps = float(meta.get("required_chars_per_sec") or 0)
    if cps <= 0 and target_ms > 0:
        cps = chars / (target_ms / 1000.0)

    actual_rate = 0.0
    if actual_ms > 0:
        actual_rate = chars / (actual_ms / 1000.0)

    base_cps = float(meta.get("required_chars_per_sec") or 0)
    if base_cps <= 0:
        base_cps = actual_rate or 4.0
    # Prefer the run's calibrated chars/sec when available.
    if meta.get("rate_ratio"):
        try:
            base_cps = float(meta["required_chars_per_sec"]) / float(meta["rate_ratio"])
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    min_rate, max_rate = exp.natural_rate_window(
        base_cps,
        pacing=str(meta.get("pacing") or "normal"),
        arousal=meta.get("arousal"),
    )
    rate_status = "natural"
    if actual_rate > max_rate:
        rate_status = "too_fast"
    elif actual_rate > 0 and actual_rate < min_rate:
        rate_status = "too_slow"

    sim = row.get("asr_similarity")
    if sim is None:
        sim = 0.0
    asr_text = str(row.get("asr_text") or "")
    fit_tempo = row.get("fit_tempo")
    fit_delta = abs(float(fit_tempo) - 1.0) if fit_tempo else 0.0
    return {
        "chars": chars,
        "actual_chars_per_sec": round(actual_rate, 3),
        "min_natural_chars_per_sec": round(min_rate, 3),
        "max_natural_chars_per_sec": round(max_rate, 3),
        "actual_rate_status": rate_status,
        "asr_empty": not bool(asr_text.strip()) if "asr_text" in row else False,
        "fit_tempo_delta": round(fit_delta, 6),
    }


def candidate_score(row: dict[str, Any]) -> float:
    if row.get("error"):
        return -1000.0
    abs_drift = float(row.get("abs_drift_ms") or 999999) / 1000.0
    timing = clamp01(1.0 - abs_drift / 1.5)
    asr = float(row.get("asr_similarity") if row.get("asr_similarity") is not None else 0.0)
    if row.get("asr_empty"):
        asr = 0.0
    rate = 1.0 if row.get("actual_rate_status") == "natural" else 0.45
    fit_delta = float(row.get("fit_tempo_delta") or 0.0)
    fit = clamp01(1.0 - fit_delta / 0.15)
    score = 0.34 * timing + 0.36 * asr + 0.20 * rate + 0.10 * fit
    if abs_drift > 2.0:
        score -= 0.20
    if row.get("asr_similarity") is not None and float(row["asr_similarity"]) < Gates.min_asr_similarity:
        score -= 0.80
    if row.get("asr_similarity") is None:
        score -= 0.20
    if fit_delta > 0.12:
        score -= 0.25
    return round(score, 6)


def summarize_selection(
    selected: list[dict[str, Any]],
    scored_rows: list[dict[str, Any]],
    gates: Gates,
) -> dict[str, Any]:
    vals = [int(r.get("abs_drift_ms") or 0) for r in selected if r.get("abs_drift_ms") is not None]
    sims = [
        float(r["asr_similarity"])
        for r in selected
        if r.get("asr_similarity") is not None
    ]
    p95 = percentile(vals, 95) if vals else 0
    max_drift = max(vals) if vals else 0
    over_2s = sum(1 for v in vals if v > 2000)
    empty_asr = sum(1 for r in selected if r.get("asr_empty"))
    missing_asr = sum(1 for r in selected if r.get("asr_similarity") is None)
    unnatural = sum(1 for r in selected if r.get("actual_rate_status") != "natural")
    high_fit = sum(1 for r in selected if float(r.get("fit_tempo_delta") or 0) > gates.max_fit_tempo_delta)
    max_rate_jump = max([float(r.get("adjacent_rate_jump") or 0) for r in selected] or [0.0])
    mean_sim = statistics.mean(sims) if sims else None
    min_sim = min(sims) if sims else None
    gate_pass = (
        p95 <= gates.p95_abs_drift_ms
        and max_drift <= gates.max_abs_drift_ms
        and over_2s <= gates.max_over_2s_count
        and empty_asr <= gates.max_empty_asr_count
        and missing_asr <= gates.max_missing_asr_count
        and unnatural <= gates.max_unnatural_rate_count
        and high_fit == 0
        and max_rate_jump <= gates.max_adjacent_rate_jump
        and (mean_sim is None or mean_sim >= gates.min_mean_asr_similarity)
        and (min_sim is None or min_sim >= gates.min_asr_similarity)
    )
    return {
        "candidate_count": len(scored_rows),
        "selected_count": len(selected),
        "gate_pass": gate_pass,
        "mean_abs_drift_ms": round(statistics.mean(vals), 1) if vals else 0,
        "p50_abs_drift_ms": round(percentile(vals, 50), 1) if vals else 0,
        "p95_abs_drift_ms": round(p95, 1),
        "max_abs_drift_ms": max_drift,
        "over_2s_count": over_2s,
        "mean_asr_similarity": round(mean_sim, 4) if mean_sim is not None else None,
        "min_asr_similarity": round(min_sim, 4) if min_sim is not None else None,
        "empty_asr_count": empty_asr,
        "missing_asr_count": missing_asr,
        "unnatural_rate_count": unnatural,
        "high_fit_tempo_count": high_fit,
        "max_adjacent_rate_jump": round(max_rate_jump, 4),
        "selected_variants": counts(r["variant"] for r in selected),
    }


def recommend_next_actions(
    summary: dict[str, Any],
    selected: list[dict[str, Any]],
    scored_rows: list[dict[str, Any]],
) -> list[str]:
    advice: list[str] = []
    if summary["gate_pass"]:
        advice.append("Promote this strategy to a larger full-episode canary.")
        return advice
    if summary.get("missing_asr_count", 0) > 0:
        advice.append("ASR is missing: run semantic validation before accepting any temporal result.")
    if summary["p95_abs_drift_ms"] > 700 or summary["over_2s_count"] > 0:
        advice.append("Timing still fails: add or prefer bounded fit candidates for medium/long segments.")
    if summary.get("empty_asr_count", 0) > 0 or (
        summary.get("min_asr_similarity") is not None and summary["min_asr_similarity"] < 0.72
    ):
        advice.append("Semantic clarity fails: reject candidates with empty/low ASR even when timing is exact.")
    for sid, candidates in group_by_segment(scored_rows).items():
        clear = [
            r for r in candidates
            if r.get("asr_similarity") is not None
            and float(r.get("asr_similarity") or 0) >= Gates.min_asr_similarity
        ]
        timed = [
            r for r in candidates
            if r.get("abs_drift_ms") is not None
            and int(r.get("abs_drift_ms") or 999999) <= Gates.max_abs_drift_ms
        ]
        if clear and timed:
            best_clear = min(clear, key=lambda r: int(r.get("abs_drift_ms") or 999999))
            best_timed = max(timed, key=lambda r: float(r.get("asr_similarity") or 0))
            if (
                int(best_clear.get("abs_drift_ms") or 0) > Gates.max_abs_drift_ms
                and float(best_timed.get("asr_similarity") or 0) < Gates.min_asr_similarity
            ):
                advice.append(
                    "Segment "
                    f"{sid} has no candidate satisfying both clarity and timing: "
                    f"best clear={best_clear['variant']} drift={best_clear.get('drift_ms')}ms "
                    f"asr={best_clear.get('asr_similarity')}; "
                    f"best timed={best_timed['variant']} drift={best_timed.get('drift_ms')}ms "
                    f"asr={best_timed.get('asr_similarity')}. "
                    "Next iteration should generate a shorter spoken_text or synthesize planned subsegments."
                )
    if summary.get("unnatural_rate_count", 0) > 0:
        advice.append("Rate naturalness fails: route too_slow segments to expansion/pause planning and too_fast segments to compression.")
    if summary.get("high_fit_tempo_count", 0) > 0:
        advice.append("Fit ratio too aggressive: tighten fit window or prefer retranslation over stretching.")
    if summary.get("max_adjacent_rate_jump", 0) > 0.28:
        advice.append("Continuity fails: add adjacent rate-jump penalty to candidate selection.")

    worst = sorted(selected, key=lambda r: float(r.get("candidate_score") or 0))[:3]
    for row in worst:
        advice.append(
            "Worst selected segment "
            f"{row['segment_id']} ({row['variant']}): drift={row.get('drift_ms')}ms, "
            f"asr={row.get('asr_similarity')}, rate={row.get('actual_rate_status')}, "
            f"score={row.get('candidate_score')}."
        )
    return advice


def attach_rate_jump(selected: list[dict[str, Any]]) -> None:
    prev: dict[str, Any] | None = None
    for row in selected:
        if prev is None or row.get("job_id") != prev.get("job_id"):
            row["adjacent_rate_jump"] = 0.0
        else:
            a = float(row.get("actual_chars_per_sec") or 0)
            b = float(prev.get("actual_chars_per_sec") or 0)
            base = max(a, b, 0.001)
            row["adjacent_rate_jump"] = round(abs(a - b) / base, 4)
        prev = row


def group_by_segment(rows: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(int(row["segment_id"]), []).append(row)
    return out


def load_target_texts(pg_container: str, segment_ids: list[int]) -> dict[int, str]:
    ids = sorted({int(x) for x in segment_ids})
    if not ids:
        return {}
    query = (
        "select id,target_text,coalesce(meta::text, '{}') as meta "
        f"from segments where id in ({','.join(map(str, ids))}) order by id;"
    )
    proc = subprocess.run(
        ["docker", "exec", pg_container, "psql", "-U", "holodub", "-d", "holodub", "--csv", "-c", query],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    out: dict[int, str] = {}
    for row in csv.DictReader(proc.stdout.splitlines()):
        meta = {}
        try:
            meta = json.loads(row.get("meta") or "{}")
        except json.JSONDecodeError:
            meta = {}
        spoken = str(meta.get("tts_spoken_text") or "").strip()
        out[int(row["id"])] = spoken or row["target_text"]
    return out


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, normalize_text(a), normalize_text(b)).ratio()


def normalize_text(text: str) -> str:
    drop = set(" \t\r\n，。！？；：、,.!?;:\"'“”‘’（）()[]【】")
    return "".join(ch.lower() for ch in text if ch not in drop)


def percentile(vals: list[int], pct: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(vals[lo])
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def counts(values: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[str(v)] = out.get(str(v), 0) + 1
    return out


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Temporal Alignment Loop Scorecard",
        "",
        f"Run: `{report['run_dir']}`",
        f"Gate: {'PASS' if summary['gate_pass'] else 'FAIL'}",
        "",
        "## Summary",
        "",
        f"- Selected segments: {summary['selected_count']}",
        f"- Mean abs drift: {summary['mean_abs_drift_ms']} ms",
        f"- P95 abs drift: {summary['p95_abs_drift_ms']} ms",
        f"- Max abs drift: {summary['max_abs_drift_ms']} ms",
        f"- Mean ASR similarity: {summary['mean_asr_similarity']}",
        f"- Min ASR similarity: {summary['min_asr_similarity']}",
        f"- Empty ASR count: {summary['empty_asr_count']}",
        f"- Unnatural rate count: {summary['unnatural_rate_count']}",
        f"- Max adjacent rate jump: {summary['max_adjacent_rate_jump']}",
        f"- Selected variants: `{summary['selected_variants']}`",
        "",
        "## Advice",
        "",
    ]
    lines.extend(f"- {item}" for item in report["advice"])
    return "\n".join(lines) + "\n"


def print_summary(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("advice:")
    for item in report["advice"]:
        print(f"- {item}")
    print(f"scorecard: {Path(report['run_dir']) / 'scorecard.json'}")


if __name__ == "__main__":
    raise SystemExit(main())
