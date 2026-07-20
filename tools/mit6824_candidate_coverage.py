from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit LoRA candidate runtime coverage for MIT 6.824 production or regression jobs."
    )
    parser.add_argument("--job-ids", default="342,343,344,345,346,347")
    parser.add_argument("--output-json", default="data/validation/mit6824_candidate_coverage.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_candidate_coverage.md")
    parser.add_argument("--min-target-sec", type=float, default=18.0)
    parser.add_argument("--min-text-chars", type=int, default=180)
    parser.add_argument("--dense-min-target-sec", type=float, default=9.0)
    parser.add_argument("--dense-min-chars-per-sec", type=float, default=4.9)
    parser.add_argument("--near-long-min-target-sec", type=float, default=16.0)
    parser.add_argument("--near-long-min-text-chars", type=int, default=70)
    parser.add_argument("--short-dense-min-target-sec", type=float, default=6.0)
    parser.add_argument("--short-dense-max-target-sec", type=float, default=9.0)
    parser.add_argument("--short-dense-min-chars-per-sec", type=float, default=5.5)
    parser.add_argument("--short-transition-min-target-sec", type=float, default=5.0)
    parser.add_argument("--short-transition-max-target-sec", type=float, default=10.0)
    parser.add_argument("--short-transition-min-chars-per-sec", type=float, default=5.0)
    parser.add_argument("--short-term-number-min-target-sec", type=float, default=6.0)
    parser.add_argument("--short-term-number-max-target-sec", type=float, default=9.0)
    parser.add_argument("--short-term-number-min-chars-per-sec", type=float, default=4.8)
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when eligible synthesized rows lack candidate runtime.")
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


def split_int_csv(raw: str) -> list[int]:
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


def count_non_ws(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def looks_like_short_transition_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    q_positions = [pos for pos in (compact.find("？"), compact.find("?")) if pos >= 0]
    if not q_positions:
        return False
    question_pos = min(q_positions)
    tail_chars = len(compact) - question_pos - 1
    return 0 <= tail_chars <= 16


TERM_NUMBER_PATTERN = re.compile(r"(\b\d+\b|[一二三四五六七八九十]+号|任期|term|index|raft)", re.I)


def looks_like_short_term_number(text: str) -> bool:
    return bool(TERM_NUMBER_PATTERN.search(text))


def eligibility(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    text = str(row.get("target_text") or "")
    chars = count_non_ws(text)
    target_sec = float(row.get("original_duration_ms") or 0) / 1000.0
    chars_per_sec = float(chars) / target_sec if target_sec > 0 else 0.0
    duration_eligible = args.min_target_sec <= 0 or target_sec >= args.min_target_sec
    text_eligible = args.min_text_chars <= 0 or chars >= args.min_text_chars
    dense_eligible = (
        args.dense_min_target_sec > 0
        and args.dense_min_chars_per_sec > 0
        and target_sec >= args.dense_min_target_sec
        and chars_per_sec >= args.dense_min_chars_per_sec
    )
    near_long_eligible = (
        args.near_long_min_target_sec > 0
        and args.near_long_min_text_chars > 0
        and target_sec >= args.near_long_min_target_sec
        and chars >= args.near_long_min_text_chars
    )
    short_dense_eligible = (
        args.short_dense_min_target_sec > 0
        and args.short_dense_max_target_sec > 0
        and args.short_dense_min_chars_per_sec > 0
        and target_sec >= args.short_dense_min_target_sec
        and target_sec <= args.short_dense_max_target_sec
        and chars_per_sec >= args.short_dense_min_chars_per_sec
    )
    short_transition_eligible = (
        args.short_transition_min_target_sec > 0
        and args.short_transition_max_target_sec > 0
        and args.short_transition_min_chars_per_sec > 0
        and target_sec >= args.short_transition_min_target_sec
        and target_sec <= args.short_transition_max_target_sec
        and chars_per_sec >= args.short_transition_min_chars_per_sec
        and looks_like_short_transition_question(text)
    )
    short_term_number_eligible = (
        args.short_term_number_min_target_sec > 0
        and args.short_term_number_max_target_sec > 0
        and args.short_term_number_min_chars_per_sec > 0
        and target_sec >= args.short_term_number_min_target_sec
        and target_sec <= args.short_term_number_max_target_sec
        and chars_per_sec >= args.short_term_number_min_chars_per_sec
        and looks_like_short_term_number(text)
    )
    reasons: list[str] = []
    if duration_eligible:
        reasons.append("target_sec_gte_min")
    if text_eligible:
        reasons.append("text_chars_gte_min")
    if dense_eligible:
        reasons.append("dense_mid_segment")
    if near_long_eligible:
        reasons.append("near_long_dense")
    if short_dense_eligible:
        reasons.append("short_dense_segment")
    if short_transition_eligible:
        reasons.append("short_transition_question")
    if short_term_number_eligible:
        reasons.append("short_term_number_anchor")
    return {
        "eligible": bool(reasons),
        "reasons": reasons or ["below_candidate_threshold"],
        "target_sec": target_sec,
        "target_chars": chars,
        "chars_per_sec": round(chars_per_sec, 4),
    }


def row_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() == "true"


def annotate_row(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    info = eligibility(row, args)
    status = str(row.get("status") or "")
    runtime_requested = row_bool(row.get("runtime_execution_requested"))
    audio_mode = str(row.get("runtime_audio_selection_mode") or "")
    fallback_reason = str(row.get("runtime_fallback_reason") or "")
    issue = ""
    if info["eligible"] and status == "synthesized":
        if fallback_reason:
            issue = f"eligible_candidate_fallback:{fallback_reason}"
        elif not runtime_requested and not audio_mode:
            issue = "eligible_synthesized_missing_candidate_runtime"
        elif audio_mode != "candidate_audio":
            issue = f"eligible_synthesized_not_candidate_audio:{audio_mode or 'missing'}"
    elif info["eligible"] and status != "synthesized":
        if fallback_reason:
            issue = f"eligible_pending_fallback:{fallback_reason}"
        else:
            issue = f"eligible_pending:{status}"
    annotated = {
        "job_id": row.get("job_id"),
        "segment_id": row.get("segment_id"),
        "ordinal": row.get("ordinal"),
        "status": status,
        "original_duration_ms": row.get("original_duration_ms"),
        "tts_duration_ms": row.get("tts_duration_ms"),
        "target_sec": info["target_sec"],
        "target_chars": info["target_chars"],
        "chars_per_sec": info["chars_per_sec"],
        "eligible": info["eligible"],
        "eligibility_reasons": info["reasons"],
        "runtime_execution_requested": runtime_requested,
        "runtime_audio_selection_mode": audio_mode,
        "runtime_selected_family": row.get("runtime_selected_family") or "",
        "runtime_fallback_reason": fallback_reason,
        "issue": issue,
    }
    return annotated


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    synthesized = [row for row in rows if row["status"] == "synthesized"]
    eligible = [row for row in rows if row["eligible"]]
    eligible_synthesized = [row for row in eligible if row["status"] == "synthesized"]
    missing_runtime = [
        row for row in eligible_synthesized if row["issue"].startswith("eligible_synthesized_missing_candidate_runtime")
    ]
    not_candidate_audio = [
        row for row in eligible_synthesized if row["issue"].startswith("eligible_synthesized_not_candidate_audio")
    ]
    candidate_fallback = [
        row for row in eligible_synthesized if row["issue"].startswith("eligible_candidate_fallback")
    ]
    hard_rows = missing_runtime + not_candidate_audio + candidate_fallback
    pending = [row for row in eligible if row["status"] != "synthesized"]
    pending_fallback = [row for row in pending if row["runtime_fallback_reason"]]
    selected_counts = Counter(row["runtime_selected_family"] or "none" for row in synthesized)
    return {
        "total_segments": len(rows),
        "synthesized_segments": len(synthesized),
        "eligible_segments": len(eligible),
        "eligible_synthesized_segments": len(eligible_synthesized),
        "eligible_candidate_audio_segments": len(
            [
                row
                for row in eligible_synthesized
                if row["runtime_execution_requested"] and row["runtime_audio_selection_mode"] == "candidate_audio" and not row["runtime_fallback_reason"]
            ]
        ),
        "eligible_missing_candidate_runtime": len(missing_runtime),
        "eligible_not_candidate_audio_segments": len(not_candidate_audio),
        "eligible_candidate_fallback_segments": len(candidate_fallback),
        "eligible_pending_segments": len(pending),
        "eligible_pending_fallback_segments": len(pending_fallback),
        "fallback_segments": len([row for row in rows if row["runtime_fallback_reason"]]),
        "selected_family_counts": dict(sorted(selected_counts.items())),
        "hard_issue_count": len(hard_rows),
    }


def build_report(raw_rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    rows = [annotate_row(row, args) for row in raw_rows]
    hard_rows = [row for row in rows if row["status"] == "synthesized" and row["issue"]]
    fallback_pending_rows = [row for row in rows if row["issue"].startswith("eligible_pending_fallback")]
    return {
        "schema_version": "holodub.mit6824_candidate_coverage.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "job_ids": split_int_csv(args.job_ids),
        "policy": {
            "min_target_sec": args.min_target_sec,
            "min_text_chars": args.min_text_chars,
            "dense_min_target_sec": args.dense_min_target_sec,
            "dense_min_chars_per_sec": args.dense_min_chars_per_sec,
            "near_long_min_target_sec": args.near_long_min_target_sec,
            "near_long_min_text_chars": args.near_long_min_text_chars,
            "short_dense_min_target_sec": args.short_dense_min_target_sec,
            "short_dense_max_target_sec": args.short_dense_max_target_sec,
            "short_dense_min_chars_per_sec": args.short_dense_min_chars_per_sec,
            "short_transition_min_target_sec": args.short_transition_min_target_sec,
            "short_transition_max_target_sec": args.short_transition_max_target_sec,
            "short_transition_min_chars_per_sec": args.short_transition_min_chars_per_sec,
            "short_term_number_min_target_sec": args.short_term_number_min_target_sec,
            "short_term_number_max_target_sec": args.short_term_number_max_target_sec,
            "short_term_number_min_chars_per_sec": args.short_term_number_min_chars_per_sec,
        },
        "summary": summarize(rows),
        "hard_rows": hard_rows,
        "fallback_pending_rows": fallback_pending_rows,
        "pending_eligible_rows": [row for row in rows if row["issue"].startswith("eligible_pending")],
        "rows": rows,
    }


def query_rows(args: argparse.Namespace, root: Path) -> list[dict[str, Any]]:
    if args.no_db:
        return []
    ids = split_int_csv(args.job_ids)
    if not ids:
        return []
    id_csv = ",".join(str(item) for item in ids)
    sql = f"""
select jsonb_build_object(
  'job_id', s.job_id,
  'segment_id', s.id,
  'ordinal', s.ordinal,
  'status', coalesce(s.status,''),
  'original_duration_ms', coalesce(s.original_duration_ms,0),
  'tts_duration_ms', coalesce(s.tts_duration_ms,0),
  'target_text', coalesce(s.target_text,''),
  'runtime_execution_requested', coalesce(s.meta->'tts_lora_candidate_runtime'->>'execution_requested',''),
  'runtime_audio_selection_mode', coalesce(s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode',''),
  'runtime_selected_family', coalesce(s.meta->'tts_lora_candidate_runtime'->>'selected_family',''),
  'runtime_fallback_reason', coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','')
)::text
from segments s
where s.job_id in ({id_csv})
order by s.job_id, s.ordinal;
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
            "-q",
            "-t",
            "-A",
            "-c",
            sql,
        ],
    )
    proc = run(cmd, cwd=root, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout)[-2000:])
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report["summary"]
    lines = [
        "# MIT 6.824 Candidate Coverage",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Jobs: `{report['job_ids']}`",
        f"- Synthesized: `{summary['synthesized_segments']}/{summary['total_segments']}`",
        f"- Eligible synthesized: `{summary['eligible_synthesized_segments']}`",
        f"- Eligible candidate audio: `{summary['eligible_candidate_audio_segments']}`",
        f"- Eligible missing candidate runtime: `{summary['eligible_missing_candidate_runtime']}`",
        f"- Eligible not candidate audio: `{summary['eligible_not_candidate_audio_segments']}`",
        f"- Eligible candidate fallback: `{summary['eligible_candidate_fallback_segments']}`",
        f"- Eligible pending: `{summary['eligible_pending_segments']}`",
        f"- Eligible pending fallback risk: `{summary['eligible_pending_fallback_segments']}`",
        f"- Fallback segments: `{summary['fallback_segments']}`",
        f"- Selected families: `{json.dumps(summary['selected_family_counts'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Hard Rows",
        "",
        "| Job | Segment | Ordinal | Status | Target sec | Chars | Issue |",
        "| ---: | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    for row in report.get("hard_rows") or []:
        lines.append(
            f"| {row['job_id']} | {row['segment_id']} | {row['ordinal']} | {row['status']} | "
            f"{row['target_sec']:.3f} | {row['target_chars']} | {row['issue']} |"
        )
    lines.extend(
        [
            "",
            "## Pending Fallback Risk Rows",
            "",
            "| Job | Segment | Ordinal | Status | Target sec | Chars | Issue |",
            "| ---: | ---: | ---: | --- | ---: | ---: | --- |",
        ]
    )
    for row in report.get("fallback_pending_rows") or []:
        lines.append(
            f"| {row['job_id']} | {row['segment_id']} | {row['ordinal']} | {row['status']} | "
            f"{row['target_sec']:.3f} | {row['target_chars']} | {row['issue']} |"
        )
    lines.extend(
        [
            "",
            "## Pending Eligible Rows",
            "",
            "| Job | Segment | Ordinal | Status | Target sec | Chars | Reasons |",
            "| ---: | ---: | ---: | --- | ---: | ---: | --- |",
        ]
    )
    for row in report.get("pending_eligible_rows") or []:
        lines.append(
            f"| {row['job_id']} | {row['segment_id']} | {row['ordinal']} | {row['status']} | "
            f"{row['target_sec']:.3f} | {row['target_chars']} | {','.join(row['eligibility_reasons'])} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    rows = query_rows(args, root)
    report = build_report(rows, args)
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
    if args.strict and report["summary"]["hard_issue_count"] > 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
