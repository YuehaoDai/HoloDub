from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a repair queue from a strict LoRA candidate gate report and split speech-rate plan."
    )
    parser.add_argument("--gate-report", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--speech-rate-plan", default="")
    parser.add_argument("--listening-pack", default="")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", default="")
    parser.add_argument("--max-fit-delta", type=float, default=0.05)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def read_jsonl_by_sample(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("sample_id") is not None:
            rows[str(row["sample_id"])] = row
    return rows


def load_plans(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    payload = read_json(path)
    planned = payload.get("planned") if isinstance(payload.get("planned"), list) else []
    policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
    max_allowed_cps = policy.get("max_part_chars_per_sec")
    rows: dict[str, dict[str, Any]] = {}
    for row in planned:
        if not isinstance(row, dict) or row.get("sample_id") is None:
            continue
        normalized = dict(row)
        if normalized.get("max_part_chars_per_sec") is None and max_allowed_cps is not None:
            normalized["max_part_chars_per_sec"] = max_allowed_cps
        rows[str(normalized["sample_id"])] = normalized
    return rows


def target_sec(feature: dict[str, Any]) -> float | None:
    value = feature.get("target_ms")
    if value is None:
        value = feature.get("audio_ms")
    try:
        return round(float(value) / 1000.0, 3)
    except (TypeError, ValueError):
        return None


def risk_parts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for part in plan.get("parts") or []:
        if not isinstance(part, dict) or not part.get("speech_rate_risk"):
            continue
        rows.append(
            {
                "sample_id": part.get("sample_id"),
                "target_sec": part.get("target_sec"),
                "required_chars_per_sec": part.get("required_chars_per_sec"),
                "text": part.get("text"),
            }
        )
    return rows


def choose_action(top: dict[str, Any], plan: dict[str, Any]) -> tuple[str, str]:
    reasons = {str(item) for item in (top.get("reject_reasons") or [])}
    if plan.get("needs_text_compression"):
        return "text_compression_before_tts", "split_part_required_speech_rate_above_safe_limit"
    if "bad_join_quality" in reasons:
        return "join_boundary_repair", "best_duration_or_asr_candidate_failed_join_quality"
    if "low_asr" in reasons:
        return "text_completeness_repair_before_training", "best_candidate_low_asr_without_density_flag"
    return "manual_diagnosis", "all_candidates_failed"


def build_queue(
    *,
    gate_report: dict[str, Any],
    features: dict[str, dict[str, Any]],
    plans: dict[str, dict[str, Any]],
    gate_report_path: Path,
    features_path: Path,
    speech_rate_plan_path: Path | None,
    listening_pack: str,
    max_fit_delta: float,
) -> dict[str, Any]:
    selected_ids = {str(row.get("sample_id")) for row in gate_report.get("rows", []) if isinstance(row, dict)}
    entries: list[dict[str, Any]] = []
    for decision in gate_report.get("decisions") or []:
        if not isinstance(decision, dict) or decision.get("choice") is not None:
            continue
        sample_id = str(decision.get("sample_id") or "")
        candidates = [item for item in (decision.get("candidates") or []) if isinstance(item, dict)]
        top = candidates[0] if candidates else {}
        plan = plans.get(sample_id, {})
        feature = features.get(sample_id, {})
        reason_counts: Counter[str] = Counter()
        for candidate in candidates:
            for reason in candidate.get("reject_reasons") or []:
                reason_counts[str(reason)] += 1
        action, reason = choose_action(top, plan)
        entries.append(
            {
                "sample_id": sample_id,
                "segment_id": feature.get("segment_id"),
                "target_sec": target_sec(feature),
                "action": action,
                "reason": reason,
                "no_selection_reason": decision.get("no_selection_reason"),
                "speech_rate": {
                    "needs_text_compression": bool(plan.get("needs_text_compression")),
                    "max_required_chars_per_sec": plan.get("max_required_chars_per_sec"),
                    "max_allowed_chars_per_sec": plan.get("max_part_chars_per_sec"),
                    "risk_parts": risk_parts(plan),
                },
                "best_candidate": {
                    "label": top.get("label"),
                    "similarity": top.get("similarity"),
                    "abs_drift_ms": top.get("abs_drift_ms"),
                    "join_quality_pass": top.get("join_quality_pass"),
                    "join_quality_reasons": top.get("join_quality_reasons"),
                    "reject_reasons": top.get("reject_reasons"),
                    "path": top.get("path"),
                },
                "failure_reason_counts": dict(sorted(reason_counts.items())),
                "source_text": feature.get("source_text"),
                "current_target_text": feature.get("text"),
            }
        )

    return {
        "schema_version": "holodub.lora_repair_queue.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "gate_report": str(gate_report_path),
        "features": str(features_path),
        "speech_rate_plan": str(speech_rate_plan_path or ""),
        "listening_pack": listening_pack,
        "policy": {
            "selected_fit_tempo_delta_max": max_fit_delta,
            "low_asr_is_hard_failure": True,
            "full_episode_retry_allowed": False,
            "full_episode_retry_blocker": (
                "strict_highfit_holdout_not_fully_selected" if entries else ""
            ),
        },
        "summary": {
            "holdout_count": len(gate_report.get("decisions") or []),
            "selected_strict_count": len(selected_ids),
            "blocked_count": len(entries),
            "blocked_by_action": dict(sorted(Counter(row["action"] for row in entries).items())),
        },
        "entries": entries,
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# LoRA Repair Queue",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Strict selected: `{summary['selected_strict_count']} / {summary['holdout_count']}`",
        f"- Blocked: `{summary['blocked_count']}`",
        f"- Blocked by action: `{summary['blocked_by_action']}`",
        f"- Listening pack: `{payload.get('listening_pack') or ''}`",
        "",
        "## Blocked Anchors",
        "",
        "| Sample | Action | Reason | Max cps | Allowed cps | Best candidate | Best reject |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
    ]
    for entry in payload.get("entries") or []:
        best = entry.get("best_candidate") or {}
        speech_rate = entry.get("speech_rate") or {}
        lines.append(
            f"| {entry.get('sample_id')} | {entry.get('action')} | {entry.get('reason')} | "
            f"{speech_rate.get('max_required_chars_per_sec')} | {speech_rate.get('max_allowed_chars_per_sec')} | "
            f"{best.get('label')} | "
            f"{best.get('reject_reasons')} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    gate_path = Path(args.gate_report)
    features_path = Path(args.features)
    speech_rate_path = Path(args.speech_rate_plan) if args.speech_rate_plan else None
    payload = build_queue(
        gate_report=read_json(gate_path),
        features=read_jsonl_by_sample(features_path),
        plans=load_plans(speech_rate_path),
        gate_report_path=gate_path,
        features_path=features_path,
        speech_rate_plan_path=speech_rate_path,
        listening_pack=args.listening_pack,
        max_fit_delta=float(args.max_fit_delta),
    )
    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_md:
        write_markdown(Path(args.output_md), payload)
    print(json.dumps({"output_json": str(output_json), "summary": payload["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
