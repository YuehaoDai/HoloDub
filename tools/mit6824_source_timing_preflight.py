from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mit6824_finalize_lecture import (
    DEFAULT_COMPOSE_FILES,
    compact_source_timing_plan_risk_row,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight sparse/fragmented source timing risks for a MIT 6.824 episode."
    )
    parser.add_argument("--lecture", type=int, default=0, help="Lecture number to inspect; defaults to in-progress.")
    parser.add_argument("--episode-id", type=int, default=0, help="Explicit episode id; overrides --lecture.")
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--output-json", default="data/validation/mit6824_source_timing_preflight.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_source_timing_preflight.md")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument(
        "--status-filter",
        default="translated,synthesized",
        help="Comma-separated segment statuses to inspect.",
    )
    parser.add_argument("--print-full", action="store_true", help="Print the full report instead of a compact summary.")
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


def write_markdown(path: Path, payload: dict[str, Any], root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    episode = payload.get("episode") if isinstance(payload.get("episode"), dict) else {}
    lines = [
        "# MIT 6.824 Source Timing Preflight",
        "",
        f"- generated_at: `{payload.get('generated_at', '')}`",
        f"- lecture: `{episode.get('lecture', '')}`",
        f"- episode_id: `{episode.get('id', '')}`",
        f"- status: `{payload.get('status', '')}`",
        f"- analyzed_segments: `{summary.get('analyzed_segments', 0)}`",
        f"- risk_count: `{summary.get('risk_count', 0)}`",
        f"- high_priority_count: `{summary.get('high_priority_count', 0)}`",
        "",
    ]
    by_reason = summary.get("by_reason") if isinstance(summary.get("by_reason"), dict) else {}
    if by_reason:
        lines.extend(["## Reasons", ""])
        for reason, count in by_reason.items():
            lines.append(f"- `{reason}`: {count}")
        lines.append("")
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    if entries:
        lines.extend(["## Top Risks", ""])
        lines.append("| priority | label | reasons | target | tts | source density | target density |")
        lines.append("|---:|---|---|---:|---:|---:|---:|")
        for row in entries[:30]:
            metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
            reasons = ", ".join(str(reason) for reason in row.get("reasons", []))
            lines.append(
                "| {priority} | `{label}` | {reasons} | {target_ms} | {tts_duration_ms} | {source_density} | {target_density} |".format(
                    priority=row.get("priority", ""),
                    label=row.get("label", ""),
                    reasons=reasons,
                    target_ms=row.get("target_ms", ""),
                    tts_duration_ms=row.get("tts_duration_ms", ""),
                    source_density=metrics.get("source_units_per_sec", ""),
                    target_density=metrics.get("target_units_per_sec", ""),
                )
            )
        lines.append("")
    json_path = payload.get("output_json")
    if json_path:
        lines.append(f"JSON: `{json_path}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_episode(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    valid = [row for row in episodes if isinstance(row, dict) and row.get("id")]
    if not valid:
        return None
    return sorted(valid, key=lambda row: int(row.get("id") or 0), reverse=True)[0]


def select_episode(tracker: dict[str, Any], lecture: int, episode_id: int) -> dict[str, Any]:
    if episode_id > 0:
        return {"id": episode_id, "lecture": lecture or None, "selection": "explicit_episode_id"}
    lectures = tracker.get("lectures") if isinstance(tracker.get("lectures"), list) else []
    if lecture > 0:
        for row in lectures:
            if int(row.get("lecture") or 0) != lecture:
                continue
            episode = latest_episode(row.get("episodes") or [])
            if episode:
                return {
                    "id": int(episode.get("id") or 0),
                    "lecture": lecture,
                    "title": row.get("title", ""),
                    "selection": "latest_for_lecture",
                }
    for row in lectures:
        if row.get("status") != "in_progress":
            continue
        episode = latest_episode(row.get("episodes") or [])
        if episode:
            return {
                "id": int(episode.get("id") or 0),
                "lecture": int(row.get("lecture") or 0),
                "title": row.get("title", ""),
                "selection": "in_progress_lecture",
            }
    return {"id": 0, "lecture": lecture or None, "selection": "none"}


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
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


def sql_list(values: list[str]) -> str:
    cleaned = [value.strip().replace("'", "''") for value in values if value.strip()]
    if not cleaned:
        cleaned = ["translated", "synthesized"]
    return ", ".join(f"'{value}'" for value in cleaned)


def priority_value(row: dict[str, Any], default: int = 99) -> int:
    raw = row.get("priority")
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def query_episode_rows(args: argparse.Namespace, root: Path, episode_id: int) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    if episode_id <= 0:
        return [], {"reason": "missing_episode_id"}
    statuses = sql_list(str(args.status_filter or "").split(","))
    sql = f"""
select j.chapter_ordinal::text as chapter_ordinal,
       s.ordinal::text as segment_ordinal,
       s.id::text as segment_id,
       concat('c', lpad(j.chapter_ordinal::text, 2, '0'), '_s', lpad(s.ordinal::text, 4, '0'), '_seg', s.id::text) as label,
       coalesce(s.status,'') as status,
       coalesce(s.original_duration_ms::text,'') as original_duration_ms,
       coalesce(s.tts_duration_ms::text,'') as tts_duration_ms,
       coalesce(s.tts_audio_rel_path,'') as tts_audio_relpath,
       replace(replace(left(coalesce(s.source_text,''), 1200), E'\\t', ' '), E'\\n', ' ') as source_text,
       replace(replace(left(coalesce(s.target_text,''), 1200), E'\\t', ' '), E'\\n', ' ') as target_text
from jobs j
join segments s on s.job_id = j.id
where j.episode_id = {int(episode_id)}
  and coalesce(s.status,'') in ({statuses})
order by j.chapter_ordinal, s.ordinal, s.id;
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
            "-P",
            "footer=off",
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
    )
    proc = run(cmd, cwd=root, timeout=60)
    if proc.returncode != 0:
        return [], {"reason": "postgres_query_failed", "command": cmd, "stdout_tail": proc.stdout[-3000:]}
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return [], None
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(row) for row in reader], None


def build_report(
    *,
    episode: dict[str, Any],
    rows: list[dict[str, str]],
    query_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    status_counts = Counter()
    reason_counts = Counter()
    for row in rows:
        status_counts[str(row.get("status") or "unknown")] += 1
        risk = compact_source_timing_plan_risk_row(row)
        if not risk:
            continue
        risk["status"] = row.get("status", "")
        entries.append(risk)
        for reason in risk.get("reasons") or []:
            reason_counts[str(reason).split(":", 1)[0]] += 1
    entries.sort(
        key=lambda row: (
            priority_value(row),
            int(row.get("chapter_ordinal") or 0),
            int(row.get("segment_ordinal") or 0),
            int(row.get("segment_id") or 0),
        )
    )
    high_priority = [row for row in entries if priority_value(row) <= 1]
    status = "available"
    if query_error:
        status = "unavailable"
    return {
        "schema_version": "holodub.mit6824.source_timing_preflight.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "episode": episode,
        "summary": {
            "analyzed_segments": len(rows),
            "risk_count": len(entries),
            "high_priority_count": len(high_priority),
            "by_status": dict(sorted(status_counts.items())),
            "by_reason": dict(sorted(reason_counts.items())),
        },
        "entries": entries,
        "query_error": query_error,
    }


def build_skipped_report(episode: dict[str, Any], reason: str) -> dict[str, Any]:
    payload = build_report(episode=episode, rows=[], query_error=None)
    payload["status"] = "skipped"
    payload["skip_reason"] = reason
    return payload


def main() -> int:
    args = parse_args()
    root = repo_root()
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths are required")
    tracker = load_json(resolve(root, args.tracker))
    episode = select_episode(tracker, args.lecture, args.episode_id)
    if int(episode.get("id") or 0) <= 0:
        payload = build_skipped_report(episode, "no_active_or_selected_episode")
        query_error = None
    else:
        rows, query_error = query_episode_rows(args, root, int(episode.get("id") or 0))
        payload = build_report(episode=episode, rows=rows, query_error=query_error)
    payload["output_json"] = rel_to_root(output_json, root)
    payload["output_md"] = rel_to_root(output_md, root)
    write_json(output_json, payload)
    write_markdown(output_md, payload, root)
    if args.print_full:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            json.dumps(
                {
                    "status": payload.get("status"),
                    "episode": payload.get("episode"),
                    "summary": payload.get("summary"),
                    "output_json": payload.get("output_json"),
                    "output_md": payload.get("output_md"),
                    "query_error": payload.get("query_error"),
                    "top_risk_labels": [row.get("label") for row in payload.get("entries", [])[:10]],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 1 if query_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
