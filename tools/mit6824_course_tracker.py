from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]
LECTURE_RE = re.compile(r"(?:lec|lecture)[-_ ]?0?(\d{1,2})", re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a MIT 6.824 dubbing production tracker from local source videos and HoloDub DB state."
    )
    parser.add_argument("--source-dir", default="data/source_videos/mit-6.824-2020")
    parser.add_argument("--lecture-start", type=int, default=8)
    parser.add_argument("--lecture-end", type=int, default=20)
    parser.add_argument("--output-json", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_course_tracker.md")
    parser.add_argument("--no-db", action="store_true", help="Skip Docker/Postgres episode lookup.")
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--compose-file", action="append", default=[])
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def rel_to_root(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def rel_to_data_root(path: Path, root: Path) -> str:
    data_root = root / "data"
    try:
        return str(path.resolve().relative_to(data_root.resolve())).replace("/", "\\")
    except ValueError:
        return rel_to_root(path, root)


def parse_lecture_number(*values: str | None) -> int | None:
    for value in values:
        if not value:
            continue
        match = LECTURE_RE.search(value)
        if match:
            return int(match.group(1))
    return None


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run(cmd: list[str], *, cwd: Path, timeout: int = 60) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
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


def load_probe_rows(source_dir: Path) -> dict[int, dict[str, Any]]:
    report_path = source_dir / "ffprobe-report.jsonl"
    rows: dict[int, dict[str, Any]] = {}
    if not report_path.exists():
        return rows
    for line in report_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        lecture = int(row.get("lecture") or 0)
        if lecture <= 0:
            continue
        probe = row.get("probe") or {}
        fmt = probe.get("format") or {}
        duration_sec = None
        if fmt.get("duration"):
            try:
                duration_sec = float(fmt["duration"])
            except (TypeError, ValueError):
                duration_sec = None
        rows[lecture] = {
            "lecture": lecture,
            "title": row.get("title") or "",
            "official_page": row.get("official_page") or "",
            "youtube_url": row.get("youtube_url") or "",
            "duration_sec": duration_sec,
            "probe_file": row.get("file") or "",
            "format_name": fmt.get("format_name") or "",
        }
    return rows


def scan_source_videos(source_dir: Path, root: Path) -> dict[int, dict[str, Any]]:
    probe_rows = load_probe_rows(source_dir)
    rows: dict[int, dict[str, Any]] = {}
    for path in sorted(source_dir.iterdir() if source_dir.exists() else []):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".mp4", ".mkv", ".mov", ".webm"}:
            continue
        lecture = parse_lecture_number(path.name)
        if lecture is None:
            continue
        probe = probe_rows.get(lecture, {})
        title = probe.get("title") or path.stem
        rows[lecture] = {
            "lecture": lecture,
            "title": title,
            "source_video_relpath": rel_to_data_root(path, root),
            "source_video_repo_relpath": rel_to_root(path, root),
            "source_video_abspath": str(path.resolve()),
            "source_file_present": True,
            "source_bytes": path.stat().st_size,
            "duration_sec": probe.get("duration_sec"),
            "official_page": probe.get("official_page") or "",
            "youtube_url": probe.get("youtube_url") or "",
            "format_name": probe.get("format_name") or "",
        }
    return rows


def load_episode_rows(args: argparse.Namespace, root: Path) -> list[dict[str, str]]:
    sql = r"""
select id,
       coalesce(name,'') as name,
       coalesce(status,'') as status,
       coalesce(source_video_rel_path,'') as source_video_rel_path,
       coalesce(output_rel_path,'') as output_rel_path,
       coalesce(completed_at::text,'') as completed_at,
       coalesce(created_at::text,'') as created_at,
       coalesce(updated_at::text,'') as updated_at,
       coalesce(total_chapters::text,'') as total_chapters
from episodes
order by id;
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
    output = run(cmd, cwd=root, timeout=60)
    lines = [line for line in output.splitlines() if line and not line.startswith("(")]
    if not lines:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [dict(row) for row in reader]


def group_episodes_by_lecture(episode_rows: list[dict[str, str]]) -> dict[int, list[dict[str, str]]]:
    grouped: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in episode_rows:
        lecture = parse_lecture_number(row.get("source_video_rel_path"), row.get("name"))
        if lecture is not None:
            grouped[lecture].append(row)
    return grouped


def episode_id(row: dict[str, Any]) -> int:
    try:
        return int(row.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def latest_episode(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not episodes:
        return None
    return sorted(episodes, key=episode_id, reverse=True)[0]


def classify_lecture(source: dict[str, Any] | None, episodes: list[dict[str, str]]) -> tuple[str, str]:
    if not source:
        return "missing_source", "add_source_video"
    latest = latest_episode(episodes)
    if latest and latest.get("status") in {"pending", "chaptering", "dispatched", "running", "merging", "judging", "reworking"}:
        return "in_progress", "monitor_or_finalize_episode"
    if latest and latest.get("status") == "completed" and latest.get("output_rel_path"):
        return "episode_completed", "finalize_acceptance_artifacts"
    if episodes:
        return "has_stale_attempts", "inspect_previous_attempts_then_restart"
    return "not_started", "create_episode"


def start_payload(lecture: int, source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not source or not source.get("source_video_relpath"):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(source.get("title") or "").lower()).strip("-")
    name = f"mit6824-lec{lecture:02d}" + (f"-{slug}" if slug else "") + "-prod-v1"
    return {
        "name": name,
        "input_relpath": source["source_video_relpath"].replace("\\", "/"),
        "source_language": "en",
        "target_language": "zh-CN",
        "auto_start": True,
        "max_retries": 0,
        "config": {
            "course": "mit6824-2020",
            "lecture": lecture,
            "production_profile": "mit6824-course-prod-v1",
            "lora_candidate_opt_in": True,
        },
    }


def source_preflight(source: dict[str, Any] | None) -> dict[str, Any]:
    source = source or {}
    errors: list[str] = []
    warnings: list[str] = []
    if not source.get("source_file_present"):
        errors.append("source_file_missing")
    source_bytes = source.get("source_bytes")
    try:
        if source_bytes is not None and int(source_bytes) <= 0:
            errors.append("source_file_empty")
    except (TypeError, ValueError):
        warnings.append("source_bytes_not_numeric")
    duration_sec = source.get("duration_sec")
    try:
        if duration_sec is None or float(duration_sec) <= 0:
            warnings.append("source_duration_missing_or_invalid")
    except (TypeError, ValueError):
        warnings.append("source_duration_not_numeric")
    return {
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
        "source_file_present": bool(source.get("source_file_present")),
        "source_bytes": source_bytes,
        "duration_sec": duration_sec,
        "source_video": source.get("source_video_repo_relpath") or source.get("source_video_relpath") or "",
    }


def source_preflight_summary(lectures: list[dict[str, Any]]) -> dict[str, Any]:
    blocked: list[int] = []
    warning_lectures: list[int] = []
    ready: list[int] = []
    by_error: dict[str, int] = {}
    by_warning: dict[str, int] = {}
    total_duration_sec = 0.0
    duration_count = 0
    for row in lectures:
        lecture = int(row.get("lecture") or 0)
        preflight = row.get("source_preflight") if isinstance(row.get("source_preflight"), dict) else {}
        if preflight.get("ready") is True:
            ready.append(lecture)
        else:
            blocked.append(lecture)
        for error in preflight.get("errors") or []:
            by_error[str(error)] = by_error.get(str(error), 0) + 1
        warnings = preflight.get("warnings") or []
        if warnings:
            warning_lectures.append(lecture)
        for warning in warnings:
            by_warning[str(warning)] = by_warning.get(str(warning), 0) + 1
        try:
            duration = float(preflight.get("duration_sec"))
        except (TypeError, ValueError):
            duration = 0.0
        if duration > 0:
            total_duration_sec += duration
            duration_count += 1
    return {
        "total": len(lectures),
        "ready": ready,
        "blocked": blocked,
        "warning_lectures": warning_lectures,
        "ready_count": len(ready),
        "blocked_count": len(blocked),
        "warning_count": len(warning_lectures),
        "by_error": by_error,
        "by_warning": by_warning,
        "known_duration_count": duration_count,
        "total_duration_sec": round(total_duration_sec, 3),
    }


def training_policy() -> dict[str, Any]:
    return {
        "default": "do_not_train_per_episode",
        "adapter_scope": "course_or_domain_profile",
        "single_issue_actions": {
            "misread_term_or_number": "spoken_text_glossary_or_normalization",
            "single_long_clause_truncation": "split_candidate_gate_or_rescue",
            "single_bad_join": "split_planner_or_join_quality_fix",
        },
        "train_when": [
            "same failure family repeats across multiple lectures",
            "current adapter fails canary gates",
            "current adapter fails full-episode shadow plus human review",
            "new human-approved aligned audio materially changes the dataset",
            "candidate_policy_split_planning_normalization_cannot_reach_target_behavior",
        ],
        "data_floor": "tens_of_minutes_human_approved_aligned_audio",
        "data_target": "1_to_3_hours_gold_audio_before_candidate_promotion",
        "stable_target": "2_to_5_hours_gold_audio_before_default_profile_consideration",
        "holdout_rule": "fixed_held_out_canary_and_episode_set_no_train_overlap",
    }


def build_tracker(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    source_dir = Path(args.source_dir)
    if not source_dir.is_absolute():
        source_dir = root / source_dir
    sources = scan_source_videos(source_dir, root)
    episode_rows = [] if args.no_db else load_episode_rows(args, root)
    episodes_by_lecture = group_episodes_by_lecture(episode_rows)
    lectures: list[dict[str, Any]] = []
    for lecture in range(args.lecture_start, args.lecture_end + 1):
        source = sources.get(lecture)
        episodes = episodes_by_lecture.get(lecture, [])
        status, next_action = classify_lecture(source, episodes)
        source_status = source_preflight(source)
        lectures.append(
            {
                "lecture": lecture,
                "title": (source or {}).get("title") or "",
                "status": status,
                "next_action": next_action,
                "source": source or {"lecture": lecture, "source_file_present": False},
                "source_preflight": source_status,
                "episodes": episodes,
                "start_payload": start_payload(lecture, source),
                "acceptance_artifacts_required": [
                    "final_video",
                    "final_audio",
                    "final_mix_asr_report",
                    "fit_audit",
                    "risk_queue",
                    "human_review_pack",
                    "adapter_profile_provenance",
                ],
            }
        )
    summary: dict[str, Any] = {
        "total": len(lectures),
        "by_status": {},
        "first_not_started": None,
        "ready_to_start": [],
    }
    for row in lectures:
        status = row["status"]
        summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
        if row["status"] == "not_started" and row["source_preflight"].get("ready"):
            summary["ready_to_start"].append(row["lecture"])
            if summary["first_not_started"] is None:
                summary["first_not_started"] = row["lecture"]
    summary["source_preflight"] = source_preflight_summary(lectures)
    return {
        "schema_version": "holodub.mit6824_course_tracker.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": rel_to_root(source_dir, root),
        "lecture_start": args.lecture_start,
        "lecture_end": args.lecture_end,
        "summary": summary,
        "training_policy": training_policy(),
        "lectures": lectures,
    }


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return ""
    total = int(round(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def write_markdown(path: Path, tracker: dict[str, Any]) -> None:
    lines = [
        "# MIT 6.824 Dub Production Tracker",
        "",
        f"Generated: `{tracker['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- Lecture range: `{tracker['lecture_start']}-{tracker['lecture_end']}`",
        f"- Status counts: `{json.dumps(tracker['summary']['by_status'], ensure_ascii=False, sort_keys=True)}`",
        f"- First ready lecture: `{tracker['summary']['first_not_started']}`",
        f"- Source preflight: `{json.dumps(tracker['summary'].get('source_preflight', {}), ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Queue",
        "",
        "| Lecture | Title | Duration | Source Health | Status | Next Action | Source | Episodes |",
        "| --- | --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in tracker["lectures"]:
        source = row.get("source") or {}
        preflight = row.get("source_preflight") if isinstance(row.get("source_preflight"), dict) else {}
        source_health = "ready" if preflight.get("ready") is True else "blocked"
        if preflight.get("warnings"):
            source_health += "+warnings"
        episodes = row.get("episodes") or []
        episode_summary = ", ".join(
            f"#{ep.get('id')} {ep.get('status')}" + (f" -> {ep.get('output_rel_path')}" if ep.get("output_rel_path") else "")
            for ep in episodes
        )
        lines.append(
            "| {lecture} | {title} | {duration} | {source_health} | {status} | {next_action} | {source_path} | {episodes} |".format(
                lecture=row["lecture"],
                title=(row.get("title") or "").replace("|", "\\|"),
                duration=fmt_duration(source.get("duration_sec")),
                source_health=source_health,
                status=row["status"],
                next_action=row["next_action"],
                source_path=(source.get("source_video_relpath") or "").replace("|", "\\|"),
                episodes=episode_summary.replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Training Decision Rule",
            "",
            "- Do not train one adapter per lecture.",
            "- Fix isolated pronunciation, numeric, and formatting issues in spoken-text rules, glossary, or normalization.",
            "- Fix isolated long-clause truncation through split planning, candidate gates, and rescue policy first.",
            "- Consider training only when the same failure family repeats across multiple lectures or the current adapter fails canary/full-episode gates.",
            "- Promote only course/domain-scoped adapters with fixed held-out validation and no train/holdout overlap.",
            "",
            "## Acceptance Artifacts Per Lecture",
            "",
            "- final video",
            "- final audio",
            "- final-mix ASR report",
            "- risk queue",
            "- human review pack",
            "- adapter/profile provenance",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    tracker = build_tracker(args, root)
    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    if not output_json.is_absolute():
        output_json = root / output_json
    if not output_md.is_absolute():
        output_md = root / output_md
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(tracker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(output_md, tracker)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "summary": tracker["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
