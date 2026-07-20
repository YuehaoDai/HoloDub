from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_ARTIFACTS = [
    "final_video",
    "final_audio",
    "final_mix_asr_report",
    "fit_audit",
    "risk_queue",
    "human_review_pack",
    "adapter_profile_provenance",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a human-facing MIT 6.824 lecture delivery index from existing status artifacts."
    )
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--course-advance-status", default="data/validation/mit6824_course_advance_status.json")
    parser.add_argument("--training-decision", default="data/validation/mit6824_training_decision.json")
    parser.add_argument("--goal-status", default="data/validation/mit6824_goal_status.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_delivery_index.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_delivery_index.md")
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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": str(path)}
    return payload if isinstance(payload, dict) else {}


def artifact(row: dict[str, Any], key: str) -> dict[str, Any]:
    artifacts = row.get("artifacts") if isinstance(row.get("artifacts"), dict) else {}
    value = artifacts.get(key)
    return value if isinstance(value, dict) else {"present": False, "path": "", "size_bytes": 0}


def absolute_artifact_path(row: dict[str, Any], key: str, root: Path) -> str:
    path = artifact(row, key).get("path")
    if not isinstance(path, str) or not path:
        return ""
    resolved = resolve(root, path)
    return str(resolved) if resolved else ""


def first_lecture_with_status(lectures: list[dict[str, Any]], status: str) -> int | None:
    for row in lectures:
        if row.get("status") == status:
            try:
                return int(row.get("lecture") or 0)
            except (TypeError, ValueError):
                return None
    return None


def acceptance_action_command(row: dict[str, Any], key: str, fallback: str) -> str:
    actions = row.get("acceptance_actions")
    if isinstance(actions, dict):
        command = actions.get(key)
        if isinstance(command, str) and command:
            return command
    return fallback


def acceptance_action_value(row: dict[str, Any], key: str, fallback: str) -> str:
    actions = row.get("acceptance_actions")
    if isinstance(actions, dict):
        value = actions.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def readiness(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("acceptance_readiness")
    return value if isinstance(value, dict) else {}


def readiness_has_hard_errors(row: dict[str, Any]) -> bool:
    value = readiness(row)
    if not value:
        return False
    return value.get("ready_for_human_review") is False


def delivery_action(
    row: dict[str, Any],
    *,
    next_not_started: int | None,
    course_next_action: str,
    course_advance_status: str = "",
) -> dict[str, Any]:
    lecture = int(row.get("lecture") or 0)
    status = str(row.get("status") or "")
    missing = row.get("missing") if isinstance(row.get("missing"), list) else []
    if status == "accepted":
        return {
            "state": "done",
            "human_action": "none",
            "automation_action": "none",
            "accept_dry_run_report": "",
            "accept_dry_run_command": "",
            "accept_command": "",
            "blocker_command": "",
        }
    if status == "awaiting_human_acceptance":
        if readiness_has_hard_errors(row):
            return {
                "state": "content_blocked",
                "human_action": "inspect_content_readiness_errors_before_acceptance",
                "automation_action": f"python tools\\mit6824_finalize_lecture.py --lecture {lecture} --refresh-tracker --watch --execute --poll-sec 300",
                "accept_dry_run_report": "",
                "accept_dry_run_command": "",
                "accept_command": "",
                "blocker_command": "",
                "content_errors": readiness(row).get("content_errors") or [],
                "content_warnings": readiness(row).get("content_warnings") or [],
            }
        dry_run_report = f"data/validation/mit6824_acceptance/lec-{lecture:02d}/acceptance_dry_run_report.json"
        accept_dry_run_command = f"python tools\\mit6824_accept_lecture.py --lecture {lecture} --reviewer user --notes \"accepted after human review\" --refresh-status --auto-advance-next --dry-run --dry-run-output {dry_run_report}"
        accept_command = f"python tools\\mit6824_accept_lecture.py --lecture {lecture} --reviewer user --notes \"accepted after human review\" --refresh-status --auto-advance-next"
        blocker_command = f"python tools\\mit6824_record_review_blockers.py --lecture {lecture} --reviewer user --notes \"needs repair after human review\" --blocker \"describe blocker\" --refresh-status"
        return {
            "state": "review_ready",
            "human_action": "open_review_pack_and_final_video_then_accept_or_record_blockers",
            "automation_action": "wait_for_human_acceptance",
            "accept_dry_run_report": acceptance_action_value(row, "accept_dry_run_report", dry_run_report),
            "accept_dry_run_command": acceptance_action_command(
                row,
                "accept_dry_run_command",
                accept_dry_run_command,
            ),
            "accept_command": acceptance_action_command(row, "accept_command", accept_command),
            "blocker_command": acceptance_action_command(row, "blocker_command", blocker_command),
        }
    if status == "review_blocked":
        return {
            "state": "blocked",
            "human_action": "repair_blockers_then_refinalize",
            "automation_action": "manual_repair_then_finalize",
            "accept_dry_run_report": "",
            "accept_dry_run_command": "",
            "accept_command": "",
            "blocker_command": "",
        }
    if status == "missing_acceptance_artifacts":
        return {
            "state": "needs_finalization",
            "human_action": "none",
            "automation_action": f"python tools\\mit6824_finalize_lecture.py --lecture {lecture} --refresh-tracker --watch --execute --poll-sec 300",
            "accept_dry_run_report": "",
            "accept_dry_run_command": "",
            "accept_command": "",
            "blocker_command": "",
            "missing": missing,
        }
    if status == "in_progress":
        return {
            "state": "running",
            "human_action": "none",
            "automation_action": "wait_for_episode_completion_and_finalizer",
            "accept_dry_run_report": "",
            "accept_dry_run_command": "",
            "accept_command": "",
            "blocker_command": "",
        }
    if status == "not_started" and lecture == next_not_started:
        if course_advance_status == "waiting_for_source_preflight":
            return {
                "state": "source_blocked",
                "human_action": "repair_source_preflight_before_start",
                "automation_action": "add_or_fix_source_video_then_refresh_tracker",
                "accept_dry_run_report": "",
                "accept_dry_run_command": "",
                "accept_command": "",
                "blocker_command": "",
            }
        if course_advance_status == "waiting_for_human_acceptance":
            return {
                "state": "waiting_previous_acceptance",
                "human_action": "none",
                "automation_action": "wait_for_previous_lecture_acceptance",
                "accept_dry_run_report": "",
                "accept_dry_run_command": "",
                "accept_command": "",
                "blocker_command": "",
            }
        state = "next_to_start"
        human_action = "none"
        if course_next_action == "wait_for_v25_candidate_regression":
            automation = "wait_for_lecture_7_regression_freeze_then_auto_start"
        elif course_next_action.startswith("monitor_v25_candidate_regression"):
            automation = course_next_action
        elif course_next_action.startswith("diagnose_v25_candidate_regression"):
            automation = course_next_action
            human_action = "inspect_v25_candidate_regression"
            state = "regression_needs_diagnosis"
        elif course_next_action == "wait_for_v25_finalize_then_auto_start_lecture_8":
            automation = course_next_action
        else:
            automation = f"python tools\\mit6824_start_lecture.py --lecture {lecture} --execute"
        return {
            "state": state,
            "human_action": human_action,
            "automation_action": automation,
            "accept_dry_run_report": "",
            "accept_dry_run_command": "",
            "accept_command": "",
            "blocker_command": "",
        }
    if status == "not_started":
        return {
            "state": "queued",
            "human_action": "none",
            "automation_action": "wait_for_previous_lecture_acceptance",
            "accept_dry_run_report": "",
            "accept_dry_run_command": "",
            "accept_command": "",
            "blocker_command": "",
        }
    return {
        "state": "unknown",
        "human_action": "inspect_acceptance_status",
        "automation_action": "manual_triage",
        "accept_dry_run_report": "",
        "accept_dry_run_command": "",
        "accept_command": "",
        "blocker_command": "",
    }


def build_index(
    *,
    root: Path,
    acceptance_status: dict[str, Any],
    course_advance: dict[str, Any],
    training_decision: dict[str, Any],
    goal_status: dict[str, Any],
) -> dict[str, Any]:
    lectures = [row for row in acceptance_status.get("lectures") or [] if isinstance(row, dict)]
    next_not_started = first_lecture_with_status(lectures, "not_started")
    course_next = str(goal_status.get("next_action") or "")
    course_advance_next = course_advance.get("next_action") if isinstance(course_advance.get("next_action"), dict) else {}
    training_summary = training_decision.get("summary") if isinstance(training_decision.get("summary"), dict) else {}
    rows: list[dict[str, Any]] = []
    for row in lectures:
        lecture = int(row.get("lecture") or 0)
        action = delivery_action(
            row,
            next_not_started=next_not_started,
            course_next_action=course_next,
            course_advance_status=str(course_advance_next.get("status", "")),
        )
        rows.append(
            {
                "lecture": lecture,
                "title": row.get("title") or "",
                "status": row.get("status") or "",
                "state": action["state"],
                "human_action": action["human_action"],
                "automation_action": action["automation_action"],
                "accept_dry_run_report": action["accept_dry_run_report"],
                "accept_dry_run_command": action["accept_dry_run_command"],
                "accept_command": action["accept_command"],
                "blocker_command": action["blocker_command"],
                "acceptance_actions": row.get("acceptance_actions") if isinstance(row.get("acceptance_actions"), dict) else {},
                "acceptance_readiness": readiness(row),
                "human_review": row.get("human_review") if isinstance(row.get("human_review"), dict) else {},
                "missing": row.get("missing") if isinstance(row.get("missing"), list) else [],
                "final_video": artifact(row, "final_video"),
                "final_video_abs_path": absolute_artifact_path(row, "final_video", root),
                "human_review_pack": artifact(row, "human_review_pack"),
                "human_review_pack_abs_path": absolute_artifact_path(row, "human_review_pack", root),
                "final_mix_asr_report": artifact(row, "final_mix_asr_report"),
                "risk_queue": artifact(row, "risk_queue"),
                "adapter_profile_provenance": artifact(row, "adapter_profile_provenance"),
            }
        )
    status_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    for row in rows:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        state_counts[row["state"]] = state_counts.get(row["state"], 0) + 1
    review_ready = [row["lecture"] for row in rows if row["state"] == "review_ready"]
    content_blocked = [row["lecture"] for row in rows if row["state"] == "content_blocked"]
    source_blocked = [row["lecture"] for row in rows if row["state"] == "source_blocked"]
    accepted = [row["lecture"] for row in rows if row["state"] == "done"]
    return {
        "schema_version": "holodub.mit6824_delivery_index.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_lectures": len(rows),
            "status_counts": status_counts,
            "state_counts": state_counts,
            "accepted": accepted,
            "review_ready": review_ready,
            "content_blocked": content_blocked,
            "source_blocked": source_blocked,
            "next_not_started": next_not_started,
            "course_next_action": course_next,
            "course_advance_status": course_advance_next.get("status", ""),
            "after_acceptance": course_advance_next.get("after_acceptance", {})
            if isinstance(course_advance_next.get("after_acceptance"), dict)
            else {},
            "training_recommendation": training_summary.get("recommendation", ""),
            "training_readiness": (training_summary.get("training_readiness") or {}).get("status", ""),
        },
        "rows": rows,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, index: dict[str, Any]) -> None:
    summary = index.get("summary") if isinstance(index.get("summary"), dict) else {}
    lines = [
        "# MIT 6.824 Delivery Index",
        "",
        f"Generated: `{index.get('generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Status counts: `{json.dumps(summary.get('status_counts', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- State counts: `{json.dumps(summary.get('state_counts', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- Review ready: `{summary.get('review_ready', [])}`",
        f"- Content blocked: `{summary.get('content_blocked', [])}`",
        f"- Source blocked: `{summary.get('source_blocked', [])}`",
        f"- Accepted: `{summary.get('accepted', [])}`",
        f"- Next not-started: `{summary.get('next_not_started')}`",
        f"- Course next action: `{summary.get('course_next_action', '')}`",
        f"- After acceptance: `{json.dumps(summary.get('after_acceptance', {}), ensure_ascii=False, sort_keys=True)}`",
        f"- Training recommendation: `{summary.get('training_recommendation', '')}`",
        "",
        "## Lectures",
        "",
        "| Lecture | Title | State | Human Action | Automation Action | Video | Review Pack |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for row in index.get("rows") or []:
        lines.append(
            "| {lecture} | {title} | {state} | {human_action} | {automation_action} | {video} | {review} |".format(
                lecture=row.get("lecture", ""),
                title=str(row.get("title") or "").replace("|", "\\|"),
                state=row.get("state", ""),
                human_action=str(row.get("human_action") or "").replace("|", "\\|"),
                automation_action=str(row.get("automation_action") or "").replace("|", "\\|"),
                video=(row.get("final_video") or {}).get("path", ""),
                review=(row.get("human_review_pack") or {}).get("path", ""),
            )
        )
    content_blocked_rows = [row for row in index.get("rows") or [] if row.get("state") == "content_blocked"]
    if content_blocked_rows:
        lines.extend(["", "## Content Readiness Blocks", ""])
        for row in content_blocked_rows:
            readiness = row.get("acceptance_readiness") if isinstance(row.get("acceptance_readiness"), dict) else {}
            lines.append(f"### Lecture {row.get('lecture')}: {row.get('title') or ''}")
            lines.append(f"- Errors: `{json.dumps(readiness.get('content_errors') or [], ensure_ascii=False)}`")
            lines.append(f"- Warnings: `{json.dumps(readiness.get('content_warnings') or [], ensure_ascii=False)}`")
            lines.append(f"- Repair: `{row.get('automation_action') or ''}`")
    lines.extend(
        [
            "",
            "## Accept Dry-run Commands",
            "",
        ]
    )
    any_command = False
    for row in index.get("rows") or []:
        command = row.get("accept_dry_run_command")
        if command:
            any_command = True
            report = row.get("accept_dry_run_report") or ""
            report_note = f" report `{report}`;" if report else ""
            lines.append(f"- Lecture {row.get('lecture')}:{report_note} command `{command}`")
    if not any_command:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Accept Commands",
            "",
        ]
    )
    any_command = False
    for row in index.get("rows") or []:
        command = row.get("accept_command")
        if command:
            any_command = True
            lines.append(f"- Lecture {row.get('lecture')}: `{command}`")
    if not any_command:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Blocker Commands",
            "",
        ]
    )
    any_command = False
    for row in index.get("rows") or []:
        command = str(row.get("blocker_command") or "")
        if command:
            any_command = True
            lines.append(f"- Lecture {row.get('lecture')}: `{command}`")
    if not any_command:
        lines.append("- None")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    acceptance_path = resolve(root, args.acceptance_status)
    advance_path = resolve(root, args.course_advance_status)
    training_path = resolve(root, args.training_decision)
    goal_path = resolve(root, args.goal_status)
    index = build_index(
        root=root,
        acceptance_status=read_json(acceptance_path),
        course_advance=read_json(advance_path),
        training_decision=read_json(training_path),
        goal_status=read_json(goal_path),
    )
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    write_json(output_json, index)
    write_markdown(output_md, index)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "summary": index["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
