from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clone a completed episode into a LoRA candidate shadow episode queued at tts_duration."
    )
    parser.add_argument("--source-episode-id", type=int, required=True)
    parser.add_argument("--shadow-name", required=True)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--no-enqueue", action="store_true")
    parser.add_argument("--rollback", action="store_true", help="Validate the clone SQL and roll back instead of creating rows.")
    parser.add_argument("--requested-by", default="lora_shadow_clone")
    parser.add_argument("--reason", default="lora_shadow_episode_tts")
    parser.add_argument("--queue-key", default="holodub:tasks")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--redis-service", default="redis")
    parser.add_argument("--compose-file", action="append", default=[])
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run(cmd: list[str], *, cwd: Path, input_text: str | None = None, timeout: int = 120) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed rc={proc.returncode}: {' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def clone_sql(source_episode_id: int, shadow_name: str, *, rollback: bool = False) -> str:
    finish = "rollback;" if rollback else "commit;"
    return f"""
begin;
with src_ep as (
    select *
    from episodes
    where id = {int(source_episode_id)}
    for share
),
new_ep as (
    insert into episodes (
        tenant_key,
        name,
        source_video_rel_path,
        source_language,
        target_language,
        duration_ms,
        total_chapters,
        glossary,
        reference_card,
        episode_judge_score,
        episode_judge_meta,
        status,
        output_rel_path,
        error_message,
        completed_at,
        vocals_rel_path,
        bgm_rel_path,
        asr_done_at,
        glossary_done_at,
        output_layout_version,
        chapters_manifest_rel_path,
        loudnorm_stats,
        llm_chapters,
        rework_attempts,
        rework_status,
        accumulated_cost_usd,
        created_at,
        updated_at
    )
    select
        tenant_key,
        {sql_quote(shadow_name)},
        source_video_rel_path,
        source_language,
        target_language,
        duration_ms,
        total_chapters,
        glossary,
        reference_card,
        null,
        null,
        'running',
        null,
        null,
        null,
        vocals_rel_path,
        bgm_rel_path,
        asr_done_at,
        glossary_done_at,
        output_layout_version,
        null,
        loudnorm_stats,
        llm_chapters,
        '[]'::jsonb,
        null,
        0,
        now(),
        now()
    from src_ep
    returning *
),
src_jobs as (
    select j.*
    from jobs j
    where j.episode_id = {int(source_episode_id)}
    order by j.chapter_ordinal, j.id
),
new_jobs as (
    insert into jobs (
        name,
        status,
        current_stage,
        source_language,
        target_language,
        input_rel_path,
        vocals_rel_path,
        bgm_rel_path,
        output_rel_path,
        config,
        error_message,
        retry_count,
        heartbeat_at,
        tenant_key,
        external_id,
        max_retries,
        webhook_url,
        webhook_secret,
        started_at,
        completed_at,
        deadline_at,
        cancel_requested_at,
        cancelled_at,
        translation_summary,
        episode_id,
        chapter_ordinal,
        chapter_start_ms,
        chapter_end_ms,
        chapter_title,
        chapter_title_translated,
        chapter_summary_md,
        chapter_judge_score,
        chapter_judge_meta,
        created_at,
        updated_at
    )
    select
        j.name,
        'queued',
        'tts_duration',
        j.source_language,
        j.target_language,
        j.input_rel_path,
        j.vocals_rel_path,
        j.bgm_rel_path,
        null,
        j.config,
        null,
        0,
        null,
        j.tenant_key,
        null,
        j.max_retries,
        j.webhook_url,
        j.webhook_secret,
        null,
        null,
        j.deadline_at,
        null,
        null,
        j.translation_summary,
        e.id,
        j.chapter_ordinal,
        j.chapter_start_ms,
        j.chapter_end_ms,
        j.chapter_title,
        j.chapter_title_translated,
        j.chapter_summary_md,
        null,
        null,
        now(),
        now()
    from src_jobs j
    cross join new_ep e
    order by j.chapter_ordinal, j.id
    returning *
),
job_map as (
    select
        sj.id as source_job_id,
        nj.id as shadow_job_id,
        sj.chapter_ordinal,
        sj.name
    from src_jobs sj
    join new_jobs nj on nj.chapter_ordinal = sj.chapter_ordinal
),
new_segments as (
    insert into segments (
        job_id,
        speaker_id,
        speaker_label,
        ordinal,
        start_ms,
        end_ms,
        original_duration_ms,
        source_text,
        target_text,
        split_reason,
        tts_audio_rel_path,
        tts_duration_ms,
        status,
        meta,
        voice_profile_id,
        judge_score,
        judge_meta,
        parent_segment_id,
        created_at,
        updated_at
    )
    select
        jm.shadow_job_id,
        s.speaker_id,
        s.speaker_label,
        s.ordinal,
        s.start_ms,
        s.end_ms,
        s.original_duration_ms,
        s.source_text,
        s.target_text,
        s.split_reason,
        '',
        0,
        'translated',
        (
            case
                when jsonb_typeof(s.meta) = 'object' then s.meta
                else '{{}}'::jsonb
            end
            - 'tts_lora_candidate'
            - 'tts_lora_candidate_runtime'
            - 'tts_agent'
            - 'tts_spoken_text'
            - 'tts_text_normalized'
            - 'tts_text_rewrites'
            - 'voice_config_source'
            - 'voice_config_sample_relpaths'
        ) || jsonb_build_object(
            'shadow_source_job_id', jm.source_job_id,
            'shadow_source_segment_id', s.id,
            'shadow_source_episode_id', {int(source_episode_id)}
        ),
        s.voice_profile_id,
        null,
        null,
        s.parent_segment_id,
        now(),
        now()
    from segments s
    join job_map jm on jm.source_job_id = s.job_id
    order by jm.chapter_ordinal, s.ordinal
    returning job_id
)
select jsonb_build_object(
    'source_episode_id', {int(source_episode_id)},
    'shadow_episode_id', (select id from new_ep),
    'shadow_name', {sql_quote(shadow_name)},
    'enqueued', false,
    'chapters', (
        select jsonb_agg(
            jsonb_build_object(
                'source_job_id', jm.source_job_id,
                'shadow_job_id', jm.shadow_job_id,
                'ordinal', jm.chapter_ordinal,
                'segments', (
                    select count(*)
                    from new_segments ns
                    where ns.job_id = jm.shadow_job_id
                )
            )
            order by jm.chapter_ordinal
        )
        from job_map jm
    )
) as report;
{finish}
"""


def run_clone(args: argparse.Namespace, root: Path) -> dict[str, Any]:
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
            "-v",
            "ON_ERROR_STOP=1",
        ],
    )
    stdout = run(
        cmd,
        cwd=root,
        input_text=clone_sql(args.source_episode_id, args.shadow_name, rollback=args.rollback),
        timeout=180,
    )
    json_line = ""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            json_line = line
    if not json_line:
        raise RuntimeError(f"clone report missing from psql output:\n{stdout}")
    return json.loads(json_line)


def enqueue_jobs(args: argparse.Namespace, root: Path, report: dict[str, Any]) -> None:
    for chapter in report.get("chapters") or []:
        payload = {
            "job_id": int(chapter["shadow_job_id"]),
            "stage": "tts_duration",
            "attempt": 0,
            "requested_by": args.requested_by,
            "reason": args.reason,
        }
        cmd = compose_command(
            args,
            [
                "exec",
                "-T",
                args.redis_service,
                "redis-cli",
                "RPUSH",
                args.queue_key,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ],
        )
        run(cmd, cwd=root, timeout=30)
    report["enqueued"] = True
    report["queue_key"] = args.queue_key
    report["requested_by"] = args.requested_by
    report["reason"] = args.reason


def main() -> int:
    args = parse_args()
    root = repo_root()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = root / run_dir
    run_dir.mkdir(parents=True, exist_ok=True)

    report = run_clone(args, root)
    if args.rollback:
        report["rolled_back"] = True
    elif not args.no_enqueue:
        enqueue_jobs(args, root, report)
    report_path = run_dir / "clone_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
