from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check MIT 6.824 production watcher processes.")
    parser.add_argument("--output-json", default="data/validation/mit6824_watchdog_status.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_watchdog_status.md")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--restart-status-watch", action="store_true")
    parser.add_argument("--allow-missing", action="store_true", help="Report missing watchers but exit 0.")
    parser.add_argument("--status-watch-poll-sec", type=float, default=300.0)
    parser.add_argument("--status-watch-timeout-sec", type=float, default=43200.0)
    parser.add_argument("--status-watch-stdout", default="data/validation/mit6824_status_refresh_watch.out.log")
    parser.add_argument("--status-watch-stderr", default="data/validation/mit6824_status_refresh_watch.err.log")
    parser.add_argument("--v25-finalize-status", default="data/training/lora_runs/main_flow_candidate_episode147_v25/auto_finalize_status.json")
    parser.add_argument("--course-tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def lecture_status(tracker: dict[str, Any], lecture: int) -> str:
    for row in tracker.get("lectures") or []:
        if isinstance(row, dict) and int(row.get("lecture") or 0) == lecture:
            return str(row.get("status") or "")
    return ""


def acceptance_status(acceptance: dict[str, Any], lecture: int) -> str:
    for row in acceptance.get("lectures") or []:
        if isinstance(row, dict) and int(row.get("lecture") or 0) == lecture:
            return str(row.get("status") or "")
    return ""


def lifecycle_state(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    finalize = load_json(resolve(root, getattr(args, "v25_finalize_status", "")))
    tracker = load_json(resolve(root, getattr(args, "course_tracker", "")))
    acceptance = load_json(resolve(root, getattr(args, "acceptance_status", "")))
    finalize_status = str(finalize.get("status") or "")
    v25_finalized = finalize.get("returncode") == 0 or finalize_status in {"finalized", "complete", "completed"}
    lec8_status = lecture_status(tracker, 8)
    lec8_acceptance_status = acceptance_status(acceptance, 8)
    lecture8_started = bool(lec8_status and lec8_status != "not_started")
    return {
        "v25_finalized": v25_finalized,
        "lecture8_status": lec8_status,
        "lecture8_acceptance_status": lec8_acceptance_status,
        "lecture8_started": lecture8_started,
    }


def process_rows() -> list[dict[str, Any]]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,CommandLine | "
        "ConvertTo-Json -Compress -Depth 3",
    ]
    proc = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:] or proc.stdout[-2000:])
    raw = proc.stdout.strip()
    if not raw:
        return []
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload = [payload]
    rows = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        rows.append(
            {
                "pid": int(row.get("ProcessId") or 0),
                "command_line": str(row.get("CommandLine") or ""),
            }
        )
    return rows


def docker_health(root: Path, timeout_sec: float = 12.0) -> dict[str, Any]:
    command = ["docker", "version", "--format", "{{json .Server.Version}}"]
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "status": "unavailable",
            "reason": "timeout",
            "command": command,
            "timeout_sec": timeout_sec,
            "stdout_tail": (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else "",
        }
    stdout = proc.stdout or ""
    version = stdout.strip().strip('"')
    if proc.returncode == 0 and version:
        return {
            "status": "available",
            "reason": "ok",
            "command": command,
            "server_version": version,
            "returncode": proc.returncode,
        }
    return {
        "status": "unavailable",
        "reason": "command_failed",
        "command": command,
        "returncode": proc.returncode,
        "stdout_tail": stdout[-2000:],
    }


def expected_specs(args: argparse.Namespace, lifecycle: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    lifecycle = lifecycle or {}
    v25_required = not bool(lifecycle.get("v25_finalized"))
    lecture8_auto_advance_required = not bool(lifecycle.get("lecture8_started"))
    lecture8_acceptance_status = str(lifecycle.get("lecture8_acceptance_status") or "")
    lecture8_finalizer_required = bool(lifecycle.get("lecture8_started")) and lecture8_acceptance_status not in {
        "awaiting_human_acceptance",
        "accepted",
        "review_blocked",
    }
    return [
        {
            "id": "v25_watcher",
            "label": "Lecture 7 v25 generation watcher",
            "patterns": ["overnight_watch_v25.py"],
            "required": v25_required,
            "auto_repair": False,
            "restart_command": "python data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\overnight_watch_v25.py",
        },
        {
            "id": "v25_auto_finalizer",
            "label": "Lecture 7 v25 auto-finalizer",
            "patterns": ["auto_finalize_loop.py"],
            "required": v25_required,
            "auto_repair": False,
            "restart_command": "python data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\auto_finalize_loop.py",
        },
        {
            "id": "lecture8_auto_advance",
            "label": "Lecture 8 auto-advance watcher",
            "patterns": [
                "mit6824_auto_advance.py",
                "--lecture 8",
                "--allow-regression-open-issues",
                "--rebuild-go-services-before-start",
                "--restart-ml-service-before-start",
            ],
            "required": lecture8_auto_advance_required,
            "auto_repair": False,
            "restart_command": (
                "python tools\\mit6824_auto_advance.py --lecture 8 "
                "--wait-finalize-status data\\training\\lora_runs\\main_flow_candidate_episode147_v25\\auto_finalize_status.json "
                "--watch --execute --allow-regression-open-issues "
                "--regression-open-issue-note lecture7_fixture_open_issues_do_not_block_lecture8_delivery "
                "--rebuild-go-services-before-start --restart-ml-service-before-start --poll-sec 300 --timeout-sec 43200"
            ),
        },
        {
            "id": "lecture8_finalizer",
            "label": "Lecture 8 finalizer watcher",
            "patterns": ["mit6824_finalize_lecture.py", "--lecture 8"],
            "required": lecture8_finalizer_required,
            "auto_repair": False,
            "restart_command": (
                "python tools\\mit6824_finalize_lecture.py --lecture 8 "
                "--refresh-tracker --watch --execute --poll-sec 300 --timeout-sec 172800"
            ),
        },
        {
            "id": "status_refresh_watch",
            "label": "Read-only course status refresh watcher",
            "patterns": ["mit6824_status_refresh.py", "--watch"],
            "required": True,
            "auto_repair": True,
            "restart_command": (
                f"python tools\\mit6824_status_refresh.py --watch --poll-sec {args.status_watch_poll_sec:g} "
                f"--timeout-sec {args.status_watch_timeout_sec:g}"
            ),
        },
    ]


def matches_spec(row: dict[str, Any], spec: dict[str, Any]) -> bool:
    command_line = str(row.get("command_line") or "").lower()
    if not command_line or "get-ciminstance win32_process" in command_line:
        return False
    return all(str(pattern).lower() in command_line for pattern in spec.get("patterns") or [])


def restart_status_watch(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    stdout_path = resolve(root, args.status_watch_stdout)
    stderr_path = resolve(root, args.status_watch_stderr)
    if stdout_path is None or stderr_path is None:
        raise ValueError("status watcher log paths required")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")
    cmd = [
        args.python,
        "tools\\mit6824_status_refresh.py",
        "--watch",
        "--poll-sec",
        f"{args.status_watch_poll_sec:g}",
        "--timeout-sec",
        f"{args.status_watch_timeout_sec:g}",
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=creationflags,
    )
    stdout_handle.close()
    stderr_handle.close()
    return {
        "command": cmd,
        "pid": proc.pid,
        "stdout": rel_to_root(stdout_path, root),
        "stderr": rel_to_root(stderr_path, root),
    }


def build_report(
    *,
    args: argparse.Namespace,
    root: Path,
    rows: list[dict[str, Any]],
    restarted: dict[str, Any] | None = None,
    lifecycle: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lifecycle = lifecycle or {}
    runtime = runtime or {"docker": {"status": "not_checked", "reason": "not_checked"}}
    specs = expected_specs(args, lifecycle)
    watcher_rows = []
    missing = []
    for spec in specs:
        matches = [row for row in rows if matches_spec(row, spec)]
        status = "running" if matches else "missing"
        if status == "missing" and spec.get("required"):
            missing.append(spec["id"])
        watcher_rows.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "status": status,
                "required": bool(spec.get("required")),
                "auto_repair": bool(spec.get("auto_repair")),
                "pids": [row["pid"] for row in matches],
                "restart_command": spec.get("restart_command", ""),
            }
        )
    if restarted:
        for row in watcher_rows:
            if row["id"] == "status_refresh_watch":
                row["status"] = "restarted"
                row["pids"] = [restarted["pid"]]
                break
        missing = [item for item in missing if item != "status_refresh_watch"]
    docker = runtime.get("docker") if isinstance(runtime.get("docker"), dict) else {}
    docker_missing = bool(docker and docker.get("status") not in {"available", "not_checked"})
    missing_reasons = list(missing)
    if docker_missing:
        missing_reasons.append("docker_engine_unavailable")
    return {
        "schema_version": "holodub.mit6824_watchdog.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lifecycle": lifecycle,
        "runtime": runtime,
        "summary": {
            "required": len([spec for spec in specs if spec.get("required")]),
            "running_or_restarted": len([row for row in watcher_rows if row["status"] in {"running", "restarted"}]),
            "missing_required": missing,
            "missing_reasons": missing_reasons,
            "status": "ok" if not missing_reasons else "needs_attention",
            "status_refresh_restarted": bool(restarted),
            "docker_status": docker.get("status", "not_checked"),
        },
        "watchers": watcher_rows,
        "restart": restarted,
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# MIT 6.824 Watchdog Status",
        "",
        f"Generated: `{report.get('generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Status: `{summary.get('status', '')}`",
        f"- Running or restarted: `{summary.get('running_or_restarted', 0)}/{summary.get('required', 0)}`",
        f"- Missing required: `{summary.get('missing_required', [])}`",
        f"- Status refresh restarted: `{summary.get('status_refresh_restarted', False)}`",
        f"- Docker status: `{summary.get('docker_status', 'not_checked')}`",
        f"- Missing reasons: `{summary.get('missing_reasons', [])}`",
        "",
        "## Watchers",
        "",
        "| ID | Status | PIDs | Auto Repair | Restart Command |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report.get("watchers") or []:
        lines.append(
            "| {id} | {status} | {pids} | {auto_repair} | `{restart}` |".format(
                id=row.get("id", ""),
                status=row.get("status", ""),
                pids=",".join(str(pid) for pid in row.get("pids") or []),
                auto_repair=row.get("auto_repair", False),
                restart=str(row.get("restart_command") or "").replace("|", "\\|"),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    lifecycle = lifecycle_state(args, root)
    runtime = {"docker": docker_health(root)}
    rows = process_rows()
    initial = build_report(args=args, root=root, rows=rows, lifecycle=lifecycle, runtime=runtime)
    restarted = None
    missing = initial.get("summary", {}).get("missing_required") or []
    if args.restart_status_watch and "status_refresh_watch" in missing:
        restarted = restart_status_watch(args, root)
        rows = process_rows()
    report = build_report(args=args, root=root, rows=rows, restarted=restarted, lifecycle=lifecycle, runtime=runtime)
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
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if args.allow_missing or report["summary"]["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
