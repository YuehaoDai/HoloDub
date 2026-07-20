from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finalize an episode-level LoRA candidate opt-in shadow run with lineage, monitor, ASR audit, and review pack."
    )
    parser.add_argument("--source-episode-id", type=int, required=True)
    parser.add_argument("--shadow-episode-id", type=int, required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--skip-review-pack", action="store_true")
    parser.add_argument("--skip-clips", action="store_true")
    parser.add_argument("--asr-timeout-sec", type=int, default=10800)
    parser.add_argument("--focus-source-segments", default="10693,10694,10695,10696,10697,10698,10700,10701,10702,10720,10725,10728,10733,10751,10786,10808,10833,10835,10866")
    parser.add_argument("--compose-file", action="append", default=[])
    return parser.parse_args()


def run(cmd: list[str], *, cwd: Path, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    command = ["docker", "compose"]
    for file in files:
        command.extend(["-f", file])
    command.extend(extra)
    return command


def lineage_backfill_sql(source_episode_id: int, shadow_episode_id: int) -> str:
    return (
        "update segments s set meta = "
        "(case when jsonb_typeof(s.meta) = 'object' then s.meta else '{}'::jsonb end) || "
        "jsonb_build_object('shadow_source_job_id', srcj.id, 'shadow_source_segment_id', srcs.id) "
        "from jobs sj "
        f"join jobs srcj on srcj.episode_id = {int(source_episode_id)} and srcj.chapter_ordinal = sj.chapter_ordinal "
        "join segments srcs on srcs.job_id = srcj.id "
        f"where s.job_id = sj.id and sj.episode_id = {int(shadow_episode_id)} and srcs.ordinal = s.ordinal "
        "and abs((coalesce(sj.chapter_start_ms,0) + coalesce(s.start_ms,0)) - "
        "(coalesce(srcj.chapter_start_ms,0) + coalesce(srcs.start_ms,0))) <= 500 "
        "and abs((coalesce(s.end_ms,0) - coalesce(s.start_ms,0)) - "
        "(coalesce(srcs.end_ms,0) - coalesce(srcs.start_ms,0))) <= 1000;"
    )


def backfill_lineage(args: argparse.Namespace, repo_root: Path) -> str:
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
            "-X",
            "-A",
            "-t",
            "-c",
            lineage_backfill_sql(args.source_episode_id, args.shadow_episode_id),
        ],
    )
    return run(cmd, cwd=repo_root, timeout=120).stdout.strip()


def episode_completion_sql(shadow_episode_id: int) -> str:
    return f"""
select json_build_object(
  'episode_id', e.id,
  'episode_status', coalesce(e.status, ''),
  'episode_output_relpath', coalesce(e.output_rel_path, ''),
  'jobs_total', count(j.id),
  'jobs_completed', count(j.id) filter (where coalesce(j.status, '') = 'completed'),
  'jobs_running', count(j.id) filter (where coalesce(j.status, '') = 'running'),
  'jobs_queued', count(j.id) filter (where coalesce(j.status, '') = 'queued'),
  'jobs_failed', count(j.id) filter (where coalesce(j.status, '') = 'failed'),
  'jobs_not_completed', coalesce(
    json_agg(
      json_build_object(
        'id', j.id,
        'chapter_ordinal', j.chapter_ordinal,
        'status', coalesce(j.status, ''),
        'current_stage', coalesce(j.current_stage, ''),
        'error_message', coalesce(j.error_message, '')
      )
      order by j.chapter_ordinal
    ) filter (where j.id is not null and coalesce(j.status, '') <> 'completed'),
    '[]'::json
  )
)
from episodes e
left join jobs j on j.episode_id = e.id
where e.id = {int(shadow_episode_id)}
group by e.id;
"""


def query_episode_completion(args: argparse.Namespace, repo_root: Path) -> dict[str, Any]:
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
            "-X",
            "-A",
            "-t",
            "-c",
            episode_completion_sql(args.shadow_episode_id),
        ],
    )
    raw = run(cmd, cwd=repo_root, timeout=120).stdout.strip()
    if not raw:
        return {"episode_id": args.shadow_episode_id, "missing": True}
    first_line = raw.splitlines()[0]
    try:
        parsed = json.loads(first_line)
    except json.JSONDecodeError:
        return {"episode_id": args.shadow_episode_id, "parse_error": raw[-1000:]}
    return parsed if isinstance(parsed, dict) else {"episode_id": args.shadow_episode_id, "invalid_shape": True}


def data_root_path(repo_root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else repo_root / path


def final_mix_wait_reason(episode: dict[str, Any], repo_root: Path, data_root: str) -> str:
    if episode.get("missing"):
        return "episode_missing"
    if episode.get("parse_error") or episode.get("invalid_shape"):
        return "episode_completion_status_unreadable"
    if str(episode.get("episode_status") or "") != "completed":
        return "episode_not_completed"
    try:
        jobs_total = int(episode.get("jobs_total") or 0)
        jobs_completed = int(episode.get("jobs_completed") or 0)
    except (TypeError, ValueError):
        return "job_completion_counts_unreadable"
    if jobs_total <= 0:
        return "episode_has_no_jobs"
    if jobs_completed < jobs_total:
        return "chapter_jobs_not_completed"
    relpath = str(episode.get("episode_output_relpath") or "").strip()
    if not relpath:
        return "episode_output_relpath_empty"
    media_path = data_root_path(repo_root, data_root) / relpath
    if not media_path.exists():
        return "final_media_missing"
    return ""


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_monitor(args: argparse.Namespace, repo_root: Path, run_dir: Path) -> dict[str, Any]:
    output = run_dir / "episode_candidate_monitor_final.json"
    cmd = [
        args.python,
        "tools/lora_episode_candidate_monitor.py",
        "--episode-id",
        str(args.shadow_episode_id),
        "--api-base",
        args.api_base,
        "--output",
        str(output),
        "--focus-source-segments",
        args.focus_source_segments,
    ]
    completed = subprocess.run(
        cmd,
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if completed.returncode not in (0, 1):
        raise subprocess.CalledProcessError(completed.returncode, cmd, output=completed.stdout)
    return load_json(output)


def remaining_segments(monitor: dict[str, Any]) -> int:
    progress = monitor.get("progress") if isinstance(monitor.get("progress"), dict) else {}
    return int(progress.get("segments_remaining") or 0)


def review_segment_ids(monitor: dict[str, Any]) -> list[int]:
    ids: set[int] = set()
    for row in monitor.get("focus_segments") or []:
        if isinstance(row, dict) and row.get("segment_id"):
            ids.add(int(row["segment_id"]))
    for row in monitor.get("risk_queue") or []:
        if isinstance(row, dict) and row.get("segment_id"):
            ids.add(int(row["segment_id"]))
    for issue in monitor.get("validation_issues") or []:
        if isinstance(issue, dict) and issue.get("segment_id"):
            ids.add(int(issue["segment_id"]))
    return sorted(ids)


def run_final_mix_asr(args: argparse.Namespace, repo_root: Path, run_dir: Path) -> Path:
    output_dir = run_dir / "final_mix_asr"
    cmd = [
        args.python,
        "tools/lora_final_mix_asr_audit.py",
        "--episode-id",
        str(args.shadow_episode_id),
        "--data-root",
        args.data_root,
        "--output-dir",
        str(output_dir),
        "--extract-audio-wav",
        "--run-asr",
        "--resume",
        "--asr-timeout-sec",
        str(args.asr_timeout_sec),
    ]
    run(cmd, cwd=repo_root, timeout=args.asr_timeout_sec + 1800)
    return output_dir / "final_mix_asr_report.json"


def run_review_pack(
    args: argparse.Namespace,
    repo_root: Path,
    run_dir: Path,
    *,
    final_mix_asr_report: Path | None,
    monitor: dict[str, Any],
) -> Path:
    output_dir = run_dir / "human_review_pack"
    ids = review_segment_ids(monitor)
    cmd = [
        args.python,
        "tools/lora_main_flow_review_pack.py",
        "--episode-id",
        str(args.shadow_episode_id),
        "--api-base",
        args.api_base,
        "--data-root",
        args.data_root,
        "--output-dir",
        str(output_dir),
        "--review-segment-ids",
        ",".join(str(item) for item in ids),
    ]
    if final_mix_asr_report is not None:
        cmd.extend(["--final-mix-asr-report", str(final_mix_asr_report)])
    if args.skip_clips:
        cmd.append("--skip-clips")
    run(cmd, cwd=repo_root, timeout=3600)
    return output_dir / "index.html"


def write_finalize_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = repo_root / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    backfill_output = backfill_lineage(args, repo_root)
    monitor = run_monitor(args, repo_root, run_dir)
    remaining = remaining_segments(monitor)
    summary: dict[str, Any] = {
        "schema_version": "holodub.lora_episode_candidate_finalize.v1",
        "source_episode_id": args.source_episode_id,
        "shadow_episode_id": args.shadow_episode_id,
        "run_dir": str(run_dir),
        "lineage_backfill": backfill_output,
        "monitor": str(run_dir / "episode_candidate_monitor_final.json"),
        "remaining_segments": remaining,
        "segments_completed": remaining == 0,
        "completed": False,
        "episode_completion": None,
        "final_mix_asr_report": None,
        "human_review_pack": None,
        "elapsed_sec": None,
    }
    if remaining > 0 and not args.allow_incomplete:
        summary["status"] = "incomplete_wait_for_episode"
        summary["elapsed_sec"] = round(time.time() - started, 1)
        write_finalize_summary(run_dir / "finalize_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    episode_completion = query_episode_completion(args, repo_root)
    summary["episode_completion"] = episode_completion
    wait_reason = final_mix_wait_reason(episode_completion, repo_root, args.data_root)
    if wait_reason and not args.allow_incomplete:
        summary["status"] = "incomplete_wait_for_episode_merge"
        summary["wait_reason"] = wait_reason
        summary["elapsed_sec"] = round(time.time() - started, 1)
        write_finalize_summary(run_dir / "finalize_summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    final_mix_report: Path | None = None
    if not args.skip_asr:
        final_mix_report = run_final_mix_asr(args, repo_root, run_dir)
        summary["final_mix_asr_report"] = str(final_mix_report)
    if not args.skip_review_pack:
        review_pack = run_review_pack(args, repo_root, run_dir, final_mix_asr_report=final_mix_report, monitor=monitor)
        summary["human_review_pack"] = str(review_pack)

    summary["status"] = "finalized"
    summary["completed"] = True
    summary["elapsed_sec"] = round(time.time() - started, 1)
    write_finalize_summary(run_dir / "finalize_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
