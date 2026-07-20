from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit MIT 6.824 episode segment duration-fit usage so audible tempo stretching is not hidden by low drift."
    )
    parser.add_argument("--episode-segments", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--selected-fit-warning-threshold", type=float, default=0.05)
    parser.add_argument("--selected-fit-hard-threshold", type=float, default=0.08)
    parser.add_argument("--candidate-fit-hard-threshold", type=float, default=0.08)
    parser.add_argument("--candidate-fit-severe-threshold", type=float, default=0.20)
    parser.add_argument("--max-selected-fit-warning-ratio", type=float, default=0.0)
    parser.add_argument("--top-risk-limit", type=int, default=40)
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve(root: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root / path


def rel_to_root(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def runtime_for(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    runtime = meta.get("tts_lora_candidate_runtime")
    if isinstance(runtime, dict):
        return runtime
    plan = meta.get("tts_lora_candidate")
    if isinstance(plan, dict):
        nested = plan.get("runtime")
        if isinstance(nested, dict):
            return nested
        return plan
    runtime = {}
    return runtime if isinstance(runtime, dict) else {}


def agent_meta_for(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    agent = meta.get("tts_agent")
    return agent if isinstance(agent, dict) else {}


def agent_failure_meta_for(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    agent = meta.get("tts_agent_failure")
    return agent if isinstance(agent, dict) else {}


def segment_label(row: dict[str, Any]) -> str:
    label = str(row.get("label") or "").strip()
    if label:
        return label
    segment_id = row.get("segment_id")
    if segment_id not in (None, ""):
        return f"seg{segment_id}"
    chapter = row.get("chapter_ordinal")
    segment = row.get("segment_ordinal")
    if chapter is not None and segment is not None:
        return f"c{chapter}_s{segment}"
    return "unknown_segment"


def text_snippet(text: Any, limit: int = 96) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "..."


def candidate_score_close(candidate: dict[str, Any], expected: float | None) -> bool:
    score = number(candidate.get("score"))
    if score is None or expected is None:
        return False
    return abs(score - expected) <= 1e-6


def selected_candidate(runtime: dict[str, Any]) -> dict[str, Any] | None:
    candidates = runtime.get("candidates") if isinstance(runtime.get("candidates"), list) else []
    candidates = [item for item in candidates if isinstance(item, dict)]
    if not candidates:
        return None

    gate = runtime.get("gate_decision") if isinstance(runtime.get("gate_decision"), dict) else {}
    selected_family = str(runtime.get("selected_family") or gate.get("selected_family") or "")
    expected_score = number(gate.get("score"))
    selected_strategy = str(runtime.get("selected_split_strategy") or "")

    if expected_score is not None:
        for candidate in candidates:
            if candidate_score_close(candidate, expected_score):
                return candidate

    family_matches = [candidate for candidate in candidates if str(candidate.get("family") or "") == selected_family]
    if selected_strategy:
        strategy_matches = [
            candidate for candidate in family_matches if str(candidate.get("split_strategy") or "") == selected_strategy
        ]
        if strategy_matches:
            family_matches = strategy_matches

    gate_pass_matches = [candidate for candidate in family_matches if candidate.get("gate_pass") is True]
    if gate_pass_matches:
        family_matches = gate_pass_matches
    if family_matches:
        return max(family_matches, key=lambda item: number(item.get("score")) or -1.0)
    return None


def candidate_fit_delta(candidate: dict[str, Any]) -> float | None:
    delta = number(candidate.get("fit_tempo_delta"))
    if delta is not None:
        return abs(delta)
    target_sec = number(candidate.get("target_sec"))
    pre_fit_sec = number(candidate.get("pre_fit_duration_sec")) or number(candidate.get("duration_sec"))
    if target_sec and pre_fit_sec and target_sec > 0:
        return abs(target_sec - pre_fit_sec) / target_sec
    return None


def selected_row(
    row: dict[str, Any],
    runtime: dict[str, Any],
    candidate: dict[str, Any] | None,
    *,
    warning_threshold: float,
) -> dict[str, Any] | None:
    if candidate is None:
        return None
    delta = candidate_fit_delta(candidate)
    if delta is None:
        return None
    target_ms = number(row.get("original_duration_ms"))
    selected = {
        "label": segment_label(row),
        "segment_id": row.get("segment_id"),
        "family": candidate.get("family") or runtime.get("selected_family") or "",
        "audio_relpath": candidate.get("audio_relpath") or runtime.get("audio_relpath") or "",
        "runtime_audio_relpath": runtime.get("audio_relpath") or "",
        "fit_tempo_delta": rounded(delta),
        "fit_applied": candidate.get("fit_applied") is True,
        "target_ms": int(target_ms) if target_ms is not None else None,
        "pre_fit_duration_sec": rounded(number(candidate.get("pre_fit_duration_sec"))),
        "post_fit_duration_sec": rounded(number(candidate.get("post_fit_duration_sec"))),
        "duration_sec": rounded(number(candidate.get("duration_sec"))),
        "pre_fit_drift_ms": rounded(number(candidate.get("pre_fit_drift_ms")), 3),
        "post_fit_drift_ms": rounded(number(candidate.get("post_fit_drift_ms")) or number(candidate.get("drift_ms")), 3),
        "asr_similarity": rounded(number(candidate.get("similarity"))),
        "f0_range_semitones_10_90": rounded(number(candidate.get("f0_range_semitones_10_90"))),
        "selected_split_strategy": runtime.get("selected_split_strategy") or candidate.get("split_strategy") or "",
        "text_snippet": text_snippet(row.get("target_text") or row.get("source_text")),
    }
    selected["warning"] = bool(delta > warning_threshold)
    return selected


def final_implied_fit_delta(row: dict[str, Any], agent: dict[str, Any]) -> float | None:
    delta = number(agent.get("final_implied_fit_delta"))
    if delta is not None:
        return abs(delta)
    target_ms = number(row.get("original_duration_ms"))
    actual_ms = number(row.get("tts_duration_ms"))
    if target_ms and actual_ms and target_ms > 0:
        return abs(actual_ms / target_ms - 1.0)
    return None


def agent_fit_row(
    row: dict[str, Any],
    agent: dict[str, Any],
    *,
    warning_threshold: float,
    source: str,
) -> dict[str, Any] | None:
    delta = final_implied_fit_delta(row, agent)
    if delta is None:
        return None
    target_ms = number(agent.get("target_ms")) or number(row.get("original_duration_ms"))
    actual_ms = number(agent.get("final_actual_ms")) or number(row.get("tts_duration_ms"))
    result = {
        "label": segment_label(row),
        "segment_id": row.get("segment_id"),
        "source": source,
        "family": agent.get("lora_selected_family") or "",
        "audio_selection_mode": agent.get("lora_audio_selection_mode") or "",
        "fallback_reason": agent.get("lora_fallback_reason") or "",
        "final_reason": agent.get("final_reason") or "",
        "final_implied_fit_delta": rounded(delta),
        "target_ms": int(target_ms) if target_ms is not None else None,
        "final_actual_ms": int(actual_ms) if actual_ms is not None else None,
        "final_abs_drift_ms": agent.get("final_abs_drift_ms"),
        "hard_cap_exceeded": agent.get("hard_cap_exceeded") is True,
        "output_not_persisted": agent.get("output_not_persisted") is True,
        "text_snippet": text_snippet(row.get("target_text") or row.get("source_text")),
    }
    result["warning"] = bool(delta > warning_threshold)
    return result


def summarize_values(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p90": None, "max": None}
    return {
        "count": len(values),
        "mean": rounded(mean(values)),
        "p50": rounded(median(values)),
        "p90": rounded(quantile(values, 0.9)),
        "max": rounded(max(values)),
    }


def recommendation(
    *,
    selected_warning_count: int,
    selected_warning_ratio: float,
    selected_hard_count: int,
    candidate_hard_ratio: float,
    candidate_severe_ratio: float,
    max_selected_warning_ratio: float,
) -> str:
    selected_policy_fails = selected_hard_count > 0 or selected_warning_ratio > max_selected_warning_ratio
    raw_duration_is_systemic = candidate_hard_ratio >= 0.25 or candidate_severe_ratio >= 0.10
    if selected_policy_fails and raw_duration_is_systemic:
        return "repair_fit_policy_and_start_duration_prosody_training_feasibility"
    if selected_policy_fails:
        return "repair_fit_policy_before_acceptance"
    if raw_duration_is_systemic:
        return "start_duration_prosody_training_feasibility"
    if selected_warning_count:
        return "human_review_selected_fit_warnings"
    return "fit_policy_ok"


def audit(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    episode_segments = resolve(root, args.episode_segments)
    if episode_segments is None:
        raise ValueError("episode segments path required")
    rows = read_json(episode_segments)
    if not isinstance(rows, list):
        raise ValueError("episode segments must be a JSON list")

    runtime_segments = 0
    selected_rows: list[dict[str, Any]] = []
    selected_fit_values: list[float] = []
    final_fit_rows: list[dict[str, Any]] = []
    final_fit_values: list[float] = []
    failure_fit_rows: list[dict[str, Any]] = []
    failure_fit_values: list[float] = []
    candidate_fit_values: list[float] = []
    selected_families: Counter[str] = Counter()
    mode_counts: Counter[str] = Counter()
    fallback_counts: Counter[str] = Counter()

    for row in rows:
        if not isinstance(row, dict):
            continue
        runtime = runtime_for(row)
        if runtime:
            runtime_segments += 1
        selected_family = str(runtime.get("selected_family") or "none")
        selected_families[selected_family] += 1
        mode_counts[str(runtime.get("audio_selection_mode") or "none")] += 1
        fallback_reason = str(runtime.get("fallback_reason") or "")
        if fallback_reason:
            fallback_counts[fallback_reason] += 1

        candidates = runtime.get("candidates") if isinstance(runtime.get("candidates"), list) else []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            delta = candidate_fit_delta(candidate)
            if delta is not None:
                candidate_fit_values.append(delta)

        chosen = selected_candidate(runtime)
        selected = selected_row(
            row,
            runtime,
            chosen,
            warning_threshold=args.selected_fit_warning_threshold,
        )
        if selected is not None:
            selected_rows.append(selected)
            selected_fit_values.append(float(selected["fit_tempo_delta"]))

        agent = agent_meta_for(row)
        final_fit = agent_fit_row(
            row,
            agent,
            warning_threshold=args.selected_fit_warning_threshold,
            source="tts_agent",
        ) if agent else None
        if final_fit is not None:
            final_fit_rows.append(final_fit)
            final_fit_values.append(float(final_fit["final_implied_fit_delta"]))

        agent_failure = agent_failure_meta_for(row)
        failure_fit = agent_fit_row(
            row,
            agent_failure,
            warning_threshold=args.selected_fit_warning_threshold,
            source="tts_agent_failure",
        ) if agent_failure else None
        if failure_fit is not None:
            failure_fit_rows.append(failure_fit)
            failure_fit_values.append(float(failure_fit["final_implied_fit_delta"]))

    selected_warning_count = sum(
        1 for value in selected_fit_values if value > float(args.selected_fit_warning_threshold)
    )
    selected_hard_count = sum(1 for value in selected_fit_values if value > float(args.selected_fit_hard_threshold))
    final_warning_count = sum(1 for value in final_fit_values if value > float(args.selected_fit_warning_threshold))
    final_hard_count = sum(1 for value in final_fit_values if value > float(args.selected_fit_hard_threshold))
    failure_warning_count = sum(1 for value in failure_fit_values if value > float(args.selected_fit_warning_threshold))
    failure_hard_count = sum(1 for value in failure_fit_values if value > float(args.selected_fit_hard_threshold))
    selected_fit_applied_count = sum(1 for row in selected_rows if row.get("fit_applied") is True)
    candidate_hard_count = sum(1 for value in candidate_fit_values if value > float(args.candidate_fit_hard_threshold))
    candidate_severe_count = sum(
        1 for value in candidate_fit_values if value > float(args.candidate_fit_severe_threshold)
    )
    selected_warning_ratio = selected_warning_count / len(selected_fit_values) if selected_fit_values else 0.0
    candidate_hard_ratio = candidate_hard_count / len(candidate_fit_values) if candidate_fit_values else 0.0
    candidate_severe_ratio = candidate_severe_count / len(candidate_fit_values) if candidate_fit_values else 0.0
    selected_hard_ratio = selected_hard_count / len(selected_fit_values) if selected_fit_values else 0.0
    final_warning_ratio = final_warning_count / len(final_fit_values) if final_fit_values else 0.0
    final_hard_ratio = final_hard_count / len(final_fit_values) if final_fit_values else 0.0
    failure_warning_ratio = failure_warning_count / len(failure_fit_values) if failure_fit_values else 0.0
    failure_hard_ratio = failure_hard_count / len(failure_fit_values) if failure_fit_values else 0.0

    rec = recommendation(
        selected_warning_count=selected_warning_count + final_warning_count,
        selected_warning_ratio=max(selected_warning_ratio, final_warning_ratio),
        selected_hard_count=selected_hard_count + final_hard_count,
        candidate_hard_ratio=candidate_hard_ratio,
        candidate_severe_ratio=candidate_severe_ratio,
        max_selected_warning_ratio=args.max_selected_fit_warning_ratio,
    )
    status = "pass"
    if (
        selected_hard_count > 0
        or final_hard_count > 0
        or selected_warning_ratio > float(args.max_selected_fit_warning_ratio)
        or final_warning_ratio > float(args.max_selected_fit_warning_ratio)
    ):
        status = "fail"
    elif candidate_hard_ratio >= 0.25 or candidate_severe_ratio >= 0.10:
        status = "review"

    risk_segments = sorted(
        [
            *(dict(row, risk_kind="selected_candidate_fit") for row in selected_rows if row.get("warning")),
            *(dict(row, risk_kind="final_implied_fit") for row in final_fit_rows if row.get("warning")),
            *(dict(row, risk_kind="rejected_final_implied_fit") for row in failure_fit_rows if row.get("warning")),
        ],
        key=lambda item: max(
            float(item.get("fit_tempo_delta") or 0.0),
            float(item.get("final_implied_fit_delta") or 0.0),
        ),
        reverse=True,
    )[: max(0, int(args.top_risk_limit))]

    return {
        "schema_version": "holodub.mit6824.fit_audit.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode_segments": rel_to_root(episode_segments, root),
        "status": status,
        "recommendation": rec,
        "thresholds": {
            "selected_fit_warning_threshold": args.selected_fit_warning_threshold,
            "selected_fit_hard_threshold": args.selected_fit_hard_threshold,
            "candidate_fit_hard_threshold": args.candidate_fit_hard_threshold,
            "candidate_fit_severe_threshold": args.candidate_fit_severe_threshold,
            "max_selected_fit_warning_ratio": args.max_selected_fit_warning_ratio,
        },
        "summary": {
            "segments": len([row for row in rows if isinstance(row, dict)]),
            "runtime_segments": runtime_segments,
            "selected_family_counts": dict(sorted(selected_families.items())),
            "audio_selection_mode_counts": dict(sorted(mode_counts.items())),
            "fallback_reason_counts": dict(sorted(fallback_counts.items())),
            "selected_fit": {
                **summarize_values(selected_fit_values),
                "fit_applied_count": selected_fit_applied_count,
                "gt_warning_threshold": selected_warning_count,
                "gt_warning_ratio": rounded(selected_warning_ratio),
                "gt_hard_threshold": selected_hard_count,
                "gt_hard_ratio": rounded(selected_hard_ratio),
            },
            "final_implied_fit": {
                **summarize_values(final_fit_values),
                "gt_warning_threshold": final_warning_count,
                "gt_warning_ratio": rounded(final_warning_ratio),
                "gt_hard_threshold": final_hard_count,
                "gt_hard_ratio": rounded(final_hard_ratio),
            },
            "rejected_final_implied_fit": {
                **summarize_values(failure_fit_values),
                "gt_warning_threshold": failure_warning_count,
                "gt_warning_ratio": rounded(failure_warning_ratio),
                "gt_hard_threshold": failure_hard_count,
                "gt_hard_ratio": rounded(failure_hard_ratio),
            },
            "candidate_fit": {
                **summarize_values(candidate_fit_values),
                "gt_hard_threshold": candidate_hard_count,
                "gt_hard_ratio": rounded(candidate_hard_ratio),
                "gt_severe_threshold": candidate_severe_count,
                "gt_severe_ratio": rounded(candidate_severe_ratio),
            },
        },
        "training_signal": {
            "duration_or_tempo_systemic": candidate_hard_ratio >= 0.25 or candidate_severe_ratio >= 0.10,
            "smoke_training_recommended": rec
            in {
                "repair_fit_policy_and_start_duration_prosody_training_feasibility",
                "start_duration_prosody_training_feasibility",
            },
            "eligible_for_promoted_training": False,
            "reason": "raw candidate duration mismatch is systemic; promoted adapter still requires human-approved aligned audio and heldout validation",
        },
        "fit_policy_recommendation": {
            "production_selected_max_fit_delta": min(0.05, float(args.selected_fit_warning_threshold)),
            "do_not_accept_episode_with_selected_fit_warning": True,
            "regenerate_before_acceptance": status == "fail",
            "notes": [
                "Low final drift is not sufficient when post-fit tempo correction is audible.",
                "Prefer re-splitting, text compression, or new candidates over stretching selected audio past the warning threshold.",
            ],
        },
        "risk_segments": risk_segments,
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    selected = summary["selected_fit"]
    candidate = summary["candidate_fit"]
    lines = [
        "# MIT 6.824 Fit Audit",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "## Verdict",
        "",
        f"- Status: `{payload['status']}`",
        f"- Recommendation: `{payload['recommendation']}`",
        f"- Episode segments: `{payload['episode_segments']}`",
        "",
        "## Selected Audio Fit",
        "",
        f"- Count: `{selected['count']}`",
        f"- Fit applied count: `{selected['fit_applied_count']}`",
        f"- Mean / p50 / p90 / max: `{selected['mean']}` / `{selected['p50']}` / `{selected['p90']}` / `{selected['max']}`",
        f"- Above warning threshold: `{selected['gt_warning_threshold']}` (`{selected['gt_warning_ratio']}`)",
        f"- Above hard threshold: `{selected['gt_hard_threshold']}` (`{selected['gt_hard_ratio']}`)",
        "",
        "## Final Audio Implied Fit",
        "",
        f"- Count: `{summary['final_implied_fit']['count']}`",
        f"- Mean / p50 / p90 / max: `{summary['final_implied_fit']['mean']}` / `{summary['final_implied_fit']['p50']}` / `{summary['final_implied_fit']['p90']}` / `{summary['final_implied_fit']['max']}`",
        f"- Above warning threshold: `{summary['final_implied_fit']['gt_warning_threshold']}` (`{summary['final_implied_fit']['gt_warning_ratio']}`)",
        f"- Above hard threshold: `{summary['final_implied_fit']['gt_hard_threshold']}` (`{summary['final_implied_fit']['gt_hard_ratio']}`)",
        "",
        "## Candidate Raw Fit Pressure",
        "",
        f"- Count: `{candidate['count']}`",
        f"- Mean / p50 / p90 / max: `{candidate['mean']}` / `{candidate['p50']}` / `{candidate['p90']}` / `{candidate['max']}`",
        f"- Above hard threshold: `{candidate['gt_hard_threshold']}` (`{candidate['gt_hard_ratio']}`)",
        f"- Above severe threshold: `{candidate['gt_severe_threshold']}` (`{candidate['gt_severe_ratio']}`)",
        "",
        "## Training Signal",
        "",
        f"- Duration/tempo systemic: `{payload['training_signal']['duration_or_tempo_systemic']}`",
        f"- Smoke training recommended: `{payload['training_signal']['smoke_training_recommended']}`",
        f"- Eligible for promoted training: `{payload['training_signal']['eligible_for_promoted_training']}`",
        "",
        "## Highest Selected Fit Risks",
        "",
        "| Segment | Risk | Family | Fit Delta | Final Implied | Fit Applied | Pre Drift ms | Post Drift ms | Text |",
        "| --- | --- | --- | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    for row in payload.get("risk_segments") or []:
        text = str(row.get("text_snippet") or "").replace("|", "\\|")
        lines.append(
            f"| {row.get('label')} | {row.get('risk_kind')} | {row.get('family')} | "
            f"{row.get('fit_tempo_delta')} | {row.get('final_implied_fit_delta')} | "
            f"{row.get('fit_applied')} | {row.get('pre_fit_drift_ms')} | {row.get('post_fit_drift_ms')} | {text} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    payload = audit(args, root)
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(output_md, payload)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "status": payload["status"],
                "recommendation": payload["recommendation"],
                "summary": payload["summary"],
                "training_signal": payload["training_signal"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
