from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ANCHORS = [
    10694,
    10700,
    10701,
    10702,
    10715,
    10733,
    10738,
    10746,
    10751,
    10778,
    10786,
    10808,
    10835,
    10866,
]

OPEN_VALIDATION_REASONS = {
    "eligible_synthesized_without_candidate_audio",
    "candidate_fallback",
    "drift_exceeds_hard_limit",
    "agent_hard_cap_exceeded",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build or freeze the MIT 6.824 lecture 7 LoRA/candidate regression suite manifest."
    )
    parser.add_argument("--run-dir", default="data/training/lora_runs/main_flow_candidate_episode147_v25")
    parser.add_argument("--suite-name", default="mit6824-lec07-lora-candidate-v25")
    parser.add_argument("--source-episode-id", type=int, default=147)
    parser.add_argument("--shadow-episode-id", type=int, default=179)
    parser.add_argument("--anchor-source-segments", default=",".join(str(item) for item in DEFAULT_ANCHORS))
    parser.add_argument("--candidate-coverage", default="data/validation/mit6824_candidate_coverage.json")
    parser.add_argument("--candidate-coverage-md", default="data/validation/mit6824_candidate_coverage.md")
    parser.add_argument("--candidate-coverage-job-ids", default="342,343,344,345,346,347")
    parser.add_argument("--refresh-candidate-coverage", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output-json", default="data/validation/mit6824_regression/lecture7_v25_regression_suite.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_regression/lecture7_v25_regression_suite.md")
    parser.add_argument("--freeze", action="store_true", help="Require completed run and hard gates before writing frozen=true.")
    parser.add_argument(
        "--allow-open-issues",
        action="store_true",
        help="Lock the fixture with recorded non-blocking open issues instead of marking it fully frozen.",
    )
    parser.add_argument(
        "--open-issue-note",
        action="append",
        default=[],
        help="Human-readable note explaining why open issues are accepted for fixture locking.",
    )
    parser.add_argument("--max-drift-ms", type=float, default=500.0)
    parser.add_argument("--max-review-drift-ms", type=float, default=300.0)
    parser.add_argument("--fail-low-judge", action="store_true", help="Treat low segment judge rows as freeze blockers.")
    parser.add_argument("--no-db", action="store_true", help="Do not query Postgres to fill anchors missing from monitor focus rows.")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--python", default=sys.executable)
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


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]
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


def refresh_candidate_coverage(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [
        getattr(args, "python", sys.executable),
        "tools/mit6824_candidate_coverage.py",
        "--job-ids",
        getattr(args, "candidate_coverage_job_ids", "342,343,344,345,346,347"),
        "--output-json",
        args.candidate_coverage,
        "--output-md",
        getattr(args, "candidate_coverage_md", "data/validation/mit6824_candidate_coverage.md"),
    ]
    proc = run(cmd, cwd=root, timeout=180)
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "output": parse_json_output(proc.stdout),
        "stdout_tail": proc.stdout[-3000:],
        "stderr_tail": proc.stderr[-3000:],
    }


def split_int_csv(raw: str) -> list[int]:
    values: list[int] = []
    for item in raw.split(","):
        item = item.strip().removeprefix("seg")
        if item:
            values.append(int(item))
    return values


def validation_reason_counts(validation_issues: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in validation_issues:
        reason = ""
        if isinstance(issue, dict):
            reason = str(issue.get("reason") or "unknown")
        else:
            reason = "unknown"
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def index_monitor_rows(monitor: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for collection in ("focus_segments",):
        for row in monitor.get(collection) or []:
            if not isinstance(row, dict):
                continue
            source_id = row.get("source_segment_id")
            if not source_id:
                continue
            rows[int(source_id)] = row
    return rows


def db_anchor_rows(args: argparse.Namespace, root: Path, source_ids: list[int]) -> dict[int, dict[str, Any]]:
    if args.no_db or not source_ids:
        return {}
    source_csv = ",".join(str(item) for item in sorted(set(source_ids)))
    sql = f"""
select (s.meta->>'shadow_source_segment_id')::bigint as source_segment_id,
       s.id as segment_id,
       j.id as chapter_id,
       j.chapter_ordinal as chapter_ordinal,
       s.ordinal as ordinal,
       coalesce(s.status,'') as status,
       s.original_duration_ms as target_ms,
       s.tts_duration_ms as tts_duration_ms,
       coalesce((s.tts_duration_ms - s.original_duration_ms)::text,'') as drift_ms,
       coalesce(s.tts_audio_rel_path,'') as tts_audio_relpath,
       coalesce(s.judge_score::text,'') as judge_score,
       coalesce(s.judge_meta->>'verdict','') as judge_verdict,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode','') as runtime_audio_selection_mode,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'selected_family','') as runtime_selected_family,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'selected_split_strategy','') as runtime_selected_split_strategy,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') as runtime_fallback_reason,
       coalesce(jsonb_array_length(s.meta->'tts_lora_candidate_runtime'->'candidates')::text,'') as runtime_candidate_count,
       coalesce(jsonb_array_length(s.meta->'tts_lora_candidate_runtime'->'split_plan'->'parts')::text,'') as runtime_split_parts
from jobs j
join segments s on s.job_id = j.id
where j.episode_id = {int(args.shadow_episode_id)}
  and (s.meta->>'shadow_source_segment_id') ~ '^[0-9]+$'
  and (s.meta->>'shadow_source_segment_id')::bigint in ({source_csv})
order by source_segment_id;
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
        return {}
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if len(lines) < 2:
        return {}
    reader = csv.DictReader(lines, delimiter="\t")
    rows: dict[int, dict[str, Any]] = {}
    for raw in reader:
        try:
            source_id = int(raw.get("source_segment_id") or 0)
        except (TypeError, ValueError):
            continue
        row: dict[str, Any] = dict(raw)
        for key in ("segment_id", "chapter_id", "chapter_ordinal", "ordinal", "target_ms", "tts_duration_ms"):
            if row.get(key) not in {"", None}:
                try:
                    row[key] = int(row[key])
                except (TypeError, ValueError):
                    pass
        for key in ("drift_ms", "judge_score"):
            if row.get(key) not in {"", None}:
                try:
                    row[key] = float(row[key])
                except (TypeError, ValueError):
                    pass
        for key in ("runtime_candidate_count", "runtime_split_parts"):
            if row.get(key) not in {"", None}:
                try:
                    row[key] = int(row[key])
                except (TypeError, ValueError):
                    pass
        rows[source_id] = row
    return rows


def anchor_status(source_id: int, row: dict[str, Any] | None, *, max_drift_ms: float, allow_low_judge: bool) -> dict[str, Any]:
    if not row:
        return {
            "source_segment_id": source_id,
            "status": "missing_from_monitor",
            "pass": False,
            "reject_reasons": ["missing_from_monitor_focus_segments"],
        }
    reject_reasons: list[str] = []
    status = str(row.get("status") or "")
    if status != "synthesized":
        reject_reasons.append(f"not_synthesized:{status}")
    fallback = str(row.get("runtime_fallback_reason") or "")
    if fallback:
        reject_reasons.append(f"fallback:{fallback}")
    mode = str(row.get("runtime_audio_selection_mode") or row.get("plan_audio_selection_mode") or "")
    if mode and mode != "candidate_audio":
        reject_reasons.append(f"not_candidate_audio:{mode}")
    drift = row.get("drift_ms")
    try:
        abs_drift = abs(float(drift))
    except (TypeError, ValueError):
        abs_drift = None
    if status == "synthesized" and abs_drift is not None and abs_drift > max_drift_ms:
        reject_reasons.append(f"high_drift_ms:{abs_drift:g}")
    judge_score = row.get("judge_score")
    try:
        judge = float(judge_score)
    except (TypeError, ValueError):
        judge = None
    if judge is not None and judge < 0.80 and not allow_low_judge:
        reject_reasons.append(f"low_judge:{judge:g}")
    gate = row.get("gate_decision") if isinstance(row.get("gate_decision"), dict) else {}
    gate_status = str(gate.get("status") or "")
    if gate_status and gate_status != "selected":
        reject_reasons.append(f"gate_not_selected:{gate_status}")
    return {
        "source_segment_id": source_id,
        "shadow_segment_id": row.get("segment_id"),
        "chapter_ordinal": row.get("chapter_ordinal"),
        "ordinal": row.get("ordinal"),
        "status": status,
        "pass": not reject_reasons,
        "reject_reasons": reject_reasons,
        "runtime_audio_selection_mode": row.get("runtime_audio_selection_mode"),
        "runtime_selected_family": row.get("runtime_selected_family"),
        "runtime_selected_split_strategy": row.get("runtime_selected_split_strategy"),
        "runtime_candidate_count": row.get("runtime_candidate_count"),
        "runtime_split_parts": row.get("runtime_split_parts"),
        "drift_ms": row.get("drift_ms"),
        "target_ms": row.get("target_ms"),
        "tts_duration_ms": row.get("tts_duration_ms"),
        "judge_score": row.get("judge_score"),
        "judge_verdict": row.get("judge_verdict"),
        "judge_issues": row.get("judge_issues") if isinstance(row.get("judge_issues"), list) else [],
        "tts_audio_relpath": row.get("tts_audio_relpath"),
    }


def build_suite(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    run_dir = resolve(root, args.run_dir)
    if run_dir is None:
        raise ValueError("run_dir required")
    monitor_path = run_dir / "episode_candidate_monitor_final.json"
    finalize_path = run_dir / "finalize_summary.json"
    auto_finalize_path = run_dir / "auto_finalize_status.json"
    run_notes_path = run_dir / "run_notes.md"
    coverage_path = resolve(root, args.candidate_coverage)
    coverage_refresh = None
    if getattr(args, "refresh_candidate_coverage", True) and not getattr(args, "no_db", False):
        coverage_refresh = refresh_candidate_coverage(args, root)
    monitor = read_json(monitor_path)
    finalize_summary = read_json(finalize_path)
    auto_finalize = read_json(auto_finalize_path)
    candidate_coverage = read_json(coverage_path)
    source_ids = split_int_csv(args.anchor_source_segments)
    row_index = index_monitor_rows(monitor)
    db_rows = db_anchor_rows(args, root, [source_id for source_id in source_ids if source_id not in row_index])
    row_index.update(db_rows)
    allow_low_judge = not args.fail_low_judge
    anchors = [
        anchor_status(source_id, row_index.get(source_id), max_drift_ms=args.max_drift_ms, allow_low_judge=allow_low_judge)
        for source_id in source_ids
    ]

    progress = monitor.get("progress") if isinstance(monitor.get("progress"), dict) else {}
    drift = monitor.get("drift") if isinstance(monitor.get("drift"), dict) else {}
    candidate = monitor.get("candidate") if isinstance(monitor.get("candidate"), dict) else {}
    agent = monitor.get("agent") if isinstance(monitor.get("agent"), dict) else {}
    risk_queue = monitor.get("risk_queue") if isinstance(monitor.get("risk_queue"), list) else []
    validation_issues = monitor.get("validation_issues") if isinstance(monitor.get("validation_issues"), list) else []
    remaining = int(progress.get("segments_remaining") or finalize_summary.get("remaining_segments") or 0)
    completed = remaining == 0 or bool(finalize_summary.get("completed")) or bool(auto_finalize.get("completed"))
    hard_issues: list[str] = []
    if coverage_refresh and coverage_refresh.get("returncode") != 0:
        hard_issues.append("candidate_coverage_refresh_failed")
    if not completed:
        hard_issues.append(f"run_incomplete:remaining_segments={remaining}")
    if int(candidate.get("fallback_reason_counts", {}).get("", 0) if isinstance(candidate.get("fallback_reason_counts"), dict) else 0) < 0:
        hard_issues.append("invalid_fallback_counts")
    fallback_rows = candidate.get("fallback_rows") if isinstance(candidate.get("fallback_rows"), list) else []
    if fallback_rows:
        hard_issues.append(f"fallback_rows:{len(fallback_rows)}")
    high_drift_count = int(drift.get("high_drift_count") or 0)
    if high_drift_count:
        hard_issues.append(f"high_drift_count:{high_drift_count}")
    validation_counts = validation_reason_counts(validation_issues)
    for reason, count in sorted(validation_counts.items()):
        hard_issues.append(f"validation_issue:{reason}:{count}")
    failed_anchors = [row for row in anchors if not row.get("pass")]
    if failed_anchors:
        hard_issues.append(f"failed_anchors:{len(failed_anchors)}")
    if agent.get("hard_cap_count"):
        hard_issues.append(f"hard_cap_count:{agent.get('hard_cap_count')}")
    if agent.get("low_judge_count") and not allow_low_judge:
        hard_issues.append(f"low_judge_count:{agent.get('low_judge_count')}")
    coverage_summary = candidate_coverage.get("summary") if isinstance(candidate_coverage.get("summary"), dict) else {}
    coverage_hard = int(coverage_summary.get("hard_issue_count") or 0)
    coverage_pending = int(coverage_summary.get("eligible_pending_segments") or 0)
    if coverage_hard:
        hard_issues.append(f"candidate_coverage_hard_issues:{coverage_hard}")
    if completed and coverage_pending:
        hard_issues.append(f"candidate_coverage_pending_after_completion:{coverage_pending}")
    blocking_hard_issues = [
        issue
        for issue in hard_issues
        if issue.startswith("run_incomplete:")
        or issue == "candidate_coverage_refresh_failed"
        or issue.startswith("candidate_coverage_pending_after_completion:")
        or (
            issue.startswith("validation_issue:")
            and issue.split(":", 2)[1] not in OPEN_VALIDATION_REASONS
        )
    ]
    open_issues = [issue for issue in hard_issues if issue not in blocking_hard_issues]

    final_mix_asr_report = finalize_summary.get("final_mix_asr_report") or auto_finalize.get("final_mix_asr_report")
    human_review_pack = finalize_summary.get("human_review_pack") or auto_finalize.get("human_review_pack")
    fully_frozen = bool(args.freeze and not hard_issues)
    fixture_locked = bool(args.freeze and (fully_frozen or (args.allow_open_issues and not blocking_hard_issues)))
    if fully_frozen:
        lock_status = "frozen"
    elif fixture_locked:
        lock_status = "locked_with_open_issues"
    elif args.freeze:
        lock_status = "freeze_refused"
    else:
        lock_status = "draft"
    return {
        "schema_version": "holodub.mit6824.regression_suite.v1",
        "suite_name": args.suite_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frozen": fully_frozen,
        "fixture_locked": fixture_locked,
        "lock_status": lock_status,
        "source_episode_id": args.source_episode_id,
        "shadow_episode_id": args.shadow_episode_id,
        "role": "lecture_7_lora_candidate_regression_fixture",
        "run_dir": rel_to_root(run_dir, root),
        "inputs": {
            "monitor": rel_to_root(monitor_path, root),
            "finalize_summary": rel_to_root(finalize_path, root),
            "auto_finalize_status": rel_to_root(auto_finalize_path, root),
            "run_notes": rel_to_root(run_notes_path, root),
            "candidate_coverage": rel_to_root(coverage_path, root),
            "candidate_coverage_refresh": coverage_refresh,
            "final_mix_asr_report": str(final_mix_asr_report or ""),
            "human_review_pack": str(human_review_pack or ""),
            "db_anchor_rows_used": len(db_rows),
        },
        "summary": {
            "completed": completed,
            "remaining_segments": remaining,
            "segments_synthesized": progress.get("segments_synthesized"),
            "segments_total": progress.get("segments_total"),
            "candidate_audio_segments": candidate.get("candidate_audio_segments"),
            "eligible_segments": candidate.get("eligible_segments"),
            "mean_abs_drift_ms": drift.get("mean_abs_drift_ms"),
            "max_abs_drift_ms": drift.get("max_abs_drift_ms"),
            "candidate_audio_mean_abs_drift_ms": drift.get("candidate_audio_mean_abs_drift_ms"),
            "candidate_audio_max_abs_drift_ms": drift.get("candidate_audio_max_abs_drift_ms"),
            "fallback_rows": len(fallback_rows),
            "risk_queue_count": len(risk_queue),
            "validation_issue_count": len(validation_issues),
            "validation_issue_reason_counts": validation_counts,
            "low_judge_count": agent.get("low_judge_count"),
            "hard_cap_count": agent.get("hard_cap_count"),
            "anchor_count": len(anchors),
            "anchor_pass_count": len([row for row in anchors if row.get("pass")]),
            "candidate_coverage_hard_issue_count": coverage_hard,
            "candidate_coverage_pending_eligible_segments": coverage_pending,
            "candidate_coverage_eligible_candidate_audio_segments": coverage_summary.get("eligible_candidate_audio_segments"),
            "candidate_coverage_eligible_synthesized_segments": coverage_summary.get("eligible_synthesized_segments"),
        },
        "hard_issues": hard_issues,
        "blocking_hard_issues": blocking_hard_issues,
        "open_issues": open_issues,
        "open_issue_notes": list(args.open_issue_note or []),
        "anchors": anchors,
        "risk_queue": risk_queue,
        "validation_issues": validation_issues,
        "freeze_policy": {
            "requires_completed": True,
            "requires_no_fallback_rows": True,
            "requires_no_high_drift": True,
            "requires_all_anchors_pass": True,
            "requires_no_hard_cap": True,
            "requires_candidate_coverage": True,
            "low_judge_allowed": allow_low_judge,
            "allow_open_issues": bool(args.allow_open_issues),
            "open_issues_do_not_mark_frozen": True,
            "max_drift_ms": args.max_drift_ms,
            "max_review_drift_ms": args.max_review_drift_ms,
        },
    }


def write_markdown(path: Path, suite: dict[str, Any]) -> None:
    summary = suite.get("summary") if isinstance(suite.get("summary"), dict) else {}
    lines = [
        "# MIT 6.824 Lecture 7 Regression Suite",
        "",
        f"Generated: `{suite.get('generated_at')}`",
        f"Suite: `{suite.get('suite_name')}`",
        f"Frozen: `{suite.get('frozen')}`",
        f"Fixture locked: `{suite.get('fixture_locked')}`",
        f"Lock status: `{suite.get('lock_status')}`",
        "",
        "## Summary",
        "",
        f"- Completed: `{summary.get('completed')}`",
        f"- Remaining segments: `{summary.get('remaining_segments')}`",
        f"- Synthesized: `{summary.get('segments_synthesized')}/{summary.get('segments_total')}`",
        f"- Candidate audio: `{summary.get('candidate_audio_segments')}`",
        f"- Candidate coverage: `{summary.get('candidate_coverage_eligible_candidate_audio_segments')}/{summary.get('candidate_coverage_eligible_synthesized_segments')}` eligible synthesized, hard `{summary.get('candidate_coverage_hard_issue_count')}`",
        f"- Mean abs drift: `{summary.get('mean_abs_drift_ms')}ms`",
        f"- Max abs drift: `{summary.get('max_abs_drift_ms')}ms`",
        f"- Risk queue: `{summary.get('risk_queue_count')}`",
        f"- Anchor pass: `{summary.get('anchor_pass_count')}/{summary.get('anchor_count')}`",
        "",
        "## Hard Issues",
        "",
    ]
    hard_issues = suite.get("hard_issues") if isinstance(suite.get("hard_issues"), list) else []
    if hard_issues:
        lines.extend(f"- `{issue}`" for issue in hard_issues)
    else:
        lines.append("- None")
    open_issues = suite.get("open_issues") if isinstance(suite.get("open_issues"), list) else []
    blocking_issues = suite.get("blocking_hard_issues") if isinstance(suite.get("blocking_hard_issues"), list) else []
    notes = suite.get("open_issue_notes") if isinstance(suite.get("open_issue_notes"), list) else []
    lines.extend(["", "## Open Issue Lock", ""])
    lines.append(f"- Blocking hard issues: `{blocking_issues}`")
    lines.append(f"- Non-blocking open issues: `{open_issues}`")
    lines.append(f"- Notes: `{notes}`")
    lines.extend(
        [
            "",
            "## Anchors",
            "",
            "| Source Segment | Pass | Family | Drift ms | Judge | Reasons |",
            "| ---: | --- | --- | ---: | ---: | --- |",
        ]
    )
    for row in suite.get("anchors") or []:
        reasons = ", ".join(str(item) for item in row.get("reject_reasons") or [])
        lines.append(
            f"| {row.get('source_segment_id')} | {row.get('pass')} | {row.get('runtime_selected_family') or ''} | "
            f"{row.get('drift_ms') if row.get('drift_ms') is not None else ''} | {row.get('judge_score') if row.get('judge_score') is not None else ''} | {reasons} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    suite = build_suite(args, root)
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    if args.freeze and suite.get("hard_issues"):
        suite["freeze_refused"] = not bool(suite.get("fixture_locked"))
    write_json(output_json, suite)
    write_markdown(output_md, suite)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "frozen": suite.get("frozen"),
                "fixture_locked": suite.get("fixture_locked"),
                "lock_status": suite.get("lock_status"),
                "hard_issues": suite.get("hard_issues"),
                "blocking_hard_issues": suite.get("blocking_hard_issues"),
                "open_issues": suite.get("open_issues"),
                "summary": suite.get("summary"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 2 if args.freeze and suite.get("hard_issues") and not suite.get("fixture_locked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
