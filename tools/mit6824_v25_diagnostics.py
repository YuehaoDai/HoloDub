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
    parser = argparse.ArgumentParser(description="Read-only diagnostics for the active MIT 6.824 v25 regression run.")
    parser.add_argument("--job-ids", default="342,343,344,345,346,347")
    parser.add_argument("--max-frontier", type=int, default=12)
    parser.add_argument("--goal-status", default="data/validation/mit6824_goal_status.json")
    parser.add_argument("--status-refresh", default="data/validation/mit6824_status_refresh.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_v25_diagnostics.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_v25_diagnostics.md")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
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


def job_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            ids.append(int(part))
    return ids


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run_psql(args: argparse.Namespace, root: Path, sql: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        cmd,
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def parse_table(raw: str) -> list[dict[str, str]]:
    lines = [line for line in raw.splitlines() if line and not line.startswith("(")]
    if len(lines) < 2:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(row) for row in reader]


def query_job_summary(args: argparse.Namespace, root: Path, ids: list[int]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    id_csv = ",".join(str(item) for item in ids)
    sql = f"""
select j.id,
       coalesce(j.status,'') as status,
       coalesce(j.current_stage,'') as current_stage,
       coalesce(j.error_message,'') as error_message,
       coalesce(j.updated_at::text,'') as job_updated_at,
       count(s.id)::text as segments,
       count(*) filter (where s.status='synthesized')::text as synthesized,
       count(*) filter (where s.status='translated')::text as translated,
       count(*) filter (where s.status not in ('synthesized','translated'))::text as other_status,
       coalesce(max(s.updated_at) filter (where s.status='synthesized')::text,'') as latest_synth_at,
       coalesce(min(s.ordinal) filter (where s.status='translated')::text,'') as next_translated_ordinal
from jobs j
left join segments s on s.job_id=j.id
where j.id in ({id_csv})
group by j.id
order by j.id;
"""
    proc = run_psql(args, root, sql)
    if proc.returncode != 0:
        return [], {"returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:], "stdout_tail": proc.stdout[-2000:]}
    return parse_table(proc.stdout), {"returncode": 0}


def query_frontier(args: argparse.Namespace, root: Path, ids: list[int]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    id_csv = ",".join(str(item) for item in ids)
    limit = max(1, int(args.max_frontier))
    sql = f"""
select j.id as job_id,
       coalesce(j.status,'') as job_status,
       s.id as segment_id,
       s.ordinal,
       coalesce((s.meta->>'shadow_source_segment_id'),'') as source_segment_id,
       coalesce(s.status,'') as segment_status,
       s.original_duration_ms,
       length(coalesce(s.target_text,'')) as target_text_chars,
       left(replace(coalesce(s.target_text,''), E'\\n', ' '), 160) as target_text_preview,
       coalesce(s.updated_at::text,'') as segment_updated_at,
       coalesce((s.meta->'tts_lora_candidate'->>'enabled'),'') as candidate_plan_enabled,
       coalesce((s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode'),'') as runtime_audio_selection_mode,
       coalesce((s.meta->'tts_lora_candidate_runtime'->>'selected_family'),'') as runtime_selected_family
from jobs j
join segments s on s.job_id=j.id
where j.id in ({id_csv})
  and s.status='translated'
order by j.id, s.ordinal, s.id
limit {limit};
"""
    proc = run_psql(args, root, sql)
    if proc.returncode != 0:
        return [], {"returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:], "stdout_tail": proc.stdout[-2000:]}
    return parse_table(proc.stdout), {"returncode": 0}


def query_fallback_risks(args: argparse.Namespace, root: Path, ids: list[int]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    id_csv = ",".join(str(item) for item in ids)
    sql = f"""
select j.id as job_id,
       coalesce(j.status,'') as job_status,
       s.id as segment_id,
       s.ordinal,
       coalesce((s.meta->>'shadow_source_segment_id'),'') as source_segment_id,
       coalesce(s.status,'') as segment_status,
       s.original_duration_ms,
       s.tts_duration_ms,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') as fallback_reason,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode','') as audio_selection_mode,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'selected_family','') as selected_family,
       coalesce(jsonb_array_length(s.meta->'tts_lora_candidate_runtime'->'candidates')::text,'') as candidate_count,
       left(replace(coalesce(s.target_text,''), E'\\n', ' '), 160) as target_text_preview
from jobs j
join segments s on s.job_id=j.id
where j.id in ({id_csv})
  and coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') <> ''
order by j.id, s.ordinal, s.id;
"""
    proc = run_psql(args, root, sql)
    if proc.returncode != 0:
        return [], {"returncode": proc.returncode, "stderr_tail": proc.stderr[-2000:], "stdout_tail": proc.stdout[-2000:]}
    return parse_table(proc.stdout), {"returncode": 0}


def build_report(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    ids = job_ids(args.job_ids)
    goal = read_json(resolve(root, args.goal_status))
    status_refresh = read_json(resolve(root, args.status_refresh))
    job_summary, job_summary_status = query_job_summary(args, root, ids)
    frontier, frontier_status = query_frontier(args, root, ids)
    fallback_risks, fallback_risk_status = query_fallback_risks(args, root, ids)
    goal_active = goal.get("active_regression") if isinstance(goal.get("active_regression"), dict) else {}
    refresh_summary = status_refresh.get("summary") if isinstance(status_refresh.get("summary"), dict) else {}
    metrics = goal_active.get("metrics") if isinstance(goal_active.get("metrics"), dict) else {}
    if not metrics:
        metrics = refresh_summary.get("active_regression") if isinstance(refresh_summary.get("active_regression"), dict) else {}
    health = goal_active.get("health") if isinstance(goal_active.get("health"), dict) else {}
    if not health:
        health = refresh_summary.get("active_regression_health") if isinstance(refresh_summary.get("active_regression_health"), dict) else {}
    running_jobs = [row for row in job_summary if row.get("status") == "running"]
    errors = [row for row in job_summary if row.get("error_message")]
    return {
        "schema_version": "holodub.mit6824_v25_diagnostics.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_ids": ids,
        "paths": {
            "goal_status": rel_to_root(resolve(root, args.goal_status), root),
            "status_refresh": rel_to_root(resolve(root, args.status_refresh), root),
        },
        "summary": {
            "metrics": metrics,
            "health": health,
            "running_job_ids": [row.get("id") for row in running_jobs],
            "error_job_ids": [row.get("id") for row in errors],
            "frontier_count": len(frontier),
            "fallback_risk_count": len(fallback_risks),
            "recommendation": "inspect_errors" if errors else "wait_or_monitor_frontier",
        },
        "job_summary_status": job_summary_status,
        "frontier_status": frontier_status,
        "fallback_risk_status": fallback_risk_status,
        "job_summary": job_summary,
        "frontier": frontier,
        "fallback_risks": fallback_risks,
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    metrics = summary.get("metrics") if isinstance(summary.get("metrics"), dict) else {}
    health = summary.get("health") if isinstance(summary.get("health"), dict) else {}
    lines = [
        "# MIT 6.824 v25 Diagnostics",
        "",
        f"Generated: `{report.get('generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Synthesized: `{metrics.get('synthesized', '')}/{metrics.get('total', '')}`",
        f"- Candidate audio: `{metrics.get('candidate_audio', '')}`",
        f"- Fallback: `{metrics.get('fallback', '')}`",
        f"- Since latest synth: `{metrics.get('since_latest_synth', '')}`",
        f"- Health: `{health.get('status', '')}` ({health.get('reason', '')})",
        f"- Running jobs: `{summary.get('running_job_ids', [])}`",
        f"- Error jobs: `{summary.get('error_job_ids', [])}`",
        f"- Frontier rows: `{summary.get('frontier_count', 0)}`",
        f"- Fallback risk rows: `{summary.get('fallback_risk_count', 0)}`",
        f"- Recommendation: `{summary.get('recommendation', '')}`",
        "",
        "## Job Summary",
        "",
        "| Job | Status | Stage | Synthesized | Translated | Other | Latest Synth | Next Ordinal | Error |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    for row in report.get("job_summary") or []:
        lines.append(
            "| {id} | {status} | {stage} | {syn}/{segments} | {translated} | {other} | {latest} | {next_ord} | {error} |".format(
                id=row.get("id", ""),
                status=row.get("status", ""),
                stage=row.get("current_stage", ""),
                syn=row.get("synthesized", ""),
                segments=row.get("segments", ""),
                translated=row.get("translated", ""),
                other=row.get("other_status", ""),
                latest=row.get("latest_synth_at", ""),
                next_ord=row.get("next_translated_ordinal", ""),
                error=str(row.get("error_message", "")).replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Fallback Risk Rows",
            "",
            "| Job | Segment | Ordinal | Source | Status | Target ms | TTS ms | Reason | Mode | Family | Candidates | Preview |",
            "| ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in report.get("fallback_risks") or []:
        preview = str(row.get("target_text_preview") or "").replace("|", "\\|")
        lines.append(
            "| {job} | {segment} | {ordinal} | {source} | {status} | {target_ms} | {tts_ms} | {reason} | {mode} | {family} | {count} | {preview} |".format(
                job=row.get("job_id", ""),
                segment=row.get("segment_id", ""),
                ordinal=row.get("ordinal", ""),
                source=row.get("source_segment_id", ""),
                status=row.get("segment_status", ""),
                target_ms=row.get("original_duration_ms", ""),
                tts_ms=row.get("tts_duration_ms", ""),
                reason=str(row.get("fallback_reason", "")).replace("|", "\\|"),
                mode=row.get("audio_selection_mode", ""),
                family=row.get("selected_family", ""),
                count=row.get("candidate_count", ""),
                preview=preview,
            )
        )
    lines.extend(
        [
            "",
            "## Frontier",
            "",
            "| Job | Segment | Ordinal | Source | Target ms | Chars | Candidate Plan | Preview |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in report.get("frontier") or []:
        preview = str(row.get("target_text_preview") or "").replace("|", "\\|")
        lines.append(
            "| {job} | {segment} | {ordinal} | {source} | {ms} | {chars} | {plan} | {preview} |".format(
                job=row.get("job_id", ""),
                segment=row.get("segment_id", ""),
                ordinal=row.get("ordinal", ""),
                source=row.get("source_segment_id", ""),
                ms=row.get("original_duration_ms", ""),
                chars=row.get("target_text_chars", ""),
                plan=row.get("candidate_plan_enabled", ""),
                preview=preview,
            )
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
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
