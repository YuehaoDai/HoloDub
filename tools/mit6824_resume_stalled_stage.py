from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely requeue a stale MIT 6.824 chapter stage that is marked running but has no worker lease."
    )
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--stage", default="tts_duration")
    parser.add_argument("--min-stale-sec", type=int, default=900)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--allow-expired-lease-requeue",
        action="store_true",
        help="Required for execution when the stage lease is already missing. Long stages can outlive the lease while still running.",
    )
    parser.add_argument("--requested-by", default="codex_v25_recovery")
    parser.add_argument("--reason", default="resume_stalled_stage")
    parser.add_argument("--queue-key", default="holodub:tasks")
    parser.add_argument("--queue-front", action="store_true", help="LPUSH the recovery task instead of RPUSH.")
    parser.add_argument("--delayed-queue-key", default="holodub:tasks:delayed")
    parser.add_argument("--lease-prefix", default="holodub:lease")
    parser.add_argument("--output-json", default="data/validation/mit6824_resume_stalled_stage.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_resume_stalled_stage.md")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--redis-service", default="redis")
    parser.add_argument("--compose-file", action="append", default=[])
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


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run_cmd(args: argparse.Namespace, root: Path, extra: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        compose_command(args, extra),
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def run_psql(args: argparse.Namespace, root: Path, sql: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return run_cmd(
        args,
        root,
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
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        timeout=timeout,
    )


def run_redis(args: argparse.Namespace, root: Path, redis_args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run_cmd(args, root, ["exec", "-T", args.redis_service, "redis-cli", *redis_args], timeout=timeout)


def parse_table(raw: str) -> list[dict[str, str]]:
    lines = [line for line in raw.splitlines() if line and not line.startswith("(") and line not in {"BEGIN", "COMMIT"}]
    if len(lines) < 2:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(row) for row in reader]


def query_one(args: argparse.Namespace, root: Path, sql: str) -> tuple[dict[str, str] | None, dict[str, Any]]:
    proc = run_psql(args, root, sql)
    status = {
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr[-2000:],
        "stdout_tail": proc.stdout[-2000:],
    }
    if proc.returncode != 0:
        return None, status
    rows = parse_table(proc.stdout)
    return (rows[0] if rows else None), status


def query_all(args: argparse.Namespace, root: Path, sql: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    proc = run_psql(args, root, sql)
    status = {
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr[-2000:],
        "stdout_tail": proc.stdout[-2000:],
    }
    if proc.returncode != 0:
        return [], status
    return parse_table(proc.stdout), status


def int_field(row: dict[str, str] | None, key: str, default: int = 0) -> int:
    if not row:
        return default
    try:
        return int(float(row.get(key, "") or default))
    except ValueError:
        return default


def redis_int(proc: subprocess.CompletedProcess[str]) -> int | None:
    if proc.returncode != 0:
        return None
    raw = proc.stdout.strip()
    try:
        return int(raw)
    except ValueError:
        return None


def decode_task(raw: str) -> dict[str, Any] | None:
    try:
        task = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return task if isinstance(task, dict) else None


def matching_tasks(raw_tasks: str, job_id: int, stage: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for line in raw_tasks.splitlines():
        task = decode_task(line.strip())
        if not task:
            continue
        if int(task.get("job_id") or 0) == job_id and str(task.get("stage") or "") == stage:
            matches.append(task)
    return matches


def collect_state(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    job, job_status = query_one(
        args,
        root,
        f"""
select id,
       coalesce(status,'') as status,
       coalesce(current_stage,'') as current_stage,
       coalesce(error_message,'') as error_message,
       coalesce(updated_at::text,'') as updated_at,
       coalesce(heartbeat_at::text,'') as heartbeat_at,
       extract(epoch from (now() - coalesce(heartbeat_at, updated_at)))::int::text as heartbeat_stale_sec
from jobs
where id = {args.job_id};
""",
    )
    stage_run, stage_status = query_one(
        args,
        root,
        f"""
select id,
       job_id,
       coalesce(stage,'') as stage,
       coalesce(status,'') as status,
       attempt::text as attempt,
       coalesce(started_at::text,'') as started_at,
       coalesce(finished_at::text,'') as finished_at,
       coalesce(error_message,'') as error_message,
       extract(epoch from (now() - started_at))::int::text as stage_age_sec
from job_stage_runs
where job_id = {args.job_id}
  and stage = '{args.stage}'
order by id desc
limit 1;
""",
    )
    counts, counts_status = query_all(
        args,
        root,
        f"""
select coalesce(status,'') as status, count(*)::text as count
from segments
where job_id = {args.job_id}
group by status
order by status;
""",
    )
    frontier, frontier_status = query_all(
        args,
        root,
        f"""
select ordinal::text as ordinal,
       id::text as segment_id,
       coalesce((meta->>'shadow_source_segment_id'),'') as source_segment_id,
       coalesce(status,'') as status,
       original_duration_ms::text as original_duration_ms,
       tts_duration_ms::text as tts_duration_ms,
       coalesce(tts_audio_rel_path,'') as tts_audio_path,
       left(replace(coalesce(target_text,''), E'\\n', ' '), 140) as target_text_preview
from segments
where job_id = {args.job_id}
  and status <> 'synthesized'
order by ordinal
limit 8;
""",
    )
    queue_proc = run_redis(args, root, ["LRANGE", args.queue_key, "0", "-1"])
    delayed_proc = run_redis(args, root, ["ZRANGE", args.delayed_queue_key, "0", "-1"])
    lease_key = f"{args.lease_prefix}:job:{args.job_id}:stage:{args.stage}"
    lease_proc = run_redis(args, root, ["EXISTS", lease_key])
    queue_matches = matching_tasks(queue_proc.stdout, args.job_id, args.stage)
    delayed_matches = matching_tasks(delayed_proc.stdout, args.job_id, args.stage)
    status_counts = {row.get("status", ""): int_field(row, "count") for row in counts}
    return {
        "job": job,
        "stage_run": stage_run,
        "status_counts": status_counts,
        "frontier": frontier,
        "lease_key": lease_key,
        "lease_exists": redis_int(lease_proc),
        "queue_len": len([line for line in queue_proc.stdout.splitlines() if line.strip()]),
        "delayed_len": len([line for line in delayed_proc.stdout.splitlines() if line.strip()]),
        "queue_matches": queue_matches,
        "delayed_matches": delayed_matches,
        "query_status": {
            "job": job_status,
            "stage_run": stage_status,
            "counts": counts_status,
            "frontier": frontier_status,
            "queue": {
                "returncode": queue_proc.returncode,
                "stderr_tail": queue_proc.stderr[-2000:],
            },
            "delayed": {
                "returncode": delayed_proc.returncode,
                "stderr_tail": delayed_proc.stderr[-2000:],
            },
            "lease": {
                "returncode": lease_proc.returncode,
                "stderr_tail": lease_proc.stderr[-2000:],
            },
        },
    }


def evaluate(args: argparse.Namespace, state: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    job = state.get("job") if isinstance(state.get("job"), dict) else None
    stage_run = state.get("stage_run") if isinstance(state.get("stage_run"), dict) else None
    counts = state.get("status_counts") if isinstance(state.get("status_counts"), dict) else {}
    if not job:
        blockers.append("job_not_found")
    else:
        if job.get("status") != "running":
            blockers.append(f"job_status_{job.get('status')}")
        if job.get("current_stage") != args.stage:
            blockers.append(f"job_stage_{job.get('current_stage')}")
        stale_sec = int_field(job, "heartbeat_stale_sec")
        if stale_sec < args.min_stale_sec:
            blockers.append(f"heartbeat_not_stale_{stale_sec}s")
    if not stage_run:
        blockers.append("stage_run_not_found")
    else:
        if stage_run.get("status") != "running":
            blockers.append(f"stage_run_status_{stage_run.get('status')}")
        if stage_run.get("finished_at"):
            blockers.append("stage_run_already_finished")
    lease_exists = int(state.get("lease_exists") or 0)
    if lease_exists != 0:
        blockers.append("stage_lease_exists")
    elif not args.allow_expired_lease_requeue:
        blockers.append("expired_lease_requires_allow_expired_lease_requeue")
    if state.get("queue_matches"):
        blockers.append("matching_ready_queue_task_exists")
    if state.get("delayed_matches"):
        blockers.append("matching_delayed_queue_task_exists")
    if args.stage == "tts_duration" and int(counts.get("translated") or 0) <= 0:
        blockers.append("no_translated_segments_to_resume")
    if args.stage == "merge" and int(counts.get("synthesized") or 0) <= 0:
        blockers.append("no_synthesized_segments_to_merge")
    if args.stage in {"tts_duration", "merge"} and int(counts.get("synthesized") or 0) <= 0:
        warnings.append("no_synthesized_segments_seen")
    return not blockers, blockers, warnings


def execute_requeue(args: argparse.Namespace, root: Path, state: dict[str, Any]) -> dict[str, Any]:
    stage_run = state.get("stage_run") if isinstance(state.get("stage_run"), dict) else {}
    stage_run_id = int_field(stage_run, "id")
    duration_ms = max(0, int_field(stage_run, "stage_age_sec") * 1000)
    meta = json.dumps(
        {
            "requeued_by": args.requested_by,
            "reason": args.reason,
            "requeued_at": datetime.now(timezone.utc).isoformat(),
            "previous_status": "running",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    sql = f"""
begin;
update job_stage_runs
   set status = 'stale_requeued',
       error_message = 'stale running stage requeued by mit6824_resume_stalled_stage',
       duration_ms = {duration_ms},
       finished_at = now(),
       updated_at = now(),
       meta = '{meta.replace("'", "''")}'::jsonb
 where id = {stage_run_id}
   and status = 'running'
   and finished_at is null;
update jobs
   set status = 'queued',
       current_stage = '{args.stage}',
       error_message = '',
       updated_at = now()
 where id = {args.job_id}
   and status = 'running'
   and current_stage = '{args.stage}';
commit;
"""
    db_proc = run_psql(args, root, sql)
    payload = {
        "job_id": args.job_id,
        "stage": args.stage,
        "attempt": 0,
        "requested_by": args.requested_by,
        "reason": args.reason,
    }
    queue_op = "LPUSH" if args.queue_front else "RPUSH"
    redis_proc = run_redis(args, root, [queue_op, args.queue_key, json.dumps(payload, separators=(",", ":"))])
    return {
        "db": {
            "returncode": db_proc.returncode,
            "stdout_tail": db_proc.stdout[-2000:],
            "stderr_tail": db_proc.stderr[-2000:],
        },
        "redis": {
            "returncode": redis_proc.returncode,
            "stdout_tail": redis_proc.stdout[-2000:],
            "stderr_tail": redis_proc.stderr[-2000:],
        },
        "payload": payload,
        "queue_op": queue_op,
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    state = report.get("state") if isinstance(report.get("state"), dict) else {}
    job = state.get("job") if isinstance(state.get("job"), dict) else {}
    stage_run = state.get("stage_run") if isinstance(state.get("stage_run"), dict) else {}
    lines = [
        "# MIT 6.824 Stalled Stage Resume",
        "",
        f"Generated: `{report.get('generated_at')}`",
        f"Executed: `{summary.get('executed')}`",
        f"Safe to execute: `{summary.get('safe_to_execute')}`",
        f"Blockers: `{summary.get('blockers')}`",
        f"Warnings: `{summary.get('warnings')}`",
        "",
        "## Job",
        "",
        f"- Job: `{job.get('id', '')}`",
        f"- Status: `{job.get('status', '')}`",
        f"- Stage: `{job.get('current_stage', '')}`",
        f"- Heartbeat stale sec: `{job.get('heartbeat_stale_sec', '')}`",
        "",
        "## Stage Run",
        "",
        f"- Stage run: `{stage_run.get('id', '')}`",
        f"- Status: `{stage_run.get('status', '')}`",
        f"- Started: `{stage_run.get('started_at', '')}`",
        f"- Age sec: `{stage_run.get('stage_age_sec', '')}`",
        "",
        "## Queue",
        "",
        f"- Ready queue len: `{state.get('queue_len')}`",
        f"- Delayed queue len: `{state.get('delayed_len')}`",
        f"- Lease key: `{state.get('lease_key')}`",
        f"- Lease exists: `{state.get('lease_exists')}`",
        f"- Matching ready tasks: `{len(state.get('queue_matches') or [])}`",
        f"- Matching delayed tasks: `{len(state.get('delayed_matches') or [])}`",
    ]
    if report.get("execution"):
        lines.extend(
            [
                "",
                "## Execution",
                "",
                f"- DB return code: `{report['execution']['db'].get('returncode')}`",
                f"- Redis return code: `{report['execution']['redis'].get('returncode')}`",
                f"- Payload: `{json.dumps(report['execution'].get('payload'), ensure_ascii=False, sort_keys=True)}`",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    state = collect_state(args, root)
    safe, blockers, warnings = evaluate(args, state)
    execution: dict[str, Any] | None = None
    if args.execute and safe:
        execution = execute_requeue(args, root, state)
    elif args.execute and not safe:
        warnings.append("execute_requested_but_refused")
    report = {
        "schema_version": "holodub.mit6824_resume_stalled_stage.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "job_id": args.job_id,
            "stage": args.stage,
            "min_stale_sec": args.min_stale_sec,
            "execute": args.execute,
            "allow_expired_lease_requeue": args.allow_expired_lease_requeue,
            "queue_front": args.queue_front,
        },
        "summary": {
            "safe_to_execute": safe,
            "executed": bool(execution),
            "blockers": blockers,
            "warnings": warnings,
        },
        "state": state,
        "execution": execution,
    }
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
    return 0 if safe or not args.execute else 2


if __name__ == "__main__":
    raise SystemExit(main())
