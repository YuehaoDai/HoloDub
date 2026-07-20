from __future__ import annotations

import argparse
import json
import re
import wave
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FAMILY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("tail_truncation", re.compile(r"tail|truncat|cut.?off|ending|missing.*end|吞|截断|句尾", re.I)),
    ("mixed_latin_term", re.compile(r"latin|english|raft|craq|zookeeper|aurora|spanner|cops|blockstack|term|术语|英文", re.I)),
    ("number_or_section", re.compile(r"number|digit|section|chapter|\\d\\.\\d|号|点|编号|数字", re.I)),
    ("join_or_pause", re.compile(r"join|pause|gap|boundary|split|停顿|拼接|断句", re.I)),
    ("duration_or_tempo", re.compile(r"drift|duration|tempo|speed|fit_delta|时长|语速|漂移", re.I)),
    ("flat_prosody", re.compile(r"f0|prosody|pitch|flat|robot|intonation|音调|平|机器人", re.I)),
    ("low_asr_similarity", re.compile(r"low_asr|similarity|asr|transcript|识别", re.I)),
]
HUMAN_REVIEWED_STATUSES = {"accepted", "review_blocked"}
PENDING_REVIEW_STATUSES = {"awaiting_human_acceptance"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize MIT 6.824 acceptance evidence and decide whether new LoRA training is justified."
    )
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_training_decision.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_training_decision.md")
    parser.add_argument("--min-reviewed-lectures", type=int, default=2)
    parser.add_argument("--min-repeat-lectures", type=int, default=2)
    parser.add_argument("--min-family-count", type=int, default=3)
    parser.add_argument("--gold-audio-floor-minutes", type=int, default=30)
    parser.add_argument("--gold-audio-promotion-hours", type=float, default=1.0)
    parser.add_argument("--gold-audio-stable-hours", type=float, default=2.0)
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


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": str(path)}
    return data if isinstance(data, dict) else {}


def wav_duration_seconds(path: Path | None) -> float | None:
    if path is None or not path.exists() or path.suffix.lower() != ".wav":
        return None
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if rate <= 0:
                return None
            return handle.getnframes() / float(rate)
    except (wave.Error, OSError, EOFError):
        return None


def classify_family(text: str) -> str:
    for family, pattern in FAMILY_RULES:
        if pattern.search(text):
            return family
    return "other_quality_issue"


def artifact_path(row: dict[str, Any], key: str, root: Path) -> Path | None:
    artifacts = row.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    info = artifacts.get(key)
    if not isinstance(info, dict) or not info.get("present"):
        return None
    return resolve(root, str(info.get("path") or ""))


def collect_asr_issues(row: dict[str, Any], root: Path, *, include_rows: bool = True) -> list[dict[str, Any]]:
    path = artifact_path(row, "final_mix_asr_report", root)
    report = read_json(path)
    issues: list[dict[str, Any]] = []
    if include_rows:
        for key in ("flagged_rows", "worst_rows"):
            for item in report.get(key) or []:
                if not isinstance(item, dict):
                    continue
                flags = item.get("flags") if isinstance(item.get("flags"), list) else []
                detail = " ".join(str(x) for x in flags) or f"similarity={item.get('similarity')}"
                issues.append(
                    {
                        "source": f"final_mix_asr.{key}",
                        "family": classify_family(detail),
                        "detail": detail,
                        "segment_id": item.get("segment_id"),
                        "similarity": item.get("similarity"),
                        "flags": flags,
                        "label": item.get("label"),
                    }
                )
    gate = report.get("gate") if isinstance(report.get("gate"), dict) else {}
    if gate and not gate.get("pass", True):
        detail = json.dumps(gate, ensure_ascii=False, sort_keys=True)
        issues.append({"source": "final_mix_asr.gate", "family": classify_family(detail), "detail": detail})
    return issues


def risk_detail(item: dict[str, Any]) -> str:
    parts: list[str] = []
    if item.get("source"):
        parts.append(str(item["source"]))
    if item.get("priority") is not None:
        parts.append(f"priority={item.get('priority')}")
    flags = item.get("flags") if isinstance(item.get("flags"), list) else []
    parts.extend(str(flag) for flag in flags)
    reasons = item.get("reasons") if isinstance(item.get("reasons"), list) else []
    parts.extend(str(reason) for reason in reasons)
    if item.get("reason"):
        parts.append(str(item["reason"]))
    if item.get("selected_candidate"):
        parts.append(f"selected_candidate={item.get('selected_candidate')}")
    if item.get("similarity") is not None:
        parts.append(f"similarity={item.get('similarity')}")
    expected = str(item.get("expected_text") or "")
    asr = str(item.get("asr_text") or "")
    if expected or asr:
        parts.append("expected_asr_mismatch")
    return " ".join(part for part in parts if part)


def collect_risk_issues(row: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    path = artifact_path(row, "risk_queue", root)
    report = read_json(path)
    issues: list[dict[str, Any]] = []
    for item in report.get("entries") or report.get("risk_rows") or []:
        if not isinstance(item, dict):
            continue
        detail = risk_detail(item) or json.dumps(item, ensure_ascii=False, sort_keys=True)
        issues.append(
            {
                "source": item.get("source") or "risk_queue",
                "family": classify_family(detail),
                "detail": detail,
                "segment_id": item.get("segment_id"),
                "similarity": item.get("similarity"),
                "priority": item.get("priority"),
                "flags": item.get("flags") if isinstance(item.get("flags"), list) else [],
                "label": item.get("label"),
            }
        )
    return issues


def collect_review_issues(row: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    path = artifact_path(row, "human_review_pack", root)
    if path is None:
        return []
    manifest_path = path.parent / "review_manifest.json" if path.name.lower() == "index.html" else path
    report = read_json(manifest_path)
    issues: list[dict[str, Any]] = []
    for item in report.get("review_rows") or []:
        if not isinstance(item, dict):
            continue
        reasons = item.get("reasons") if isinstance(item.get("reasons"), list) else []
        if not reasons:
            risk_items = item.get("risk_items") if isinstance(item.get("risk_items"), list) else []
            reasons = [json.dumps(x, ensure_ascii=False, sort_keys=True) for x in risk_items if isinstance(x, dict)]
        detail = " ".join(str(x) for x in reasons)
        if detail:
            issues.append(
                {
                    "source": "human_review_pack",
                    "family": classify_family(detail),
                    "detail": detail,
                    "segment_id": item.get("segment_id"),
                }
            )
    return issues


def collect_fit_audit_issues(row: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    path = artifact_path(row, "fit_audit", root)
    report = read_json(path)
    if not report:
        return []
    issues: list[dict[str, Any]] = []
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    selected_fit = summary.get("selected_fit") if isinstance(summary.get("selected_fit"), dict) else {}
    candidate_fit = summary.get("candidate_fit") if isinstance(summary.get("candidate_fit"), dict) else {}
    if report.get("status") and report.get("status") != "pass":
        issues.append(
            {
                "source": "fit_audit.summary",
                "family": "duration_or_tempo",
                "detail": (
                    f"status={report.get('status')} recommendation={report.get('recommendation')} "
                    f"selected_gt_warning={selected_fit.get('gt_warning_threshold')} "
                    f"candidate_gt_hard={candidate_fit.get('gt_hard_threshold')} "
                    f"candidate_gt_severe={candidate_fit.get('gt_severe_threshold')}"
                ),
                "fit_audit_status": report.get("status"),
                "recommendation": report.get("recommendation"),
            }
        )
    for item in report.get("risk_segments") or []:
        if not isinstance(item, dict):
            continue
        issues.append(
            {
                "source": "fit_audit.risk_segments",
                "family": "duration_or_tempo",
                "detail": f"selected_fit_tempo_delta={item.get('fit_tempo_delta')} fit_applied={item.get('fit_applied')}",
                "segment_id": item.get("segment_id"),
                "label": item.get("label"),
                "fit_tempo_delta": item.get("fit_tempo_delta"),
                "selected_family": item.get("family"),
            }
        )
    return issues


def collect_human_review_blocker_issues(row: dict[str, Any]) -> list[dict[str, Any]]:
    review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
    blockers = review.get("blockers") if isinstance(review.get("blockers"), list) else []
    issues: list[dict[str, Any]] = []
    for item in blockers:
        if not isinstance(item, dict):
            continue
        detail = str(item.get("description") or item.get("reason") or "")
        if detail:
            issues.append(
                {
                    "source": "human_review_blocker",
                    "family": classify_family(detail),
                    "detail": detail,
                    "segment_id": item.get("segment_id"),
                    "label": item.get("label"),
                }
            )
    notes = str(review.get("notes") or "")
    if notes and not issues:
        issues.append({"source": "human_review_blocker", "family": classify_family(notes), "detail": notes})
    return issues


def readiness(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("acceptance_readiness")
    return value if isinstance(value, dict) else {}


def readiness_ready(row: dict[str, Any]) -> bool:
    return readiness(row).get("ready_for_human_review") is True


def lecture_issues(row: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    issues = []
    risk_issues = collect_risk_issues(row, root)
    issues.extend(collect_asr_issues(row, root, include_rows=not risk_issues))
    issues.extend(risk_issues)
    issues.extend(collect_review_issues(row, root))
    issues.extend(collect_fit_audit_issues(row, root))
    issues.extend(collect_human_review_blocker_issues(row))
    for issue in issues:
        issue["lecture"] = row.get("lecture")
        issue["title"] = row.get("title")
    return issues


def decision_for_family(family: str) -> str:
    if family in {"number_or_section", "mixed_latin_term"}:
        return "spoken_text_glossary_or_normalization"
    if family in {"join_or_pause", "tail_truncation", "duration_or_tempo"}:
        return "split_candidate_gate_or_rescue"
    if family == "flat_prosody":
        return "candidate_policy_or_gold_audio_before_training"
    if family == "low_asr_similarity":
        return "asr_review_then_rule_or_candidate_fix"
    return "manual_triage"


def training_readiness(args: argparse.Namespace, reviewed_count: int, strong_families: list[dict[str, Any]]) -> dict[str, Any]:
    gold_audio_minutes = float(getattr(args, "_gold_audio_minutes", 0.0) or 0.0)
    gold_floor_met = gold_audio_minutes >= float(args.gold_audio_floor_minutes)
    if reviewed_count < args.min_reviewed_lectures:
        status = "insufficient_cross_lecture_evidence"
        blockers = [f"reviewed_lectures:{reviewed_count}<{args.min_reviewed_lectures}"]
    elif not strong_families:
        status = "collect_more_evidence_or_fix_with_rules_candidates"
        blockers = ["no_repeated_failure_family_across_required_lectures"]
    elif not gold_floor_met:
        status = "insufficient_human_approved_audio"
        blockers = [f"human_approved_aligned_audio:{gold_audio_minutes:.2f}<{args.gold_audio_floor_minutes}min"]
    else:
        status = "course_adapter_feasibility_review"
        blockers = []
    return {
        "status": status,
        "per_episode_training_allowed": False,
        "adapter_granularity": "course_or_domain_profile",
        "human_approved_aligned_audio_minutes": round(gold_audio_minutes, 3),
        "gold_audio_floor_minutes": args.gold_audio_floor_minutes,
        "gold_audio_floor_met": gold_floor_met,
        "gold_audio_promotion_target_met": gold_audio_minutes >= float(args.gold_audio_promotion_hours) * 60.0,
        "gold_audio_stable_target_met": gold_audio_minutes >= float(args.gold_audio_stable_hours) * 60.0,
        "blockers": blockers,
        "requires_before_training": [
            f"same_failure_family_count>={args.min_family_count}",
            f"same_failure_family_lectures>={args.min_repeat_lectures}",
            f"reviewed_lectures>={args.min_reviewed_lectures}",
            f"human_approved_aligned_audio_floor>={args.gold_audio_floor_minutes}min",
            "fixed_heldout_canary_and_episode_set",
            "no_train_holdout_overlap",
        ],
    }


def collect_gold_audio(row: dict[str, Any], root: Path) -> dict[str, Any]:
    path = artifact_path(row, "final_audio", root)
    duration_sec = wav_duration_seconds(path)
    return {
        "lecture": row.get("lecture"),
        "title": row.get("title") or "",
        "status": row.get("status") or "",
        "final_audio": rel_to_root(path, root) if path else "",
        "duration_sec": round(duration_sec, 3) if duration_sec is not None else None,
        "duration_minutes": round(duration_sec / 60.0, 3) if duration_sec is not None else None,
    }


def build_decision(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    status_path = resolve(root, args.acceptance_status)
    status = read_json(status_path)
    lecture_rows = status.get("lectures") if isinstance(status.get("lectures"), list) else []
    human_reviewed_rows = [
        row
        for row in lecture_rows
        if isinstance(row, dict)
        and row.get("status") in HUMAN_REVIEWED_STATUSES
    ]
    pending_review_rows = [
        row
        for row in lecture_rows
        if isinstance(row, dict)
        and row.get("status") in PENDING_REVIEW_STATUSES
    ]
    pending_content_ready_rows = [row for row in pending_review_rows if readiness_ready(row)]
    pending_content_blocked_rows = [row for row in pending_review_rows if readiness(row) and not readiness_ready(row)]
    all_issues: list[dict[str, Any]] = []
    for row in human_reviewed_rows:
        all_issues.extend(lecture_issues(row, root))
    fit_audit_rows: list[dict[str, Any]] = []
    for row in human_reviewed_rows:
        path = artifact_path(row, "fit_audit", root)
        report = read_json(path)
        if report:
            fit_audit_rows.append(
                {
                    "lecture": row.get("lecture"),
                    "title": row.get("title") or "",
                    "path": rel_to_root(path, root) if path else "",
                    "status": report.get("status"),
                    "recommendation": report.get("recommendation"),
                    "training_signal": report.get("training_signal") if isinstance(report.get("training_signal"), dict) else {},
                    "summary": report.get("summary") if isinstance(report.get("summary"), dict) else {},
                }
            )

    family_counts = Counter(issue["family"] for issue in all_issues)
    lectures_by_family: dict[str, set[int]] = defaultdict(set)
    for issue in all_issues:
        try:
            lectures_by_family[issue["family"]].add(int(issue.get("lecture") or 0))
        except (TypeError, ValueError):
            pass
    gold_audio_rows = [
        collect_gold_audio(row, root)
        for row in human_reviewed_rows
        if row.get("status") == "accepted" and bool(row.get("human_accepted"))
    ]
    measured_gold_minutes = sum(
        float(row["duration_minutes"]) for row in gold_audio_rows if row.get("duration_minutes") is not None
    )

    family_rows = []
    for family, count in family_counts.most_common():
        lecture_count = len(lectures_by_family[family])
        family_rows.append(
            {
                "family": family,
                "issue_count": count,
                "lecture_count": lecture_count,
                "lectures": sorted(x for x in lectures_by_family[family] if x),
                "default_action": decision_for_family(family),
                "training_signal": count >= args.min_family_count and lecture_count >= args.min_repeat_lectures,
            }
        )

    strong_training_families = [row for row in family_rows if row["training_signal"]]
    reviewed_count = len(human_reviewed_rows)
    lecture_local_fit_training_signal = any(
        ((row.get("training_signal") or {}).get("smoke_training_recommended") is True)
        or row.get("recommendation")
        in {
            "repair_fit_policy_and_start_duration_prosody_training_feasibility",
            "start_duration_prosody_training_feasibility",
        }
        for row in fit_audit_rows
    )
    if lecture_local_fit_training_signal:
        recommendation = "start_lecture_local_duration_prosody_lora_experiment"
    elif reviewed_count < args.min_reviewed_lectures:
        recommendation = "do_not_train_insufficient_cross_lecture_evidence"
    elif strong_training_families and measured_gold_minutes < float(args.gold_audio_floor_minutes):
        recommendation = "do_not_train_collect_human_approved_audio"
    elif strong_training_families:
        recommendation = "consider_course_level_adapter_training"
    else:
        recommendation = "do_not_train_use_rules_candidates_and_collect_more_evidence"
    setattr(args, "_gold_audio_minutes", measured_gold_minutes)
    training_readiness_state = training_readiness(args, reviewed_count, strong_training_families)
    gold_audio_summary = {
        "accepted_lectures": len(gold_audio_rows),
        "measured_minutes": round(measured_gold_minutes, 3),
        "floor_minutes": args.gold_audio_floor_minutes,
        "promotion_target_hours": args.gold_audio_promotion_hours,
        "stable_target_hours": args.gold_audio_stable_hours,
        "floor_met": measured_gold_minutes >= float(args.gold_audio_floor_minutes),
        "promotion_target_met": measured_gold_minutes >= float(args.gold_audio_promotion_hours) * 60.0,
        "stable_target_met": measured_gold_minutes >= float(args.gold_audio_stable_hours) * 60.0,
        "lectures": gold_audio_rows,
    }

    return {
        "schema_version": "holodub.mit6824_training_decision.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "acceptance_status": rel_to_root(status_path, root),
        "policy": {
            "default": "do_not_train_per_episode",
            "episode_adapter_policy": "never_train_one_adapter_per_lecture_by_default",
            "min_reviewed_lectures": args.min_reviewed_lectures,
            "min_repeat_lectures": args.min_repeat_lectures,
            "min_family_count": args.min_family_count,
            "adapter_scope": "course_or_domain_profile",
            "gold_audio_floor_minutes": args.gold_audio_floor_minutes,
            "gold_audio_promotion_hours": args.gold_audio_promotion_hours,
            "gold_audio_stable_hours": args.gold_audio_stable_hours,
            "single_episode_failures_route_to": [
                "spoken_text_glossary_or_normalization",
                "split_candidate_gate_or_rescue",
                "lecture_local_duration_prosody_lora_experiment_when_fit_audit_fails",
                "manual_review_anchor_collection",
            ],
        },
        "summary": {
            "reviewed_lectures": reviewed_count,
            "human_reviewed_lectures": [int(row.get("lecture") or 0) for row in human_reviewed_rows if row.get("lecture")],
            "pending_review_lectures": [int(row.get("lecture") or 0) for row in pending_review_rows if row.get("lecture")],
            "pending_content_ready_lectures": [
                int(row.get("lecture") or 0) for row in pending_content_ready_rows if row.get("lecture")
            ],
            "pending_content_blocked_lectures": [
                int(row.get("lecture") or 0) for row in pending_content_blocked_rows if row.get("lecture")
            ],
            "issue_count": len(all_issues),
            "family_counts": dict(family_counts),
            "recommendation": recommendation,
            "strong_training_families": strong_training_families,
            "training_readiness": training_readiness_state,
            "gold_audio": gold_audio_summary,
            "fit_audits": fit_audit_rows,
            "lecture_local_fit_training_signal": lecture_local_fit_training_signal,
        },
        "families": family_rows,
        "issues": all_issues[:500],
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    gold_audio = payload["summary"].get("gold_audio") if isinstance(payload["summary"].get("gold_audio"), dict) else {}
    lines = [
        "# MIT 6.824 Training Decision",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "## Recommendation",
        "",
        f"`{payload['summary']['recommendation']}`",
        "",
        "## Training Readiness",
        "",
        f"- Status: `{payload['summary']['training_readiness']['status']}`",
        f"- Per-episode training allowed: `{payload['summary']['training_readiness']['per_episode_training_allowed']}`",
        f"- Adapter granularity: `{payload['summary']['training_readiness']['adapter_granularity']}`",
        f"- Human-approved aligned audio: `{gold_audio.get('measured_minutes', 0)}min` / floor `{gold_audio.get('floor_minutes', 0)}min`",
        f"- Blockers: `{json.dumps(payload['summary']['training_readiness']['blockers'], ensure_ascii=False)}`",
        "",
        "## Evidence",
        "",
        f"- Human-reviewed lectures: `{payload['summary'].get('human_reviewed_lectures', [])}`",
        f"- Pending review lectures: `{payload['summary'].get('pending_review_lectures', [])}`",
        f"- Pending content-ready lectures: `{payload['summary'].get('pending_content_ready_lectures', [])}`",
        f"- Pending content-blocked lectures: `{payload['summary'].get('pending_content_blocked_lectures', [])}`",
        f"- Issue count: `{payload['summary']['issue_count']}`",
        f"- Family counts: `{json.dumps(payload['summary']['family_counts'], ensure_ascii=False, sort_keys=True)}`",
        f"- Gold audio floor met: `{gold_audio.get('floor_met', False)}`",
        f"- Lecture-local fit training signal: `{payload['summary'].get('lecture_local_fit_training_signal', False)}`",
        "",
        "## Families",
        "",
        "| Family | Issues | Lectures | Default Action | Training Signal |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for row in payload.get("families") or []:
        lines.append(
            f"| {row['family']} | {row['issue_count']} | {row['lectures']} | {row['default_action']} | {row['training_signal']} |"
        )
    lines.extend(
        [
            "",
            "## Fit Audits",
            "",
            "| Lecture | Status | Recommendation | Selected Fit > Warning | Candidate Fit > Hard | Candidate Fit > Severe |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for row in payload["summary"].get("fit_audits") or []:
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        selected = summary.get("selected_fit") if isinstance(summary.get("selected_fit"), dict) else {}
        candidate = summary.get("candidate_fit") if isinstance(summary.get("candidate_fit"), dict) else {}
        lines.append(
            f"| {row.get('lecture')} | {row.get('status')} | {row.get('recommendation')} | "
            f"{selected.get('gt_warning_threshold')} | {candidate.get('gt_hard_threshold')} | {candidate.get('gt_severe_threshold')} |"
        )
    lines.extend(
        [
            "",
            "## Policy",
            "",
            "- Do not promote one adapter per lecture by default.",
            "- A rejected lecture may start a lecture-local LoRA experiment when fit audit shows systemic duration/prosody failure.",
            "- Prefer spoken-text, glossary, normalization, split planning, candidate gate, or rescue policy for isolated failures.",
            "- Consider course/domain adapter training only after repeated failure families appear across multiple reviewed lectures.",
            "- Require human-approved aligned audio and a fixed held-out set before promoting a new adapter.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    payload = build_decision(args, root)
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
                "summary": payload["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
