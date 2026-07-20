from __future__ import annotations

import argparse
import csv
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]
TARGET_LECTURE_START = 8
TARGET_LECTURE_END = 20
TARGET_OBJECTIVE = "MIT 6.824 lectures 8-20 dubbing and acceptance; lecture 7 is regression fixture only"
TARGET_OBJECTIVE_DETAIL = (
    "Complete MIT 6.824 2020 lectures 8-20 Chinese dubbing production delivery and acceptance; "
    "lecture 7 / v25 remains only the LoRA candidate and main-flow regression fixture."
)
TRAINING_POLICY_SUMMARY = (
    "Do not train one adapter per lecture by default; train only course/domain-scoped adapters after "
    "repeated cross-lecture systemic failures, enough human-approved aligned audio, and fixed holdout validation."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build one MIT 6.824 dubbing goal status snapshot.")
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--training-decision", default="data/validation/mit6824_training_decision.json")
    parser.add_argument("--course-advance-status", default="data/validation/mit6824_course_advance_status.json")
    parser.add_argument("--auto-advance-status", default="data/validation/mit6824_auto_advance_status.json")
    parser.add_argument("--lecture-finalize-status", default="data/validation/mit6824_acceptance/lec-08/finalize_status.json")
    parser.add_argument("--v25-finalize-status", default="data/training/lora_runs/main_flow_candidate_episode147_v25/auto_finalize_status.json")
    parser.add_argument("--active-regression-jobs", default="342,343,344,345,346,347")
    parser.add_argument("--slow-progress-sec", type=int, default=900)
    parser.add_argument("--stale-progress-sec", type=int, default=1800)
    parser.add_argument("--output-json", default="data/validation/mit6824_goal_status.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_goal_status.md")
    parser.add_argument("--no-db", action="store_true")
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


def load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": str(path)}
    return payload if isinstance(payload, dict) else {"_invalid_json_shape": True, "_path": str(path)}


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run(cmd: list[str], *, cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def active_job_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.append(int(part))
    return ids


def query_active_regression(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    ids = active_job_ids(args.active_regression_jobs)
    if args.no_db or not ids:
        return {"enabled": False, "reason": "no_db_or_no_job_ids", "job_ids": ids}
    id_csv = ",".join(str(item) for item in ids)
    sql = f"""
select count(*) filter (where status='synthesized') as synthesized,
       count(*) as total,
       count(*) filter (where meta->'tts_lora_candidate_runtime'->>'audio_selection_mode'='candidate_audio') as candidate_audio,
       count(*) filter (where coalesce(meta->'tts_lora_candidate_runtime'->>'fallback_reason','') <> '') as fallback,
       round(avg(abs(tts_duration_ms-original_duration_ms)) filter (where status='synthesized'),2) as mean_abs_drift,
       max(abs(tts_duration_ms-original_duration_ms)) filter (where status='synthesized') as max_abs_drift,
       coalesce(max(updated_at) filter (where status='synthesized')::text,'') as latest_synth,
       coalesce((now() - max(updated_at) filter (where status='synthesized'))::text,'') as since_latest_synth,
       coalesce(extract(epoch from (now() - max(updated_at) filter (where status='synthesized')))::bigint::text,'') as since_latest_synth_sec
from segments
where job_id in ({id_csv});
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
    result: dict[str, Any] = {
        "enabled": True,
        "job_ids": ids,
        "returncode": proc.returncode,
    }
    if proc.returncode != 0:
        result["error"] = (proc.stderr or proc.stdout)[-2000:]
        return result
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if len(lines) < 2:
        result["error"] = "empty_psql_output"
        result["stdout_tail"] = proc.stdout[-2000:]
        return result
    reader = csv.DictReader(lines, delimiter="\t")
    rows = [dict(row) for row in reader]
    result["metrics"] = rows[0] if rows else {}
    result["job_breakdown"] = query_active_regression_jobs(args, root, id_csv)
    result["health"] = classify_active_regression_health(result, args)
    return result


def query_active_regression_jobs(args: argparse.Namespace, root: Path, id_csv: str) -> list[dict[str, Any]]:
    sql = f"""
select j.id,
       coalesce(j.status,'') as status,
       coalesce(j.current_stage,'') as current_stage,
       coalesce(j.error_message,'') as error_message,
       coalesce(j.updated_at::text,'') as updated_at,
       count(s.id)::text as segments,
       count(*) filter (where s.status='synthesized')::text as synthesized,
       count(*) filter (where s.status='translated')::text as translated,
       count(*) filter (where s.status not in ('synthesized','translated'))::text as other_status
from jobs j
left join segments s on s.job_id=j.id
where j.id in ({id_csv})
group by j.id
order by j.id;
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
        return []
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if len(lines) < 2:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(row) for row in reader]


def classify_active_regression_health(result: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    jobs = result.get("job_breakdown") if isinstance(result.get("job_breakdown"), list) else []
    try:
        total = int(metrics.get("total") or 0)
        synthesized = int(metrics.get("synthesized") or 0)
        since_sec = int(metrics.get("since_latest_synth_sec") or 0)
    except (TypeError, ValueError):
        return {"status": "unknown", "reason": "metrics_parse_failed"}
    running_jobs = [row for row in jobs if row.get("status") == "running"]
    errored_jobs = [row for row in jobs if row.get("error_message")]
    if errored_jobs:
        return {"status": "error", "reason": "job_error_message", "jobs": [row.get("id") for row in errored_jobs]}
    if total and synthesized >= total:
        return {"status": "complete", "reason": "all_segments_synthesized"}
    if running_jobs and since_sec < args.slow_progress_sec:
        return {"status": "running_recent", "reason": "recent_synthesis", "running_jobs": [row.get("id") for row in running_jobs]}
    if running_jobs and since_sec < args.stale_progress_sec:
        return {"status": "running_slow", "reason": "no_recent_synthesis_but_job_running", "running_jobs": [row.get("id") for row in running_jobs]}
    if running_jobs:
        return {"status": "stale_needs_diagnosis", "reason": "running_job_without_synthesis_past_threshold", "running_jobs": [row.get("id") for row in running_jobs]}
    return {"status": "no_running_job", "reason": "segments_remaining_but_no_job_running"}


def first_not_started(tracker: dict[str, Any] | None) -> int | None:
    if not tracker:
        return None
    summary = tracker.get("summary")
    if isinstance(summary, dict) and summary.get("first_not_started"):
        return int(summary["first_not_started"])
    for row in tracker.get("lectures") or []:
        if isinstance(row, dict) and row.get("status") == "not_started":
            return int(row.get("lecture") or 0)
    return None


def lecture_number(row: dict[str, Any]) -> int | None:
    try:
        lecture = int(row.get("lecture") or 0)
    except (TypeError, ValueError):
        return None
    return lecture if lecture > 0 else None


def course_delivery_state(tracker: dict[str, Any] | None, acceptance: dict[str, Any] | None) -> dict[str, Any]:
    tracker_summary = tracker.get("summary") if isinstance(tracker, dict) else {}
    acceptance_summary = acceptance.get("summary") if isinstance(acceptance, dict) else {}
    lectures = tracker.get("lectures") if isinstance(tracker, dict) and isinstance(tracker.get("lectures"), list) else []
    lecture_range = [
        int(row.get("lecture"))
        for row in lectures
        if isinstance(row, dict) and row.get("lecture") is not None
    ]
    in_progress = [
        lecture
        for row in lectures
        if isinstance(row, dict)
        for lecture in [lecture_number(row)]
        if lecture is not None and row.get("status") == "in_progress"
    ]
    delivered = [
        lecture
        for row in lectures
        if isinstance(row, dict)
        for lecture in [lecture_number(row)]
        if lecture is not None and row.get("status") == "delivered"
    ]
    not_started = acceptance_summary.get("not_started") if isinstance(acceptance_summary, dict) else None
    if not isinstance(not_started, list):
        not_started = [
            int(row.get("lecture"))
            for row in lectures
            if isinstance(row, dict) and row.get("status") == "not_started" and row.get("lecture") is not None
        ]
    next_not_started_lecture = first_not_started(tracker)
    current_frontier = in_progress[0] if in_progress else next_not_started_lecture
    return {
        "confirmed_user_goal": "dub_and_accept_mit6824_2020_lectures_8_to_20",
        "target_lectures": [min(lecture_range), max(lecture_range)] if lecture_range else [TARGET_LECTURE_START, TARGET_LECTURE_END],
        "production_delivery_range": {
            "start": TARGET_LECTURE_START,
            "end": TARGET_LECTURE_END,
            "lectures": list(range(TARGET_LECTURE_START, TARGET_LECTURE_END + 1)),
        },
        "current_production_frontier": current_frontier,
        "in_progress_lectures": in_progress,
        "delivered_lectures": delivered,
        "tracker_status_counts": tracker_summary.get("by_status") if isinstance(tracker_summary, dict) else {},
        "acceptance_status_counts": acceptance_summary.get("by_status") if isinstance(acceptance_summary, dict) else {},
        "not_started_lectures": not_started,
        "accepted_lectures": acceptance_summary.get("accepted", []) if isinstance(acceptance_summary, dict) else [],
        "ready_for_review_lectures": acceptance_summary.get("ready_for_review", []) if isinstance(acceptance_summary, dict) else [],
        "next_not_started_lecture": next_not_started_lecture,
        "next_production_lecture": current_frontier,
        "current_regression_fixture": "lecture_7_v25",
        "lecture_7_role": "regression_fixture_only_not_a_delivery_target",
        "delivery_start_rule": "start_lecture_8_after_lecture_7_regression_fixture_freezes",
        "training_policy_summary": TRAINING_POLICY_SUMMARY,
    }


def next_action(status: dict[str, Any]) -> str:
    v25 = status.get("active_regression") or {}
    v25_metrics = v25.get("metrics") if isinstance(v25, dict) else {}
    if isinstance(v25_metrics, dict):
        try:
            total = int(v25_metrics.get("total") or 0)
            synthesized = int(v25_metrics.get("synthesized") or 0)
        except (TypeError, ValueError):
            total = synthesized = 0
        if total and synthesized < total:
            health = v25.get("health") if isinstance(v25.get("health"), dict) else {}
            health_status = str(health.get("status") or "")
            if health_status in {"error", "stale_needs_diagnosis", "no_running_job", "unknown"}:
                return f"diagnose_v25_candidate_regression_{health_status}"
            if health_status == "running_slow":
                return "monitor_v25_candidate_regression_slow"
            return "wait_for_v25_candidate_regression"
    auto_advance = status.get("auto_advance_status") or {}
    if isinstance(auto_advance, dict) and auto_advance.get("status") == "waiting_for_finalize":
        return "wait_for_v25_finalize_then_auto_start_lecture_8"
    lecture_finalize = status.get("lecture_finalize_status") or {}
    if isinstance(lecture_finalize, dict) and str(lecture_finalize.get("status") or "").startswith("waiting_"):
        return "wait_for_lecture_8_episode_or_completion"
    acceptance = status.get("acceptance_status") or {}
    summary = acceptance.get("summary") if isinstance(acceptance, dict) else {}
    if isinstance(summary, dict) and summary.get("ready_for_review"):
        return "human_review_ready_lectures"
    return "refresh_tracker_and_continue_next_lecture"


def build_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    tracker = load_json(resolve(root, args.tracker))
    acceptance = load_json(resolve(root, args.acceptance_status))
    training = load_json(resolve(root, args.training_decision))
    status = {
        "schema_version": "holodub.mit6824_goal_status.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "objective": TARGET_OBJECTIVE,
        "progress_contract": {
            "delivery_range": "lectures_8_to_20",
            "objective_detail": TARGET_OBJECTIVE_DETAIL,
            "lecture_7_role": "lora_candidate_regression_fixture",
            "training_policy": "do_not_train_per_episode",
            "adapter_scope": "course_or_domain_profile",
            "completion_rule": "each_lecture_requires_final_video_audio_asr_risk_queue_review_pack_provenance_and_user_acceptance",
        },
        "course_delivery_state": course_delivery_state(tracker, acceptance),
        "paths": {
            "tracker": rel_to_root(resolve(root, args.tracker), root),
            "acceptance_status": rel_to_root(resolve(root, args.acceptance_status), root),
            "training_decision": rel_to_root(resolve(root, args.training_decision), root),
            "course_advance_status": rel_to_root(resolve(root, args.course_advance_status), root),
            "auto_advance_status": rel_to_root(resolve(root, args.auto_advance_status), root),
            "lecture_finalize_status": rel_to_root(resolve(root, args.lecture_finalize_status), root),
            "v25_finalize_status": rel_to_root(resolve(root, args.v25_finalize_status), root),
        },
        "course_tracker_summary": tracker.get("summary") if tracker else None,
        "next_not_started_lecture": first_not_started(tracker),
        "acceptance_status": acceptance,
        "training_decision": training,
        "course_advance_status": load_json(resolve(root, args.course_advance_status)),
        "auto_advance_status": load_json(resolve(root, args.auto_advance_status)),
        "lecture_finalize_status": load_json(resolve(root, args.lecture_finalize_status)),
        "v25_finalize_status": load_json(resolve(root, args.v25_finalize_status)),
        "active_regression": query_active_regression(args, root),
    }
    status["next_action"] = next_action(status)
    return status


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, status: dict[str, Any]) -> None:
    tracker_summary = status.get("course_tracker_summary") or {}
    delivery_state = status.get("course_delivery_state") or {}
    acceptance = status.get("acceptance_status") or {}
    acceptance_summary = acceptance.get("summary") if isinstance(acceptance, dict) else {}
    training = status.get("training_decision") or {}
    training_summary = training.get("summary") if isinstance(training, dict) else {}
    course_advance = status.get("course_advance_status") or {}
    course_next = course_advance.get("next_action") if isinstance(course_advance, dict) else {}
    active = status.get("active_regression") or {}
    metrics = active.get("metrics") if isinstance(active, dict) else {}
    if not isinstance(metrics, dict):
        metrics = {}
    health = active.get("health") if isinstance(active, dict) else {}
    if not isinstance(health, dict):
        health = {}
    job_breakdown = active.get("job_breakdown") if isinstance(active, dict) and isinstance(active.get("job_breakdown"), list) else []
    lines = [
        "# MIT 6.824 Goal Status",
        "",
        f"Generated: `{status['generated_at']}`",
        "",
        "## Objective",
        "",
        TARGET_OBJECTIVE,
        "",
        TARGET_OBJECTIVE_DETAIL,
        "",
        "## Current Action",
        "",
        f"`{status.get('next_action')}`",
        "",
        "## Course Progress",
        "",
        f"- Tracker summary: `{json.dumps(tracker_summary, ensure_ascii=False, sort_keys=True)}`",
        f"- Acceptance summary: `{json.dumps(acceptance_summary, ensure_ascii=False, sort_keys=True)}`",
        f"- Next not-started lecture: `{status.get('next_not_started_lecture')}`",
        f"- Current production frontier: `{delivery_state.get('current_production_frontier')}`",
        f"- Not-started lectures: `{delivery_state.get('not_started_lectures', [])}`",
        f"- Lecture 7 role: `{delivery_state.get('lecture_7_role', '')}`",
        f"- Delivery start rule: `{delivery_state.get('delivery_start_rule', '')}`",
        "",
        "## Active Lecture 7 Regression",
        "",
        f"- Jobs: `{','.join(str(item) for item in active.get('job_ids', []))}`",
        f"- Synthesized: `{metrics.get('synthesized', '')}/{metrics.get('total', '')}`",
        f"- Candidate audio: `{metrics.get('candidate_audio', '')}`",
        f"- Fallback: `{metrics.get('fallback', '')}`",
        f"- Mean abs drift: `{metrics.get('mean_abs_drift', '')}ms`",
        f"- Max abs drift: `{metrics.get('max_abs_drift', '')}ms`",
        f"- Latest synth: `{metrics.get('latest_synth', '')}`",
        f"- Since latest synth: `{metrics.get('since_latest_synth', '')}`",
        f"- Health: `{health.get('status', '') if isinstance(health, dict) else ''}` ({health.get('reason', '') if isinstance(health, dict) else ''})",
        "",
        "### Job Breakdown",
        "",
        "| Job | Status | Stage | Synthesized | Translated | Other |",
        "| ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for row in job_breakdown:
        lines.append(
            f"| {row.get('id', '')} | {row.get('status', '')} | {row.get('current_stage', '')} | "
            f"{row.get('synthesized', '')}/{row.get('segments', '')} | {row.get('translated', '')} | {row.get('other_status', '')} |"
        )
    lines.extend(
        [
        "",
        "## Automation",
        "",
        f"- Course advance: `{course_next.get('status', '') if isinstance(course_next, dict) else ''}`",
        f"- Auto-advance: `{(status.get('auto_advance_status') or {}).get('status', '')}`",
        f"- Lecture finalizer: `{(status.get('lecture_finalize_status') or {}).get('status', '')}`",
        f"- v25 finalizer rc: `{(status.get('v25_finalize_status') or {}).get('returncode', '')}`",
        "",
        "## Training Decision",
        "",
        f"- Recommendation: `{training_summary.get('recommendation', '') if isinstance(training_summary, dict) else ''}`",
        f"- Policy: {TRAINING_POLICY_SUMMARY}",
        "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    status = build_status(args, root)
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths are required")
    write_json(output_json, status)
    write_markdown(output_md, status)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "next_action": status.get("next_action"),
                "active_regression": status.get("active_regression", {}).get("metrics", {}),
                "active_regression_health": status.get("active_regression", {}).get("health", {}),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
