from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh all read-only MIT 6.824 course status artifacts.")
    parser.add_argument("--lecture-start", type=int, default=8)
    parser.add_argument("--lecture-end", type=int, default=20)
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--tracker-md", default="data/validation/mit6824_course_tracker.md")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--acceptance-status-md", default="data/validation/mit6824_acceptance_status.md")
    parser.add_argument("--training-decision", default="data/validation/mit6824_training_decision.json")
    parser.add_argument("--training-decision-md", default="data/validation/mit6824_training_decision.md")
    parser.add_argument("--candidate-coverage", default="data/validation/mit6824_candidate_coverage.json")
    parser.add_argument("--candidate-coverage-md", default="data/validation/mit6824_candidate_coverage.md")
    parser.add_argument("--candidate-coverage-job-ids", default="342,343,344,345,346,347")
    parser.add_argument("--source-timing-preflight", default="data/validation/mit6824_source_timing_preflight.json")
    parser.add_argument("--source-timing-preflight-md", default="data/validation/mit6824_source_timing_preflight.md")
    parser.add_argument("--acceptance-gate-check", default="data/validation/mit6824_acceptance_gate_check.json")
    parser.add_argument("--acceptance-gate-check-md", default="data/validation/mit6824_acceptance_gate_check.md")
    parser.add_argument("--delivery-index", default="data/validation/mit6824_delivery_index.json")
    parser.add_argument("--delivery-index-md", default="data/validation/mit6824_delivery_index.md")
    parser.add_argument("--batch-review", default="data/validation/mit6824_batch_review.json")
    parser.add_argument("--batch-review-md", default="data/validation/mit6824_batch_review.md")
    parser.add_argument("--watchdog-status", default="data/validation/mit6824_watchdog_status.json")
    parser.add_argument("--watchdog-status-md", default="data/validation/mit6824_watchdog_status.md")
    parser.add_argument("--v25-diagnostics", default="data/validation/mit6824_v25_diagnostics.json")
    parser.add_argument("--v25-diagnostics-md", default="data/validation/mit6824_v25_diagnostics.md")
    parser.add_argument("--handoff-report", default="data/validation/mit6824_handoff_report.json")
    parser.add_argument("--handoff-report-md", default="data/validation/mit6824_handoff_report.md")
    parser.add_argument("--runtime-blockers", default="data/validation/mit6824_runtime_blockers.json")
    parser.add_argument("--runtime-blockers-md", default="data/validation/mit6824_runtime_blockers.md")
    parser.add_argument("--auto-advance-status", default="data/validation/mit6824_auto_advance_status.json")
    parser.add_argument("--goal-status", default="data/validation/mit6824_goal_status.json")
    parser.add_argument("--goal-status-md", default="data/validation/mit6824_goal_status.md")
    parser.add_argument("--course-advance-status", default="data/validation/mit6824_course_advance_status.json")
    parser.add_argument("--course-advance-history", default="data/validation/mit6824_course_advance_history.jsonl")
    parser.add_argument("--output-json", default="data/validation/mit6824_status_refresh.json")
    parser.add_argument("--history-jsonl", default="data/validation/mit6824_status_refresh_history.jsonl")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-sec", type=float, default=300.0)
    parser.add_argument("--timeout-sec", type=float, default=0.0)
    parser.add_argument(
        "--no-degraded-on-docker-unavailable",
        action="store_false",
        dest="degraded_on_docker_unavailable",
        default=True,
        help="Exit on Docker-dependent step failures instead of keeping a degraded file-based refresh alive.",
    )
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


def parse_json_output(raw: str) -> dict[str, Any] | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(raw[start : end + 1])
                return payload if isinstance(payload, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def run_step(cmd: list[str], *, cwd: Path, timeout: int = 180) -> dict[str, Any]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "json": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-3000:],
    }


def docker_health(root: Path, timeout_sec: float = 8.0) -> dict[str, Any]:
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
        return {"status": "available", "reason": "ok", "command": command, "server_version": version}
    return {
        "status": "unavailable",
        "reason": "command_failed",
        "command": command,
        "returncode": proc.returncode,
        "stdout_tail": stdout[-2000:],
    }


COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]
ACTIVE_EPISODE_STATUSES = {"pending", "chaptering", "dispatched", "running", "merging", "judging", "reworking"}


def docker_compose_base() -> list[str]:
    command = ["docker", "compose"]
    for compose_file in COMPOSE_FILES:
        command.extend(["-f", compose_file])
    return command


def current_tracker_episode(tracker: dict[str, Any]) -> dict[str, Any] | None:
    lectures = tracker.get("lectures")
    if not isinstance(lectures, list):
        return None
    for lecture in lectures:
        if not isinstance(lecture, dict):
            continue
        episodes = lecture.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            continue
        valid = [episode for episode in episodes if isinstance(episode, dict) and episode.get("id")]
        if not valid:
            continue
        latest = sorted(valid, key=lambda row: int(row.get("id") or 0))[-1]
        latest_status = str(latest.get("status") or "")
        if lecture.get("status") != "in_progress" and latest_status not in ACTIVE_EPISODE_STATUSES:
            continue
        return {
            "lecture": lecture.get("lecture"),
            "title": lecture.get("title"),
            "episode_id": int(latest.get("id")),
            "episode_name": latest.get("name", ""),
            "episode_status": latest_status,
            "total_chapters": latest.get("total_chapters", ""),
        }
    return None


def parse_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def run_text(cmd: list[str], *, cwd: Path, timeout: int = 30) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": 124,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "error": "timeout",
        }
    return {"returncode": proc.returncode, "stdout": proc.stdout or ""}


def read_current_production(root: Path, tracker: dict[str, Any], docker: dict[str, Any]) -> dict[str, Any]:
    episode = current_tracker_episode(tracker)
    if not episode:
        return {"status": "not_running", "reason": "no_in_progress_tracker_episode"}
    if docker.get("status") != "available":
        return {"status": "unavailable", "reason": "docker_unavailable", **episode}

    episode_id = parse_int(episode.get("episode_id"))
    sql = f"""
select j.id,
       coalesce(j.chapter_ordinal, 0),
       coalesce(j.status, ''),
       coalesce(j.current_stage, ''),
       regexp_replace(coalesce(j.error_message, ''), E'[\\t\\n\\r]+', ' ', 'g'),
       coalesce(j.updated_at::text, ''),
       coalesce(j.heartbeat_at::text, ''),
       coalesce(floor(extract(epoch from (now() - j.heartbeat_at)))::text, ''),
       count(s.id),
       count(s.id) filter (where s.status = 'synthesized'),
       count(s.id) filter (where s.status = 'translated'),
       count(s.id) filter (where s.status not in ('synthesized','translated')),
       count(s.id) filter (where s.status = 'synthesized'
           and s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode' = 'candidate_audio'),
       count(s.id) filter (where s.status = 'synthesized'
           and coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') <> ''),
       count(s.id) filter (where s.status = 'synthesized'
           and s.meta ? 'tts_lora_candidate_runtime')
from jobs j
left join segments s on s.job_id = j.id
where j.episode_id = {episode_id}
group by j.id
order by coalesce(j.chapter_ordinal, 0), j.id;
"""
    psql_cmd = docker_compose_base() + [
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "holodub",
        "-d",
        "holodub",
        "-q",
        "-t",
        "-A",
        "-F",
        "\t",
        "-c",
        sql,
    ]
    psql = run_text(psql_cmd, cwd=root, timeout=30)
    if psql["returncode"] != 0:
        return {
            "status": "unavailable",
            "reason": "db_query_failed",
            **episode,
            "stdout_tail": str(psql.get("stdout", ""))[-2000:],
        }

    jobs: list[dict[str, Any]] = []
    totals = {
        "segments": 0,
        "synthesized": 0,
        "translated": 0,
        "other": 0,
        "candidate_audio": 0,
        "fallback": 0,
        "runtime_present": 0,
    }
    for raw_line in str(psql.get("stdout", "")).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 15:
            continue
        row = {
            "job_id": parse_int(parts[0]),
            "chapter_ordinal": parse_int(parts[1]),
            "status": parts[2],
            "current_stage": parts[3],
            "error_message": parts[4],
            "updated_at": parts[5],
            "heartbeat_at": parts[6],
            "heartbeat_age_sec": parse_int(parts[7], -1),
            "segments": parse_int(parts[8]),
            "synthesized": parse_int(parts[9]),
            "translated": parse_int(parts[10]),
            "other": parse_int(parts[11]),
            "candidate_audio": parse_int(parts[12]),
            "fallback": parse_int(parts[13]),
            "runtime_present": parse_int(parts[14]),
        }
        jobs.append(row)
        for key in totals:
            totals[key] += parse_int(row.get(key))

    queue = read_current_queue(root, [job["job_id"] for job in jobs])
    return {
        "status": "running" if jobs else "not_running",
        **episode,
        "jobs": jobs,
        "totals": totals,
        "queue": queue,
    }


def parse_task_json(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def read_current_queue(root: Path, job_ids: list[int]) -> dict[str, Any]:
    immediate_cmd = docker_compose_base() + [
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "LRANGE",
        "holodub:tasks",
        "0",
        "-1",
    ]
    delayed_cmd = docker_compose_base() + [
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "ZRANGE",
        "holodub:tasks:delayed",
        "0",
        "-1",
        "WITHSCORES",
    ]
    alternate_immediate_cmd = docker_compose_base() + [
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "LRANGE",
        "holodub:stage_tasks",
        "0",
        "-1",
    ]
    alternate_delayed_cmd = docker_compose_base() + [
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "ZRANGE",
        "holodub:delayed_stage_tasks",
        "0",
        "-1",
        "WITHSCORES",
    ]
    lease_keys_cmd = docker_compose_base() + [
        "exec",
        "-T",
        "redis",
        "redis-cli",
        "--raw",
        "KEYS",
        "holodub:lease:job:*:stage:*",
    ]
    immediate = run_text(immediate_cmd, cwd=root, timeout=15)
    delayed = run_text(delayed_cmd, cwd=root, timeout=15)
    alternate_immediate = run_text(alternate_immediate_cmd, cwd=root, timeout=15)
    alternate_delayed = run_text(alternate_delayed_cmd, cwd=root, timeout=15)
    lease_keys = run_text(lease_keys_cmd, cwd=root, timeout=15)
    job_set = set(job_ids)
    immediate_tasks = [
        task
        for line in str(immediate.get("stdout", "")).splitlines()
        if (task := parse_task_json(line.strip())) is not None
    ]
    delayed_lines = [line.strip() for line in str(delayed.get("stdout", "")).splitlines() if line.strip()]
    delayed_tasks: list[dict[str, Any]] = []
    for idx in range(0, len(delayed_lines), 2):
        task = parse_task_json(delayed_lines[idx])
        if task is None:
            continue
        if idx + 1 < len(delayed_lines):
            task["score_ms"] = parse_int(delayed_lines[idx + 1])
        delayed_tasks.append(task)
    alternate_immediate_tasks = [
        task
        for line in str(alternate_immediate.get("stdout", "")).splitlines()
        if (task := parse_task_json(line.strip())) is not None
    ]
    alternate_delayed_lines = [
        line.strip() for line in str(alternate_delayed.get("stdout", "")).splitlines() if line.strip()
    ]
    alternate_delayed_tasks: list[dict[str, Any]] = []
    for idx in range(0, len(alternate_delayed_lines), 2):
        task = parse_task_json(alternate_delayed_lines[idx])
        if task is None:
            continue
        if idx + 1 < len(alternate_delayed_lines):
            task["score_ms"] = parse_int(alternate_delayed_lines[idx + 1])
        alternate_delayed_tasks.append(task)
    active_leases = [
        key
        for key in str(lease_keys.get("stdout", "")).splitlines()
        if key.strip()
    ]
    return {
        "immediate": immediate_tasks,
        "delayed": delayed_tasks,
        "current_episode_immediate": [task for task in immediate_tasks if parse_int(task.get("job_id")) in job_set],
        "current_episode_delayed": [task for task in delayed_tasks if parse_int(task.get("job_id")) in job_set],
        "active_leases": active_leases,
        "current_episode_active_leases": [
            key
            for key in active_leases
            if any(f":job:{job_id}:stage:" in key for job_id in job_set)
        ],
        "alternate_immediate": alternate_immediate_tasks,
        "alternate_delayed": alternate_delayed_tasks,
        "alternate_current_episode_immediate": [
            task for task in alternate_immediate_tasks if parse_int(task.get("job_id")) in job_set
        ],
        "alternate_current_episode_delayed": [
            task for task in alternate_delayed_tasks if parse_int(task.get("job_id")) in job_set
        ],
        "read_errors": [
            name
            for name, result in (
                ("immediate", immediate),
                ("delayed", delayed),
                ("alternate_immediate", alternate_immediate),
                ("alternate_delayed", alternate_delayed),
                ("lease_keys", lease_keys),
            )
            if result.get("returncode") != 0
        ],
    }


def read_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": str(path)}
    return payload if isinstance(payload, dict) else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def build_steps(args: argparse.Namespace, *, degraded_without_docker: bool = False) -> list[tuple[str, list[str], int]]:
    file_based_steps = [
        (
            "training_decision",
            [
                args.python,
                "tools/mit6824_training_decision.py",
                "--acceptance-status",
                args.acceptance_status,
                "--output-json",
                args.training_decision,
                "--output-md",
                args.training_decision_md,
            ],
            180,
        ),
        (
            "acceptance_gate_check",
            [
                args.python,
                "tools/mit6824_acceptance_gate_check.py",
                "--acceptance-status",
                args.acceptance_status,
                "--tracker",
                args.tracker,
                "--output-json",
                args.acceptance_gate_check,
                "--output-md",
                args.acceptance_gate_check_md,
            ],
            60,
        ),
        (
            "watchdog",
            [
                args.python,
                "tools/mit6824_watchdog.py",
                "--allow-missing",
                "--output-json",
                args.watchdog_status,
                "--output-md",
                args.watchdog_status_md,
            ],
            60,
        ),
        (
            "runtime_blockers",
            [
                args.python,
                "tools/mit6824_runtime_blockers.py",
                "--status-refresh",
                getattr(args, "output_json", "data/validation/mit6824_status_refresh.json"),
                "--watchdog-status",
                args.watchdog_status,
                "--output-json",
                args.runtime_blockers,
                "--output-md",
                args.runtime_blockers_md,
            ],
            60,
        ),
        (
            "goal_status",
            [
                args.python,
                "tools/mit6824_goal_status.py",
                "--no-db",
                "--tracker",
                args.tracker,
                "--acceptance-status",
                args.acceptance_status,
                "--training-decision",
                args.training_decision,
                "--course-advance-status",
                args.course_advance_status,
                "--output-json",
                args.goal_status,
                "--output-md",
                args.goal_status_md,
            ],
            180,
        ),
        (
            "delivery_index",
            [
                args.python,
                "tools/mit6824_delivery_index.py",
                "--acceptance-status",
                args.acceptance_status,
                "--course-advance-status",
                args.course_advance_status,
                "--training-decision",
                args.training_decision,
                "--goal-status",
                args.goal_status,
                "--output-json",
                args.delivery_index,
                "--output-md",
                args.delivery_index_md,
            ],
            180,
        ),
        (
            "batch_review",
            [
                args.python,
                "tools/mit6824_batch_review.py",
                "--acceptance-status",
                args.acceptance_status,
                "--training-decision",
                args.training_decision,
                "--delivery-index",
                args.delivery_index,
                "--output-json",
                args.batch_review,
                "--output-md",
                args.batch_review_md,
            ],
            180,
        ),
        (
            "handoff_report",
            [
                args.python,
                "tools/mit6824_handoff_report.py",
                "--status-refresh",
                getattr(args, "output_json", "data/validation/mit6824_status_refresh.json"),
                "--status-refresh-history",
                getattr(args, "history_jsonl", "data/validation/mit6824_status_refresh_history.jsonl"),
                "--goal-status",
                args.goal_status,
                "--delivery-index",
                args.delivery_index,
                "--watchdog-status",
                args.watchdog_status,
                "--candidate-coverage",
                args.candidate_coverage,
                "--auto-advance-status",
                args.auto_advance_status,
                "--output-json",
                args.handoff_report,
                "--output-md",
                args.handoff_report_md,
            ],
            60,
        ),
    ]
    if degraded_without_docker:
        return file_based_steps
    return [
        (
            "course_tracker",
            [
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
            ],
            180,
        ),
        (
            "acceptance_status",
            [
                args.python,
                "tools/mit6824_acceptance_status.py",
                "--tracker",
                args.tracker,
                "--output-json",
                args.acceptance_status,
                "--output-md",
                args.acceptance_status_md,
            ],
            180,
        ),
        (
            "training_decision",
            [
                args.python,
                "tools/mit6824_training_decision.py",
                "--acceptance-status",
                args.acceptance_status,
                "--output-json",
                args.training_decision,
                "--output-md",
                args.training_decision_md,
            ],
            180,
        ),
        (
            "candidate_coverage",
            [
                args.python,
                "tools/mit6824_candidate_coverage.py",
                "--job-ids",
                args.candidate_coverage_job_ids,
                "--output-json",
                args.candidate_coverage,
                "--output-md",
                args.candidate_coverage_md,
            ],
            180,
        ),
        (
            "source_timing_preflight",
            [
                args.python,
                "tools/mit6824_source_timing_preflight.py",
                "--tracker",
                args.tracker,
                "--output-json",
                getattr(args, "source_timing_preflight", "data/validation/mit6824_source_timing_preflight.json"),
                "--output-md",
                getattr(args, "source_timing_preflight_md", "data/validation/mit6824_source_timing_preflight.md"),
            ],
            90,
        ),
        (
            "course_advance",
            [
                args.python,
                "tools/mit6824_course_advance.py",
                "--refresh",
                "--lecture-start",
                str(args.lecture_start),
                "--lecture-end",
                str(args.lecture_end),
                "--tracker",
                args.tracker,
                "--tracker-md",
                args.tracker_md,
                "--acceptance-status",
                args.acceptance_status,
                "--acceptance-status-md",
                args.acceptance_status_md,
                "--output",
                args.course_advance_status,
                "--history-jsonl",
                args.course_advance_history,
            ],
            240,
        ),
        (
            "acceptance_gate_check",
            [
                args.python,
                "tools/mit6824_acceptance_gate_check.py",
                "--acceptance-status",
                args.acceptance_status,
                "--tracker",
                args.tracker,
                "--output-json",
                args.acceptance_gate_check,
                "--output-md",
                args.acceptance_gate_check_md,
            ],
            60,
        ),
        (
            "watchdog",
            [
                args.python,
                "tools/mit6824_watchdog.py",
                "--allow-missing",
                "--output-json",
                args.watchdog_status,
                "--output-md",
                args.watchdog_status_md,
            ],
            60,
        ),
        (
            "runtime_blockers",
            [
                args.python,
                "tools/mit6824_runtime_blockers.py",
                "--status-refresh",
                getattr(args, "output_json", "data/validation/mit6824_status_refresh.json"),
                "--watchdog-status",
                args.watchdog_status,
                "--output-json",
                args.runtime_blockers,
                "--output-md",
                args.runtime_blockers_md,
            ],
            60,
        ),
        (
            "goal_status",
            [
                args.python,
                "tools/mit6824_goal_status.py",
                "--tracker",
                args.tracker,
                "--acceptance-status",
                args.acceptance_status,
                "--training-decision",
                args.training_decision,
                "--course-advance-status",
                args.course_advance_status,
                "--output-json",
                args.goal_status,
                "--output-md",
                args.goal_status_md,
            ],
            180,
        ),
        (
            "v25_diagnostics",
            [
                args.python,
                "tools/mit6824_v25_diagnostics.py",
                "--goal-status",
                args.goal_status,
                "--status-refresh",
                getattr(args, "output_json", "data/validation/mit6824_status_refresh.json"),
                "--output-json",
                args.v25_diagnostics,
                "--output-md",
                args.v25_diagnostics_md,
            ],
            90,
        ),
        (
            "delivery_index",
            [
                args.python,
                "tools/mit6824_delivery_index.py",
                "--acceptance-status",
                args.acceptance_status,
                "--course-advance-status",
                args.course_advance_status,
                "--training-decision",
                args.training_decision,
                "--goal-status",
                args.goal_status,
                "--output-json",
                args.delivery_index,
                "--output-md",
                args.delivery_index_md,
            ],
            180,
        ),
        (
            "batch_review",
            [
                args.python,
                "tools/mit6824_batch_review.py",
                "--acceptance-status",
                args.acceptance_status,
                "--training-decision",
                args.training_decision,
                "--delivery-index",
                args.delivery_index,
                "--output-json",
                args.batch_review,
                "--output-md",
                args.batch_review_md,
            ],
            180,
        ),
        (
            "handoff_report",
            [
                args.python,
                "tools/mit6824_handoff_report.py",
                "--status-refresh",
                getattr(args, "output_json", "data/validation/mit6824_status_refresh.json"),
                "--status-refresh-history",
                getattr(args, "history_jsonl", "data/validation/mit6824_status_refresh_history.jsonl"),
                "--goal-status",
                args.goal_status,
                "--delivery-index",
                args.delivery_index,
                "--watchdog-status",
                args.watchdog_status,
                "--candidate-coverage",
                args.candidate_coverage,
                "--auto-advance-status",
                args.auto_advance_status,
                "--output-json",
                args.handoff_report,
                "--output-md",
                args.handoff_report_md,
            ],
            60,
        ),
    ]


def one_refresh(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    docker = docker_health(root)
    degraded = bool(args.degraded_on_docker_unavailable and docker.get("status") != "available")
    steps: dict[str, Any] = {}
    for name, cmd, timeout in build_steps(args, degraded_without_docker=degraded):
        steps[name] = run_step(cmd, cwd=root, timeout=timeout)
    goal = read_json(resolve(root, args.goal_status)) or {}
    tracker = read_json(resolve(root, args.tracker)) or {}
    coverage = read_json(resolve(root, args.candidate_coverage)) or {}
    source_timing_path = getattr(args, "source_timing_preflight", "data/validation/mit6824_source_timing_preflight.json")
    source_timing_md = getattr(args, "source_timing_preflight_md", "data/validation/mit6824_source_timing_preflight.md")
    source_timing = read_json(resolve(root, source_timing_path)) or {}
    delivery = read_json(resolve(root, args.delivery_index)) or {}
    acceptance_gate = read_json(resolve(root, args.acceptance_gate_check)) or {}
    batch_review = read_json(resolve(root, args.batch_review)) or {}
    watchdog = read_json(resolve(root, args.watchdog_status)) or {}
    blockers = read_json(resolve(root, args.runtime_blockers)) or {}
    handoff = read_json(resolve(root, args.handoff_report)) or {}
    diagnostics = read_json(resolve(root, args.v25_diagnostics)) or {}
    active = goal.get("active_regression") if isinstance(goal, dict) else {}
    metrics = active.get("metrics") if isinstance(active, dict) else {}
    health = active.get("health") if isinstance(active, dict) else {}
    summary = {
        "runtime": {"docker": docker, "degraded_without_docker": degraded},
        "next_action": goal.get("next_action") if isinstance(goal, dict) else "",
        "current_production": read_current_production(root, tracker, docker),
        "active_regression": metrics if isinstance(metrics, dict) else {},
        "active_regression_health": health if isinstance(health, dict) else {},
        "course_tracker_summary": goal.get("course_tracker_summary") if isinstance(goal, dict) else None,
        "candidate_coverage": coverage.get("summary") if isinstance(coverage, dict) else None,
        "source_timing_preflight": {
            "status": source_timing.get("status"),
            "episode": source_timing.get("episode"),
            "summary": source_timing.get("summary"),
            "output_json": source_timing_path,
            "output_md": source_timing_md,
        }
        if isinstance(source_timing, dict)
        else None,
        "delivery_index": delivery.get("summary") if isinstance(delivery, dict) else None,
        "acceptance_gate": {
            "status": acceptance_gate.get("status"),
            "pass": acceptance_gate.get("pass"),
            "lecture": acceptance_gate.get("lecture"),
            "next_lecture": acceptance_gate.get("next_lecture"),
            "summary": acceptance_gate.get("summary"),
            "output_json": args.acceptance_gate_check,
            "output_md": args.acceptance_gate_check_md,
        }
        if isinstance(acceptance_gate, dict)
        else None,
        "batch_review": batch_review.get("summary") if isinstance(batch_review, dict) else None,
        "watchdog": watchdog.get("summary") if isinstance(watchdog, dict) else None,
        "runtime_blockers": blockers.get("summary") if isinstance(blockers, dict) else None,
        "handoff": handoff.get("next_action") if isinstance(handoff, dict) else None,
        "v25_diagnostics": diagnostics.get("summary") if isinstance(diagnostics, dict) else None,
    }
    return {
        "schema_version": "holodub.mit6824_status_refresh.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "degraded_without_docker": degraded,
        "steps": steps,
        "summary": summary,
    }


def main() -> int:
    args = parse_args()
    root = repo_root()
    output = resolve(root, args.output_json)
    history = resolve(root, args.history_jsonl)
    if output is None or history is None:
        raise ValueError("output paths required")
    deadline = time.time() + args.timeout_sec if args.timeout_sec > 0 else None
    while True:
        payload = one_refresh(args, root)
        write_json(output, payload)
        append_jsonl(history, payload)
        print(
            json.dumps(
                {
                    "output_json": rel_to_root(output, root),
                    "summary": payload.get("summary"),
                    "failed_steps": [
                        name for name, row in (payload.get("steps") or {}).items() if row.get("returncode") != 0
                    ],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        failed = [row for row in (payload.get("steps") or {}).values() if row.get("returncode") != 0]
        if not args.watch:
            return 1 if failed else 0
        if deadline and time.time() >= deadline:
            return 124
        time.sleep(max(1.0, args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main())
