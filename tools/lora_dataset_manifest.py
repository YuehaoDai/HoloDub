#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "training" / "lora_corpus"
SCHEMA_VERSION = "holodub.indextts2_lora_manifest.v1"
HARD_RUNTIME_FAILURE_REASONS = {
    "bad_join_quality",
    "high_drift",
    "high_fit_delta",
    "low_asr",
    "low_term_recall",
    "tail_truncation",
}


@dataclass
class Stats:
    accepted: int = 0
    rejected: int = 0
    missing_audio: int = 0
    total_audio_ms: int = 0
    train: int = 0
    val: int = 0
    test: int = 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a HoloDub IndexTTS2 LoRA dataset manifest."
    )
    parser.add_argument(
        "--episode-ids",
        default="",
        help="Comma-separated episode ids. Empty means all synthesized segments.",
    )
    parser.add_argument("--pg-container", default="holodub-postgres-1")
    parser.add_argument("--data-root", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--run-name", default="")
    parser.add_argument("--max-samples", type=int, default=0)
    parser.add_argument("--min-judge-score", type=float, default=0.98)
    parser.add_argument("--min-duration-ms", type=int, default=1200)
    parser.add_argument("--max-duration-ms", type=int, default=45_000)
    parser.add_argument("--min-chars", type=int, default=4)
    parser.add_argument("--max-chars", type=int, default=220)
    parser.add_argument("--max-abs-drift-ratio", type=float, default=0.18)
    parser.add_argument("--max-abs-drift-ms", type=int, default=3500)
    parser.add_argument("--min-asr-similarity", type=float, default=0.0)
    parser.add_argument("--min-term-recall", type=float, default=0.0)
    parser.add_argument(
        "--asr-jsonl",
        action="append",
        default=[],
        help="Optional results.asr.jsonl files to merge by segment_id.",
    )
    parser.add_argument(
        "--fit-audit-json",
        action="append",
        default=[],
        help="Optional mit6824_fit_audit JSON files. Risk segments above --max-fit-tempo-delta are rejected.",
    )
    parser.add_argument("--max-fit-tempo-delta", type=float, default=0.0)
    parser.add_argument(
        "--exclude-segment-ids",
        default="",
        help="Comma/newline-separated segment ids, e.g. 15916,seg15964.",
    )
    parser.add_argument(
        "--exclude-segment-ids-file",
        action="append",
        default=[],
        help="Optional files containing comma/newline-separated segment ids.",
    )
    parser.add_argument(
        "--max-agent-final-fit-delta",
        type=float,
        default=0.0,
        help="Reject rows whose persisted SegmentAgent final_implied_fit_delta exceeds this value.",
    )
    parser.add_argument(
        "--max-final-fit-delta",
        type=float,
        default=0.0,
        help="Reject rows whose persisted final TTS fit audit implied delta exceeds this value.",
    )
    parser.add_argument(
        "--reject-runtime-hard-failures",
        action="store_true",
        help="Reject rows whose selected LoRA runtime or SegmentAgent metadata contains hard failure evidence.",
    )
    parser.add_argument("--val-ratio", type=float, default=0.05)
    parser.add_argument("--test-ratio", type=float, default=0.05)
    parser.add_argument("--seed", default="holodub-lora-v1")
    parser.add_argument(
        "--tier",
        choices=("silver", "gold"),
        default="silver",
        help="gold should only be used for human-recorded or human-approved audio.",
    )
    args = parser.parse_args()

    rows = load_segments(args.pg_container, parse_episode_ids(args.episode_ids))
    asr_by_segment = load_asr_rows(args.asr_jsonl)
    fit_by_segment = load_fit_audit_rows(args.fit_audit_json)
    excluded_segment_ids = load_excluded_segment_ids(
        args.exclude_segment_ids,
        args.exclude_segment_ids_file,
    )

    run_name = args.run_name or datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = args.out_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    stats = Stats()

    for row in rows:
        record = build_record(
            row,
            args.data_root,
            args.tier,
            asr_by_segment.get(int(row["id"])),
            fit_by_segment.get(int(row["id"])),
        )
        reasons = rejection_reasons(record, args, excluded_segment_ids)
        if reasons:
            record["rejection_reasons"] = reasons
            rejected.append(record)
            stats.rejected += 1
            if "missing_audio" in reasons:
                stats.missing_audio += 1
            continue
        record["split"] = assign_split(
            str(record["segment_id"]),
            args.seed,
            val_ratio=args.val_ratio,
            test_ratio=args.test_ratio,
        )
        accepted.append(record)
        stats.accepted += 1
        stats.total_audio_ms += int(record["audio_ms"])
        if record["split"] == "train":
            stats.train += 1
        elif record["split"] == "val":
            stats.val += 1
        else:
            stats.test += 1
        if args.max_samples > 0 and len(accepted) >= args.max_samples:
            break

    write_jsonl(run_dir / "all.jsonl", accepted)
    write_jsonl(run_dir / "train.jsonl", [r for r in accepted if r["split"] == "train"])
    write_jsonl(run_dir / "val.jsonl", [r for r in accepted if r["split"] == "val"])
    write_jsonl(run_dir / "test.jsonl", [r for r in accepted if r["split"] == "test"])
    write_jsonl(run_dir / "rejected.jsonl", rejected)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "tier": args.tier,
        "episode_ids": parse_episode_ids(args.episode_ids),
        "filters": {
            "min_judge_score": args.min_judge_score,
            "min_duration_ms": args.min_duration_ms,
            "max_duration_ms": args.max_duration_ms,
            "min_chars": args.min_chars,
            "max_chars": args.max_chars,
            "max_abs_drift_ratio": args.max_abs_drift_ratio,
            "max_abs_drift_ms": args.max_abs_drift_ms,
            "min_asr_similarity": args.min_asr_similarity,
            "min_term_recall": args.min_term_recall,
            "max_fit_tempo_delta": args.max_fit_tempo_delta,
            "exclude_segment_ids": sorted(excluded_segment_ids),
            "max_agent_final_fit_delta": args.max_agent_final_fit_delta,
            "reject_runtime_hard_failures": args.reject_runtime_hard_failures,
        },
        "stats": {
            "accepted": stats.accepted,
            "rejected": stats.rejected,
            "missing_audio": stats.missing_audio,
            "total_audio_hours": round(stats.total_audio_ms / 3_600_000, 4),
            "train": stats.train,
            "val": stats.val,
            "test": stats.test,
        },
        "files": {
            "all": "all.jsonl",
            "train": "train.jsonl",
            "val": "val.jsonl",
            "test": "test.jsonl",
            "rejected": "rejected.jsonl",
        },
        "warnings": dataset_warnings(args.tier, stats),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (run_dir / "README.md").write_text(render_readme(manifest), encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), **manifest["stats"]}, ensure_ascii=False, indent=2))
    return 0


def parse_episode_ids(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def load_excluded_segment_ids(raw: str, files: list[str]) -> set[int]:
    ids = set(parse_segment_id_tokens(raw))
    for file in files:
        path = Path(file)
        if not path.exists():
            raise FileNotFoundError(path)
        ids.update(parse_segment_id_tokens(path.read_text(encoding="utf-8")))
    return ids


def parse_segment_id_tokens(raw: str) -> list[int]:
    out: list[int] = []
    for token in raw.replace(";", ",").replace("\n", ",").split(","):
        value = token.strip()
        if not value:
            continue
        if value.lower().startswith("seg"):
            value = value[3:]
        if value.isdigit():
            out.append(int(value))
    return out


def load_segments(pg_container: str, episode_ids: list[int]) -> list[dict[str, str]]:
    episode_filter = ""
    if episode_ids:
        ids = ",".join(str(int(x)) for x in episode_ids)
        episode_filter = f"and j.episode_id in ({ids})"
    query = f"""
select
  s.id,
  s.job_id,
  j.episode_id,
  s.ordinal,
  coalesce(s.speaker_label, '') as speaker_label,
  coalesce(s.voice_profile_id, 0) as voice_profile_id,
  coalesce(vp.sample_rel_paths::text, '[]') as voice_sample_relpaths,
  s.start_ms,
  s.end_ms,
  s.end_ms - s.start_ms as target_ms,
  coalesce(s.tts_duration_ms, 0) as audio_ms,
  coalesce(s.tts_audio_rel_path, '') as audio_relpath,
  coalesce(s.source_text, '') as source_text,
  coalesce(s.target_text, '') as target_text,
  coalesce(s.meta::text, '{{}}') as meta,
  coalesce(s.judge_score, 0) as judge_score,
  coalesce(s.judge_meta::text, '{{}}') as judge_meta
from segments s
join jobs j on j.id = s.job_id
left join voice_profiles vp on vp.id = s.voice_profile_id
where s.status = 'synthesized'
  and coalesce(s.tts_audio_rel_path, '') <> ''
  and coalesce(s.target_text, '') <> ''
  {episode_filter}
order by j.episode_id, s.job_id, s.ordinal;
"""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            pg_container,
            "psql",
            "-U",
            "holodub",
            "-d",
            "holodub",
            "--csv",
            "-c",
            query,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return list(csv.DictReader(proc.stdout.splitlines()))


def load_asr_rows(paths: list[str]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        rows: list[dict[str, Any]]
        if path.suffix.lower() == ".jsonl":
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        else:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload if isinstance(payload, list) else payload.get("results") or payload.get("rows") or []
        for row in rows:
            sid = int(row.get("segment_id") or row.get("id") or 0)
            if sid <= 0:
                continue
            similarity = row.get("asr_similarity")
            if similarity is None:
                similarity = row.get("similarity")
            if similarity is None and row.get("ok") is True:
                similarity = 1.0
            normalized = dict(row)
            normalized["asr_similarity"] = float(similarity or 0)
            previous = out.get(sid)
            if previous is None or float(normalized.get("asr_similarity") or 0) > float(previous.get("asr_similarity") or 0):
                out[sid] = normalized
    return out


def load_fit_audit_rows(paths: list[str]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("risk_segments") if isinstance(payload, dict) else []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            sid = int(row.get("segment_id") or 0)
            if sid <= 0:
                continue
            previous = out.get(sid)
            if previous is None or float(row.get("fit_tempo_delta") or 0) > float(previous.get("fit_tempo_delta") or 0):
                out[sid] = row
    return out


def build_record(
    row: dict[str, str],
    data_root: Path,
    tier: str,
    asr_row: dict[str, Any] | None,
    fit_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = parse_json(row.get("meta"), {})
    judge_meta = parse_json(row.get("judge_meta"), {})
    voice_samples = parse_json(row.get("voice_sample_relpaths"), [])
    tts_agent = meta.get("tts_agent") if isinstance(meta.get("tts_agent"), dict) else {}
    final_fit_audit = meta.get("tts_final_fit_audit") if isinstance(meta.get("tts_final_fit_audit"), dict) else {}
    candidate_runtime = extract_candidate_runtime(meta)
    candidate_gate = candidate_runtime.get("gate_decision") if isinstance(candidate_runtime.get("gate_decision"), dict) else {}
    selected_candidate = extract_selected_candidate(candidate_runtime)
    selected_join_quality = selected_candidate.get("join_quality")
    selected_join_quality_pass: bool | None = None
    if isinstance(selected_join_quality, dict):
        selected_join_quality_pass = bool(selected_join_quality.get("pass"))
    runtime_reject_reasons = sorted(
        {
            str(reason)
            for reason in list_from_any(selected_candidate.get("reject_reasons"))
            + list_from_any(candidate_gate.get("reject_reasons"))
            if str(reason)
        }
    )
    asr_expected = str((asr_row or {}).get("expected_text") or (asr_row or {}).get("target_text") or "").strip()
    text = str(asr_expected or meta.get("tts_spoken_text") or row.get("target_text") or "").strip()
    if asr_expected:
        text_source = "final_mix_asr_expected_text"
    else:
        text_source = "tts_spoken_text" if meta.get("tts_spoken_text") else "target_text"
    target_ms = int(row["target_ms"])
    audio_ms = int(row["audio_ms"])
    audio_relpath = row["audio_relpath"].replace("\\", "/")
    audio_path = data_root / audio_relpath
    chars = speakable_len(text)
    drift_ms = audio_ms - target_ms
    record = {
        "schema_version": SCHEMA_VERSION,
        "tier": tier,
        "sample_id": f"seg{row['id']}",
        "segment_id": int(row["id"]),
        "episode_id": int(row["episode_id"]),
        "job_id": int(row["job_id"]),
        "ordinal": int(row["ordinal"]),
        "speaker_label": row.get("speaker_label") or "",
        "voice_profile_id": int(row.get("voice_profile_id") or 0),
        "voice_sample_relpaths": voice_samples if isinstance(voice_samples, list) else [],
        "text": text,
        "text_source": text_source,
        "source_text": row.get("source_text") or "",
        "audio_relpath": audio_relpath,
        "audio_path": str(audio_path.resolve()),
        "audio_exists": audio_path.exists(),
        "target_ms": target_ms,
        "audio_ms": audio_ms,
        "drift_ms": drift_ms,
        "abs_drift_ms": abs(drift_ms),
        "drift_ratio": round(abs(drift_ms) / max(1, target_ms), 6),
        "chars": chars,
        "target_chars_per_sec": round(chars / max(0.001, target_ms / 1000.0), 4),
        "audio_chars_per_sec": round(chars / max(0.001, audio_ms / 1000.0), 4),
        "judge_score": float(row.get("judge_score") or 0),
        "judge_meta": judge_meta,
        "asr_similarity": float((asr_row or {}).get("asr_similarity") or 0),
        "critical_term_recall": float((asr_row or {}).get("critical_term_recall") or 0),
        "fit_tempo_delta": float((fit_row or {}).get("fit_tempo_delta") or 0),
        "fit_applied": bool((fit_row or {}).get("fit_applied")) if fit_row else False,
        "agent_final_implied_fit_delta": as_float(tts_agent.get("final_implied_fit_delta"), 0.0),
        "final_fit_audit_pass": final_fit_audit.get("pass") if "pass" in final_fit_audit else None,
        "final_fit_audit_status": str(final_fit_audit.get("status") or ""),
        "final_fit_audit_reject_reason": str(final_fit_audit.get("reject_reason") or ""),
        "final_fit_audit_implied_fit_delta": as_float(final_fit_audit.get("final_implied_fit_delta"), 0.0),
        "agent_hard_cap_exceeded": bool(tts_agent.get("hard_cap_exceeded")),
        "lora_selected_family": str(
            candidate_runtime.get("selected_family")
            or candidate_gate.get("selected_family")
            or tts_agent.get("lora_selected_family")
            or ""
        ),
        "lora_audio_selection_mode": str(
            candidate_runtime.get("audio_selection_mode")
            or tts_agent.get("lora_audio_selection_mode")
            or ""
        ),
        "runtime_gate_status": str(candidate_gate.get("status") or ""),
        "runtime_selected_fit_tempo_delta": as_float(selected_candidate.get("fit_tempo_delta"), 0.0),
        "runtime_selected_similarity": as_float(selected_candidate.get("similarity"), 0.0),
        "runtime_selected_reject_reasons": runtime_reject_reasons,
        "runtime_fit_reject_reason": str(selected_candidate.get("fit_reject_reason") or ""),
        "runtime_join_quality_pass": selected_join_quality_pass,
        "duration_targets": {
            "tokens_at_23_4375": round((target_ms / 1000.0) * 23.4375),
            "tokens_at_50": round((target_ms / 1000.0) * 50.0),
        },
        "training_policy": {
            "use": "pilot_only" if tier == "silver" else "train",
            "risk": "synthetic_teacher_audio" if tier == "silver" else "",
        },
    }
    if asr_row:
        record["asr_text"] = asr_row.get("asr_text") or ""
        record["critical_terms"] = asr_row.get("critical_terms") or []
        record["source_text"] = asr_row.get("source_text") or record["source_text"]
    if fit_row:
        record["fit_audit"] = {
            "label": fit_row.get("label"),
            "family": fit_row.get("family"),
            "audio_relpath": fit_row.get("audio_relpath"),
            "pre_fit_drift_ms": fit_row.get("pre_fit_drift_ms"),
            "post_fit_drift_ms": fit_row.get("post_fit_drift_ms"),
        }
    if final_fit_audit:
        record["final_fit_audit"] = final_fit_audit
    return record


def rejection_reasons(
    record: dict[str, Any],
    args: argparse.Namespace,
    excluded_segment_ids: set[int],
) -> list[str]:
    reasons: list[str] = []
    if int(record["segment_id"]) in excluded_segment_ids:
        reasons.append("excluded_segment_id")
    if not record["audio_exists"]:
        reasons.append("missing_audio")
    if int(record["audio_ms"]) <= 0:
        reasons.append("missing_audio_duration")
    if int(record["audio_ms"]) < args.min_duration_ms:
        reasons.append("too_short_audio")
    if int(record["audio_ms"]) > args.max_duration_ms:
        reasons.append("too_long_audio")
    if int(record["chars"]) < args.min_chars:
        reasons.append("too_few_chars")
    if int(record["chars"]) > args.max_chars:
        reasons.append("too_many_chars")
    if float(record["judge_score"]) < args.min_judge_score:
        reasons.append("low_judge_score")
    if int(record["abs_drift_ms"]) > args.max_abs_drift_ms:
        reasons.append("high_abs_drift_ms")
    if float(record["drift_ratio"]) > args.max_abs_drift_ratio:
        reasons.append("high_drift_ratio")
    if args.min_asr_similarity > 0 and float(record["asr_similarity"]) < args.min_asr_similarity:
        reasons.append("low_asr_similarity")
    if args.min_term_recall > 0 and float(record["critical_term_recall"]) < args.min_term_recall:
        reasons.append("low_term_recall")
    if args.max_fit_tempo_delta > 0 and float(record.get("fit_tempo_delta") or 0) > args.max_fit_tempo_delta:
        reasons.append("high_fit_tempo_delta")
    if (
        args.max_agent_final_fit_delta > 0
        and float(record.get("agent_final_implied_fit_delta") or 0) > args.max_agent_final_fit_delta
    ):
        reasons.append("high_agent_final_fit_delta")
    if record.get("final_fit_audit_pass") is False:
        reasons.append("final_fit_audit_failed")
    max_final_fit_delta = getattr(args, "max_final_fit_delta", 0.0) or args.max_agent_final_fit_delta
    if (
        max_final_fit_delta > 0
        and float(record.get("final_fit_audit_implied_fit_delta") or 0) > max_final_fit_delta
    ):
        reasons.append("high_final_fit_audit_delta")
    if args.reject_runtime_hard_failures and runtime_has_hard_failure(record):
        reasons.append("runtime_hard_failure")
    return reasons


def runtime_has_hard_failure(record: dict[str, Any]) -> bool:
    if bool(record.get("agent_hard_cap_exceeded")):
        return True
    if str(record.get("runtime_fit_reject_reason") or ""):
        return True
    if record.get("runtime_join_quality_pass") is False:
        return True
    reasons = {str(reason) for reason in list_from_any(record.get("runtime_selected_reject_reasons"))}
    return bool(reasons & HARD_RUNTIME_FAILURE_REASONS)


def extract_candidate_runtime(meta: dict[str, Any]) -> dict[str, Any]:
    direct = meta.get("tts_lora_candidate_runtime")
    if isinstance(direct, dict):
        return direct
    candidate = meta.get("tts_lora_candidate")
    if isinstance(candidate, dict):
        runtime = candidate.get("runtime")
        if isinstance(runtime, dict):
            return runtime
    return {}


def extract_selected_candidate(runtime: dict[str, Any]) -> dict[str, Any]:
    selected = runtime.get("selected_candidate")
    if isinstance(selected, dict):
        return selected
    selected_family = str(
        runtime.get("selected_family")
        or (runtime.get("gate_decision") or {}).get("selected_family")
        or ""
    )
    candidates = runtime.get("candidates")
    if isinstance(candidates, list):
        for candidate in candidates:
            if isinstance(candidate, dict) and selected_family and candidate.get("family") == selected_family:
                return candidate
    return {}


def list_from_any(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def assign_split(sample_id: str, seed: str, *, val_ratio: float, test_ratio: float) -> str:
    digest = hashlib.sha256(f"{seed}:{sample_id}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    if bucket < test_ratio:
        return "test"
    if bucket < test_ratio + val_ratio:
        return "val"
    return "train"


def parse_json(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def speakable_len(text: str) -> int:
    return len([c for c in text if not c.isspace()])


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def dataset_warnings(tier: str, stats: Stats) -> list[str]:
    warnings: list[str] = []
    if tier == "silver":
        warnings.append(
            "silver data is synthetic TTS output; use for LoRA smoke tests or duration calibration only"
        )
    if stats.accepted == 0:
        warnings.append("no accepted samples; relax filters or provide gold data")
    if stats.total_audio_ms < 30 * 60 * 1000:
        warnings.append("accepted audio is under 30 minutes; too small for meaningful LoRA quality claims")
    return warnings


def render_readme(manifest: dict[str, Any]) -> str:
    stats = manifest["stats"]
    return f"""# HoloDub IndexTTS2 LoRA Corpus

Schema: `{manifest["schema_version"]}`

Tier: `{manifest["tier"]}`

Accepted samples: `{stats["accepted"]}`

Accepted audio hours: `{stats["total_audio_hours"]}`

Splits: train `{stats["train"]}`, val `{stats["val"]}`, test `{stats["test"]}`

Warnings:

{chr(10).join('- ' + w for w in manifest["warnings"]) or '- none'}

This manifest is an input contract for the LoRA pilot. Silver data should not be
used as proof of final quality because it is generated by the current TTS model.
Gold data should be human-recorded or explicitly human-approved.
"""


if __name__ == "__main__":
    sys.exit(main())
