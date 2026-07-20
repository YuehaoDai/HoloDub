from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one human handoff report for MIT 6.824 delivery automation.")
    parser.add_argument("--status-refresh", default="data/validation/mit6824_status_refresh.json")
    parser.add_argument("--status-refresh-history", default="data/validation/mit6824_status_refresh_history.jsonl")
    parser.add_argument("--goal-status", default="data/validation/mit6824_goal_status.json")
    parser.add_argument("--delivery-index", default="data/validation/mit6824_delivery_index.json")
    parser.add_argument("--watchdog-status", default="data/validation/mit6824_watchdog_status.json")
    parser.add_argument("--runtime-blockers", default="data/validation/mit6824_runtime_blockers.json")
    parser.add_argument("--candidate-coverage", default="data/validation/mit6824_candidate_coverage.json")
    parser.add_argument("--auto-advance-status", default="data/validation/mit6824_auto_advance_status.json")
    parser.add_argument("--v25-finalize-status", default="data/training/lora_runs/main_flow_candidate_episode147_v25/auto_finalize_status.json")
    parser.add_argument("--lecture8-finalize-status", default="data/validation/mit6824_acceptance/lec-08/finalize_status.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_handoff_report.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_handoff_report.md")
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_history(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def progress_point(row: dict[str, Any]) -> dict[str, Any] | None:
    summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
    metrics = summary.get("active_regression") if isinstance(summary.get("active_regression"), dict) else {}
    synthesized = int_value(metrics.get("synthesized"))
    total = int_value(metrics.get("total"))
    timestamp = parse_time(str(row.get("generated_at") or ""))
    if synthesized is None or total is None or timestamp is None:
        return None
    return {"time": timestamp, "synthesized": synthesized, "total": total}


def estimate_progress(history: list[dict[str, Any]], current_metrics: dict[str, Any], *, window_sec: int = 7200) -> dict[str, Any]:
    current_synthesized = int_value(current_metrics.get("synthesized"))
    total = int_value(current_metrics.get("total"))
    if current_synthesized is None or total is None or total <= 0:
        return {"status": "unavailable", "reason": "missing_current_metrics"}
    points = [point for row in history if (point := progress_point(row)) is not None]
    points = [point for point in points if point["total"] == total and point["synthesized"] <= current_synthesized]
    if not points:
        return {"status": "unavailable", "reason": "missing_history", "remaining_segments": max(0, total - current_synthesized)}
    latest = points[-1]
    cutoff = latest["time"].timestamp() - window_sec
    window_points = [point for point in points if point["time"].timestamp() >= cutoff and point["synthesized"] < current_synthesized]
    if not window_points:
        return {
            "status": "no_recent_delta",
            "reason": "no_progress_delta_in_window",
            "synthesized": current_synthesized,
            "total": total,
            "remaining_segments": max(0, total - current_synthesized),
        }
    baseline = window_points[0]
    elapsed_sec = max(0.0, latest["time"].timestamp() - baseline["time"].timestamp())
    delta = current_synthesized - int(baseline["synthesized"])
    if elapsed_sec <= 0 or delta <= 0:
        return {
            "status": "no_recent_delta",
            "reason": "non_positive_delta",
            "synthesized": current_synthesized,
            "total": total,
            "remaining_segments": max(0, total - current_synthesized),
        }
    segments_per_hour = delta / elapsed_sec * 3600.0
    remaining = max(0, total - current_synthesized)
    eta_sec = remaining / segments_per_hour * 3600.0 if segments_per_hour > 0 else None
    return {
        "status": "estimated",
        "window_sec": window_sec,
        "baseline_time": baseline["time"].isoformat(),
        "latest_time": latest["time"].isoformat(),
        "delta_segments": delta,
        "elapsed_sec": round(elapsed_sec, 3),
        "segments_per_hour": round(segments_per_hour, 3),
        "synthesized": current_synthesized,
        "total": total,
        "remaining_segments": remaining,
        "eta_sec": round(eta_sec, 3) if eta_sec is not None else None,
        "eta_hours": round(eta_sec / 3600.0, 3) if eta_sec is not None else None,
    }


def status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    return summary if isinstance(summary, dict) else {}


def watcher_recovery_commands(watchdog: dict[str, Any]) -> list[str]:
    commands = ["python tools\\mit6824_watchdog.py --restart-status-watch"]
    for row in watchdog.get("watchers") or []:
        if not isinstance(row, dict) or row.get("status") != "missing":
            continue
        command = str(row.get("restart_command") or "")
        if command and command not in commands:
            commands.append(command)
    return commands


def review_ready_commands(delivery: dict[str, Any]) -> list[str]:
    commands: list[str] = []
    for row in delivery.get("rows") or []:
        if not isinstance(row, dict) or row.get("state") != "review_ready":
            continue
        if row.get("human_review_pack_abs_path"):
            commands.append(f"open review pack: {row['human_review_pack_abs_path']}")
        if row.get("final_video_abs_path"):
            commands.append(f"open final video: {row['final_video_abs_path']}")
        if row.get("accept_dry_run_report"):
            commands.append(f"open dry-run report: {row['accept_dry_run_report']}")
        if row.get("accept_dry_run_command"):
            commands.append(str(row["accept_dry_run_command"]))
        if row.get("accept_command"):
            commands.append(str(row["accept_command"]))
        if row.get("blocker_command"):
            commands.append(str(row["blocker_command"]))
    return commands


def content_blocked_commands(delivery: dict[str, Any]) -> list[str]:
    commands = [
        "open data\\validation\\mit6824_delivery_index.md",
        "open data\\validation\\mit6824_acceptance_status.md",
    ]
    for row in delivery.get("rows") or []:
        if not isinstance(row, dict) or row.get("state") != "content_blocked":
            continue
        automation = str(row.get("automation_action") or "")
        if automation and automation not in commands:
            commands.append(automation)
    return commands


def source_blocked_commands() -> list[str]:
    return [
        "open data\\validation\\mit6824_course_tracker.md",
        "open data\\validation\\mit6824_course_advance_status.json",
        "Get-ChildItem data\\source_videos\\mit-6.824-2020",
        "python tools\\mit6824_course_tracker.py",
        "python tools\\mit6824_course_advance.py --refresh",
    ]


def choose_next_action(
    *,
    status_refresh: dict[str, Any],
    goal: dict[str, Any],
    delivery: dict[str, Any],
    watchdog: dict[str, Any],
    candidate_coverage: dict[str, Any],
    auto_advance: dict[str, Any],
    v25_finalize: dict[str, Any],
    lecture8_finalize: dict[str, Any],
) -> dict[str, Any]:
    refresh_summary = status_summary(status_refresh)
    watchdog_summary = status_summary(watchdog)
    delivery_summary = status_summary(delivery)
    coverage_summary = status_summary(candidate_coverage)
    goal_action = str(goal.get("next_action") or refresh_summary.get("next_action") or "")
    auto_status = str(auto_advance.get("status") or "")
    pending_fallback = int(coverage_summary.get("eligible_pending_fallback_segments") or 0)
    missing_watchers = watchdog_summary.get("missing_required") if isinstance(watchdog_summary, dict) else []
    missing_reasons = watchdog_summary.get("missing_reasons") if isinstance(watchdog_summary, dict) else []
    docker_status = str(watchdog_summary.get("docker_status") or "")
    if docker_status and docker_status not in {"available", "not_checked"} or (
        isinstance(missing_reasons, list) and "docker_engine_unavailable" in missing_reasons
    ):
        return {
            "status": "repair_docker_engine",
            "reason": "docker_engine_unavailable_blocks_episode8_pipeline_and_watchers",
            "commands": [
                "Start Docker Desktop or Docker Desktop Service with sufficient permissions",
                "open data\\validation\\mit6824_runtime_blockers.md",
                "docker version",
                "wsl -l -v",
                "python tools\\mit6824_watchdog.py --allow-missing",
                "python tools\\mit6824_watchdog.py --restart-status-watch --allow-missing",
                "python tools\\mit6824_finalize_lecture.py --lecture 8 --refresh-tracker --watch --execute --poll-sec 300 --timeout-sec 172800",
            ],
        }
    if missing_watchers:
        return {
            "status": "repair_watchers",
            "reason": "one_or_more_required_watchers_missing",
            "commands": watcher_recovery_commands(watchdog),
        }
    if auto_status == "regression_freeze_failed":
        return {
            "status": "inspect_regression_freeze",
            "reason": "v25 completed but regression suite freeze failed",
            "commands": [
                "python tools\\mit6824_candidate_coverage.py --job-ids 342,343,344,345,346,347",
                "python tools\\mit6824_regression_suite.py --freeze",
                "open data\\validation\\mit6824_regression\\lecture7_v25_regression_suite.md",
            ],
            "hard_issues": auto_advance.get("hard_issues") or [],
        }
    if auto_status == "ml_service_restart_failed":
        return {
            "status": "repair_ml_service_restart",
            "reason": "auto-advance could not restart ml-service before lecture 8",
            "commands": [
                "docker compose -f docker-compose.yml -f docker-compose.ml-app.yml -f docker-compose.lora-shadow.yml restart ml-service",
                "python tools\\mit6824_auto_advance.py --lecture 8 --wait-finalize-status data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\auto_finalize_status.json --execute --restart-ml-service-before-start",
            ],
        }
    if auto_status in {"start_failed", "blocked_active_run"}:
        return {
            "status": "inspect_lecture8_start",
            "reason": f"auto-advance could not start lecture 8: {auto_status}",
            "commands": [
                "python tools\\mit6824_start_lecture.py --lecture 8",
                "python tools\\mit6824_course_tracker.py",
            ],
        }
    review_commands = review_ready_commands(delivery)
    if review_commands:
        return {
            "status": "human_review_ready",
            "reason": "one_or_more_lectures_are_ready_for_human_review",
            "commands": review_commands,
        }
    if delivery_summary.get("content_blocked"):
        return {
            "status": "repair_acceptance_content",
            "reason": "one_or_more_lectures_have_content_readiness_errors_before_human_acceptance",
            "commands": content_blocked_commands(delivery),
        }
    if delivery_summary.get("source_blocked") or delivery_summary.get("course_advance_status") == "waiting_for_source_preflight":
        return {
            "status": "repair_source_preflight",
            "reason": "next_lecture_source_preflight_failed_before_start",
            "commands": source_blocked_commands(),
        }
    if goal_action.startswith("diagnose_v25_candidate_regression"):
        return {
            "status": "diagnose_v25_regression",
            "reason": goal_action,
            "commands": [
                "python tools\\mit6824_status_refresh.py",
                "python tools\\mit6824_v25_diagnostics.py",
                "python tools\\mit6824_watchdog.py --restart-status-watch",
            ],
        }
    if goal_action == "monitor_v25_candidate_regression_slow":
        return {
            "status": "monitor_v25_regression_slow",
            "reason": "v25 is running but has slowed below the recent-progress threshold",
            "commands": [
                "python tools\\mit6824_status_refresh.py",
                "python tools\\mit6824_v25_diagnostics.py",
                "python tools\\mit6824_watchdog.py --restart-status-watch",
            ],
        }
    if goal_action == "wait_for_v25_candidate_regression":
        if pending_fallback:
            return {
                "status": "monitor_v25_fallback_risk",
                "reason": f"{pending_fallback} eligible pending segment(s) already have fallback_reason",
                "commands": [
                    "python tools\\mit6824_candidate_coverage.py --job-ids 342,343,344,345,346,347",
                    "python tools\\mit6824_v25_diagnostics.py",
                    "open data\\validation\\mit6824_candidate_coverage.md",
                ],
            }
        return {
            "status": "wait_for_v25",
            "reason": "v25 is still running and the watcher set is healthy",
            "commands": ["python tools\\mit6824_status_refresh.py"],
        }
    if goal_action == "wait_for_v25_finalize_then_auto_start_lecture_8":
        return {
            "status": "wait_for_v25_finalizer_auto_start",
            "reason": "v25 synthesis appears complete; finalizer/auto-advance should own the next step",
            "commands": [
                "python tools\\mit6824_watchdog.py --restart-status-watch",
                "python tools\\mit6824_auto_advance.py --lecture 8 --wait-finalize-status data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\auto_finalize_status.json",
            ],
        }
    if str(lecture8_finalize.get("status") or "").startswith("waiting_"):
        return {
            "status": "wait_for_lecture8_completion_or_finalizer",
            "reason": str(lecture8_finalize.get("status") or ""),
            "commands": ["python tools\\mit6824_status_refresh.py"],
        }
    return {
        "status": "refresh_and_inspect",
        "reason": goal_action or "unknown",
        "commands": [
            "python tools\\mit6824_status_refresh.py",
            "open data\\validation\\mit6824_delivery_index.md",
            "open data\\validation\\mit6824_goal_status.md",
        ],
    }


def build_report(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    paths = {
        "status_refresh": resolve(root, args.status_refresh),
        "status_refresh_history": resolve(root, args.status_refresh_history),
        "goal_status": resolve(root, args.goal_status),
        "delivery_index": resolve(root, args.delivery_index),
        "watchdog_status": resolve(root, args.watchdog_status),
        "runtime_blockers": resolve(root, args.runtime_blockers),
        "candidate_coverage": resolve(root, args.candidate_coverage),
        "auto_advance_status": resolve(root, args.auto_advance_status),
        "v25_finalize_status": resolve(root, args.v25_finalize_status),
        "lecture8_finalize_status": resolve(root, args.lecture8_finalize_status),
    }
    status_refresh = read_json(paths["status_refresh"])
    goal = read_json(paths["goal_status"])
    delivery = read_json(paths["delivery_index"])
    watchdog = read_json(paths["watchdog_status"])
    runtime_blockers = read_json(paths["runtime_blockers"])
    candidate_coverage = read_json(paths["candidate_coverage"])
    auto_advance = read_json(paths["auto_advance_status"])
    v25_finalize = read_json(paths["v25_finalize_status"])
    lecture8_finalize = read_json(paths["lecture8_finalize_status"])
    next_action = choose_next_action(
        status_refresh=status_refresh,
        goal=goal,
        delivery=delivery,
        watchdog=watchdog,
        candidate_coverage=candidate_coverage,
        auto_advance=auto_advance,
        v25_finalize=v25_finalize,
        lecture8_finalize=lecture8_finalize,
    )
    refresh_summary = status_summary(status_refresh)
    active = goal.get("active_regression") if isinstance(goal.get("active_regression"), dict) else {}
    active_metrics = active.get("metrics") if isinstance(active.get("metrics"), dict) else {}
    active_health = active.get("health") if isinstance(active.get("health"), dict) else {}
    current_metrics = active_metrics or refresh_summary.get("active_regression", {})
    progress = estimate_progress(read_history(paths["status_refresh_history"]), current_metrics)
    return {
        "schema_version": "holodub.mit6824_handoff.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "paths": {key: rel_to_root(path, root) for key, path in paths.items()},
        "next_action": next_action,
        "objective": goal.get("objective")
        or "MIT 6.824 lectures 8-20 dubbing and acceptance; lecture 7 is regression fixture only",
        "progress_contract": goal.get("progress_contract")
        or {
            "delivery_range": "lectures_8_to_20",
            "lecture_7_role": "lora_candidate_regression_fixture",
            "training_policy": "do_not_train_per_episode",
            "adapter_scope": "course_or_domain_profile",
            "regression_fixture_lock_policy": "frozen_requires_zero_hard_issues_locked_with_open_issues_allows_documented_nonblocking_fixture_issues",
        },
        "summary": {
            "goal_next_action": goal.get("next_action") or refresh_summary.get("next_action", ""),
            "active_regression": current_metrics,
            "active_regression_health": active_health or refresh_summary.get("active_regression_health", {}),
            "progress_estimate": progress,
            "watchdog": status_summary(watchdog) or refresh_summary.get("watchdog"),
            "runtime_blockers": status_summary(runtime_blockers) or refresh_summary.get("runtime_blockers"),
            "candidate_coverage": refresh_summary.get("candidate_coverage") or status_summary(candidate_coverage),
            "delivery_index": refresh_summary.get("delivery_index") or status_summary(delivery),
            "training_decision": (refresh_summary.get("training_decision") or {}).get("summary", {})
            if isinstance(refresh_summary.get("training_decision"), dict)
            else {},
            "auto_advance_status": auto_advance.get("status", ""),
            "v25_finalize_returncode": v25_finalize.get("returncode", ""),
            "lecture8_finalize_status": lecture8_finalize.get("status", ""),
        },
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    next_action = report.get("next_action") if isinstance(report.get("next_action"), dict) else {}
    metrics = summary.get("active_regression") if isinstance(summary.get("active_regression"), dict) else {}
    health = summary.get("active_regression_health") if isinstance(summary.get("active_regression_health"), dict) else {}
    progress = summary.get("progress_estimate") if isinstance(summary.get("progress_estimate"), dict) else {}
    contract = report.get("progress_contract") if isinstance(report.get("progress_contract"), dict) else {}
    delivery = summary.get("delivery_index") if isinstance(summary.get("delivery_index"), dict) else {}
    lines = [
        "# MIT 6.824 Handoff Report",
        "",
        f"Generated: `{report.get('generated_at')}`",
        "",
        "## Objective",
        "",
        f"- Target: `{report.get('objective', '')}`",
        f"- Delivery range: `{contract.get('delivery_range', '')}`",
        f"- Lecture 7 role: `{contract.get('lecture_7_role', '')}`",
        f"- Training policy: `{contract.get('training_policy', '')}`",
        f"- Adapter scope: `{contract.get('adapter_scope', '')}`",
        f"- Fixture lock policy: `{contract.get('regression_fixture_lock_policy', 'frozen_requires_zero_hard_issues_locked_with_open_issues_allows_documented_nonblocking_fixture_issues')}`",
        "",
        "## Next Action",
        "",
        f"- Status: `{next_action.get('status', '')}`",
        f"- Reason: `{next_action.get('reason', '')}`",
        "",
        "## Commands",
        "",
    ]
    commands = next_action.get("commands") if isinstance(next_action.get("commands"), list) else []
    if commands:
        lines.extend(f"- `{command}`" for command in commands)
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Current State",
            "",
            f"- Goal next action: `{summary.get('goal_next_action', '')}`",
            f"- v25 synthesized: `{metrics.get('synthesized', '')}/{metrics.get('total', '')}`",
            f"- v25 candidate audio: `{metrics.get('candidate_audio', '')}`",
            f"- v25 fallback: `{metrics.get('fallback', '')}`",
            f"- v25 mean drift: `{metrics.get('mean_abs_drift', '')}ms`",
            f"- v25 max drift: `{metrics.get('max_abs_drift', '')}ms`",
            f"- v25 health: `{health.get('status', '')}` ({health.get('reason', '')})",
            f"- v25 progress estimate: `{json.dumps(progress, ensure_ascii=False, sort_keys=True)}`",
            f"- Watchdog: `{json.dumps(summary.get('watchdog', {}), ensure_ascii=False, sort_keys=True)}`",
            f"- Candidate coverage: `{json.dumps(summary.get('candidate_coverage', {}), ensure_ascii=False, sort_keys=True)}`",
            f"- Delivery: `{json.dumps(summary.get('delivery_index', {}), ensure_ascii=False, sort_keys=True)}`",
            f"- Next production lecture: `{delivery.get('next_not_started', '')}`",
            f"- Auto-advance: `{summary.get('auto_advance_status', '')}`",
            f"- v25 finalizer rc: `{summary.get('v25_finalize_returncode', '')}`",
            f"- Lecture 8 finalizer: `{summary.get('lecture8_finalize_status', '')}`",
            "",
            "## Training Guardrail",
            "",
            "- Do not train one adapter per lecture.",
            "- Use rules/glossary/normalization for isolated pronunciation, number, and formatting issues.",
            "- Use split/candidate/gate/rescue repairs for isolated long-clause, join, tail, or tempo issues.",
            "- Consider course/domain adapter training only after repeated failure families appear across multiple reviewed lectures with enough human-approved aligned audio and fixed held-out validation.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    report = build_report(args, root)
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    write_json(output_json, report)
    write_markdown(output_md, report)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "next_action": report["next_action"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
