#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import temporal_plan_experiment as exp


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "validation" / "temporal_closed_loop_canary"
DEFAULT_PLAN = "10543:old,10663:old|compact,10514:compact|old,10517:compact|old"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Run a closed-loop temporal/prosody canary over selected segments."
    )
    parser.add_argument("--episode-id", type=int, default=146)
    parser.add_argument("--auto-sample", action="store_true")
    parser.add_argument(
        "--segment-plan",
        default=DEFAULT_PLAN,
        help="Comma-separated sid:variant|variant plan. First passing variant wins.",
    )
    parser.add_argument("--per-bucket", type=int, default=2)
    parser.add_argument("--top-drift", type=int, default=4)
    parser.add_argument("--max-samples", type=int, default=6)
    parser.add_argument("--pg-container", default="holodub-postgres-1")
    parser.add_argument("--cps-mode", choices=("default", "episode"), default="episode")
    parser.add_argument("--llm-compact", action="store_true")
    parser.add_argument("--candidate-repeats", type=int, default=4)
    parser.add_argument("--escalate-repeats", type=int, default=12)
    parser.add_argument("--max-chunks", type=int, default=7)
    parser.add_argument("--min-chunk-chars", type=int, default=20)
    parser.add_argument("--max-chunk-chars", type=int, default=70)
    parser.add_argument("--final-fit-max-delta", type=float, default=0.10)
    parser.add_argument("--quality-fit-max-delta", type=float, default=0.10)
    parser.add_argument("--quality-max-adjacent-rate-jump", type=float, default=0.24)
    parser.add_argument("--quality-min-boundary-asr", type=float, default=0.92)
    parser.add_argument("--quality-min-chunk-boundary-asr", type=float, default=0.85)
    parser.add_argument("--quality-min-term-recall", type=float, default=0.98)
    parser.add_argument("--quality-min-chunk-term-recall", type=float, default=1.0)
    parser.add_argument("--quality-min-text-retention", type=float, default=0.85)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    run_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    plan = build_auto_plan(args) if args.auto_sample else parse_plan(args.segment_plan)
    records: list[dict[str, Any]] = []
    for segment_id, variants in plan:
        selected: dict[str, Any] | None = None
        attempts: list[dict[str, Any]] = []
        for variant in variants:
            summary = run_chunk_select(args, segment_id, variant, args.candidate_repeats)
            attempts.append(summary)
            if summary.get("quality_gate_pass"):
                selected = summary
                break
            if args.escalate_repeats > args.candidate_repeats:
                retry = run_chunk_select(args, segment_id, variant, args.escalate_repeats)
                retry["escalated_from_repeats"] = args.candidate_repeats
                attempts.append(retry)
                if retry.get("quality_gate_pass"):
                    selected = retry
                    break
        records.append(
            {
                "segment_id": segment_id,
                "selected_variant": selected.get("text_variant") if selected else "",
                "gate_pass": bool(selected),
                "quality_gate_pass": bool(selected),
                "selected_summary": selected,
                "attempts": attempts,
            }
        )

    scorecard = {
        "episode_id": args.episode_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "auto_sample": args.auto_sample,
        "plan": [{"segment_id": sid, "variants": variants} for sid, variants in plan],
        "segment_count": len(records),
        "pass_count": sum(1 for row in records if row["gate_pass"]),
        "gate_pass": all(row["gate_pass"] for row in records),
        "metrics": aggregate_metrics(records),
        "records": records,
    }
    (run_dir / "scorecard.json").write_text(
        json.dumps(scorecard, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(scorecard, ensure_ascii=False, indent=2))
    return 0 if scorecard["gate_pass"] else 1


def parse_plan(raw: str) -> list[tuple[int, list[str]]]:
    plan: list[tuple[int, list[str]]] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        sid_raw, variants_raw = item.split(":", 1)
        variants = [part.strip() for part in variants_raw.split("|") if part.strip()]
        if not variants:
            raise ValueError(f"missing variants in plan item: {item}")
        plan.append((int(sid_raw), variants))
    return plan


def build_auto_plan(args: argparse.Namespace) -> list[tuple[int, list[str]]]:
    segments = exp.load_episode_segments(args.pg_container, args.episode_id)
    if not segments:
        raise RuntimeError(f"no synthesized segments found for episode {args.episode_id}")
    cps = 4.0
    if args.cps_mode == "episode":
        cps = exp.estimate_episode_chars_per_sec(segments) or cps
    samples = exp.choose_samples(
        segments,
        per_bucket=args.per_bucket,
        top_drift=args.top_drift,
        max_samples=args.max_samples,
    )
    return [(sample.segment_id, route_variants(sample, cps)) for sample in samples]


def route_variants(sample: exp.SegmentSample, cps: float) -> list[str]:
    plan = exp.build_timing_plan(
        exp.sample_tts_text(sample),
        sample.target_ms,
        cps,
        pacing=exp.sample_pacing(sample),
        arousal=exp.sample_arousal(sample),
    )
    rate_status = str(plan.get("rate_status") or "")
    baseline_abs = abs(sample.baseline_tts_ms - sample.target_ms)
    if sample.target_ms <= 8_000:
        return ["old", "compact"] if baseline_abs > 700 else ["old"]
    if sample.target_ms <= 25_000:
        if rate_status == "too_fast" or sample.baseline_tts_ms > sample.target_ms + 1_500:
            return ["compact", "old"]
        return ["old", "compact"]
    if rate_status == "too_fast" or sample.baseline_tts_ms > sample.target_ms + 1_500:
        return ["compact", "old"]
    return ["old", "compact"]


def run_chunk_select(
    args: argparse.Namespace,
    segment_id: int,
    variant: str,
    candidate_repeats: int,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(REPO_ROOT / "tools" / "temporal_chunk_select_experiment.py"),
        "--episode-id",
        str(args.episode_id),
        "--segment-id",
        str(segment_id),
        "--text-variant",
        variant,
        "--candidate-repeats",
        str(candidate_repeats),
        "--max-chunks",
        str(args.max_chunks),
        "--min-chunk-chars",
        str(args.min_chunk_chars),
        "--max-chunk-chars",
        str(args.max_chunk_chars),
        "--final-fit-max-delta",
        str(args.final_fit_max_delta),
    ]
    if args.llm_compact:
        command.append("--llm-compact")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        command,
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=3600,
    )
    summary = extract_last_json(proc.stdout or "")
    summary["text_variant"] = variant
    summary["segment_id"] = segment_id
    summary["candidate_repeats"] = candidate_repeats
    summary["returncode"] = proc.returncode
    if proc.returncode != 0:
        summary["stderr"] = proc.stderr[-4000:]
    annotate_quality(summary, args)
    return summary


def annotate_quality(summary: dict[str, Any], args: argparse.Namespace) -> None:
    issues: list[str] = []
    features = summary.get("selection_features") or {}
    if not summary.get("gate_pass"):
        issues.append("basic_gate_failed")
    effective_tempo_delta = float(
        summary.get("effective_tempo_delta", features.get("fit_tempo_delta") or 0.0) or 0.0
    )
    if effective_tempo_delta > args.quality_fit_max_delta:
        issues.append("fit_tempo_delta")
    if float(features.get("max_adjacent_rate_jump") or 0.0) > args.quality_max_adjacent_rate_jump:
        issues.append("max_adjacent_rate_jump")
    if float(summary.get("boundary_asr_similarity") or 0.0) < args.quality_min_boundary_asr:
        issues.append("boundary_asr_similarity")
    if float(summary.get("min_boundary_asr_similarity") or 0.0) < args.quality_min_chunk_boundary_asr:
        issues.append("min_boundary_asr_similarity")
    if float(summary.get("boundary_term_recall") or 0.0) < args.quality_min_term_recall:
        issues.append("boundary_term_recall")
    if float(summary.get("min_boundary_term_recall") or 0.0) < args.quality_min_chunk_term_recall:
        issues.append("min_boundary_term_recall")
    if float(summary.get("text_retention_ratio") or 0.0) < args.quality_min_text_retention:
        issues.append("text_retention_ratio")
    summary["quality_gate_pass"] = bool(summary.get("gate_pass") and not issues)
    summary["quality_issues"] = issues


def aggregate_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [
        row.get("selected_summary") or {}
        for row in records
        if row.get("selected_summary")
    ]
    if not selected:
        return {}
    abs_drifts = sorted(float(row.get("abs_drift_ms") or 0.0) for row in selected)
    boundary_asr = [float(row.get("boundary_asr_similarity") or 0.0) for row in selected]
    min_boundary_asr = [float(row.get("min_boundary_asr_similarity") or 0.0) for row in selected]
    term_recall = [float(row.get("boundary_term_recall") or 0.0) for row in selected]
    min_term_recall = [float(row.get("min_boundary_term_recall") or 0.0) for row in selected]
    retention = [float(row.get("text_retention_ratio") or 0.0) for row in selected]
    fit_delta = [
        float(
            row.get(
                "effective_tempo_delta",
                (row.get("selection_features") or {}).get("fit_tempo_delta") or 0.0,
            )
            or 0.0
        )
        for row in selected
    ]
    adjacent_jump = [
        float((row.get("selection_features") or {}).get("max_adjacent_rate_jump") or 0.0)
        for row in selected
    ]
    return {
        "mean_abs_drift_ms": round(sum(abs_drifts) / len(abs_drifts), 3),
        "p95_abs_drift_ms": round(percentile(abs_drifts, 95), 3),
        "max_abs_drift_ms": round(max(abs_drifts), 3),
        "mean_boundary_asr": round(sum(boundary_asr) / len(boundary_asr), 6),
        "min_boundary_asr": round(min(min_boundary_asr), 6),
        "mean_term_recall": round(sum(term_recall) / len(term_recall), 6),
        "min_term_recall": round(min(min_term_recall), 6),
        "min_text_retention_ratio": round(min(retention), 6),
        "max_fit_tempo_delta": round(max(fit_delta), 6),
        "max_adjacent_rate_jump": round(max(adjacent_jump), 6),
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(len(values) - 1, lo + 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def extract_last_json(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for idx in range(len(text) - 1, -1, -1):
        if text[idx] != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            continue
        if text[idx + end :].strip():
            continue
        if isinstance(value, dict):
            return value
    return {"gate_pass": False, "parse_error": "no summary JSON found"}


if __name__ == "__main__":
    raise SystemExit(main())
