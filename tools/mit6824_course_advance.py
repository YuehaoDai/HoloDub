from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


READY_FINALIZE_STATUSES = {"finalized", "complete", "completed", "finalized_for_human_review"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decide and optionally run the next safe MIT 6.824 course-level dubbing action."
    )
    parser.add_argument("--lecture-start", type=int, default=8)
    parser.add_argument("--lecture-end", type=int, default=20)
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--tracker-md", default="data/validation/mit6824_course_tracker.md")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--acceptance-status-md", default="data/validation/mit6824_acceptance_status.md")
    parser.add_argument("--acceptance-root", default="data/validation/mit6824_acceptance")
    parser.add_argument(
        "--wait-finalize-status",
        default="data/training/lora_runs/main_flow_candidate_episode147_v25/auto_finalize_status.json",
        help="Regression/finalizer status that must pass before lecture-start can be created. Empty disables this gate.",
    )
    parser.add_argument("--output", default="data/validation/mit6824_course_advance_status.json")
    parser.add_argument("--history-jsonl", default="data/validation/mit6824_course_advance_history.jsonl")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--execute", action="store_true", help="Run the selected start/finalize action. Default is dry-run.")
    parser.add_argument("--watch", action="store_true", help="Keep polling safe next actions until human review or completion.")
    parser.add_argument("--poll-sec", type=float, default=300.0)
    parser.add_argument("--timeout-sec", type=float, default=0.0)
    parser.add_argument("--refresh", action="store_true", help="Refresh tracker and acceptance status before deciding.")
    parser.add_argument(
        "--allow-unaccepted-previous",
        action="store_true",
        help="Allow starting a later lecture before the previous lecture is human accepted.",
    )
    parser.add_argument(
        "--spawn-finalizer",
        action="store_true",
        help="After starting a lecture, spawn its finalizer watcher as a background process.",
    )
    parser.add_argument("--finalizer-poll-sec", type=float, default=300.0)
    parser.add_argument("--finalizer-timeout-sec", type=float, default=172800.0)
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


def run(cmd: list[str], *, cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def parse_json_output(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
        return decoded if isinstance(decoded, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                decoded = json.loads(raw[start : end + 1])
                return decoded if isinstance(decoded, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def command_result(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "json": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-4000:],
    }


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


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def refresh_tracker(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [
        args.python,
        "tools/mit6824_course_tracker.py",
        "--lecture-start",
        str(args.lecture_start),
        "--lecture-end",
        str(args.lecture_end),
        "--output-json",
        args.tracker,
        "--output-md",
        args.tracker_md,
    ]
    return command_result(cmd, run(cmd, cwd=root, timeout=120))


def refresh_acceptance_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [
        args.python,
        "tools/mit6824_acceptance_status.py",
        "--tracker",
        args.tracker,
        "--acceptance-root",
        args.acceptance_root,
        "--output-json",
        args.acceptance_status,
        "--output-md",
        args.acceptance_status_md,
    ]
    return command_result(cmd, run(cmd, cwd=root, timeout=120))


def finalize_gate(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    path = resolve(root, args.wait_finalize_status)
    if path is None:
        return {"ready": True, "status": "not_configured", "path": ""}
    payload = read_json(path)
    returncode = payload.get("returncode")
    status = payload.get("status")
    ready = returncode == 0 or status in READY_FINALIZE_STATUSES or bool(payload.get("completed"))
    return {
        "ready": ready,
        "status": "ready" if ready else "waiting_for_finalize",
        "path": rel_to_root(path, root),
        "returncode": returncode,
        "finalize_status": status,
        "completed": bool(payload.get("completed")),
        "updated_at_epoch": payload.get("updated_at_epoch"),
    }


def acceptance_by_lecture(acceptance: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for row in acceptance.get("lectures") or []:
        if not isinstance(row, dict):
            continue
        try:
            lecture = int(row.get("lecture") or 0)
        except (TypeError, ValueError):
            continue
        if lecture:
            rows[lecture] = row
    return rows


def tracker_by_lecture(tracker: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for row in tracker.get("lectures") or []:
        if not isinstance(row, dict):
            continue
        try:
            lecture = int(row.get("lecture") or 0)
        except (TypeError, ValueError):
            continue
        if lecture:
            rows[lecture] = row
    return rows


def finalizer_status_path(args: argparse.Namespace, root: Path, lecture: int) -> Path:
    base = resolve(root, args.acceptance_root)
    if base is None:
        base = root / "data" / "validation" / "mit6824_acceptance"
    return base / f"lec-{lecture:02d}" / "finalize_status.json"


def finalizer_command(args: argparse.Namespace, lecture: int, *, execute: bool = True) -> list[str]:
    cmd = [
        args.python,
        "tools/mit6824_finalize_lecture.py",
        "--lecture",
        str(lecture),
        "--tracker",
        args.tracker,
        "--tracker-md",
        getattr(args, "tracker_md", "data/validation/mit6824_course_tracker.md"),
        "--lecture-start",
        str(getattr(args, "lecture_start", 8)),
        "--lecture-end",
        str(getattr(args, "lecture_end", 20)),
        "--acceptance-root",
        args.acceptance_root,
        "--refresh-tracker",
        "--watch",
        "--poll-sec",
        str(args.finalizer_poll_sec),
        "--timeout-sec",
        str(args.finalizer_timeout_sec),
    ]
    if execute:
        cmd.append("--execute")
    return cmd


def start_dry_run_report_arg(args: argparse.Namespace, lecture: int) -> str:
    base = str(getattr(args, "acceptance_root", "") or "data/validation/mit6824_acceptance").rstrip("/\\")
    return f"{base}/lec-{lecture:02d}/start_dry_run_report.json"


def start_command(args: argparse.Namespace, lecture: int, *, execute: bool) -> list[str]:
    cmd = [args.python, "tools/mit6824_start_lecture.py", "--lecture", str(lecture), "--tracker", args.tracker]
    if execute:
        cmd.append("--execute")
    else:
        cmd.extend(["--dry-run-output", start_dry_run_report_arg(args, lecture)])
    return cmd


def source_preflight(row: dict[str, Any], root: Path) -> dict[str, Any]:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    raw_path = str(source.get("source_video_abspath") or source.get("source_video_repo_relpath") or "")
    path = resolve(root, raw_path) if raw_path else None
    source_file_present = bool(source.get("source_file_present")) or bool(path and path.exists())
    source_bytes = source.get("source_bytes")
    duration_sec = source.get("duration_sec")
    errors: list[str] = []
    warnings: list[str] = []
    if not source_file_present:
        errors.append("source_file_missing")
    try:
        if source_bytes is not None and int(source_bytes) <= 0:
            errors.append("source_file_empty")
    except (TypeError, ValueError):
        warnings.append("source_bytes_not_numeric")
    try:
        if duration_sec is None or float(duration_sec) <= 0:
            warnings.append("source_duration_missing_or_invalid")
    except (TypeError, ValueError):
        warnings.append("source_duration_not_numeric")
    return {
        "ready": not errors,
        "source_file_present": source_file_present,
        "source_video": rel_to_root(path, root) if path else raw_path,
        "source_bytes": source_bytes,
        "duration_sec": duration_sec,
        "title": row.get("title") or source.get("title") or "",
        "errors": errors,
        "warnings": warnings,
    }


def source_preflight_ready(preflight: dict[str, Any]) -> bool:
    return preflight.get("ready") is True


def after_acceptance_preview(
    args: argparse.Namespace,
    root: Path,
    lecture: int,
    tracker_rows: dict[int, dict[str, Any]],
    acceptance_rows: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    next_lecture = lecture + 1
    if next_lecture > args.lecture_end:
        return {
            "status": "all_lectures_accepted_after_current_acceptance",
            "lecture": None,
            "reason": "current lecture is the configured final lecture",
        }
    next_tracker = tracker_rows.get(next_lecture, {})
    next_acceptance = acceptance_rows.get(next_lecture, {})
    next_status = str(next_acceptance.get("status") or next_tracker.get("status") or "unknown")
    preflight = source_preflight(next_tracker, root)
    preview: dict[str, Any] = {
        "status": "ready_to_start_after_acceptance"
        if next_status == "not_started" and source_preflight_ready(preflight)
        else (
            "source_preflight_blocked_after_acceptance"
            if next_status == "not_started"
            else f"next_lecture_{next_status}"
        ),
        "lecture": next_lecture,
        "title": next_tracker.get("title") or next_acceptance.get("title") or "",
        "current_blocker": "waiting_for_current_lecture_human_acceptance",
        "next_status": next_status,
        "source_preflight": preflight,
    }
    if next_status == "not_started" and source_preflight_ready(preflight):
        preview.update(
            {
                "start_command": start_command(args, next_lecture, execute=True),
                "dry_run_command": start_command(args, next_lecture, execute=False),
                "start_dry_run_report": start_dry_run_report_arg(args, next_lecture),
                "finalizer_command": finalizer_command(args, next_lecture, execute=True),
            }
        )
    return preview


def active_finalizer_status(args: argparse.Namespace, root: Path, lecture: int) -> dict[str, Any]:
    path = finalizer_status_path(args, root, lecture)
    payload = read_json(path)
    raw_status = str(payload.get("status") or "")
    if not payload:
        return {"active": False, "path": rel_to_root(path, root), "status": "missing"}
    if raw_status in {"finalized_for_human_review", "dry_run"}:
        return {"active": False, "path": rel_to_root(path, root), "status": raw_status, "payload": payload}
    if raw_status in {"timeout", "start_failed", "tracker_refresh_failed"}:
        return {"active": False, "path": rel_to_root(path, root), "status": raw_status, "payload": payload}
    if raw_status.startswith("waiting_") or raw_status in {"running", "finalizing"}:
        return {"active": True, "path": rel_to_root(path, root), "status": raw_status, "payload": payload}
    return {"active": False, "path": rel_to_root(path, root), "status": raw_status or "unknown", "payload": payload}


def decide_next_action(
    *,
    args: argparse.Namespace,
    root: Path,
    tracker: dict[str, Any],
    acceptance: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    accepted_previous = True
    tracker_rows = tracker_by_lecture(tracker)
    acceptance_rows = acceptance_by_lecture(acceptance)
    for lecture in range(args.lecture_start, args.lecture_end + 1):
        row = acceptance_rows.get(lecture, {})
        status = str(row.get("status") or tracker_rows.get(lecture, {}).get("status") or "unknown")
        if status == "accepted":
            accepted_previous = True
            continue
        if not accepted_previous and not args.allow_unaccepted_previous:
            return {
                "status": "waiting_for_previous_acceptance",
                "lecture": lecture,
                "previous_lecture": lecture - 1,
                "reason": "previous lecture must be human accepted before starting the next one",
            }
        if lecture == args.lecture_start and not gate.get("ready"):
            return {
                "status": "waiting_for_regression_fixture",
                "lecture": lecture,
                "reason": "lecture 7 candidate regression/finalizer gate is not ready",
                "finalize_gate": gate,
            }
        if status == "not_started":
            preflight = source_preflight(tracker_rows.get(lecture, {}), root)
            if not source_preflight_ready(preflight):
                return {
                    "status": "waiting_for_source_preflight",
                    "lecture": lecture,
                    "reason": "source video must be present before starting the lecture",
                    "source_preflight": preflight,
                }
            return {
                "status": "ready_to_start",
                "lecture": lecture,
                "source_preflight": preflight,
                "start_command": start_command(args, lecture, execute=True),
                "dry_run_command": start_command(args, lecture, execute=False),
                "start_dry_run_report": start_dry_run_report_arg(args, lecture),
                "finalizer_command": finalizer_command(args, lecture, execute=True),
            }
        if status == "in_progress":
            return {
                "status": "waiting_for_episode_completion",
                "lecture": lecture,
                "finalizer_status": rel_to_root(finalizer_status_path(args, root, lecture), root),
                "finalizer_command": finalizer_command(args, lecture, execute=True),
            }
        if status == "missing_acceptance_artifacts":
            finalizer = active_finalizer_status(args, root, lecture)
            if finalizer.get("active"):
                return {
                    "status": "waiting_for_finalizer",
                    "lecture": lecture,
                    "finalizer_status": finalizer,
                }
            return {
                "status": "ready_to_finalize",
                "lecture": lecture,
                "finalizer_status": rel_to_root(finalizer_status_path(args, root, lecture), root),
                "finalizer_command": finalizer_command(args, lecture, execute=True),
            }
        if status == "review_blocked":
            return {
                "status": "waiting_for_review_blocker_repair",
                "lecture": lecture,
                "reason": "human review blockers must be repaired before advancing",
                "human_review": row.get("human_review") if isinstance(row.get("human_review"), dict) else {},
            }
        if status == "awaiting_human_acceptance":
            return {
                "status": "waiting_for_human_acceptance",
                "lecture": lecture,
                "human_review_pack": ((row.get("artifacts") or {}).get("human_review_pack") or {}).get("path", ""),
                "risk_queue": ((row.get("artifacts") or {}).get("risk_queue") or {}).get("path", ""),
                "after_acceptance": after_acceptance_preview(args, root, lecture, tracker_rows, acceptance_rows),
            }
        return {
            "status": "manual_triage",
            "lecture": lecture,
            "reason": f"unhandled lecture status: {status}",
            "lecture_status": status,
        }
    return {"status": "all_lectures_accepted", "lecture": None}


def spawn_background(cmd: list[str], *, cwd: Path) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, **kwargs)
    return {"pid": proc.pid, "command": cmd}


def execute_action(args: argparse.Namespace, root: Path, action: dict[str, Any]) -> dict[str, Any]:
    status = action.get("status")
    if status == "ready_to_start":
        lecture = int(action["lecture"])
        cmd = start_command(args, lecture, execute=True)
        result = command_result(cmd, run(cmd, cwd=root, timeout=120))
        payload: dict[str, Any] = {"start_lecture": result}
        if result["returncode"] == 0 and args.spawn_finalizer:
            payload["spawned_finalizer"] = spawn_background(finalizer_command(args, lecture, execute=True), cwd=root)
        return payload
    if status == "ready_to_finalize":
        lecture = int(action["lecture"])
        return {"spawned_finalizer": spawn_background(finalizer_command(args, lecture, execute=True), cwd=root)}
    return {"skipped": True, "reason": f"no executable action for status {status}"}


def build_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    refresh: dict[str, Any] = {}
    if args.refresh:
        refresh["tracker"] = refresh_tracker(args, root)
        if refresh["tracker"]["returncode"] == 0:
            refresh["acceptance_status"] = refresh_acceptance_status(args, root)
    tracker = read_json(resolve(root, args.tracker))
    acceptance = read_json(resolve(root, args.acceptance_status))
    gate = finalize_gate(args, root)
    action = decide_next_action(args=args, root=root, tracker=tracker, acceptance=acceptance, gate=gate)
    status: dict[str, Any] = {
        "schema_version": "holodub.mit6824_course_advance.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "execute": args.execute,
        "lecture_start": args.lecture_start,
        "lecture_end": args.lecture_end,
        "refresh": refresh,
        "course_tracker_summary": tracker.get("summary") if isinstance(tracker, dict) else None,
        "acceptance_summary": acceptance.get("summary") if isinstance(acceptance, dict) else None,
        "finalize_gate": gate,
        "next_action": action,
    }
    if args.execute:
        status["execution"] = execute_action(args, root, action)
    return status


def terminal_watch_status(payload: dict[str, Any]) -> bool:
    status = (payload.get("next_action") or {}).get("status")
    if status in {"waiting_for_human_acceptance", "waiting_for_source_preflight", "all_lectures_accepted", "manual_triage"}:
        return True
    if status == "waiting_for_review_blocker_repair":
        return True
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    start_result = execution.get("start_lecture") if isinstance(execution, dict) else None
    if isinstance(start_result, dict) and start_result.get("returncode") == 0:
        return True
    if isinstance(start_result, dict) and start_result.get("returncode") not in {None, 0}:
        return True
    return False


def main() -> int:
    args = parse_args()
    root = repo_root()
    output = resolve(root, args.output)
    history = resolve(root, args.history_jsonl)
    if output is None or history is None:
        raise ValueError("output paths required")
    deadline = time.time() + args.timeout_sec if args.timeout_sec > 0 else None
    while True:
        payload = build_status(args, root)
        payload["updated_at_epoch"] = time.time()
        write_json(output, payload)
        append_jsonl(history, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        status = (payload.get("next_action") or {}).get("status")
        ok_statuses = {
            "ready_to_start",
            "waiting_for_source_preflight",
            "waiting_for_regression_fixture",
            "waiting_for_episode_completion",
            "waiting_for_finalizer",
            "ready_to_finalize",
            "waiting_for_review_blocker_repair",
            "waiting_for_human_acceptance",
            "all_lectures_accepted",
        }
        if status not in ok_statuses:
            return 1
        if not args.watch or terminal_watch_status(payload):
            return 0
        if deadline and time.time() >= deadline:
            payload["watch_status"] = "timeout"
            write_json(output, payload)
            append_jsonl(history, payload)
            return 124
        time.sleep(max(1.0, args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main())
