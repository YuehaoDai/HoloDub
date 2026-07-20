from __future__ import annotations

import argparse
import html
import json
import re
import statistics
import subprocess
import wave
from collections import Counter
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a full-episode LoRA assembly against objective gates and shadow-contract invariants."
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--context-pre-sec", type=float, default=1.5)
    parser.add_argument("--context-post-sec", type=float, default=1.5)
    parser.add_argument("--max-final-audio-drift-ms", type=float, default=50.0)
    parser.add_argument("--max-final-video-drift-ms", type=float, default=100.0)
    parser.add_argument("--max-lora-drift-ms", type=float, default=300.0)
    parser.add_argument("--target-lora-mean-drift-ms", type=float, default=50.0)
    parser.add_argument("--min-lora-mean-asr", type=float, default=0.99)
    parser.add_argument("--min-lora-min-asr", type=float, default=0.96)
    parser.add_argument("--max-join-quality-failures", type=int, default=0)
    parser.add_argument("--max-tempo-delta-without-review", type=float, default=0.05)
    parser.add_argument("--max-clip-ms-without-review", type=float, default=200.0)
    parser.add_argument("--top-risk", type=int, default=20)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_path(repo_root: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    return repo_root / path


def wav_duration_ms(path: Path) -> int | None:
    try:
        with wave.open(str(path), "rb") as handle:
            return int(round(handle.getnframes() * 1000.0 / float(handle.getframerate())))
    except (OSError, wave.Error, EOFError):
        return None


def media_duration_ms(ffprobe: str, path: Path) -> int | None:
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(round(float(proc.stdout.strip()) * 1000.0))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def metric_value(summary: dict[str, Any], path: list[str], default: Any = None) -> Any:
    current: Any = summary
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def gate(name: str, passed: bool, value: Any, threshold: Any, severity: str = "fail") -> dict[str, Any]:
    return {
        "name": name,
        "pass": bool(passed),
        "value": value,
        "threshold": threshold,
        "severity": "pass" if passed else severity,
    }


def rows_from_lineage(run_dir: Path) -> list[dict[str, Any]]:
    lineage = read_json(run_dir / "episode_segment_lineage.json")
    rows = lineage.get("segments")
    return rows if isinstance(rows, list) else []


def lora_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("source_kind") == "lora_selected"]


def fallback_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("source_kind") == "fallback_existing_tts"]


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def count_bad_lora_rows(rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    high_drift = [r for r in rows if float(r.get("abs_drift_ms") or 0.0) >= args.max_lora_drift_ms]
    low_asr = [
        r for r in rows
        if r.get("similarity") is not None and float(r.get("similarity") or 0.0) < args.min_lora_min_asr
    ]
    bad_join = [r for r in rows if r.get("join_quality_pass") is False]
    low_term = [
        r for r in rows
        if r.get("critical_term_recall") is not None and float(r.get("critical_term_recall") or 0.0) < 0.8
    ]
    tempo_review = [
        r for r in rows
        if r.get("fit_tempo_delta") is not None
        and float(r.get("fit_tempo_delta") or 0.0) > args.max_tempo_delta_without_review
    ]
    clip_review = [
        r for r in rows
        if float(r.get("clipped_ms") or 0.0) > args.max_clip_ms_without_review
    ]
    return {
        "high_drift": compact_rows(high_drift),
        "low_asr": compact_rows(low_asr),
        "bad_join_quality": compact_rows(bad_join),
        "low_term_recall": compact_rows(low_term),
        "tempo_review_required": compact_rows(tempo_review),
        "clip_review_required": compact_rows(clip_review),
    }


def compact_rows(rows: list[dict[str, Any]], limit: int = 40) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows[:limit]:
        out.append(
            {
                "sample_id": row.get("sample_id"),
                "chapter_ordinal": row.get("chapter_ordinal"),
                "ordinal": row.get("ordinal"),
                "source_kind": row.get("source_kind"),
                "candidate_label": row.get("candidate_label"),
                "candidate_label_chain": row.get("candidate_label_chain"),
                "target_ms": row.get("target_ms"),
                "abs_drift_ms": row.get("abs_drift_ms"),
                "similarity": row.get("similarity"),
                "critical_term_recall": row.get("critical_term_recall"),
                "f0_range_semitones_10_90": row.get("f0_range_semitones_10_90"),
                "fit_tempo_delta": row.get("fit_tempo_delta"),
                "clipped_ms": row.get("clipped_ms"),
                "reject_reason": row.get("reject_reason"),
                "audio_path": row.get("audio_path"),
            }
        )
    return out


def risk_score(row: dict[str, Any]) -> float:
    score = float(row.get("abs_drift_ms") or 0.0) / 100.0
    if row.get("source_kind") == "fallback_existing_tts":
        score += 20.0
    score += min(10.0, float(row.get("clipped_ms") or 0.0) / 25.0)
    similarity = row.get("similarity")
    if similarity is not None:
        score += max(0.0, 0.985 - float(similarity)) * 100.0
    tempo = row.get("fit_tempo_delta")
    if tempo is not None:
        score += max(0.0, float(tempo) - 0.05) * 100.0
    return score


def read_risk_queue(run_dir: Path) -> dict[str, Any] | None:
    path = run_dir / "human_review_pack" / "risk_queue.json"
    if not path.exists():
        return None
    return read_json(path)


def strategy_contract(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "internal" / "pipeline" / "lora_candidate_strategy.go"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    checks = {
        "file_exists": path.exists(),
        "baseline_id_v3h_human_approved": "episode147_v3h_raw_stable_tailfix_human_approved" in text,
        "schema_version_2": bool(re.search(r'"schema_version"\s*:\s*2\b', text)),
        "tempo_gate_0_05": '"max_fit_delta":               0.05' in text or '"tempo_fit_max_delta":          0.05' in text,
        "comparison_only_10pct": "split_light_vector_10" in text and "forbidden_default_families" in text,
        "seg10569_anchor": "seg10569" in text and "shard01_selected" in text,
        "seg10686_anchor": "seg10686" in text and "split_mix" in text,
        "seg10518_anchor": "seg10518" in text,
        "seg10835_human_accepted_anchor": "seg10835" in text and "split4_tail_correct_atempo" in text,
    }
    return {
        "path": str(path),
        "checks": checks,
        "pass": all(checks.values()),
    }


def shadow_projection(rows: list[dict[str, Any]]) -> dict[str, Any]:
    min_target_ms = 18_000
    eligible = [
        row for row in rows
        if int(row.get("target_ms") or 0) >= min_target_ms
    ]
    split_candidates = [
        row for row in eligible
        if int(row.get("target_ms") or 0) >= min_target_ms
    ]
    eligible_ids = [str(row.get("sample_id") or "") for row in eligible if row.get("sample_id")]
    return {
        "mode": "projected_from_episode_lineage",
        "flags": {
            "LORA_CANDIDATE_LOOP_ENABLED": True,
            "LORA_CANDIDATE_EXECUTION_ENABLED": False,
            "LORA_SPLIT_PLANNING_ENABLED": True,
            "LORA_CANDIDATE_MIN_TARGET_SEC": 18,
            "LORA_CANDIDATE_MIN_TEXT_CHARS": 180,
            "LORA_CANDIDATE_MAX_PER_SEGMENT": 2,
        },
        "lineage_segments": len(rows),
        "duration_eligible_segments": len(eligible),
        "split_planning_projected_segments": len(split_candidates),
        "projected_candidate_execution_enabled": False,
        "projected_audio_selection_mode": "default_audio",
        "projected_candidate_families": ["default", "split_light_vector", "split_mix"],
        "projected_comparison_only_families": ["split_light_vector_10"],
        "duration_eligible_sample_ids": eligible_ids,
    }


def build_audit(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (repo_root / run_dir).resolve()
    summary = read_json(run_dir / "episode_result_summary.json")
    rows = rows_from_lineage(run_dir)
    lora = lora_rows(rows)
    fallback = fallback_rows(rows)

    duration = summary.get("duration") or {}
    outputs = summary.get("outputs") or {}
    final_audio = resolve_path(repo_root, outputs.get("final_audio"))
    final_video = resolve_path(repo_root, outputs.get("final_video"))
    expected_ms = int(duration.get("expected_duration_ms") or 0)
    probed_audio_ms = wav_duration_ms(final_audio) if final_audio and final_audio.exists() else None
    probed_video_ms = media_duration_ms(args.ffprobe, final_video) if final_video and final_video.exists() else None

    lora_bad = count_bad_lora_rows(lora, args)
    lora_similarity_min = metric_value(summary, ["lora_selected_metrics", "similarity", "min"])
    lora_similarity_mean = metric_value(summary, ["lora_selected_metrics", "similarity", "mean"])
    lora_drift_max = metric_value(summary, ["lora_selected_metrics", "abs_drift_ms", "max"])
    lora_drift_mean = metric_value(summary, ["lora_selected_metrics", "abs_drift_ms", "mean"])
    join_failures = metric_value(summary, ["lora_selected_metrics", "join_quality_fail_count"], 0)

    projection = shadow_projection(rows)
    hard_gates = [
        gate("final_audio_exists", bool(final_audio and final_audio.exists()), str(final_audio), "exists"),
        gate("final_video_exists", bool(final_video and final_video.exists()), str(final_video), "exists"),
        gate("summary_missing_audio_zero", len(summary.get("missing_audio") or []) == 0, len(summary.get("missing_audio") or []), 0),
        gate(
            "summary_final_audio_drift",
            float(duration.get("final_audio_abs_drift_ms") or 0.0) <= args.max_final_audio_drift_ms,
            duration.get("final_audio_abs_drift_ms"),
            f"<= {args.max_final_audio_drift_ms}",
        ),
        gate(
            "summary_final_video_drift",
            float(duration.get("final_video_abs_drift_ms") or 0.0) <= args.max_final_video_drift_ms,
            duration.get("final_video_abs_drift_ms"),
            f"<= {args.max_final_video_drift_ms}",
        ),
        gate(
            "lora_mean_drift",
            float(lora_drift_mean or 0.0) <= args.target_lora_mean_drift_ms,
            lora_drift_mean,
            f"<= {args.target_lora_mean_drift_ms}",
        ),
        gate(
            "lora_max_drift",
            float(lora_drift_max or 0.0) < args.max_lora_drift_ms,
            lora_drift_max,
            f"< {args.max_lora_drift_ms}",
        ),
        gate(
            "lora_mean_asr",
            float(lora_similarity_mean or 0.0) >= args.min_lora_mean_asr,
            lora_similarity_mean,
            f">= {args.min_lora_mean_asr}",
        ),
        gate(
            "lora_min_asr",
            float(lora_similarity_min or 0.0) >= args.min_lora_min_asr,
            lora_similarity_min,
            f">= {args.min_lora_min_asr}",
        ),
        gate(
            "join_quality_failures",
            int(join_failures or 0) <= args.max_join_quality_failures,
            join_failures,
            f"<= {args.max_join_quality_failures}",
        ),
        gate("strategy_contract", strategy_contract(repo_root)["pass"], "current worktree", "all checks true"),
        gate(
            "main_flow_shadow_default_audio_projection",
            projection["projected_candidate_execution_enabled"] is False
            and projection["projected_audio_selection_mode"] == "default_audio",
            {
                "candidate_execution_enabled": projection["projected_candidate_execution_enabled"],
                "audio_selection_mode": projection["projected_audio_selection_mode"],
            },
            "shadow only, default audio",
        ),
    ]

    review_gates = [
        gate(
            "tempo_delta_review_queue",
            len(lora_bad["tempo_review_required"]) == 0,
            len(lora_bad["tempo_review_required"]),
            f"0 rows > {args.max_tempo_delta_without_review}",
            severity="needs_review",
        ),
        gate(
            "clip_review_queue",
            len(lora_bad["clip_review_required"]) == 0,
            len(lora_bad["clip_review_required"]),
            f"0 rows > {args.max_clip_ms_without_review}ms",
            severity="needs_review",
        ),
        gate(
            "fallback_existing_tts_review_queue",
            len(fallback) == 0,
            len(fallback),
            "0 fallback rows",
            severity="needs_review",
        ),
    ]

    risk_queue = read_risk_queue(run_dir)
    risk_rows = risk_queue.get("risk_rows") if isinstance(risk_queue, dict) else []
    risk_by_id = {
        str(row.get("sample_id") or ""): row
        for row in risk_rows
        if isinstance(row, dict) and row.get("sample_id")
    } if isinstance(risk_rows, list) else {}
    first_pass = run_dir / "human_review_pack" / "first_pass_review.json"
    first_pass_count = None
    if first_pass.exists():
        first_pass_count = int((read_json(first_pass).get("count") or 0))

    top_risks = sorted(rows, key=risk_score, reverse=True)[: args.top_risk]
    fallback_reasons = Counter(str(row.get("reject_reason") or "unknown") for row in fallback)
    lora_label_counts = Counter(str(row.get("candidate_label") or "unknown") for row in lora)

    review_rows = build_objective_review_rows(
        fallback,
        lora_bad["clip_review_required"],
        lora_bad["tempo_review_required"],
        risk_by_id,
    )
    audit = {
        "schema_version": "holodub.lora_episode_objective_audit.v1",
        "run_dir": str(run_dir),
        "episode_id": summary.get("episode_id"),
        "outputs": {
            "final_audio": str(final_audio) if final_audio else None,
            "final_video": str(final_video) if final_video else None,
            "first_pass_review": str(first_pass) if first_pass.exists() else None,
            "risk_pack": str(run_dir / "human_review_pack" / "index.html"),
        },
        "duration": {
            **duration,
            "probed_audio_duration_ms": probed_audio_ms,
            "probed_audio_abs_drift_ms": abs(probed_audio_ms - expected_ms) if probed_audio_ms is not None else None,
            "probed_video_duration_ms": probed_video_ms,
            "probed_video_abs_drift_ms": abs(probed_video_ms - expected_ms) if probed_video_ms is not None else None,
        },
        "coverage": {
            **(summary.get("coverage") or {}),
            "lineage_rows": len(rows),
            "lineage_lora_rows": len(lora),
            "lineage_fallback_rows": len(fallback),
            "risk_queue_rows": len(risk_rows) if isinstance(risk_rows, list) else None,
            "first_pass_review_rows": first_pass_count,
        },
        "objective_gates": hard_gates,
        "review_gates": review_gates,
        "objective_pass": all(item["pass"] for item in hard_gates),
        "review_required": any(not item["pass"] for item in review_gates),
        "strategy_contract": strategy_contract(repo_root),
        "main_flow_shadow_projection": projection,
        "lora_selected_metrics": summary.get("lora_selected_metrics"),
        "fallback_existing_tts_metrics": summary.get("fallback_existing_tts_metrics"),
        "lora_candidate_label_counts": dict(sorted(lora_label_counts.items())),
        "fallback_reject_reason_counts": dict(sorted(fallback_reasons.items())),
        "lora_gate_exceptions": lora_bad,
        "objective_review_rows": review_rows,
        "top_objective_risks": compact_rows(top_risks, args.top_risk),
        "commands": {
            "audit": "python tools/lora_episode_objective_audit.py --run-dir "
            + str(run_dir.relative_to(repo_root) if run_dir.is_relative_to(repo_root) else run_dir),
        },
    }
    return audit


def build_objective_review_rows(
    fallback: list[dict[str, Any]],
    clip_review: list[dict[str, Any]],
    tempo_review: list[dict[str, Any]],
    risk_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    rows_by_id: dict[str, dict[str, Any]] = {}

    def add_rows(rows: list[dict[str, Any]], reason: str) -> None:
        for row in rows:
            sid = str(row.get("sample_id") or "")
            if not sid:
                continue
            current = rows_by_id.setdefault(
                sid,
                {
                    "sample_id": sid,
                    "chapter_ordinal": row.get("chapter_ordinal"),
                    "ordinal": row.get("ordinal"),
                    "source_kind": row.get("source_kind"),
                    "candidate_label": row.get("candidate_label"),
                    "candidate_label_chain": row.get("candidate_label_chain"),
                    "abs_drift_ms": row.get("abs_drift_ms"),
                    "similarity": row.get("similarity"),
                    "critical_term_recall": row.get("critical_term_recall"),
                    "f0_range_semitones_10_90": row.get("f0_range_semitones_10_90"),
                    "fit_tempo_delta": row.get("fit_tempo_delta"),
                    "clipped_ms": row.get("clipped_ms"),
                    "reject_reason": row.get("reject_reason"),
                    "audio_path": row.get("audio_path"),
                    "target_ms": row.get("target_ms"),
                    "episode_time_ms": None,
                    "episode_time": None,
                    "chapter_time_ms": None,
                    "chapter_time": None,
                    "risk_reasons": [],
                    "review_reasons": [],
                },
            )
            risk = (risk_by_id or {}).get(sid)
            if risk:
                current["target_ms"] = current.get("target_ms") or risk.get("target_ms")
                current["episode_time_ms"] = risk.get("episode_time_ms")
                current["episode_time"] = risk.get("episode_time")
                current["chapter_time_ms"] = risk.get("chapter_time_ms")
                current["chapter_time"] = risk.get("chapter_time")
                current["risk_reasons"] = risk.get("risk_reasons") or []
            if reason not in current["review_reasons"]:
                current["review_reasons"].append(reason)

    add_rows(fallback, "fallback_existing_tts")
    add_rows(clip_review, "clip_gt_200ms")
    add_rows(tempo_review, "tempo_delta_gt_0.08")
    return sorted(rows_by_id.values(), key=objective_review_sort_key)


def objective_review_sort_key(row: dict[str, Any]) -> tuple[float, str]:
    priority = 0.0
    reasons = set(row.get("review_reasons") or [])
    if "fallback_existing_tts" in reasons:
        priority += 1000.0
    priority += float(row.get("abs_drift_ms") or 0.0)
    priority += float(row.get("clipped_ms") or 0.0) * 2.0
    if row.get("fit_tempo_delta") is not None:
        priority += max(0.0, float(row.get("fit_tempo_delta") or 0.0) - 0.05) * 10000.0
    return (-priority, str(row.get("sample_id") or ""))


def safe_filename(text: str) -> str:
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() or ch in "._-" else "_")
    return "".join(out).strip("._") or "row"


def add_context_audio_clips(
    audit: dict[str, Any],
    output_dir: Path,
    ffmpeg: str,
    pre_sec: float,
    post_sec: float,
) -> None:
    final_audio_raw = audit.get("outputs", {}).get("final_audio")
    if not final_audio_raw:
        return
    final_audio = Path(str(final_audio_raw))
    if not final_audio.exists():
        return

    rows = audit.get("objective_review_rows") or []
    if not rows:
        return

    pre = max(0.0, float(pre_sec or 0.0))
    post = max(0.0, float(post_sec or 0.0))
    clip_dir = output_dir / "context_audio"
    clip_dir.mkdir(parents=True, exist_ok=True)

    generated = 0
    failed = 0
    for row in rows:
        episode_ms = row.get("episode_time_ms")
        if episode_ms is None:
            row["context_audio_error"] = "missing_episode_time_ms"
            failed += 1
            continue
        try:
            episode_sec = float(episode_ms) / 1000.0
        except (TypeError, ValueError):
            row["context_audio_error"] = "bad_episode_time_ms"
            failed += 1
            continue

        try:
            target_sec = float(row.get("target_ms") or 0.0) / 1000.0
        except (TypeError, ValueError):
            target_sec = 0.0
        if target_sec <= 0.0:
            target_sec = 8.0

        start_sec = max(0.0, episode_sec - pre)
        duration_sec = max(2.0, target_sec + pre + post)
        out_path = clip_dir / f"{safe_filename(str(row.get('sample_id') or 'row'))}_context.wav"
        try:
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{start_sec:.3f}",
                    "-t",
                    f"{duration_sec:.3f}",
                    "-i",
                    str(final_audio),
                    "-ac",
                    "1",
                    "-ar",
                    "24000",
                    str(out_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            row["context_audio_error"] = str(exc)
            failed += 1
            continue

        row["context_audio_path"] = str(out_path)
        row["context_start_sec"] = round(start_sec, 3)
        row["context_duration_sec"] = round(duration_sec, 3)
        row["context_pre_sec"] = pre
        row["context_post_sec"] = post
        generated += 1

    coverage = audit.setdefault("coverage", {})
    coverage["objective_review_context_audio_rows"] = generated
    coverage["objective_review_context_audio_failures"] = failed
    audit.setdefault("outputs", {})["objective_review_context_audio_dir"] = str(clip_dir)


def render_markdown(audit: dict[str, Any]) -> str:
    lines = [
        "# HoloDub LoRA Episode Objective Audit",
        "",
        "## Verdict",
        "",
        f"- Objective pass: {audit.get('objective_pass')}",
        f"- Review required: {audit.get('review_required')}",
        f"- Run dir: `{audit.get('run_dir')}`",
        f"- Final video: `{audit.get('outputs', {}).get('final_video')}`",
        f"- First-pass review: `{audit.get('outputs', {}).get('first_pass_review')}`",
        f"- Objective review rows: {len(audit.get('objective_review_rows') or [])}",
        f"- Objective review context clips: {audit.get('coverage', {}).get('objective_review_context_audio_rows')}",
        "",
        "## Objective Gates",
        "",
    ]
    for item in audit.get("objective_gates") or []:
        status = "PASS" if item.get("pass") else "FAIL"
        lines.append(f"- {status} `{item.get('name')}`: {item.get('value')} ({item.get('threshold')})")
    lines.extend(["", "## Review Gates", ""])
    for item in audit.get("review_gates") or []:
        status = "PASS" if item.get("pass") else str(item.get("severity") or "REVIEW").upper()
        lines.append(f"- {status} `{item.get('name')}`: {item.get('value')} ({item.get('threshold')})")

    duration = audit.get("duration") or {}
    coverage = audit.get("coverage") or {}
    lora = audit.get("lora_selected_metrics") or {}
    fallback = audit.get("fallback_existing_tts_metrics") or {}
    lines.extend(
        [
            "",
            "## Core Metrics",
            "",
            f"- Timeline segments: {coverage.get('timeline_segments')} (lineage rows {coverage.get('lineage_rows')})",
            f"- LoRA selected: {coverage.get('lora_selected_segments')}",
            f"- Existing-TTS fallback: {coverage.get('fallback_existing_tts_segments')}",
            f"- Missing audio: {coverage.get('missing_audio_segments')}",
            f"- Final audio drift: {duration.get('final_audio_abs_drift_ms')}ms",
            f"- Final video drift: {duration.get('final_video_abs_drift_ms')}ms",
            f"- LoRA mean/max drift: {metric_from(lora, 'abs_drift_ms', 'mean')}ms / {metric_from(lora, 'abs_drift_ms', 'max')}ms",
            f"- LoRA mean/min ASR: {metric_from(lora, 'similarity', 'mean')} / {metric_from(lora, 'similarity', 'min')}",
            f"- Join-quality failures: {lora.get('join_quality_fail_count')}",
            f"- Fallback mean/max drift: {metric_from(fallback, 'abs_drift_ms', 'mean')}ms / {metric_from(fallback, 'abs_drift_ms', 'max')}ms",
            "",
            "## Main-Flow Shadow Projection",
            "",
            f"- Duration-eligible segments: {audit.get('main_flow_shadow_projection', {}).get('duration_eligible_segments')}",
            f"- Projected candidate execution: {audit.get('main_flow_shadow_projection', {}).get('projected_candidate_execution_enabled')}",
            f"- Projected audio selection: {audit.get('main_flow_shadow_projection', {}).get('projected_audio_selection_mode')}",
            f"- Projected families: {audit.get('main_flow_shadow_projection', {}).get('projected_candidate_families')}",
            "",
            "## Top Risks",
            "",
        ]
    )
    for row in audit.get("top_objective_risks") or []:
        lines.append(
            "- {sample_id}: {source_kind}, {candidate_label}, drift={abs_drift_ms}ms, "
            "clip={clipped_ms}ms, asr={similarity}, tempo={fit_tempo_delta}, reason={reject_reason}".format(**row)
        )
    lines.append("")
    return "\n".join(lines)


def render_objective_review_html(audit: dict[str, Any]) -> str:
    rows = audit.get("objective_review_rows") or []
    final_video = audit.get("outputs", {}).get("final_video")
    final_audio = audit.get("outputs", {}).get("final_audio")
    css = """
    body { font-family: system-ui, -apple-system, Segoe UI, sans-serif; margin: 24px; color: #17202a; }
    h1 { font-size: 24px; margin: 0 0 8px; }
    .meta { color: #566573; margin: 4px 0; }
    .summary { display: flex; gap: 12px; flex-wrap: wrap; margin: 18px 0; }
    .pill { border: 1px solid #ccd1d1; border-radius: 999px; padding: 4px 10px; font-size: 13px; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border-bottom: 1px solid #e5e8e8; padding: 8px; text-align: left; vertical-align: top; }
    th { position: sticky; top: 0; background: #fff; z-index: 1; }
    audio { width: 250px; max-width: 28vw; }
    .reasons { display: flex; gap: 4px; flex-wrap: wrap; }
    .reason { background: #fff3cd; border: 1px solid #f5c542; border-radius: 4px; padding: 2px 6px; font-size: 12px; }
    .risk { background: #eaf2f8; border-color: #aed6f1; }
    .num { font-variant-numeric: tabular-nums; white-space: nowrap; }
    .path { max-width: 360px; overflow-wrap: anywhere; color: #5d6d7e; font-size: 12px; }
    .small { color: #566573; font-size: 12px; line-height: 1.4; }
    """
    parts = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<title>HoloDub Objective Review Pack</title>",
        f"<style>{css}</style>",
        "</head><body>",
        "<h1>HoloDub Objective Review Pack</h1>",
        f"<p class='meta'>Run: {html.escape(str(audit.get('run_dir') or ''))}</p>",
        f"<p class='meta'>Final video: {file_link(final_video)}</p>",
        f"<p class='meta'>Final audio: {file_link(final_audio)}</p>",
        "<div class='summary'>",
        f"<span class='pill'>objective_pass={html.escape(str(audit.get('objective_pass')))}</span>",
        f"<span class='pill'>review_required={html.escape(str(audit.get('review_required')))}</span>",
        f"<span class='pill'>rows={len(rows)}</span>",
        f"<span class='pill'>context_clips={html.escape(str(audit.get('coverage', {}).get('objective_review_context_audio_rows')))}</span>",
        "</div>",
        "<table>",
        "<thead><tr><th>Sample</th><th>Reasons</th><th>Segment Audio</th><th>Final Mix Context</th><th>Metrics</th><th>Lineage</th><th>Path</th></tr></thead>",
        "<tbody>",
    ]
    for row in rows:
        audio_path = row.get("audio_path")
        audio = f"<audio controls preload='none' src='{html.escape(file_uri(audio_path))}'></audio>" if audio_path else ""
        context_path = row.get("context_audio_path")
        if context_path:
            context_audio = (
                f"<audio controls preload='none' src='{html.escape(file_uri(context_path))}'></audio>"
                f"<div class='small'>starts {html.escape(str(row.get('context_start_sec')))}s, "
                f"clip {html.escape(str(row.get('context_duration_sec')))}s</div>"
            )
        else:
            context_audio = f"<span class='small'>{html.escape(str(row.get('context_audio_error') or ''))}</span>"
        reasons = "".join(
            f"<span class='reason'>{html.escape(str(reason))}</span>"
            for reason in row.get("review_reasons") or []
        )
        risk_reasons = "".join(
            f"<span class='reason risk'>{html.escape(str(reason))}</span>"
            for reason in row.get("risk_reasons") or []
        )
        metrics = "<br>".join(
            [
                metric_line("drift_ms", row.get("abs_drift_ms")),
                metric_line("clip_ms", row.get("clipped_ms")),
                metric_line("tempo_delta", row.get("fit_tempo_delta")),
                metric_line("asr", row.get("similarity")),
                metric_line("term", row.get("critical_term_recall")),
                metric_line("f0", row.get("f0_range_semitones_10_90")),
            ]
        )
        lineage = " > ".join(str(x) for x in row.get("candidate_label_chain") or [])
        parts.extend(
            [
                "<tr>",
                "<td>",
                f"<strong>{html.escape(str(row.get('sample_id') or ''))}</strong><br>",
                f"ch {html.escape(str(row.get('chapter_ordinal') or ''))}, ord {html.escape(str(row.get('ordinal') or ''))}<br>",
                f"<span class='small'>episode {html.escape(str(row.get('episode_time') or '-'))}, "
                f"chapter {html.escape(str(row.get('chapter_time') or '-'))}</span>",
                "</td>",
                f"<td><div class='reasons'>{reasons}{risk_reasons}</div><br>{html.escape(str(row.get('reject_reason') or ''))}</td>",
                f"<td>{audio}</td>",
                f"<td>{context_audio}</td>",
                f"<td class='num'>{metrics}</td>",
                f"<td>{html.escape(str(row.get('source_kind') or ''))}<br>{html.escape(str(row.get('candidate_label') or ''))}<br>{html.escape(lineage)}</td>",
                f"<td class='path'>{html.escape(str(audio_path or ''))}</td>",
                "</tr>",
            ]
        )
    parts.extend(["</tbody></table>", "</body></html>"])
    return "\n".join(parts)


def metric_line(label: str, value: Any) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, float):
        text = f"{value:.4g}"
    else:
        text = str(value)
    return f"{html.escape(label)}: {html.escape(text)}"


def file_link(raw: Any) -> str:
    if not raw:
        return ""
    uri = file_uri(str(raw))
    return f"<a href='{html.escape(uri)}'>{html.escape(str(raw))}</a>"


def file_uri(raw: Any) -> str:
    if not raw:
        return ""
    try:
        return Path(str(raw)).resolve().as_uri()
    except ValueError:
        return str(raw)


def metric_from(payload: dict[str, Any], key: str, subkey: str) -> Any:
    value = payload.get(key)
    if not isinstance(value, dict):
        return None
    return value.get(subkey)


def main() -> None:
    args = parse_args()
    audit = build_audit(args)
    run_dir = Path(audit["run_dir"])
    output_dir = Path(args.output_dir) if args.output_dir else run_dir / "objective_audit"
    if not output_dir.is_absolute():
        output_dir = (Path(args.repo_root).resolve() / output_dir).resolve()
    add_context_audio_clips(audit, output_dir, args.ffmpeg, args.context_pre_sec, args.context_post_sec)
    write_json(output_dir / "objective_audit.json", audit)
    (output_dir / "objective_audit.md").write_text(render_markdown(audit), encoding="utf-8")
    (output_dir / "objective_review.html").write_text(render_objective_review_html(audit), encoding="utf-8")
    print(
        json.dumps(
            {
                "objective_pass": audit["objective_pass"],
                "review_required": audit["review_required"],
                "output_dir": str(output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
