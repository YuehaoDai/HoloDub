#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ml-app.yml", "docker-compose.lora-shadow.yml"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a Lecture-level LoRA repair/training queue from live episode candidate runtime."
    )
    parser.add_argument("--episode-id", type=int, required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", default="")
    parser.add_argument("--max-fit-delta", type=float, default=0.05)
    parser.add_argument("--postgres-service", default="postgres")
    parser.add_argument("--postgres-user", default="holodub")
    parser.add_argument("--postgres-db", default="holodub")
    parser.add_argument("--compose-file", action="append", default=[])
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def compose_command(args: argparse.Namespace, extra: list[str]) -> list[str]:
    cmd = ["docker", "compose"]
    for file in args.compose_file or DEFAULT_COMPOSE_FILES:
        cmd.extend(["-f", file])
    cmd.extend(extra)
    return cmd


def run_psql_json(args: argparse.Namespace, root: Path, sql: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        compose_command(
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
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
        ),
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"psql failed:\n{proc.stderr}\n{proc.stdout}")
    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def selected_candidate(runtime: dict[str, Any]) -> dict[str, Any]:
    selected = as_dict(runtime.get("selected_candidate"))
    if selected:
        return selected
    family = str(runtime.get("selected_family") or "")
    strategy = runtime.get("selected_split_strategy")
    fallback: dict[str, Any] = {}
    for row in as_list(runtime.get("candidates")):
        cand = as_dict(row)
        if str(cand.get("family") or "") != family:
            continue
        if cand.get("split_strategy") == strategy:
            return cand
        if not fallback:
            fallback = cand
    return fallback


def collect_reasons(row: dict[str, Any], max_fit_delta: float) -> list[str]:
    reasons: list[str] = []
    runtime = as_dict(row.get("runtime"))
    gate = as_dict(runtime.get("gate_decision"))
    selected = selected_candidate(runtime)
    agent = as_dict(row.get("agent"))
    agent_failure = as_dict(row.get("agent_failure")) if not agent.get("final_reason") else {}

    for reason in as_list(gate.get("reject_reasons")):
        reasons.append(str(reason))
    for reason in as_list(selected.get("reject_reasons")):
        reasons.append(str(reason))
    if gate.get("needs_text_compression") is True:
        reasons.append("needs_text_compression")
    stop_reason = str(runtime.get("candidate_generation_stop_reason") or runtime.get("stop_reason") or "")
    if "needs_text_compression" in stop_reason:
        reasons.append(stop_reason)
    if as_float(selected.get("fit_tempo_delta")) > max_fit_delta:
        reasons.append("high_fit_delta")
    if selected.get("fit_reject_reason") == "high_fit_delta":
        reasons.append("high_fit_delta")
    if agent.get("hard_cap_exceeded") is True:
        reasons.append("hard_cap_exceeded")
    if as_float(agent.get("final_implied_fit_delta")) > max_fit_delta:
        reasons.append("high_final_implied_fit_delta")
    if agent_failure.get("hard_cap_exceeded") is True:
        reasons.append("hard_cap_rejected")
    if as_float(agent_failure.get("final_implied_fit_delta")) > max_fit_delta:
        reasons.append("high_final_implied_fit_delta_rejected")
    return list(dict.fromkeys(reason for reason in reasons if reason))


def choose_action(reasons: list[str]) -> str:
    reason_set = set(reasons)
    if "needs_text_compression" in reason_set or any(r.startswith("needs_text_compression") for r in reason_set):
        return "text_compression_before_tts"
    if "bad_join_quality" in reason_set:
        return "join_boundary_repair"
    if "low_asr" in reason_set or "low_term_recall" in reason_set:
        return "text_completeness_repair"
    if (
        "high_fit_delta" in reason_set
        or "high_final_implied_fit_delta" in reason_set
        or "high_final_implied_fit_delta_rejected" in reason_set
    ):
        return "speech_rate_feasibility_or_training"
    if "hard_cap_exceeded" in reason_set or "hard_cap_rejected" in reason_set:
        return "duration_policy_repair"
    return "manual_review"


def build_payload(args: argparse.Namespace, rows: list[dict[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        runtime = as_dict(row.get("runtime"))
        agent = as_dict(row.get("agent"))
        agent_failure = as_dict(row.get("agent_failure")) if not agent.get("final_reason") else {}
        selected = selected_candidate(runtime)
        reasons = collect_reasons(row, args.max_fit_delta)
        if not reasons:
            continue
        action = choose_action(reasons)
        entries.append(
            {
                "segment_id": row.get("segment_id"),
                "job_id": row.get("job_id"),
                "chapter_ordinal": row.get("chapter_ordinal"),
                "ordinal": row.get("ordinal"),
                "status": row.get("status"),
                "target_ms": row.get("target_ms"),
                "tts_duration_ms": row.get("tts_duration_ms"),
                "action": action,
                "reasons": reasons,
                "training_readiness": {
                    "source_tier": "live_shadow_failure",
                    "ready_for_lora_training": False,
                    "required_before_training": [
                        "repair_text_or_split_plan",
                        "regenerate_audio_under_5pct_fit_gate",
                        "pass_asr_term_join_tail_gates",
                        "human_accept_or_mark_silver",
                    ],
                },
                "candidate": {
                    "audio_selection_mode": runtime.get("audio_selection_mode"),
                    "selected_family": runtime.get("selected_family"),
                    "fallback_reason": runtime.get("fallback_reason"),
                    "candidate_generation_stop_reason": runtime.get("candidate_generation_stop_reason"),
                    "gate_decision": runtime.get("gate_decision"),
                    "selected_candidate": {
                        "family": selected.get("family"),
                        "split_strategy": selected.get("split_strategy"),
                        "fit_tempo_delta": selected.get("fit_tempo_delta"),
                        "fit_reject_reason": selected.get("fit_reject_reason"),
                        "abs_drift_ms": selected.get("abs_drift_ms"),
                        "similarity": selected.get("similarity"),
                        "critical_term_recall": selected.get("critical_term_recall"),
                        "reject_reasons": selected.get("reject_reasons"),
                    },
                },
                "agent": {
                    "final_reason": agent.get("final_reason"),
                    "final_abs_drift_ms": agent.get("final_abs_drift_ms"),
                    "final_implied_fit_delta": agent.get("final_implied_fit_delta"),
                    "hard_cap_exceeded": agent.get("hard_cap_exceeded"),
                },
                "agent_failure": {
                    "error": agent_failure.get("error"),
                    "final_reason": agent_failure.get("final_reason"),
                    "final_abs_drift_ms": agent_failure.get("final_abs_drift_ms"),
                    "final_implied_fit_delta": agent_failure.get("final_implied_fit_delta"),
                    "hard_cap_exceeded": agent_failure.get("hard_cap_exceeded"),
                    "hard_cap_direction": agent_failure.get("hard_cap_direction"),
                    "output_not_persisted": agent_failure.get("output_not_persisted"),
                },
                "source_text": row.get("source_text"),
                "target_text": row.get("target_text"),
            }
        )

    by_action = Counter(str(entry["action"]) for entry in entries)
    by_reason = Counter(reason for entry in entries for reason in entry["reasons"])
    return {
        "schema_version": "holodub.mit6824_lora_episode_training_queue.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "episode_id": args.episode_id,
        "policy": {
            "max_fit_delta": args.max_fit_delta,
            "lecture7_gold_filter": {
                "judge_score_min": 0.8,
                "asr_similarity_min": 0.97,
                "drift_ms_max": 500,
                "drift_ratio_max": 0.05,
                "fit_tempo_delta_max": 0.05,
                "text_chars_max": 220,
            },
            "failed_shadow_audio_is_not_gold": True,
        },
        "summary": {
            "rows_scanned": len(rows),
            "entries": len(entries),
            "by_action": dict(sorted(by_action.items())),
            "by_reason": dict(sorted(by_reason.items())),
        },
        "entries": entries,
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    lines = [
        "# MIT 6.824 LoRA Episode Training Queue",
        "",
        f"Generated: `{payload['generated_at']}`",
        f"Episode: `{payload['episode_id']}`",
        "",
        "## Summary",
        "",
        f"- Rows scanned: `{summary['rows_scanned']}`",
        f"- Entries: `{summary['entries']}`",
        f"- By action: `{summary['by_action']}`",
        f"- By reason: `{summary['by_reason']}`",
        "",
        "## Entries",
        "",
        "| Segment | Ch | Ord | Status | Action | Reasons | Fit | ASR |",
        "| ---: | ---: | ---: | --- | --- | --- | ---: | ---: |",
    ]
    for entry in payload["entries"]:
        selected = ((entry.get("candidate") or {}).get("selected_candidate") or {})
        lines.append(
            f"| {entry.get('segment_id')} | {entry.get('chapter_ordinal')} | {entry.get('ordinal')} | "
            f"{entry.get('status')} | {entry.get('action')} | {', '.join(entry.get('reasons') or [])} | "
            f"{selected.get('fit_tempo_delta')} | {selected.get('similarity')} |"
        )
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    sql = f"""
select row_to_json(row) from (
  select
    s.id as segment_id,
    s.job_id,
    j.chapter_ordinal,
    s.ordinal,
    s.status,
    s.end_ms - s.start_ms as target_ms,
    s.tts_duration_ms,
    coalesce(s.source_text, '') as source_text,
    coalesce(s.target_text, '') as target_text,
    coalesce(s.meta->'tts_lora_candidate_runtime', '{{}}'::jsonb) as runtime,
    coalesce(s.meta->'tts_agent', '{{}}'::jsonb) as agent,
    coalesce(s.meta->'tts_agent_failure', '{{}}'::jsonb) as agent_failure
  from segments s
  join jobs j on j.id = s.job_id
  where j.episode_id = {int(args.episode_id)}
    and (
      s.meta ? 'tts_lora_candidate_runtime'
      or s.meta ? 'tts_agent'
      or s.meta ? 'tts_agent_failure'
      or s.status <> 'translated'
    )
  order by j.chapter_ordinal, s.ordinal
) row;
"""
    rows = run_psql_json(args, root, sql)
    payload = build_payload(args, rows)
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output_md:
        write_markdown(Path(args.output_md), payload)
    print(json.dumps({"output_json": str(output), "summary": payload["summary"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
