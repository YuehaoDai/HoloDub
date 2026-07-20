from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wait for the current regression run to finalize, refresh the MIT 6.824 tracker, then start the next lecture."
    )
    parser.add_argument("--lecture", type=int, default=8)
    parser.add_argument("--lecture-start", type=int, default=8)
    parser.add_argument("--lecture-end", type=int, default=20)
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--tracker-md", default="data/validation/mit6824_course_tracker.md")
    parser.add_argument("--wait-finalize-status", default="")
    parser.add_argument("--require-regression-freeze", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument(
        "--allow-regression-open-issues",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow a completed regression fixture to lock with explicitly recorded non-blocking open issues.",
    )
    parser.add_argument(
        "--regression-open-issue-note",
        action="append",
        default=[],
        help="Note passed to the regression suite when allowing open issues.",
    )
    parser.add_argument("--regression-suite", default="data/validation/mit6824_regression/lecture7_v25_regression_suite.json")
    parser.add_argument("--regression-suite-md", default="data/validation/mit6824_regression/lecture7_v25_regression_suite.md")
    parser.add_argument("--candidate-coverage", default="data/validation/mit6824_candidate_coverage.json")
    parser.add_argument("--candidate-coverage-md", default="data/validation/mit6824_candidate_coverage.md")
    parser.add_argument("--candidate-coverage-job-ids", default="342,343,344,345,346,347")
    parser.add_argument("--output", default="data/validation/mit6824_auto_advance_status.json")
    parser.add_argument("--history-jsonl", default="data/validation/mit6824_auto_advance_history.jsonl")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--restart-ml-service-before-start", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--ml-service-name", default="ml-service")
    parser.add_argument("--ml-restart-timeout-sec", type=int, default=300)
    parser.add_argument("--rebuild-go-services-before-start", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--go-service-names", default="api,worker")
    parser.add_argument("--go-rebuild-timeout-sec", type=int, default=900)
    parser.add_argument(
        "--block-active-episode-name-fragment",
        action="append",
        default=["lora-candidate-episode147-v25"],
        help="Wait before service rebuild/start while an active episode name contains this fragment. Repeatable.",
    )
    parser.add_argument("--skip-active-run-check", action="store_true")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--execute", action="store_true", help="Actually start the lecture once gates clear.")
    parser.add_argument("--watch", action="store_true", help="Keep polling until the lecture starts or timeout is reached.")
    parser.add_argument("--poll-sec", type=float, default=300.0)
    parser.add_argument("--timeout-sec", type=float, default=0.0)
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else root / path


def rel_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


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


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = getattr(args, "compose_file", None) or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


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


def finalize_gate(root: Path, raw_path: str) -> tuple[bool, dict[str, Any]]:
    if not raw_path:
        return True, {"status": "not_configured"}
    path = resolve(root, raw_path)
    if not path.exists():
        return False, {"status": "missing_finalize_status", "path": rel_to_root(path, root)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, {"status": "invalid_finalize_status", "path": rel_to_root(path, root), "error": str(exc)}
    returncode = payload.get("returncode")
    status = payload.get("status")
    ready = returncode == 0 or status in {"finalized", "complete", "completed"}
    return ready, {
        "status": "finalized" if ready else "waiting_for_finalize",
        "path": rel_to_root(path, root),
        "returncode": returncode,
        "finalize_status": status,
        "updated_at_epoch": payload.get("updated_at_epoch"),
        "stdout_tail": payload.get("stdout_tail", "")[-1200:] if isinstance(payload.get("stdout_tail"), str) else "",
    }


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
    proc = run(cmd, cwd=root, timeout=120)
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "output": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-2000:],
    }


def restart_ml_service(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    service_name = getattr(args, "ml_service_name", "ml-service")
    cmd = compose_command(args, ["restart", service_name])
    proc = run(cmd, cwd=root, timeout=int(getattr(args, "ml_restart_timeout_sec", 300)))
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
    }


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def active_episode_rows(args: argparse.Namespace, root: Path) -> list[dict[str, str]]:
    fragments = [fragment for fragment in args.block_active_episode_name_fragment or [] if fragment]
    if args.skip_active_run_check or not fragments:
        return []
    where = " or ".join(f"lower(name) like lower('%' || {sql_literal(fragment)} || '%')" for fragment in fragments)
    sql = f"""
select id,
       coalesce(name,'') as name,
       coalesce(status,'') as status,
       coalesce(updated_at::text,'') as updated_at
from episodes
where ({where})
  and coalesce(status,'') not in ('completed','cancelled','failed');
"""
    cmd = compose_command(
        args,
        [
            "exec",
            "-T",
            args.postgres_service,
            "psql",
            "-U",
            args.postgres_user,
            "-d",
            args.postgres_db,
            "-P",
            "pager=off",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
    )
    proc = run(cmd, cwd=root, timeout=60)
    if proc.returncode != 0:
        return [
            {
                "id": "",
                "name": "active_run_check_failed",
                "status": "unknown",
                "updated_at": proc.stdout[-1000:],
            }
        ]
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if len(lines) <= 1:
        return []
    headers = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        values = line.split("\t")
        rows.append({headers[i]: values[i] if i < len(values) else "" for i in range(len(headers))})
    return rows


def go_service_names(args: argparse.Namespace) -> list[str]:
    raw = str(getattr(args, "go_service_names", "") or "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def rebuild_go_services(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    services = go_service_names(args)
    if not services:
        return {
            "services": [],
            "returncode": 2,
            "stdout_tail": "no Go service names configured",
        }
    build_cmd = compose_command(args, ["build", *services])
    build_proc = run(build_cmd, cwd=root, timeout=int(getattr(args, "go_rebuild_timeout_sec", 900)))
    result: dict[str, Any] = {
        "services": services,
        "build_command": build_cmd,
        "build_returncode": build_proc.returncode,
        "build_stdout_tail": build_proc.stdout[-5000:],
        "returncode": build_proc.returncode,
    }
    if build_proc.returncode != 0:
        return result
    up_cmd = compose_command(args, ["up", "-d", "--no-deps", *services])
    up_proc = run(up_cmd, cwd=root, timeout=300)
    result.update(
        {
            "up_command": up_cmd,
            "up_returncode": up_proc.returncode,
            "up_stdout_tail": up_proc.stdout[-5000:],
            "returncode": up_proc.returncode,
        }
    )
    return result


def start_lecture(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [args.python, "tools/mit6824_start_lecture.py", "--lecture", str(args.lecture), "--tracker", args.tracker]
    if args.execute:
        cmd.append("--execute")
    proc = run(cmd, cwd=root, timeout=120)
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "output": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-4000:],
    }


def refresh_candidate_coverage(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [
        args.python,
        "tools/mit6824_candidate_coverage.py",
        "--job-ids",
        args.candidate_coverage_job_ids,
        "--output-json",
        args.candidate_coverage,
        "--output-md",
        args.candidate_coverage_md,
    ]
    proc = run(cmd, cwd=root, timeout=180)
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "output": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-3000:],
    }


def freeze_regression_suite(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    coverage = refresh_candidate_coverage(args, root)
    cmd = [
        args.python,
        "tools/mit6824_regression_suite.py",
        "--freeze",
        "--candidate-coverage",
        args.candidate_coverage,
        "--output-json",
        args.regression_suite,
        "--output-md",
        args.regression_suite_md,
    ]
    if getattr(args, "allow_regression_open_issues", False):
        cmd.append("--allow-open-issues")
        for note in getattr(args, "regression_open_issue_note", []) or []:
            cmd.extend(["--open-issue-note", str(note)])
    proc = run(cmd, cwd=root, timeout=180)
    return {
        "candidate_coverage": coverage,
        "command": cmd,
        "returncode": proc.returncode,
        "output": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-5000:],
    }


def one_attempt(args: argparse.Namespace, root: Path) -> tuple[bool, dict[str, Any]]:
    finalize_ready, finalize = finalize_gate(root, args.wait_finalize_status)
    row: dict[str, Any] = {
        "lecture": args.lecture,
        "execute": args.execute,
        "updated_at_epoch": time.time(),
        "finalize_gate": finalize,
    }
    if not finalize_ready:
        row["status"] = "waiting_for_finalize"
        return False, row
    if args.require_regression_freeze:
        regression = freeze_regression_suite(args, root)
        row["regression_freeze"] = regression
        if regression["returncode"] != 0:
            output = regression.get("output") or {}
            row["status"] = "regression_freeze_failed"
            row["hard_issues"] = output.get("hard_issues") or []
            return True, row
        output = regression.get("output") or {}
        if output.get("lock_status") == "locked_with_open_issues":
            row["regression_open_issues_allowed"] = True
            row["regression_open_issues"] = output.get("open_issues") or []
    active_blockers = active_episode_rows(args, root)
    if active_blockers:
        row["status"] = "blocked_active_run_wait_before_service_restart"
        row["active_run_blockers"] = active_blockers
        return False, row
    if getattr(args, "rebuild_go_services_before_start", False) and args.execute:
        go_rebuild = rebuild_go_services(args, root)
        row["go_services_rebuild"] = go_rebuild
        if go_rebuild["returncode"] != 0:
            row["status"] = "go_services_rebuild_failed"
            return True, row
    if getattr(args, "restart_ml_service_before_start", False) and args.execute:
        ml_restart = restart_ml_service(args, root)
        row["ml_service_restart"] = ml_restart
        if ml_restart["returncode"] != 0:
            row["status"] = "ml_service_restart_failed"
            return True, row
    tracker = refresh_tracker(args, root)
    row["tracker_refresh"] = tracker
    if tracker["returncode"] != 0:
        row["status"] = "tracker_refresh_failed"
        return True, row
    starter = start_lecture(args, root)
    row["start_lecture"] = starter
    output = starter.get("output") or {}
    if starter["returncode"] == 0:
        row["status"] = output.get("status") or ("started" if args.execute else "dry_run")
        return True, row
    if starter["returncode"] == 2:
        row["status"] = output.get("status") or "blocked_active_run"
        return False, row
    row["status"] = "start_failed"
    return True, row


def main() -> int:
    args = parse_args()
    root = repo_root()
    output_path = resolve(root, args.output)
    history_path = resolve(root, args.history_jsonl)
    deadline = time.time() + args.timeout_sec if args.timeout_sec > 0 else None
    attempt = 0
    while True:
        attempt += 1
        done, row = one_attempt(args, root)
        row["attempt"] = attempt
        write_json(output_path, row)
        append_jsonl(history_path, row)
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        if done:
            return 0 if row.get("status") in {"dry_run", "started"} else 1
        if not args.watch:
            return 2
        if deadline and time.time() >= deadline:
            row["status"] = "timeout"
            write_json(output_path, row)
            append_jsonl(history_path, row)
            return 124
        time.sleep(max(1.0, args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main())
