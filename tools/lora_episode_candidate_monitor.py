from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from lora_candidate_runtime_replay import replay_runtime


DEFAULT_FOCUS_SOURCE_SEGMENTS = [
    10693,
    10694,
    10695,
    10696,
    10697,
    10698,
    10700,
    10701,
    10702,
    10720,
    10725,
    10728,
    10733,
    10751,
    10786,
    10808,
    10833,
    10835,
    10866,
]
DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor a main-flow LoRA candidate episode shadow run and write progress/quality JSON."
    )
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--history-jsonl", default="")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--timeout-sec", type=float, default=0.0)
    parser.add_argument("--poll-sec", type=float, default=30.0)
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
    parser.add_argument("--max-candidate-drift-ms", type=float, default=500.0)
    parser.add_argument("--review-drift-ms", type=float, default=300.0)
    parser.add_argument("--max-final-fit-delta", type=float, default=0.05)
    parser.add_argument("--low-judge-threshold", type=float, default=0.80)
    parser.add_argument("--focus-source-segments", default=",".join(str(x) for x in DEFAULT_FOCUS_SOURCE_SEGMENTS))
    parser.add_argument("--source-episode-id", type=int, default=0)
    parser.add_argument("--backfill-lineage", action="store_true")
    parser.add_argument("--compose-file", action="append", default=[])
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    return parser.parse_args()


def split_int_csv(raw: str) -> set[int]:
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip().removeprefix("seg")
        if item:
            values.add(int(item))
    return values


def fetch_json(api_base: str, path: str, api_key: str = "", timeout: float = 30.0) -> dict[str, Any]:
    url = api_base.rstrip("/") + path
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"GET {url} returned non-object JSON")
    return payload


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


def backfill_lineage(args: argparse.Namespace) -> str:
    if not args.backfill_lineage or not args.source_episode_id:
        return ""
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
            lineage_backfill_sql(args.source_episode_id, args.episode_id),
        ],
    )
    result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def load_episode(api_base: str, episode_id: int, api_key: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    episode = fetch_json(api_base, f"/episodes/{episode_id}", api_key)
    chapters_payload = fetch_json(api_base, f"/episodes/{episode_id}/chapters", api_key)
    chapters = [row for row in chapters_payload.get("chapters") or [] if isinstance(row, dict)]
    segments: list[dict[str, Any]] = []
    for chapter in chapters:
        job_id = chapter.get("id")
        if not job_id:
            continue
        payload = fetch_json(api_base, f"/jobs/{job_id}/segments", api_key)
        for segment in payload.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            row = dict(segment)
            row["chapter_id"] = job_id
            row["chapter_ordinal"] = chapter.get("chapter_ordinal")
            row["chapter_start_ms"] = chapter.get("chapter_start_ms")
            row["chapter_end_ms"] = chapter.get("chapter_end_ms")
            row["chapter_status"] = chapter.get("status")
            row["chapter_current_stage"] = chapter.get("current_stage")
            segments.append(row)
    return episode, chapters, segments


def count_non_ws(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def meta_of(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("meta")
    return meta if isinstance(meta, dict) else {}


def candidate_plan(row: dict[str, Any]) -> dict[str, Any]:
    raw = meta_of(row).get("tts_lora_candidate")
    return raw if isinstance(raw, dict) else {}


def candidate_runtime(row: dict[str, Any]) -> dict[str, Any]:
    meta = meta_of(row)
    plan = candidate_plan(row)
    runtime = plan.get("runtime")
    if isinstance(runtime, dict):
        return runtime
    raw = meta.get("tts_lora_candidate_runtime")
    return raw if isinstance(raw, dict) else {}


def runtime_needs_gate_replay(runtime: dict[str, Any]) -> bool:
    candidates = runtime.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    if runtime.get("fallback_reason"):
        return True
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if "bad_join_quality" in (candidate.get("reject_reasons") or []):
            return True
    return False


def compact_replayed_candidate(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    return {
        "family": row.get("family"),
        "split_strategy": row.get("split_strategy"),
        "audio_relpath": row.get("audio_relpath"),
        "duration_ms": row.get("duration_ms"),
        "drift_ms": row.get("drift_ms"),
        "similarity": row.get("similarity"),
        "critical_term_recall": row.get("critical_term_recall"),
        "fit_tempo_delta": row.get("fit_tempo_delta"),
        "gate_pass": row.get("gate_pass"),
        "strong_gate_pass": row.get("strong_gate_pass"),
        "reject_reasons": row.get("reject_reasons") or [],
        "risk_reasons": row.get("risk_reasons") or [],
        "join_quality_rescue": row.get("join_quality_rescue"),
        "gate_replay": row.get("gate_replay") or {},
    }


def compact_gate_replay(runtime: dict[str, Any]) -> dict[str, Any] | None:
    if not runtime_needs_gate_replay(runtime):
        return None
    report = replay_runtime(runtime)
    return {
        "input_selected_family": report.get("input_selected_family"),
        "input_audio_selection_mode": report.get("input_audio_selection_mode"),
        "input_fallback_reason": report.get("input_fallback_reason"),
        "replayed_selected_family": report.get("replayed_selected_family"),
        "replayed_selected_split_strategy": report.get("replayed_selected_split_strategy"),
        "replayed_audio_selection_mode": report.get("replayed_audio_selection_mode"),
        "replayed_fallback_reason": report.get("replayed_fallback_reason"),
        "changed": report.get("changed"),
        "candidate_count": report.get("candidate_count"),
        "gate_pass_count": report.get("gate_pass_count"),
        "join_quality_rescue_count": report.get("join_quality_rescue_count"),
        "selected_candidate": compact_replayed_candidate(report.get("selected_candidate")),
    }


def agent_meta(row: dict[str, Any]) -> dict[str, Any]:
    raw = meta_of(row).get("tts_agent")
    return raw if isinstance(raw, dict) else {}


def agent_failure_meta(row: dict[str, Any]) -> dict[str, Any]:
    raw = meta_of(row).get("tts_agent_failure")
    return raw if isinstance(raw, dict) else {}


def judge_meta(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("judge_meta")
    return raw if isinstance(raw, dict) else {}


def judge_score(row: dict[str, Any]) -> float | None:
    raw = row.get("judge_score")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def source_segment_id(row: dict[str, Any]) -> int:
    raw = meta_of(row).get("shadow_source_segment_id")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def target_ms(row: dict[str, Any]) -> int:
    return int(row.get("original_duration_ms") or 0)


def duration_ms(row: dict[str, Any]) -> int:
    return int(row.get("tts_duration_ms") or 0)


def timeline_start_ms(row: dict[str, Any]) -> int:
    return int(row.get("chapter_start_ms") or 0) + int(row.get("start_ms") or 0)


def timeline_end_ms(row: dict[str, Any]) -> int:
    return int(row.get("chapter_start_ms") or 0) + int(row.get("end_ms") or 0)


def text_of(row: dict[str, Any]) -> str:
    return str(row.get("tgt_text") or row.get("target_text") or "")


def source_text_of(row: dict[str, Any]) -> str:
    return str(row.get("src_text") or row.get("source_text") or "")


def arg_value(args: argparse.Namespace, name: str, default: Any) -> Any:
    return getattr(args, name, default)


def looks_like_short_transition_question(text: str) -> bool:
    compact = "".join(char for char in text if not char.isspace())
    q_indexes = [index for index in (compact.find("\uff1f"), compact.find("?")) if index >= 0]
    if not q_indexes:
        return False
    q = min(q_indexes)
    before = compact[:q]
    after = compact[q + 1 :]
    question_cues = ("\u7591\u95ee", "\u95ee\u9898", "\u8fd8\u6709\u4ec0\u4e48", "鐤戦棶", "闂", "杩樻湁浠€涔")
    continuations = ("\u597d\u7684", "\u597d\uff0c", "\u597d", "濂界殑", "濂斤紝", "濂")
    continue_verbs = ("\u7ee7\u7eed", "\u63a5\u7740", "\u8fdb\u5165", "缁х画", "鎺ョ潃", "杩涘叆")
    return (
        any(cue in before for cue in question_cues)
        and any(cue in after for cue in continuations)
        and any(cue in after for cue in continue_verbs)
    )


def looks_like_short_term_number(text: str) -> bool:
    compact = "".join(char for char in text if not char.isspace())
    marker = "\u53f7\u4efb\u671f"
    idx = compact.find(marker)
    if idx <= 0:
        return False
    last = compact[:idx][-1]
    return last.isdigit() or last in "\u96f6\u3007\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341"


def is_eligible(row: dict[str, Any], args: argparse.Namespace) -> bool:
    target_sec = target_ms(row) / 1000.0
    chars = count_non_ws(text_of(row))
    chars_per_sec = chars / target_sec if target_sec > 0 else 0.0
    duration_eligible = arg_value(args, "min_target_sec", 18.0) <= 0 or target_sec >= float(arg_value(args, "min_target_sec", 18.0))
    text_eligible = arg_value(args, "min_text_chars", 180) <= 0 or chars >= int(arg_value(args, "min_text_chars", 180))
    dense_eligible = (
        float(arg_value(args, "dense_min_target_sec", 9.0)) > 0
        and float(arg_value(args, "dense_min_chars_per_sec", 4.9)) > 0
        and target_sec >= float(arg_value(args, "dense_min_target_sec", 9.0))
        and chars_per_sec >= float(arg_value(args, "dense_min_chars_per_sec", 4.9))
    )
    near_long_eligible = (
        float(arg_value(args, "near_long_min_target_sec", 16.0)) > 0
        and int(arg_value(args, "near_long_min_text_chars", 70)) > 0
        and target_sec >= float(arg_value(args, "near_long_min_target_sec", 16.0))
        and chars >= int(arg_value(args, "near_long_min_text_chars", 70))
    )
    short_dense_eligible = (
        float(arg_value(args, "short_dense_min_target_sec", 6.0)) > 0
        and float(arg_value(args, "short_dense_max_target_sec", 9.0)) > 0
        and float(arg_value(args, "short_dense_min_chars_per_sec", 5.5)) > 0
        and target_sec >= float(arg_value(args, "short_dense_min_target_sec", 6.0))
        and target_sec <= float(arg_value(args, "short_dense_max_target_sec", 9.0))
        and chars_per_sec >= float(arg_value(args, "short_dense_min_chars_per_sec", 5.5))
    )
    short_transition_eligible = (
        float(arg_value(args, "short_transition_min_target_sec", 5.0)) > 0
        and float(arg_value(args, "short_transition_max_target_sec", 10.0)) > 0
        and float(arg_value(args, "short_transition_min_chars_per_sec", 5.0)) > 0
        and target_sec >= float(arg_value(args, "short_transition_min_target_sec", 5.0))
        and target_sec <= float(arg_value(args, "short_transition_max_target_sec", 10.0))
        and chars_per_sec >= float(arg_value(args, "short_transition_min_chars_per_sec", 5.0))
        and looks_like_short_transition_question(text_of(row))
    )
    short_term_number_eligible = (
        float(arg_value(args, "short_term_number_min_target_sec", 6.0)) > 0
        and float(arg_value(args, "short_term_number_max_target_sec", 9.0)) > 0
        and float(arg_value(args, "short_term_number_min_chars_per_sec", 4.8)) > 0
        and target_sec >= float(arg_value(args, "short_term_number_min_target_sec", 6.0))
        and target_sec <= float(arg_value(args, "short_term_number_max_target_sec", 9.0))
        and chars_per_sec >= float(arg_value(args, "short_term_number_min_chars_per_sec", 4.8))
        and looks_like_short_term_number(text_of(row))
    )
    return (
        duration_eligible
        or text_eligible
        or dense_eligible
        or near_long_eligible
        or short_dense_eligible
        or short_transition_eligible
        or short_term_number_eligible
    )


def compact_segment(row: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    plan = candidate_plan(row)
    runtime = candidate_runtime(row)
    agent = agent_meta(row)
    agent_failure = agent_failure_meta(row)
    judge = judge_meta(row)
    target = target_ms(row)
    duration = duration_ms(row)
    eligible = is_eligible(row, args) or bool(plan.get("enabled"))
    compact = {
        "segment_id": row.get("id"),
        "source_segment_id": source_segment_id(row),
        "chapter_id": row.get("chapter_id"),
        "chapter_ordinal": row.get("chapter_ordinal"),
        "ordinal": row.get("ordinal"),
        "start_ms": row.get("start_ms"),
        "end_ms": row.get("end_ms"),
        "absolute_start_ms": timeline_start_ms(row),
        "absolute_end_ms": timeline_end_ms(row),
        "status": row.get("status"),
        "eligible": eligible,
        "target_ms": target,
        "tts_duration_ms": duration,
        "drift_ms": duration - target if duration else None,
        "tts_audio_relpath": row.get("tts_audio_path") or row.get("tts_audio_rel_path") or "",
        "plan_enabled": plan.get("enabled"),
        "plan_phase": plan.get("phase"),
        "plan_audio_selection_mode": plan.get("audio_selection_mode"),
        "runtime_audio_selection_mode": runtime.get("audio_selection_mode"),
        "runtime_selected_family": runtime.get("selected_family"),
        "runtime_selected_split_strategy": runtime.get("selected_split_strategy"),
        "runtime_fallback_reason": runtime.get("fallback_reason"),
        "runtime_candidate_count": len(runtime.get("candidates") or []),
        "runtime_split_parts": len(((runtime.get("split_plan") or {}) if isinstance(runtime.get("split_plan"), dict) else {}).get("parts") or []),
        "gate_decision": runtime.get("gate_decision"),
        "agent_final_reason": agent.get("final_reason"),
        "agent_hard_cap_exceeded": bool(agent.get("hard_cap_exceeded")),
        "agent_hard_cap_direction": agent.get("hard_cap_direction"),
        "agent_final_abs_drift_ms": agent.get("final_abs_drift_ms"),
        "agent_final_implied_fit_delta": agent.get("final_implied_fit_delta"),
        "agent_failure_reason": agent_failure.get("final_reason"),
        "agent_failure_error": agent_failure.get("error"),
        "agent_failure_hard_cap_exceeded": bool(agent_failure.get("hard_cap_exceeded")),
        "agent_failure_hard_cap_direction": agent_failure.get("hard_cap_direction"),
        "agent_failure_final_abs_drift_ms": agent_failure.get("final_abs_drift_ms"),
        "agent_failure_final_implied_fit_delta": agent_failure.get("final_implied_fit_delta"),
        "judge_score": judge_score(row),
        "judge_verdict": judge.get("verdict"),
        "judge_issues": [str(item) for item in (judge.get("issues") or []) if item]
        if isinstance(judge.get("issues"), list)
        else [],
        "source_text": source_text_of(row),
        "target_text": text_of(row),
    }
    gate_replay = compact_gate_replay(runtime)
    if gate_replay is not None:
        compact["runtime_gate_replay"] = gate_replay
    return compact


def summarize(
    episode: dict[str, Any],
    chapters: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    source_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    compact = [compact_segment(row, args) for row in segments]
    chapter_progress = build_chapter_progress(chapters, compact)
    status_counts = Counter(str(row.get("status") or "") for row in compact)
    chapter_status_counts = Counter(str(row.get("status") or "") for row in chapters)
    eligible = [row for row in compact if row.get("eligible")]
    eligible_synthesized = [row for row in eligible if row.get("status") == "synthesized"]
    synthesized = [row for row in compact if row.get("status") == "synthesized"]
    candidate_audio = [row for row in eligible_synthesized if row.get("runtime_audio_selection_mode") == "candidate_audio"]
    fallback_rows = [row for row in eligible_synthesized if row.get("runtime_fallback_reason")]
    inflight_fallback_rows = [
        row
        for row in eligible
        if row.get("status") != "synthesized" and row.get("runtime_fallback_reason")
    ]
    gate_replay_rows = [row for row in eligible if row.get("runtime_gate_replay")]
    drifts = [abs(float(row.get("drift_ms") or 0.0)) for row in synthesized if row.get("drift_ms") is not None]
    candidate_drifts = [
        abs(float(row.get("drift_ms") or 0.0))
        for row in candidate_audio
        if row.get("drift_ms") is not None
    ]
    high_drift = [
        row
        for row in synthesized
        if row.get("drift_ms") is not None and abs(float(row.get("drift_ms") or 0.0)) > args.max_candidate_drift_ms
    ]
    review_drift = [
        row
        for row in synthesized
        if row.get("drift_ms") is not None
        and abs(float(row.get("drift_ms") or 0.0)) > args.review_drift_ms
        and row not in high_drift
    ]
    hard_cap_rows = [row for row in synthesized if row.get("agent_hard_cap_exceeded")]
    agent_failure_rows = [
        row
        for row in compact
        if row.get("agent_failure_error") and not row.get("agent_final_reason")
    ]
    agent_failure_hard_cap_rows = [row for row in agent_failure_rows if row.get("agent_failure_hard_cap_exceeded")]
    agent_failure_fit_rows = [
        row
        for row in agent_failure_rows
        if row.get("agent_failure_final_implied_fit_delta") is not None
        and float(row.get("agent_failure_final_implied_fit_delta") or 0.0) > args.max_final_fit_delta
    ]
    high_final_fit_rows = [
        row
        for row in synthesized
        if row.get("agent_final_implied_fit_delta") is not None
        and float(row.get("agent_final_implied_fit_delta") or 0.0) > args.max_final_fit_delta
    ]
    low_judge_rows = [
        row
        for row in synthesized
        if row.get("judge_score") is not None and float(row["judge_score"]) <= args.low_judge_threshold
    ]
    focus_ids = split_int_csv(args.focus_source_segments)
    focus_rows = [row for row in compact if int(row.get("source_segment_id") or 0) in focus_ids]
    focus_overlap_rows = build_focus_overlap_rows(compact, source_segments or [], focus_ids)
    validation_issues = []
    risk_queue = []
    for row in eligible:
        if row.get("status") == "synthesized" and row.get("runtime_audio_selection_mode") != "candidate_audio":
            issue = {
                "source_segment_id": row.get("source_segment_id"),
                "segment_id": row.get("segment_id"),
                "reason": "eligible_synthesized_without_candidate_audio",
                "value": row.get("runtime_audio_selection_mode"),
            }
            validation_issues.append(issue)
            risk_queue.append(
                risk_queue_row(row, "eligible_synthesized_without_candidate_audio", 10, row.get("runtime_audio_selection_mode"))
            )
    for row in fallback_rows:
        issue = {
            "source_segment_id": row.get("source_segment_id"),
            "segment_id": row.get("segment_id"),
            "reason": "candidate_fallback",
            "value": row.get("runtime_fallback_reason"),
        }
        validation_issues.append(issue)
        risk_queue.append(
            risk_queue_row(row, "candidate_fallback", 20, row.get("runtime_fallback_reason"))
        )
    for row in inflight_fallback_rows:
        risk_queue.append(
            risk_queue_row(row, "inflight_candidate_fallback", 50, row.get("runtime_fallback_reason"))
        )
    for row in high_drift:
        issue = {
            "source_segment_id": row.get("source_segment_id"),
            "segment_id": row.get("segment_id"),
            "reason": "drift_exceeds_hard_limit",
            "value": row.get("drift_ms"),
            "threshold": args.max_candidate_drift_ms,
            "audio_selection_mode": row.get("runtime_audio_selection_mode"),
        }
        validation_issues.append(issue)
        risk_queue.append(
            risk_queue_row(row, "drift_exceeds_hard_limit", 5, row.get("drift_ms"))
        )
    for row in review_drift:
        risk_queue.append(
            risk_queue_row(row, "review_drift_exceeds_soft_limit", 30, row.get("drift_ms"))
        )
    for row in hard_cap_rows:
        issue = {
            "source_segment_id": row.get("source_segment_id"),
            "segment_id": row.get("segment_id"),
            "reason": "agent_hard_cap_exceeded",
            "value": row.get("agent_final_abs_drift_ms") or row.get("drift_ms"),
            "direction": row.get("agent_hard_cap_direction"),
            "final_reason": row.get("agent_final_reason"),
        }
        validation_issues.append(issue)
        risk_queue.append(
            risk_queue_row(row, "agent_hard_cap_exceeded", 6, issue["value"])
        )
    for row in agent_failure_hard_cap_rows:
        issue = {
            "source_segment_id": row.get("source_segment_id"),
            "segment_id": row.get("segment_id"),
            "reason": "agent_failure_hard_cap_rejected",
            "value": row.get("agent_failure_final_abs_drift_ms"),
            "direction": row.get("agent_failure_hard_cap_direction"),
            "final_reason": row.get("agent_failure_reason"),
            "error": row.get("agent_failure_error"),
        }
        validation_issues.append(issue)
        risk_queue.append(
            risk_queue_row(row, "agent_failure_hard_cap_rejected", 6, issue["value"])
        )
    for row in high_final_fit_rows:
        issue = {
            "source_segment_id": row.get("source_segment_id"),
            "segment_id": row.get("segment_id"),
            "reason": "agent_final_fit_delta_exceeded",
            "value": row.get("agent_final_implied_fit_delta"),
            "threshold": args.max_final_fit_delta,
            "final_reason": row.get("agent_final_reason"),
            "audio_selection_mode": row.get("runtime_audio_selection_mode"),
        }
        validation_issues.append(issue)
        risk_queue.append(
            risk_queue_row(row, "agent_final_fit_delta_exceeded", 7, issue["value"])
        )
    for row in agent_failure_fit_rows:
        issue = {
            "source_segment_id": row.get("source_segment_id"),
            "segment_id": row.get("segment_id"),
            "reason": "agent_failure_fit_delta_rejected",
            "value": row.get("agent_failure_final_implied_fit_delta"),
            "threshold": args.max_final_fit_delta,
            "final_reason": row.get("agent_failure_reason"),
            "error": row.get("agent_failure_error"),
        }
        validation_issues.append(issue)
        risk_queue.append(
            risk_queue_row(row, "agent_failure_fit_delta_rejected", 7, issue["value"])
        )
    for row in low_judge_rows:
        risk_queue.append(
            risk_queue_row(row, "low_segment_judge", 70, row.get("judge_score"))
        )
    risk_queue = sorted(
        risk_queue,
        key=lambda row: (
            int(row.get("priority") or 999),
            int(row.get("chapter_ordinal") or 0),
            int(row.get("ordinal") or 0),
            int(row.get("segment_id") or 0),
        ),
    )

    return {
        "schema_version": "holodub.lora_episode_candidate_monitor.v1",
        "episode": {
            "id": episode.get("id"),
            "name": episode.get("name"),
            "status": episode.get("status"),
            "duration_ms": episode.get("duration_ms"),
            "output_relpath": episode.get("output_relpath"),
        },
        "progress": {
            "chapters_total": len(chapters),
            "chapters_by_status": dict(sorted(chapter_status_counts.items())),
            "chapter_progress": chapter_progress,
            "segments_total": len(compact),
            "segments_by_status": dict(sorted(status_counts.items())),
            "segments_synthesized": status_counts.get("synthesized", 0),
            "segments_remaining": len(compact) - status_counts.get("synthesized", 0) - status_counts.get("skipped", 0),
        },
        "candidate": {
            "eligible_segments": len(eligible),
            "candidate_audio_segments": len(candidate_audio),
            "inflight_fallback_segments": len(inflight_fallback_rows),
            "selected_family_counts": dict(sorted(Counter(str(row.get("runtime_selected_family") or "") for row in candidate_audio).items())),
            "audio_selection_counts": dict(sorted(Counter(str(row.get("runtime_audio_selection_mode") or "") for row in eligible_synthesized).items())),
            "fallback_reason_counts": dict(sorted(Counter(str(row.get("runtime_fallback_reason") or "") for row in fallback_rows).items())),
            "fallback_rows": sorted(fallback_rows, key=segment_sort_key),
            "inflight_fallback_rows": sorted(inflight_fallback_rows, key=segment_sort_key),
            "gate_replay_rows": sorted(gate_replay_rows, key=segment_sort_key),
        },
        "drift": {
            "synthesized_count": len(synthesized),
            "mean_abs_drift_ms": round(statistics.fmean(drifts), 3) if drifts else None,
            "max_abs_drift_ms": round(max(drifts), 3) if drifts else None,
            "candidate_audio_mean_abs_drift_ms": round(statistics.fmean(candidate_drifts), 3) if candidate_drifts else None,
            "candidate_audio_max_abs_drift_ms": round(max(candidate_drifts), 3) if candidate_drifts else None,
            "high_drift_count": len(high_drift),
            "review_drift_count": len(review_drift),
            "review_drift_threshold_ms": args.review_drift_ms,
        },
        "agent": {
            "hard_cap_count": len(hard_cap_rows),
            "hard_cap_rows": sorted(hard_cap_rows, key=segment_sort_key),
            "failure_count": len(agent_failure_rows),
            "failure_rows": sorted(agent_failure_rows, key=segment_sort_key),
            "failure_hard_cap_count": len(agent_failure_hard_cap_rows),
            "failure_fit_delta_count": len(agent_failure_fit_rows),
            "max_final_fit_delta": args.max_final_fit_delta,
            "final_fit_delta_count": len(high_final_fit_rows),
            "final_fit_delta_rows": sorted(high_final_fit_rows, key=segment_sort_key),
            "low_judge_threshold": args.low_judge_threshold,
            "low_judge_count": len(low_judge_rows),
            "low_judge_rows": sorted(low_judge_rows, key=segment_sort_key),
        },
        "focus_segments": sorted(focus_rows, key=lambda row: int(row.get("source_segment_id") or 0)),
        "focus_overlap_segments": focus_overlap_rows,
        "risk_queue": risk_queue,
        "validation_issues": validation_issues,
        "updated_at_epoch": time.time(),
    }


def build_focus_overlap_rows(
    compact: list[dict[str, Any]],
    source_segments: list[dict[str, Any]],
    focus_ids: set[int],
) -> list[dict[str, Any]]:
    if not source_segments or not focus_ids:
        return []
    sources: list[dict[str, Any]] = []
    for row in source_segments:
        source_id = int(row.get("id") or row.get("segment_id") or 0)
        if source_id not in focus_ids:
            continue
        start_ms = timeline_start_ms(row)
        end_ms = timeline_end_ms(row)
        if end_ms <= start_ms:
            continue
        sources.append(
            {
                "source_segment_id": source_id,
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
                "source_duration_ms": end_ms - start_ms,
                "source_text": source_text_of(row),
            }
        )

    focused: list[dict[str, Any]] = []
    for source in sources:
        source_start = int(source["source_start_ms"])
        source_end = int(source["source_end_ms"])
        source_duration = max(1, int(source["source_duration_ms"]))
        for row in compact:
            row_start = int(row.get("absolute_start_ms") or 0)
            row_end = int(row.get("absolute_end_ms") or 0)
            overlap = max(0, min(source_end, row_end) - max(source_start, row_start))
            if overlap < 500 and overlap / source_duration < 0.03:
                continue
            item = dict(row)
            item["focus_source_segment_id"] = source["source_segment_id"]
            item["focus_source_start_ms"] = source_start
            item["focus_source_end_ms"] = source_end
            item["focus_source_duration_ms"] = source_duration
            item["focus_overlap_ms"] = overlap
            item["focus_overlap_ratio"] = round(overlap / source_duration, 6)
            item["focus_source_text"] = source["source_text"]
            focused.append(item)
    return sorted(
        focused,
        key=lambda row: (
            int(row.get("focus_source_segment_id") or 0),
            int(row.get("absolute_start_ms") or 0),
            int(row.get("segment_id") or 0),
        ),
    )


def segment_sort_key(row: dict[str, Any]) -> tuple[int, int, int]:
    return (
        int(row.get("chapter_ordinal") or 0),
        int(row.get("ordinal") or 0),
        int(row.get("segment_id") or 0),
    )


def risk_queue_row(row: dict[str, Any], reason: str, priority: int, value: Any = None) -> dict[str, Any]:
    output = {
        "priority": priority,
        "reason": reason,
        "value": value,
        "segment_id": row.get("segment_id"),
        "source_segment_id": row.get("source_segment_id"),
        "chapter_id": row.get("chapter_id"),
        "chapter_ordinal": row.get("chapter_ordinal"),
        "ordinal": row.get("ordinal"),
        "status": row.get("status"),
        "target_ms": row.get("target_ms"),
        "tts_duration_ms": row.get("tts_duration_ms"),
        "drift_ms": row.get("drift_ms"),
        "runtime_selected_family": row.get("runtime_selected_family"),
        "runtime_selected_split_strategy": row.get("runtime_selected_split_strategy"),
        "runtime_fallback_reason": row.get("runtime_fallback_reason"),
        "agent_final_reason": row.get("agent_final_reason"),
        "agent_hard_cap_direction": row.get("agent_hard_cap_direction"),
        "agent_final_abs_drift_ms": row.get("agent_final_abs_drift_ms"),
        "agent_final_implied_fit_delta": row.get("agent_final_implied_fit_delta"),
        "agent_failure_reason": row.get("agent_failure_reason"),
        "agent_failure_error": row.get("agent_failure_error"),
        "agent_failure_hard_cap_direction": row.get("agent_failure_hard_cap_direction"),
        "agent_failure_final_abs_drift_ms": row.get("agent_failure_final_abs_drift_ms"),
        "agent_failure_final_implied_fit_delta": row.get("agent_failure_final_implied_fit_delta"),
        "judge_score": row.get("judge_score"),
        "judge_verdict": row.get("judge_verdict"),
        "judge_issues": row.get("judge_issues"),
        "tts_audio_relpath": row.get("tts_audio_relpath"),
    }
    if row.get("runtime_gate_replay"):
        output["runtime_gate_replay"] = row.get("runtime_gate_replay")
    return output


def build_chapter_progress(chapters: list[dict[str, Any]], compact_segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments_by_job: dict[int, list[dict[str, Any]]] = {}
    for row in compact_segments:
        try:
            job_id = int(row.get("chapter_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        if job_id:
            segments_by_job.setdefault(job_id, []).append(row)
    output: list[dict[str, Any]] = []
    for chapter in sorted(chapters, key=lambda row: (int(row.get("chapter_ordinal") or 0), int(row.get("id") or 0))):
        job_id = int(chapter.get("id") or 0)
        rows = segments_by_job.get(job_id, [])
        statuses = Counter(str(row.get("status") or "") for row in rows)
        synthesized = [row for row in rows if row.get("status") == "synthesized"]
        candidate_audio = sum(1 for row in synthesized if row.get("runtime_audio_selection_mode") == "candidate_audio")
        fallback = sum(1 for row in synthesized if row.get("runtime_fallback_reason"))
        inflight_fallback = sum(
            1
            for row in rows
            if row.get("status") != "synthesized" and row.get("runtime_fallback_reason")
        )
        output.append(
            {
                "job_id": job_id,
                "chapter_ordinal": chapter.get("chapter_ordinal"),
                "status": chapter.get("status"),
                "current_stage": chapter.get("current_stage"),
                "segments_total": len(rows),
                "segments_synthesized": statuses.get("synthesized", 0),
                "segments_skipped": statuses.get("skipped", 0),
                "segments_remaining": len(rows) - statuses.get("synthesized", 0) - statuses.get("skipped", 0),
                "segments_by_status": dict(sorted(statuses.items())),
                "candidate_audio_segments": candidate_audio,
                "candidate_fallback_segments": fallback,
                "inflight_fallback_segments": inflight_fallback,
            }
        )
    return output


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def append_history(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "updated_at_epoch": summary.get("updated_at_epoch"),
        "progress": summary.get("progress"),
        "candidate": summary.get("candidate"),
        "drift": summary.get("drift"),
        "agent": summary.get("agent"),
        "validation_issue_count": len(summary.get("validation_issues") or []),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")


def is_done(summary: dict[str, Any]) -> bool:
    progress = summary.get("progress") if isinstance(summary.get("progress"), dict) else {}
    remaining = int(progress.get("segments_remaining") or 0)
    chapter_counts = progress.get("chapters_by_status") if isinstance(progress.get("chapters_by_status"), dict) else {}
    active_or_pending = sum(int(chapter_counts.get(status) or 0) for status in ("queued", "running", "pending"))
    return remaining == 0 and active_or_pending == 0


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    deadline = time.time() + args.timeout_sec if args.timeout_sec > 0 else None
    last_summary: dict[str, Any] | None = None
    while True:
        lineage_backfill_result = backfill_lineage(args)
        episode, chapters, segments = load_episode(args.api_base, args.episode_id, args.api_key)
        source_segments: list[dict[str, Any]] = []
        if args.source_episode_id:
            try:
                _, _, source_segments = load_episode(args.api_base, args.source_episode_id, args.api_key)
            except Exception as exc:  # noqa: BLE001
                source_segments = []
                if last_summary is None:
                    last_summary = {}
                last_summary["source_episode_load_error"] = str(exc)
        last_summary = summarize(episode, chapters, segments, args=args, source_segments=source_segments)
        if lineage_backfill_result:
            last_summary["lineage_backfill"] = {
                "source_episode_id": args.source_episode_id,
                "shadow_episode_id": args.episode_id,
                "result": lineage_backfill_result,
            }
        write_summary(output, last_summary)
        if args.history_jsonl:
            append_history(Path(args.history_jsonl), last_summary)
        print(json.dumps(last_summary["progress"], ensure_ascii=False, sort_keys=True))
        if not args.watch or is_done(last_summary):
            break
        if deadline and time.time() >= deadline:
            break
        time.sleep(max(1.0, args.poll_sec))
    return 0 if last_summary and not last_summary.get("validation_issues") else 1


if __name__ == "__main__":
    raise SystemExit(main())
