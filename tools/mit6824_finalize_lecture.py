from __future__ import annotations

import argparse
import csv
import html
import json
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]
SOURCE_TIMING_LARGE_TTS_DRIFT_MS = 500
ACCEPTANCE_ACTIONS_START = "<!-- mit6824-acceptance-actions:start -->"
ACCEPTANCE_ACTIONS_END = "<!-- mit6824-acceptance-actions:end -->"
LATIN_WORD_RE = re.compile(r"[A-Za-z]+(?:['-][A-Za-z]+)?")
CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff]")
SOURCE_FRAGMENT_SPLIT_RE = re.compile(r"[.!?。！？]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Finalize one MIT 6.824 lecture into the acceptance artifact layout."
    )
    parser.add_argument("--lecture", type=int, required=True)
    parser.add_argument("--episode-id", type=int, default=0, help="Override the tracker episode selection.")
    parser.add_argument("--tracker", default="data/validation/mit6824_course_tracker.json")
    parser.add_argument("--tracker-md", default="data/validation/mit6824_course_tracker.md")
    parser.add_argument("--lecture-start", type=int, default=8)
    parser.add_argument("--lecture-end", type=int, default=20)
    parser.add_argument("--acceptance-root", default="data/validation/mit6824_acceptance")
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--execute", action="store_true", help="Run ASR/review generation. Default is dry-run.")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-sec", type=float, default=300.0)
    parser.add_argument("--timeout-sec", type=float, default=0.0)
    parser.add_argument("--asr-timeout-sec", type=int, default=10800)
    parser.add_argument("--skip-asr", action="store_true")
    parser.add_argument("--skip-review-pack", action="store_true")
    parser.add_argument("--skip-clips", action="store_true")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--refresh-tracker", action="store_true")
    parser.add_argument("--skip-status-refresh-after-finalize", action="store_true")
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


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    files = args.compose_file or DEFAULT_COMPOSE_FILES
    cmd = ["docker", "compose"]
    for file in files:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json_if_exists(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def load_tracker(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_tracker(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [
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
    ]
    proc = run(cmd, cwd=root, timeout=120)
    return command_result(cmd, proc)


def refresh_course_status(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    cmd = [args.python, "tools/mit6824_status_refresh.py"]
    proc = run(cmd, cwd=root, timeout=240)
    return command_result(cmd, proc)


def find_lecture(tracker: dict[str, Any], lecture: int) -> dict[str, Any]:
    for row in tracker.get("lectures") or []:
        if int(row.get("lecture") or 0) == lecture:
            return row
    raise ValueError(f"lecture {lecture} not found in tracker")


def latest_episode(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not episodes:
        return None
    return sorted(episodes, key=lambda row: int(row.get("id") or 0), reverse=True)[0]


def selected_episode(row: dict[str, Any], override_episode_id: int) -> dict[str, Any] | None:
    episodes = row.get("episodes") or []
    if override_episode_id:
        for episode in episodes:
            if int(episode.get("id") or 0) == override_episode_id:
                return episode
        return {"id": override_episode_id, "status": "unknown", "output_rel_path": ""}
    completed = [episode for episode in episodes if episode.get("status") == "completed" and episode.get("output_rel_path")]
    return latest_episode(completed) or latest_episode(episodes)


def command_result(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    parsed: dict[str, Any] | None = None
    raw = proc.stdout.strip()
    if raw:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                decoded = json.loads(raw[start : end + 1])
                if isinstance(decoded, dict):
                    parsed = decoded
            except json.JSONDecodeError:
                parsed = None
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "json": parsed,
        "stdout_tail": proc.stdout[-4000:],
    }


def parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"t", "true", "1", "yes", "y"}


def priority_value(row: dict[str, Any], default: int = 9) -> int:
    raw = row.get("priority")
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def counter_map(rows: list[dict[str, str]], key: str, *, empty_label: str = "none") -> dict[str, int]:
    counts = Counter(str(row.get(key) or empty_label) for row in rows)
    return dict(sorted(counts.items()))


def text_unit_count(text: str) -> int:
    return len(CJK_CHAR_RE.findall(text)) + len(LATIN_WORD_RE.findall(text))


def source_fragments(text: str) -> list[str]:
    fragments = []
    for raw in SOURCE_FRAGMENT_SPLIT_RE.split(text):
        fragment = " ".join(raw.strip().split())
        if fragment:
            fragments.append(fragment)
    return fragments


def compact_fragment_key(fragment: str) -> str:
    words = LATIN_WORD_RE.findall(fragment.lower())
    if words:
        return " ".join(words[:8])
    return "".join(CJK_CHAR_RE.findall(fragment))[:16]


def source_timing_plan_flags(
    *,
    source_text: str,
    target_text: str,
    target_ms: int,
    tts_ms: int,
) -> tuple[list[str], dict[str, Any]]:
    duration_sec = max(0.001, target_ms / 1000.0)
    source_units = text_unit_count(source_text)
    target_units = text_unit_count(target_text)
    source_units_per_sec = source_units / duration_sec
    target_units_per_sec = target_units / duration_sec
    fragments = source_fragments(source_text)
    short_fragments = [item for item in fragments if text_unit_count(item) <= 3]
    fragment_keys = [compact_fragment_key(item) for item in fragments]
    repeated = Counter(key for key in fragment_keys if key)
    max_repeat = max(repeated.values(), default=0)

    flags: list[str] = []
    if target_ms >= 12_000 and source_units_per_sec < 1.4:
        flags.append(f"low_source_density:{source_units_per_sec:.2f}_units_per_sec")
    if target_ms >= 18_000 and target_units_per_sec < 3.8:
        flags.append(f"low_target_density:{target_units_per_sec:.2f}_units_per_sec")
    if target_ms >= 8_000 and len(short_fragments) >= 4 and len(short_fragments) >= max(4, len(fragments) // 2):
        flags.append("fragmented_source_asr")
    if target_ms >= 8_000 and max_repeat >= 3:
        flags.append(f"repeated_source_fragment:{max_repeat}")
    if (
        target_ms
        and tts_ms
        and target_ms >= 3_000
        and abs(tts_ms - target_ms) > SOURCE_TIMING_LARGE_TTS_DRIFT_MS
    ):
        flags.append(f"large_tts_drift:{tts_ms - target_ms}")

    metrics = {
        "source_units": source_units,
        "target_units": target_units,
        "source_units_per_sec": round(source_units_per_sec, 3),
        "target_units_per_sec": round(target_units_per_sec, 3),
        "source_fragment_count": len(fragments),
        "short_source_fragment_count": len(short_fragments),
        "max_repeated_source_fragment": max_repeat,
    }
    return flags, metrics


def summarize_lora_provenance_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    runtime_rows = [row for row in rows if parse_bool(row.get("runtime_present"))]
    candidate_audio_rows = [row for row in runtime_rows if row.get("audio_selection_mode") == "candidate_audio"]
    fallback_rows = [row for row in runtime_rows if row.get("fallback_reason")]
    plan_rows = [row for row in rows if parse_bool(row.get("plan_present"))]
    adapter_rows = [
        row
        for row in runtime_rows
        if row.get("adapter_path") or row.get("adapter_data_relpath") or row.get("adapter_step")
    ]
    return {
        "schema_version": "holodub.mit6824.lora_runtime_provenance.v1",
        "status": "available",
        "summary": {
            "total_segments": len(rows),
            "synthesized_segments": len([row for row in rows if row.get("status") == "synthesized"]),
            "lora_plan_segments": len(plan_rows),
            "candidate_runtime_segments": len(runtime_rows),
            "candidate_audio_segments": len(candidate_audio_rows),
            "fallback_segments": len(fallback_rows),
            "selected_family_counts": counter_map(runtime_rows, "selected_family"),
            "audio_selection_mode_counts": counter_map(runtime_rows, "audio_selection_mode"),
            "fallback_reason_counts": counter_map(fallback_rows, "fallback_reason") if fallback_rows else {},
            "adapter_data_relpath_counts": counter_map(adapter_rows, "adapter_data_relpath") if adapter_rows else {},
            "adapter_step_counts": counter_map(adapter_rows, "adapter_step") if adapter_rows else {},
        },
    }


def load_episode_lora_provenance(args: argparse.Namespace, root: Path, episode_id: int) -> dict[str, Any]:
    if episode_id <= 0:
        return {
            "schema_version": "holodub.mit6824.lora_runtime_provenance.v1",
            "status": "unavailable",
            "reason": "missing_episode_id",
        }
    sql = f"""
select coalesce(s.status,'') as status,
       case when s.meta ? 'tts_lora_candidate' then 'true' else 'false' end as plan_present,
       coalesce(s.meta->'tts_lora_candidate'->>'enabled','') as plan_enabled,
       case when s.meta ? 'tts_lora_candidate_runtime' then 'true' else 'false' end as runtime_present,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode','') as audio_selection_mode,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'selected_family','') as selected_family,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') as fallback_reason,
       coalesce(s.meta->'tts_lora_candidate_runtime'->'adapter'->>'path','') as adapter_path,
       coalesce(s.meta->'tts_lora_candidate_runtime'->'adapter'->>'data_relpath','') as adapter_data_relpath,
       coalesce((s.meta->'tts_lora_candidate_runtime'->'adapter'->'metadata'->>'step'),'') as adapter_step
from jobs j
join segments s on s.job_id = j.id
where j.episode_id = {int(episode_id)}
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
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
    )
    proc = run(cmd, cwd=root, timeout=60)
    if proc.returncode != 0:
        return {
            "schema_version": "holodub.mit6824.lora_runtime_provenance.v1",
            "status": "unavailable",
            "reason": "postgres_query_failed",
            "command": cmd,
            "stdout_tail": proc.stdout[-4000:],
        }
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if not lines:
        return summarize_lora_provenance_rows([])
    reader = csv.DictReader(lines, delimiter="\t")
    return summarize_lora_provenance_rows([dict(row) for row in reader])


def compact_lora_runtime_risk_row(row: dict[str, str]) -> dict[str, Any]:
    reasons: list[str] = []
    fallback = str(row.get("fallback_reason") or "")
    audio_mode = str(row.get("audio_selection_mode") or "")
    gate_status = str(row.get("gate_status") or "")
    gate_reason = str(row.get("gate_reason") or "")
    candidate_generation_stop_reason = str(row.get("candidate_generation_stop_reason") or "")
    if fallback:
        reasons.append(f"candidate_fallback:{fallback}")
    if audio_mode == "default_audio":
        reasons.append("candidate_default_audio")
    if gate_status and gate_status != "selected":
        reasons.append(f"candidate_gate:{gate_status}")
    if gate_reason:
        reasons.append(f"candidate_gate_reason:{gate_reason}")
    if candidate_generation_stop_reason:
        reasons.append(f"candidate_stop:{candidate_generation_stop_reason}")
    try:
        target_ms = int(row.get("original_duration_ms") or 0)
        tts_ms = int(row.get("tts_duration_ms") or 0)
    except (TypeError, ValueError):
        target_ms = tts_ms = 0
    try:
        candidate_attempt_count = int(row.get("candidate_attempt_count") or 0)
    except (TypeError, ValueError):
        candidate_attempt_count = 0
    drift_ms = tts_ms - target_ms if target_ms and tts_ms else None
    return {
        "source": "lora_candidate_runtime",
        "priority": 1 if fallback else 2,
        "segment_id": int(row.get("segment_id") or 0),
        "label": str(row.get("label") or ""),
        "chapter_ordinal": int(row.get("chapter_ordinal") or 0),
        "segment_ordinal": int(row.get("segment_ordinal") or 0),
        "selected_candidate": str(row.get("selected_family") or ""),
        "audio_selection_mode": audio_mode,
        "fallback_reason": fallback,
        "candidate_attempt_count": candidate_attempt_count,
        "gate_status": gate_status,
        "gate_reason": gate_reason,
        "gate_reject_reasons": str(row.get("gate_reject_reasons") or ""),
        "candidate_generation_stop_reason": candidate_generation_stop_reason,
        "reasons": reasons,
        "target_ms": target_ms,
        "tts_duration_ms": tts_ms,
        "drift_ms": drift_ms,
        "expected_text": str(row.get("target_text_preview") or ""),
        "tts_audio_relpath": str(row.get("tts_audio_relpath") or ""),
    }


def load_episode_lora_runtime_risks(args: argparse.Namespace, root: Path, episode_id: int) -> list[dict[str, Any]]:
    if episode_id <= 0:
        return []
    sql = f"""
select j.chapter_ordinal::text as chapter_ordinal,
       s.ordinal::text as segment_ordinal,
       s.id::text as segment_id,
       concat('c', lpad(j.chapter_ordinal::text, 2, '0'), '_s', lpad(s.ordinal::text, 4, '0'), '_seg', s.id::text) as label,
       coalesce(s.status,'') as status,
       coalesce(s.original_duration_ms::text,'') as original_duration_ms,
       coalesce(s.tts_duration_ms::text,'') as tts_duration_ms,
       coalesce(s.tts_audio_rel_path,'') as tts_audio_relpath,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode','') as audio_selection_mode,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'selected_family','') as selected_family,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') as fallback_reason,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'candidate_attempt_count','') as candidate_attempt_count,
       coalesce(s.meta->'tts_lora_candidate_runtime'->'gate_decision'->>'status','') as gate_status,
       coalesce(s.meta->'tts_lora_candidate_runtime'->'gate_decision'->>'reason','') as gate_reason,
       coalesce((s.meta->'tts_lora_candidate_runtime'->'gate_decision'->'reject_reasons')::text,'') as gate_reject_reasons,
       coalesce(s.meta->'tts_lora_candidate_runtime'->>'candidate_generation_stop_reason','') as candidate_generation_stop_reason,
       left(coalesce(s.target_text,''), 220) as target_text_preview
from jobs j
join segments s on s.job_id = j.id
where j.episode_id = {int(episode_id)}
  and coalesce(s.status,'') = 'synthesized'
  and (
    coalesce(s.meta->'tts_lora_candidate_runtime'->>'fallback_reason','') <> ''
    or (
      s.meta ? 'tts_lora_candidate'
      and coalesce(s.meta->'tts_lora_candidate_runtime'->>'audio_selection_mode','') = 'default_audio'
    )
  )
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
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
    )
    proc = run(cmd, cwd=root, timeout=60)
    if proc.returncode != 0:
        return [
            {
                "source": "lora_candidate_runtime",
                "priority": 1,
                "segment_id": 0,
                "label": "lora_runtime_risk_query_failed",
                "reasons": ["lora_runtime_risk_query_failed"],
                "error": proc.stdout[-2000:],
            }
        ]
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if len(lines) <= 1:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    return [compact_lora_runtime_risk_row(dict(row)) for row in reader]


def compact_source_timing_plan_risk_row(row: dict[str, str]) -> dict[str, Any] | None:
    try:
        target_ms = int(row.get("original_duration_ms") or 0)
        tts_ms = int(row.get("tts_duration_ms") or 0)
    except (TypeError, ValueError):
        target_ms = tts_ms = 0
    source_text = str(row.get("source_text") or "")
    target_text = str(row.get("target_text") or "")
    flags, metrics = source_timing_plan_flags(
        source_text=source_text,
        target_text=target_text,
        target_ms=target_ms,
        tts_ms=tts_ms,
    )
    if not flags:
        return None
    has_large_drift = any(flag.startswith("large_tts_drift:") for flag in flags)
    hardish_flags = {"fragmented_source_asr", "low_target_density"}
    priority = 0 if has_large_drift else 1 if any(
        flag in hardish_flags or flag.startswith("low_target_density:") for flag in flags
    ) else 2
    return {
        "source": "source_timing_plan",
        "priority": priority,
        "segment_id": int(row.get("segment_id") or 0),
        "label": str(row.get("label") or ""),
        "chapter_ordinal": int(row.get("chapter_ordinal") or 0),
        "segment_ordinal": int(row.get("segment_ordinal") or 0),
        "reasons": flags,
        "target_ms": target_ms,
        "tts_duration_ms": tts_ms,
        "drift_ms": tts_ms - target_ms if target_ms and tts_ms else None,
        "metrics": metrics,
        "source_text": source_text[:300],
        "expected_text": target_text[:300],
        "tts_audio_relpath": str(row.get("tts_audio_relpath") or ""),
    }


def load_episode_source_timing_plan_risks(args: argparse.Namespace, root: Path, episode_id: int) -> list[dict[str, Any]]:
    if episode_id <= 0:
        return []
    sql = f"""
select j.chapter_ordinal::text as chapter_ordinal,
       s.ordinal::text as segment_ordinal,
       s.id::text as segment_id,
       concat('c', lpad(j.chapter_ordinal::text, 2, '0'), '_s', lpad(s.ordinal::text, 4, '0'), '_seg', s.id::text) as label,
       coalesce(s.original_duration_ms::text,'') as original_duration_ms,
       coalesce(s.tts_duration_ms::text,'') as tts_duration_ms,
       coalesce(s.tts_audio_rel_path,'') as tts_audio_relpath,
       replace(replace(left(coalesce(s.source_text,''), 1200), E'\t', ' '), E'\n', ' ') as source_text,
       replace(replace(left(coalesce(s.target_text,''), 1200), E'\t', ' '), E'\n', ' ') as target_text
from jobs j
join segments s on s.job_id = j.id
where j.episode_id = {int(episode_id)}
  and coalesce(s.status,'') = 'synthesized'
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
            "-A",
            "-F",
            "\t",
            "-c",
            sql,
        ],
    )
    proc = run(cmd, cwd=root, timeout=60)
    if proc.returncode != 0:
        return [
            {
                "source": "source_timing_plan",
                "priority": 1,
                "segment_id": 0,
                "label": "source_timing_plan_query_failed",
                "reasons": ["source_timing_plan_query_failed"],
                "error": proc.stdout[-2000:],
            }
        ]
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("(")]
    if len(lines) <= 1:
        return []
    reader = csv.DictReader(lines, delimiter="\t")
    risks: list[dict[str, Any]] = []
    for row in reader:
        risk = compact_source_timing_plan_risk_row(dict(row))
        if risk:
            risks.append(risk)
    return risks


def run_final_mix_asr(args: argparse.Namespace, root: Path, episode_id: int, lecture_dir: Path) -> dict[str, Any]:
    output_dir = lecture_dir / "final_mix_asr_audit"
    report_path = output_dir / "final_mix_asr_report.json"
    if args.skip_asr and report_path.exists():
        return {
            "skipped": True,
            "reason": "skip_asr_existing_report",
            "report": rel_to_root(report_path, root),
            "final_audio": rel_to_root(output_dir / "final_mix_audio.wav", root),
        }
    cmd = [
        args.python,
        "tools/lora_final_mix_asr_audit.py",
        "--episode-id",
        str(episode_id),
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
    proc = run(cmd, cwd=root, timeout=args.asr_timeout_sec + 1800)
    result = command_result(cmd, proc)
    result["report"] = rel_to_root(report_path, root)
    result["final_audio"] = rel_to_root(output_dir / "final_mix_audio.wav", root)
    return result


def run_fit_audit(args: argparse.Namespace, root: Path, lecture_dir: Path) -> dict[str, Any]:
    episode_segments = lecture_dir / "final_mix_asr_audit" / "episode_segments.json"
    output_json = lecture_dir / "fit_audit.json"
    output_md = lecture_dir / "fit_audit.md"
    if not episode_segments.exists():
        return {
            "skipped": True,
            "reason": "missing_episode_segments",
            "episode_segments": rel_to_root(episode_segments, root),
            "output_json": rel_to_root(output_json, root),
            "output_md": rel_to_root(output_md, root),
        }
    cmd = [
        args.python,
        "tools/mit6824_fit_audit.py",
        "--episode-segments",
        str(episode_segments),
        "--output-json",
        str(output_json),
        "--output-md",
        str(output_md),
    ]
    proc = run(cmd, cwd=root, timeout=180)
    result = command_result(cmd, proc)
    result["output_json"] = rel_to_root(output_json, root)
    result["output_md"] = rel_to_root(output_md, root)
    return result


def review_segment_ids_from_asr(report_path: Path) -> list[int]:
    if not report_path.exists():
        return []
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    ids: set[int] = set()
    for key in ("flagged_rows", "worst_rows"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("segment_id"):
                ids.add(int(row["segment_id"]))
    return sorted(ids)


def run_review_pack(
    args: argparse.Namespace,
    root: Path,
    episode_id: int,
    lecture_dir: Path,
    final_mix_asr_report: Path | None,
    extra_review_segment_ids: list[int] | None = None,
) -> dict[str, Any]:
    output_dir = lecture_dir / "human_review_pack"
    index_path = output_dir / "index.html"
    if args.skip_review_pack and index_path.exists():
        return {"skipped": True, "reason": "skip_review_pack_existing", "index": rel_to_root(index_path, root)}
    cmd = [
        args.python,
        "tools/lora_main_flow_review_pack.py",
        "--episode-id",
        str(episode_id),
        "--api-base",
        args.api_base,
        "--data-root",
        args.data_root,
        "--output-dir",
        str(output_dir),
    ]
    ids = set(extra_review_segment_ids or [])
    if final_mix_asr_report is not None and final_mix_asr_report.exists():
        ids.update(review_segment_ids_from_asr(final_mix_asr_report))
        cmd.extend(["--final-mix-asr-report", str(final_mix_asr_report)])
    if ids:
        cmd.extend(["--review-segment-ids", ",".join(str(item) for item in sorted(ids))])
    if args.skip_clips:
        cmd.append("--skip-clips")
    proc = run(cmd, cwd=root, timeout=3600)
    result = command_result(cmd, proc)
    result["index"] = rel_to_root(index_path, root)
    result["review_manifest"] = rel_to_root(output_dir / "review_manifest.json", root)
    return result


def build_acceptance_actions(lecture: int) -> dict[str, Any]:
    dry_run_report = f"data/validation/mit6824_acceptance/lec-{lecture:02d}/acceptance_dry_run_report.json"
    acceptance_gate_report = "data/validation/mit6824_acceptance_gate_check.md"
    acceptance_gate_command = (
        f"python tools\\mit6824_acceptance_gate_check.py --lecture {lecture} "
        "--output-json data/validation/mit6824_acceptance_gate_check.json "
        "--output-md data/validation/mit6824_acceptance_gate_check.md"
    )
    accept_dry_run_command = (
        f'python tools\\mit6824_accept_lecture.py --lecture {lecture} --reviewer user '
        f'--notes "accepted after human review" --refresh-status --auto-advance-next --dry-run '
        f'--dry-run-output {dry_run_report}'
    )
    accept_command = (
        f'python tools\\mit6824_accept_lecture.py --lecture {lecture} --reviewer user '
        f'--notes "accepted after human review" --refresh-status --auto-advance-next'
    )
    blocker_command = (
        f'python tools\\mit6824_record_review_blockers.py --lecture {lecture} --reviewer user '
        f'--notes "needs repair after human review" --blocker "describe blocker" --refresh-status'
    )
    return {
        "schema_version": "holodub.mit6824.acceptance_actions.v1",
        "lecture": lecture,
        "acceptance_gate_report": acceptance_gate_report,
        "acceptance_gate_command": acceptance_gate_command,
        "accept_dry_run_report": dry_run_report,
        "accept_dry_run_command": accept_dry_run_command,
        "accept_command": accept_command,
        "blocker_command": blocker_command,
        "acceptance_policy": (
            "Run accept only after reviewing final video/audio, ASR risk clips, and any listed risk queue items."
        ),
        "blocker_policy": "Run blocker when any audible or textual issue needs repair before advancing.",
        "auto_advance_next_on_accept": True,
    }


def compact_review_pack_status_summary(
    *,
    readiness: dict[str, Any] | None = None,
    training_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    readiness = readiness if isinstance(readiness, dict) else {}
    training_summary = training_summary if isinstance(training_summary, dict) else {}
    metrics = readiness.get("metrics") if isinstance(readiness.get("metrics"), dict) else {}
    asr = metrics.get("asr") if isinstance(metrics.get("asr"), dict) else {}
    risk_queue = metrics.get("risk_queue") if isinstance(metrics.get("risk_queue"), dict) else {}
    review_pack = metrics.get("review_pack") if isinstance(metrics.get("review_pack"), dict) else {}
    adapter_profile = metrics.get("adapter_profile") if isinstance(metrics.get("adapter_profile"), dict) else {}
    training_readiness = training_summary.get("training_readiness")
    return {
        "schema_version": "holodub.mit6824.review_pack_status_summary.v1",
        "acceptance_readiness": {
            "ready_for_human_review": readiness.get("ready_for_human_review"),
            "required_files_present": readiness.get("required_files_present"),
            "content_errors": readiness.get("content_errors") if isinstance(readiness.get("content_errors"), list) else [],
            "content_warnings": readiness.get("content_warnings") if isinstance(readiness.get("content_warnings"), list) else [],
            "asr_mean_similarity": asr.get("mean_similarity"),
            "asr_weighted_similarity": asr.get("weighted_similarity"),
            "asr_min_similarity": asr.get("min_similarity"),
            "review_rows": review_pack.get("review_rows"),
            "risk_entries": risk_queue.get("total_entries"),
            "candidate_audio_segments": adapter_profile.get("candidate_audio_segments"),
            "fallback_segments": adapter_profile.get("fallback_segments"),
        },
        "training": {
            "recommendation": training_summary.get("recommendation", ""),
            "training_readiness": training_readiness.get("status", "") if isinstance(training_readiness, dict) else "",
            "human_reviewed_lectures": training_summary.get("human_reviewed_lectures", []),
            "pending_review_lectures": training_summary.get("pending_review_lectures", []),
            "pending_content_ready_lectures": training_summary.get("pending_content_ready_lectures", []),
            "pending_content_blocked_lectures": training_summary.get("pending_content_blocked_lectures", []),
        },
    }


def render_status_summary_html(status_summary: dict[str, Any]) -> str:
    if not status_summary:
        return ""
    readiness = status_summary.get("acceptance_readiness") if isinstance(status_summary.get("acceptance_readiness"), dict) else {}
    training = status_summary.get("training") if isinstance(status_summary.get("training"), dict) else {}

    def metric(label: str, value: Any) -> str:
        text = "n/a" if value is None or value == "" else str(value)
        return f'<div class="metric"><strong>{html.escape(text)}</strong><span>{html.escape(label)}</span></div>'

    ready = readiness.get("ready_for_human_review")
    ready_label = "READY" if ready is True else "BLOCKED" if ready is False else "n/a"
    return f"""
      <p><b>Acceptance readiness</b></p>
      <div class="summary">
        {metric("content readiness", ready_label)}
        {metric("mean ASR", readiness.get("asr_mean_similarity"))}
        {metric("weighted ASR", readiness.get("asr_weighted_similarity"))}
        {metric("focused review clips", readiness.get("review_rows"))}
        {metric("risk queue entries", readiness.get("risk_entries"))}
      </div>
      <p><b>Training decision</b>: {html.escape(str(training.get("recommendation") or "n/a"))}</p>
      <p>Pending content-ready lectures: {html.escape(str(training.get("pending_content_ready_lectures") or []))}; human-reviewed lectures: {html.escape(str(training.get("human_reviewed_lectures") or []))}.</p>
    """


def render_acceptance_actions_html(actions: dict[str, Any], status_summary: dict[str, Any] | None = None) -> str:
    acceptance_gate_report = html.escape(str(actions.get("acceptance_gate_report") or ""))
    acceptance_gate_command = html.escape(str(actions.get("acceptance_gate_command") or ""))
    accept_dry_run_command = html.escape(str(actions.get("accept_dry_run_command") or ""))
    accept_dry_run_report = html.escape(str(actions.get("accept_dry_run_report") or ""))
    accept_command = html.escape(str(actions.get("accept_command") or ""))
    blocker_command = html.escape(str(actions.get("blocker_command") or ""))
    lecture = html.escape(str(actions.get("lecture") or ""))
    status_html = render_status_summary_html(status_summary or {})
    return f"""
    <section class="notice" data-mit6824-acceptance-actions="true">
      <h2>Lecture {lecture} Human Acceptance</h2>
      <p>Review the final video, focused clips, ASR evidence, and risk queue before accepting. If any audible or textual issue blocks delivery, record a repair blocker instead.</p>
      {status_html}
      <p><b>Acceptance gate check</b></p>
      <p>Report: <code>{acceptance_gate_report}</code></p>
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 12px;"><code>{acceptance_gate_command}</code></pre>
      <p><b>Dry-run accept rehearsal</b></p>
      <p>Report: <code>{accept_dry_run_report}</code></p>
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 12px;"><code>{accept_dry_run_command}</code></pre>
      <p><b>Accept and advance</b></p>
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 12px;"><code>{accept_command}</code></pre>
      <p><b>Record repair blocker</b></p>
      <pre style="white-space: pre-wrap; overflow-wrap: anywhere; margin: 8px 0 0;"><code>{blocker_command}</code></pre>
    </section>
"""


def inject_acceptance_actions_html(document: str, block: str) -> str:
    wrapped = f"{ACCEPTANCE_ACTIONS_START}\n{block.rstrip()}\n{ACCEPTANCE_ACTIONS_END}"
    if ACCEPTANCE_ACTIONS_START in document and ACCEPTANCE_ACTIONS_END in document:
        pattern = re.compile(
            re.escape(ACCEPTANCE_ACTIONS_START) + r".*?" + re.escape(ACCEPTANCE_ACTIONS_END),
            re.DOTALL,
        )
        return pattern.sub(lambda _match: wrapped, document, count=1)
    if "<main>" in document:
        return document.replace("<main>", f"<main>\n{wrapped}", 1)
    if "<body>" in document:
        return document.replace("<body>", f"<body>\n{wrapped}", 1)
    return f"{wrapped}\n{document}"


def review_pack_annotation_context(
    root: Path,
    lecture: int,
    *,
    acceptance_status_path: Path | None = None,
    training_decision_path: Path | None = None,
) -> dict[str, Any]:
    acceptance_status_path = acceptance_status_path or root / "data" / "validation" / "mit6824_acceptance_status.json"
    training_decision_path = training_decision_path or root / "data" / "validation" / "mit6824_training_decision.json"
    acceptance_status = load_json_if_exists(acceptance_status_path) or {}
    training_decision = load_json_if_exists(training_decision_path) or {}
    lecture_row: dict[str, Any] = {}
    for row in acceptance_status.get("lectures") or []:
        if isinstance(row, dict) and int(row.get("lecture") or 0) == lecture:
            lecture_row = row
            break
    return {
        "acceptance_readiness": lecture_row.get("acceptance_readiness")
        if isinstance(lecture_row.get("acceptance_readiness"), dict)
        else {},
        "training_summary": training_decision.get("summary") if isinstance(training_decision.get("summary"), dict) else {},
    }


def annotate_review_pack_acceptance_actions(
    root: Path,
    lecture_dir: Path,
    lecture: int,
    *,
    acceptance_readiness: dict[str, Any] | None = None,
    training_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    review_dir = lecture_dir / "human_review_pack"
    manifest_path = review_dir / "review_manifest.json"
    index_path = review_dir / "index.html"
    actions = build_acceptance_actions(lecture)
    status_summary = compact_review_pack_status_summary(
        readiness=acceptance_readiness,
        training_summary=training_summary,
    )
    result = {
        "schema_version": "holodub.mit6824.review_pack_acceptance_annotation.v1",
        "lecture": lecture,
        "actions": actions,
        "status_summary": status_summary,
        "review_manifest": rel_to_root(manifest_path, root),
        "index": rel_to_root(index_path, root),
        "manifest_updated": False,
        "html_updated": False,
    }

    manifest = load_json_if_exists(manifest_path) or {}
    if manifest_path.exists() or manifest:
        manifest["acceptance_actions"] = actions
        manifest["mit6824_status_summary"] = status_summary
        write_json(manifest_path, manifest)
        result["manifest_updated"] = True

    if index_path.exists():
        before = index_path.read_text(encoding="utf-8")
        after = inject_acceptance_actions_html(before, render_acceptance_actions_html(actions, status_summary))
        if after != before:
            index_path.write_text(after, encoding="utf-8")
        result["html_updated"] = True

    return result


def row_segment_id(row: dict[str, Any]) -> int:
    for key in ("segment_id", "sid", "id"):
        value = row.get(key)
        if value:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def risk_priority(*, source: str, row: dict[str, Any]) -> int:
    flags = [str(item) for item in row.get("flags") or []]
    similarity = row.get("similarity")
    try:
        sim = float(similarity)
    except (TypeError, ValueError):
        sim = 1.0
    if any("empty_asr" in flag or "asr_failed" in flag for flag in flags):
        return 0
    if source == "final_mix_asr.flagged_rows":
        return 1
    if sim < 0.90:
        return 1
    return 2


def compact_asr_risk_row(source: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": source,
        "priority": risk_priority(source=source, row=row),
        "segment_id": row_segment_id(row),
        "label": str(row.get("label") or ""),
        "chapter_ordinal": row.get("chapter_ordinal"),
        "segment_ordinal": row.get("segment_ordinal"),
        "similarity": row.get("similarity"),
        "flags": row.get("flags") or [],
        "expected_text": str(row.get("expected_text") or ""),
        "asr_text": str(row.get("asr_text") or ""),
    }


def compact_review_risk_row(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if row.get("reason"):
        reasons.append(str(row["reason"]))
    for key in ("reasons", "flags"):
        values = row.get(key)
        if isinstance(values, list):
            reasons.extend(str(item) for item in values if item)
    return {
        "source": "human_review_pack",
        "priority": 1 if row.get("forced_human_review") else 2,
        "segment_id": row_segment_id(row),
        "label": str(row.get("label") or ""),
        "selected_candidate": str(row.get("selected_candidate") or ""),
        "similarity": row.get("similarity"),
        "reasons": reasons,
    }


def build_risk_queue(
    *,
    lecture: int,
    episode: dict[str, Any],
    asr_report_path: Path | None,
    review_manifest_path: Path | None,
    lora_runtime_risks: list[dict[str, Any]] | None = None,
    source_timing_plan_risks: list[dict[str, Any]] | None = None,
    fit_audit_risks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()

    def add(entry: dict[str, Any]) -> None:
        segment_id = int(entry.get("segment_id") or 0)
        source = str(entry.get("source") or "")
        label = str(entry.get("label") or "")
        key = (source, segment_id, label)
        if key in seen:
            return
        seen.add(key)
        entries.append(entry)

    asr_report = load_json_if_exists(asr_report_path)
    if asr_report:
        for source_key in ("flagged_rows", "worst_rows"):
            rows = asr_report.get(source_key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    add(compact_asr_risk_row(f"final_mix_asr.{source_key}", row))

    review_manifest = load_json_if_exists(review_manifest_path)
    if review_manifest:
        for source_key in ("items", "review_rows"):
            rows = review_manifest.get(source_key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict):
                    add(compact_review_risk_row(row))
    for row in lora_runtime_risks or []:
        if isinstance(row, dict):
            add(dict(row))
    for row in source_timing_plan_risks or []:
        if isinstance(row, dict):
            add(dict(row))
    for row in fit_audit_risks or []:
        if isinstance(row, dict):
            add(
                {
                    "source": "fit_audit",
                    "priority": 0,
                    "segment_id": row.get("segment_id"),
                    "label": row.get("label"),
                    "selected_candidate": row.get("family"),
                    "risk_kind": row.get("risk_kind"),
                    "fit_tempo_delta": row.get("fit_tempo_delta"),
                    "final_implied_fit_delta": row.get("final_implied_fit_delta"),
                    "fit_applied": row.get("fit_applied"),
                    "audio_selection_mode": row.get("audio_selection_mode"),
                    "fallback_reason": row.get("fallback_reason"),
                    "final_reason": row.get("final_reason"),
                    "audio_relpath": row.get("audio_relpath"),
                    "pre_fit_drift_ms": row.get("pre_fit_drift_ms"),
                    "post_fit_drift_ms": row.get("post_fit_drift_ms"),
                    "reasons": [
                        str(row.get("risk_kind") or "fit_audit"),
                        f"selected_fit_tempo_delta:{row.get('fit_tempo_delta')}",
                        f"final_implied_fit_delta:{row.get('final_implied_fit_delta')}",
                    ],
                    "text_preview": row.get("text_snippet"),
                }
            )

    entries.sort(
        key=lambda row: (
            priority_value(row),
            int(row.get("chapter_ordinal") or 999),
            int(row.get("segment_ordinal") or 999999),
            int(row.get("segment_id") or 0),
            str(row.get("source") or ""),
        )
    )
    by_source: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for entry in entries:
        source = str(entry.get("source") or "unknown")
        priority = str(entry.get("priority") if entry.get("priority") is not None else "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        by_priority[priority] = by_priority.get(priority, 0) + 1
    return {
        "schema_version": "holodub.mit6824.risk_queue.v1",
        "lecture": lecture,
        "episode_id": int(episode.get("id") or 0),
        "source": "final_mix_asr_and_review_pack",
        "sources": {
            "final_mix_asr_report": str(asr_report_path or ""),
            "final_mix_asr_report_present": bool(asr_report_path and asr_report_path.exists()),
            "review_manifest": str(review_manifest_path or ""),
            "review_manifest_present": bool(review_manifest_path and review_manifest_path.exists()),
            "lora_runtime_risks_present": bool(lora_runtime_risks),
            "source_timing_plan_risks_present": bool(source_timing_plan_risks),
            "fit_audit_risks_present": bool(fit_audit_risks),
        },
        "summary": {
            "total_entries": len(entries),
            "by_source": by_source,
            "by_priority": by_priority,
        },
        "entries": entries,
    }


def write_acceptance_manifest(
    *,
    root: Path,
    lecture: int,
    lecture_title: str,
    lecture_dir: Path,
    episode: dict[str, Any],
    tracker_row: dict[str, Any] | None,
    lora_provenance: dict[str, Any],
    lora_runtime_risks: list[dict[str, Any]] | None = None,
    source_timing_plan_risks: list[dict[str, Any]] | None = None,
    fit_audit: dict[str, Any] | None = None,
    asr_report_path: Path | None,
    review_pack_index: Path | None,
) -> Path:
    episode_output = str(episode.get("output_rel_path") or episode.get("output_relpath") or "")
    final_video = str((Path("data") / episode_output).as_posix()) if episode_output else ""
    start_payload = (tracker_row or {}).get("start_payload") if isinstance(tracker_row, dict) else {}
    start_config = start_payload.get("config") if isinstance(start_payload, dict) and isinstance(start_payload.get("config"), dict) else {}
    artifacts = {
        "final_video": final_video,
        "final_audio": rel_to_root(lecture_dir / "final_mix_asr_audit" / "final_mix_audio.wav", root),
        "final_mix_asr_report": rel_to_root(asr_report_path, root) if asr_report_path else "",
        "fit_audit": rel_to_root(lecture_dir / "fit_audit.json", root),
        "risk_queue": rel_to_root(lecture_dir / "risk_queue.json", root),
        "human_review_pack": rel_to_root(review_pack_index, root) if review_pack_index else "",
        "adapter_profile_provenance": rel_to_root(lecture_dir / "adapter_profile_provenance.json", root),
    }
    provenance = {
        "schema_version": "holodub.mit6824.adapter_profile_provenance.v1",
        "lecture": lecture,
        "episode_id": int(episode.get("id") or 0),
        "production_profile": start_config.get("production_profile") or "mit6824-course-prod-v1",
        "lora_policy": "course_profile_not_per_episode_training",
        "adapter_scope": "course_or_domain_profile",
        "lora_candidate_opt_in": bool(start_config.get("lora_candidate_opt_in")),
        "start_config": start_config,
        "runtime": lora_provenance,
    }
    write_json(lecture_dir / "adapter_profile_provenance.json", provenance)
    review_manifest_path = lecture_dir / "human_review_pack" / "review_manifest.json"
    risk_queue = build_risk_queue(
        lecture=lecture,
        episode=episode,
        asr_report_path=asr_report_path,
        review_manifest_path=review_manifest_path if review_manifest_path.exists() else None,
        lora_runtime_risks=lora_runtime_risks,
        source_timing_plan_risks=source_timing_plan_risks,
        fit_audit_risks=(fit_audit or {}).get("risk_segments") if isinstance(fit_audit, dict) else None,
    )
    write_json(lecture_dir / "risk_queue.json", risk_queue)
    manifest = {
        "schema_version": "holodub.mit6824.acceptance.v1",
        "lecture": lecture,
        "title": lecture_title,
        "episode_id": int(episode.get("id") or 0),
        "human_accepted": False,
        "accepted_at": "",
        "artifacts": artifacts,
        "acceptance_actions": build_acceptance_actions(lecture),
        "training_decision": {
            "train_adapter": False,
            "lecture_local_experiment_allowed": True,
            "reason": "promoted per-lecture adapters are disabled by default; rejected lectures may start controlled local LoRA experiments",
        },
    }
    path = lecture_dir / "acceptance.json"
    write_json(path, manifest)
    return path


def one_attempt(args: argparse.Namespace, root: Path) -> tuple[bool, dict[str, Any]]:
    tracker_refresh = refresh_tracker(args, root) if args.refresh_tracker else None
    tracker_path = resolve(root, args.tracker)
    if tracker_path is None:
        raise ValueError("tracker path required")
    tracker = load_tracker(tracker_path)
    row = find_lecture(tracker, args.lecture)
    episode = selected_episode(row, args.episode_id)
    lecture_dir = resolve(root, args.acceptance_root)
    if lecture_dir is None:
        raise ValueError("acceptance root required")
    lecture_dir = lecture_dir / f"lec-{args.lecture:02d}"
    summary: dict[str, Any] = {
        "lecture": args.lecture,
        "title": row.get("title") or "",
        "execute": args.execute,
        "lecture_dir": rel_to_root(lecture_dir, root),
        "updated_at_epoch": time.time(),
        "episode": episode,
    }
    if tracker_refresh is not None:
        summary["tracker_refresh"] = tracker_refresh
    if not episode:
        summary["status"] = "waiting_for_episode"
        return False, summary
    episode_status = str(episode.get("status") or "")
    if episode_status != "completed" and not args.allow_incomplete:
        summary["status"] = "waiting_for_completed_episode"
        return False, summary
    if not episode.get("output_rel_path") and not episode.get("output_relpath") and not args.allow_incomplete:
        summary["status"] = "waiting_for_final_video"
        return False, summary
    if not args.execute:
        summary["status"] = "dry_run"
        summary["would_run"] = {
            "final_mix_asr": not args.skip_asr,
            "review_pack": not args.skip_review_pack,
            "acceptance_manifest": True,
        }
        return True, summary

    lecture_dir.mkdir(parents=True, exist_ok=True)
    episode_id = int(episode.get("id") or args.episode_id)
    asr_report_path = lecture_dir / "final_mix_asr_audit" / "final_mix_asr_report.json"
    final_mix = None if args.skip_asr and not asr_report_path.exists() else run_final_mix_asr(args, root, episode_id, lecture_dir)
    summary["final_mix_asr"] = final_mix
    fit_audit = run_fit_audit(args, root, lecture_dir)
    summary["fit_audit"] = fit_audit
    fit_audit_path = lecture_dir / "fit_audit.json"
    fit_audit_payload = load_json_if_exists(fit_audit_path) or {}
    lora_provenance = load_episode_lora_provenance(args, root, episode_id)
    summary["adapter_profile_provenance"] = lora_provenance
    lora_runtime_risks = load_episode_lora_runtime_risks(args, root, episode_id)
    summary["lora_runtime_risk_count"] = len(lora_runtime_risks)
    source_timing_plan_risks = load_episode_source_timing_plan_risks(args, root, episode_id)
    summary["source_timing_plan_risk_count"] = len(source_timing_plan_risks)
    extra_review_segment_ids = sorted(
        {
            int(row.get("segment_id") or 0)
            for row in [
                *lora_runtime_risks,
                *source_timing_plan_risks,
                *(
                    fit_audit_payload.get("risk_segments")
                    if isinstance(fit_audit_payload.get("risk_segments"), list)
                    else []
                ),
            ]
            if isinstance(row, dict) and int(row.get("segment_id") or 0) > 0
        }
    )
    summary["extra_review_segment_ids"] = extra_review_segment_ids
    review_pack = None
    if not args.skip_review_pack:
        review_pack = run_review_pack(
            args,
            root,
            episode_id,
            lecture_dir,
            asr_report_path if asr_report_path.exists() else None,
            extra_review_segment_ids=extra_review_segment_ids,
        )
        summary["human_review_pack"] = review_pack
    review_pack_index = lecture_dir / "human_review_pack" / "index.html"
    summary["review_pack_acceptance_actions"] = annotate_review_pack_acceptance_actions(
        root,
        lecture_dir,
        args.lecture,
    )
    manifest_path = write_acceptance_manifest(
        root=root,
        lecture=args.lecture,
        lecture_title=str(row.get("title") or ""),
        lecture_dir=lecture_dir,
        episode=episode,
        tracker_row=row,
        lora_provenance=lora_provenance,
        lora_runtime_risks=lora_runtime_risks,
        source_timing_plan_risks=source_timing_plan_risks,
        fit_audit=fit_audit_payload,
        asr_report_path=asr_report_path if asr_report_path.exists() else None,
        review_pack_index=review_pack_index if review_pack_index.exists() else None,
    )
    summary["acceptance_manifest"] = rel_to_root(manifest_path, root)
    if not getattr(args, "skip_status_refresh_after_finalize", False):
        status_refresh = refresh_course_status(args, root)
        summary["status_refresh"] = status_refresh
        if status_refresh.get("returncode") != 0:
            summary["status_refresh_warning"] = "post_finalize_status_refresh_failed"
    summary["status"] = "finalized_for_human_review"
    return True, summary


def main() -> int:
    args = parse_args()
    root = repo_root()
    output = resolve(root, f"data/validation/mit6824_acceptance/lec-{args.lecture:02d}/finalize_status.json")
    history = resolve(root, f"data/validation/mit6824_acceptance/lec-{args.lecture:02d}/finalize_history.jsonl")
    if output is None or history is None:
        raise ValueError("status paths required")
    deadline = time.time() + args.timeout_sec if args.timeout_sec > 0 else None
    attempt = 0
    while True:
        attempt += 1
        done, row = one_attempt(args, root)
        row["attempt"] = attempt
        write_json(output, row)
        append_jsonl(history, row)
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        if done:
            return 0 if row.get("status") in {"dry_run", "finalized_for_human_review"} else 1
        if not args.watch:
            return 2
        if deadline and time.time() >= deadline:
            row["status"] = "timeout"
            write_json(output, row)
            append_jsonl(history, row)
            return 124
        time.sleep(max(1.0, args.poll_sec))


if __name__ == "__main__":
    raise SystemExit(main())
