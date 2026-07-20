from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


DEFAULT_MAX_OVER_DRIFT_MS = 300
DEFAULT_MAX_UNDER_DRIFT_MS = 700


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a compact quality baseline for a completed LoRA/main-flow shadow episode."
    )
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--episode-segments", required=True)
    parser.add_argument("--final-mix-asr-report", required=True)
    parser.add_argument("--review-manifest", default="")
    parser.add_argument("--final-video", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-over-drift-ms", type=int, default=DEFAULT_MAX_OVER_DRIFT_MS)
    parser.add_argument("--max-under-drift-ms", type=int, default=DEFAULT_MAX_UNDER_DRIFT_MS)
    parser.add_argument("--worst-rows", type=int, default=12)
    return parser.parse_args()


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def pct(value: int, total: int) -> float:
    return round(value / total, 6) if total else 0.0


def segment_drift_summary(
    segments: list[dict[str, Any]],
    *,
    max_over_drift_ms: int,
    max_under_drift_ms: int,
) -> dict[str, Any]:
    synthesized = [
        row
        for row in segments
        if row.get("original_duration_ms") is not None and row.get("tts_duration_ms") is not None
    ]
    signed = [
        int(row.get("tts_duration_ms") or 0) - int(row.get("original_duration_ms") or 0)
        for row in synthesized
    ]
    abs_values = [abs(value) for value in signed]
    over_failures = [
        {
            "segment_id": row.get("segment_id"),
            "job_id": row.get("job_id"),
            "chapter_ordinal": row.get("chapter_ordinal"),
            "signed_drift_ms": value,
        }
        for row, value in zip(synthesized, signed)
        if value > max_over_drift_ms
    ]
    under_failures = [
        {
            "segment_id": row.get("segment_id"),
            "job_id": row.get("job_id"),
            "chapter_ordinal": row.get("chapter_ordinal"),
            "signed_drift_ms": value,
        }
        for row, value in zip(synthesized, signed)
        if value < -max_under_drift_ms
    ]
    worst = sorted(
        [
            {
                "segment_id": row.get("segment_id"),
                "job_id": row.get("job_id"),
                "chapter_ordinal": row.get("chapter_ordinal"),
                "original_duration_ms": row.get("original_duration_ms"),
                "tts_duration_ms": row.get("tts_duration_ms"),
                "signed_drift_ms": value,
                "abs_drift_ms": abs(value),
            }
            for row, value in zip(synthesized, signed)
        ],
        key=lambda row: int(row["abs_drift_ms"]),
        reverse=True,
    )[:12]
    return {
        "total_segments": len(segments),
        "synthesized_count": len(synthesized),
        "mean_abs_drift_ms": round(mean(abs_values), 3) if abs_values else None,
        "max_abs_drift_ms": max(abs_values) if abs_values else None,
        "max_over_drift_ms": max_over_drift_ms,
        "max_under_drift_ms": max_under_drift_ms,
        "over_drift_failure_count": len(over_failures),
        "under_drift_failure_count": len(under_failures),
        "pass": not over_failures and not under_failures,
        "over_drift_failures": over_failures,
        "under_drift_failures": under_failures,
        "worst_abs_drift_rows": worst,
    }


def candidate_runtime_summary(segments: list[dict[str, Any]]) -> dict[str, Any]:
    enabled_count = 0
    execution_requested_count = 0
    split_plan_count = 0
    selected_families: Counter[str] = Counter()
    fallback_reasons: Counter[str] = Counter()
    audio_selection_modes: Counter[str] = Counter()
    join_failures = 0
    high_fit_rejected_candidates = 0

    for row in segments:
        meta = row.get("meta") or {}
        candidate = meta.get("tts_lora_candidate") or {}
        runtime = meta.get("tts_lora_candidate_runtime") or candidate.get("runtime") or {}
        if candidate.get("enabled") is True:
            enabled_count += 1
        if runtime.get("execution_requested") is True:
            execution_requested_count += 1
        if runtime.get("split_plan"):
            split_plan_count += 1
        selected = str(runtime.get("selected_family") or candidate.get("selected_family") or "default")
        selected_families[selected] += 1
        fallback = str(runtime.get("fallback_reason") or candidate.get("fallback_reason") or "")
        if fallback:
            fallback_reasons[fallback] += 1
        mode = str(runtime.get("audio_selection_mode") or candidate.get("audio_selection_mode") or "")
        if mode:
            audio_selection_modes[mode] += 1
        for item in runtime.get("candidates") or []:
            reasons = set(item.get("reject_reasons") or [])
            if "bad_join_quality" in reasons:
                join_failures += 1
            if "high_fit_delta" in reasons:
                high_fit_rejected_candidates += 1

    return {
        "eligible_lora_count": enabled_count,
        "execution_requested_count": execution_requested_count,
        "execution_requested_ratio": pct(execution_requested_count, len(segments)),
        "split_plan_count": split_plan_count,
        "selected_family_counts": dict(sorted(selected_families.items())),
        "fallback_reason_counts": dict(sorted(fallback_reasons.items())),
        "audio_selection_mode_counts": dict(sorted(audio_selection_modes.items())),
        "bad_join_quality_rejected_candidate_count": join_failures,
        "high_fit_delta_rejected_candidate_count": high_fit_rejected_candidates,
    }


def final_mix_asr_summary(report: dict[str, Any], *, worst_rows: int) -> dict[str, Any]:
    summary = report.get("summary") or {}
    gate = report.get("gate") or {}
    rows = report.get("rows") or []
    worst = sorted(rows, key=lambda row: float(row.get("similarity") or 0.0))[:worst_rows]
    return {
        "gate_pass": bool(gate.get("pass")),
        "summary": summary,
        "worst_rows": [
            {
                "segment_id": row.get("segment_id"),
                "job_id": row.get("job_id"),
                "chapter_ordinal": row.get("chapter_ordinal"),
                "segment_ordinal": row.get("segment_ordinal"),
                "similarity": row.get("similarity"),
                "flags": row.get("flags") or [],
                "expected_text": row.get("expected_text") or row.get("target_text"),
                "asr_text": row.get("asr_text"),
            }
            for row in worst
        ],
    }


def build_baseline(args: argparse.Namespace) -> dict[str, Any]:
    segments = load_json(args.episode_segments)
    if not isinstance(segments, list):
        raise ValueError("--episode-segments must be a JSON list")
    asr_report = load_json(args.final_mix_asr_report)
    review_manifest = load_json(args.review_manifest) if args.review_manifest else None

    drift = segment_drift_summary(
        segments,
        max_over_drift_ms=args.max_over_drift_ms,
        max_under_drift_ms=args.max_under_drift_ms,
    )
    asr = final_mix_asr_summary(asr_report, worst_rows=max(1, int(args.worst_rows)))
    review_rows = (review_manifest or {}).get("review_rows") or []
    result = {
        "schema_version": 1,
        "baseline_id": f"episode{args.episode_id}_main_flow_shadow_guardfix_v1",
        "episode_id": args.episode_id,
        "status": "shadow_pass" if drift["pass"] and asr["gate_pass"] else "shadow_review_required",
        "final_video": args.final_video,
        "review_pack": (review_manifest or {}).get("artifacts", {}).get("index") if review_manifest else "",
        "quality_contract": {
            "max_over_drift_ms": args.max_over_drift_ms,
            "max_under_drift_ms": args.max_under_drift_ms,
            "final_mix_asr_gate_required": True,
            "low_similarity_count_required": 0,
            "very_low_similarity_count_required": 0,
            "join_quality_required": True,
            "max_fit_delta": 0.05,
            "high_fit_delta_is_hard_failure": True,
            "soft_override_reasons": ["flat_f0"],
            "hard_rejection_reasons": [
                "bad_join_quality",
                "high_drift",
                "high_fit_delta",
                "low_asr",
                "low_term_recall",
                "tail_truncation",
            ],
        },
        "drift": drift,
        "final_mix_asr": asr,
        "candidate_runtime": candidate_runtime_summary(segments),
        "human_review": {
            "review_row_count": len(review_rows),
            "manifest": args.review_manifest,
        },
        "source_files": {
            "episode_segments": args.episode_segments,
            "final_mix_asr_report": args.final_mix_asr_report,
            "review_manifest": args.review_manifest,
        },
    }
    return result


def main() -> int:
    args = parse_args()
    baseline = build_baseline(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": baseline["status"], "baseline_id": baseline["baseline_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
