#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import temporal_alignment_loop as loop
import temporal_plan_experiment as exp


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "validation" / "temporal_chunk_select"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Chunk-local candidate selection experiment for long HoloDub TTS segments."
    )
    parser.add_argument("--episode-id", type=int, default=146)
    parser.add_argument("--segment-id", type=int, required=True)
    parser.add_argument("--text-variant", default="compact")
    parser.add_argument("--candidate-repeats", type=int, default=4)
    parser.add_argument("--max-chunks", type=int, default=7)
    parser.add_argument("--min-chunk-chars", type=int, default=20)
    parser.add_argument("--max-chunk-chars", type=int, default=70)
    parser.add_argument("--llm-compact", action="store_true")
    parser.add_argument("--cps-mode", choices=("default", "episode"), default="episode")
    parser.add_argument("--final-fit-filter", choices=("rubberband", "atempo"), default="rubberband")
    parser.add_argument("--final-fit-max-delta", type=float, default=0.10)
    parser.add_argument("--final-fit-min-drift-ms", type=int, default=700)
    parser.add_argument("--disable-shortfall-padding", action="store_true")
    parser.add_argument("--ml-container", default="holodub-ml-service-1")
    parser.add_argument("--pg-container", default="holodub-postgres-1")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    samples = exp.load_episode_segments(args.pg_container, args.episode_id)
    by_id = {s.segment_id: s for s in samples}
    sample = by_id.get(args.segment_id)
    if sample is None:
        raise SystemExit(f"missing segment id {args.segment_id}")
    cps = 4.0
    if args.cps_mode == "episode":
        cps = exp.estimate_episode_chars_per_sec(samples) or cps

    compact_cache: dict[str, dict[str, Any]] = {}
    original_text = exp.sample_tts_text(sample)
    text = exp.text_for_variant(
        sample,
        args.text_variant,
        cps,
        llm_compact=args.llm_compact,
        compact_cache=compact_cache,
    )
    chunks = build_chunks(text, args.max_chunks, args.min_chunk_chars, args.max_chunk_chars)
    allocations = allocate_ms(chunks, sample.target_ms)

    run_dir = args.out_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name

    manifest = {
        "episode_id": args.episode_id,
        "segment_id": sample.segment_id,
        "job_id": sample.job_id,
        "ordinal": sample.ordinal,
        "text_variant": args.text_variant,
        "target_ms": sample.target_ms,
        "baseline_tts_ms": sample.baseline_tts_ms,
        "chars_per_sec": cps,
        "text": text,
        "text_chars": exp.speakable_len(text),
        "original_text_chars": exp.speakable_len(original_text),
        "text_retention_ratio": round(
            exp.speakable_len(text) / max(1, exp.speakable_len(original_text)),
            6,
        ),
        "text_complete_tail": not exp.looks_incomplete_tail(text),
        "compact_complete_sentence": (
            not exp.looks_incomplete_compact(text)
            if exp.uses_compact_text(args.text_variant) and text != original_text
            else None
        ),
        "compact_cache": compact_cache,
        "chunks": [
            {
                "index": i,
                "text": chunk,
                "chars": exp.speakable_len(chunk),
                "target_ms": allocations[i],
            }
            for i, chunk in enumerate(chunks)
        ],
        "candidate_repeats": args.candidate_repeats,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    candidates = synthesize_chunk_candidates(
        args,
        sample,
        chunks,
        allocations,
        run_id,
    )
    for row in candidates:
        hyp = loop.call_asr(args.ml_container, row["audio_relpath"], row["actual_ms"])
        row["asr_text"] = hyp
        row["asr_similarity"] = loop.similarity(row["expected_text"], hyp)
        term_eval = critical_term_eval(row["expected_text"], hyp)
        row["critical_terms"] = term_eval["terms"]
        row["critical_term_recall"] = term_eval["recall"]
        row["candidate_score"] = chunk_score(row)
        print(
            "chunk={chunk_index} r{repeat} dur={actual_ms}ms "
            "drift={drift_ms}ms asr={asr_similarity:.3f} "
            "terms={critical_term_recall:.3f} score={candidate_score:.3f}".format(**row),
            flush=True,
        )

    selected, selection_features = select_best_combo(
        candidates,
        sample.target_ms,
        args.final_fit_max_delta,
    )
    final_relpath = f"experiments/chunk_select/{run_id}/seg{sample.segment_id}-chunk_select.wav"
    concat_result = concat_audio(
        args.ml_container,
        selected,
        final_relpath,
        sample.target_ms,
        args.final_fit_max_delta,
        pad_shortfall=not args.disable_shortfall_padding,
    )
    boundary_rows = concat_result.get("timeline_rows") or selected
    raw_final_ms = exp.probe_audio_ms(args.ml_container, final_relpath)
    fit_info = maybe_apply_final_fit(
        args,
        final_relpath,
        sample.target_ms,
        raw_final_ms,
    )
    eval_relpath = fit_info.get("fit_audio_relpath") or final_relpath
    final_ms = int(fit_info.get("fit_actual_ms") or raw_final_ms)
    full_hyp = loop.call_asr(args.ml_container, eval_relpath, final_ms)
    full_similarity = loop.similarity(text, full_hyp)
    boundary_eval = boundary_asr_eval(args.ml_container, eval_relpath, boundary_rows, final_ms)
    gates = loop.Gates()
    allowed_fit_delta = min(gates.max_fit_tempo_delta, max(0.0, float(args.final_fit_max_delta)))
    effective_tempo_delta = (
        0.0
        if concat_result.get("padding_applied")
        else float(selection_features["fit_tempo_delta"])
    )
    summary = {
        "run_dir": str(run_dir),
        "selected_chunks": len(selected),
        "target_ms": sample.target_ms,
        "raw_actual_ms": raw_final_ms,
        "raw_drift_ms": raw_final_ms - sample.target_ms,
        "actual_ms": final_ms,
        "drift_ms": final_ms - sample.target_ms,
        "abs_drift_ms": abs(final_ms - sample.target_ms),
        "full_asr_similarity": full_similarity,
        "boundary_asr_similarity": boundary_eval["weighted_similarity"],
        "min_boundary_asr_similarity": boundary_eval["min_similarity"],
        "boundary_term_recall": boundary_eval["term_recall"],
        "min_boundary_term_recall": boundary_eval["min_term_recall"],
        "selection_features": selection_features,
        "effective_tempo_delta": round(effective_tempo_delta, 6),
        "concat_info": {
            key: value
            for key, value in concat_result.items()
            if key != "timeline_rows"
        },
        "text_retention_ratio": manifest["text_retention_ratio"],
        "text_complete_tail": manifest["text_complete_tail"],
        "compact_complete_sentence": manifest["compact_complete_sentence"],
        "fit_info": fit_info,
        "audio_relpath": eval_relpath,
        "raw_audio_relpath": final_relpath,
        "gate_pass": (
            abs(final_ms - sample.target_ms) <= gates.max_abs_drift_ms
            and boundary_eval["weighted_similarity"] >= gates.min_mean_asr_similarity
            and boundary_eval["min_similarity"] >= gates.min_asr_similarity
            and boundary_eval["term_recall"] >= 0.90
            and boundary_eval["min_term_recall"] >= 0.75
            and selection_features["max_adjacent_rate_jump"] <= gates.max_adjacent_rate_jump
            and effective_tempo_delta <= allowed_fit_delta
            and manifest["text_complete_tail"]
            and (manifest["compact_complete_sentence"] is not False)
            and manifest["text_retention_ratio"] >= 0.80
        ),
        "boundary_asr": boundary_eval["chunks"],
    }

    write_jsonl(run_dir / "candidates.jsonl", candidates)
    write_jsonl(run_dir / "selected_chunks.jsonl", selected)
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_chunks(text: str, max_chunks: int, min_chars: int, max_chars: int) -> list[str]:
    groups = exp.split_breath_groups(text)
    groups = split_long_groups(groups, max(min_chars, max_chars))
    groups = exp.pack_short_groups(groups, min_chars=min_chars)
    groups = merge_short_tail(groups, min_chars)
    if len(groups) > max_chunks:
        groups = exp.pack_groups(groups, max_chunks)
        groups = merge_short_tail(groups, min_chars)
    return groups or [text]


def split_long_groups(groups: list[str], max_chars: int) -> list[str]:
    if max_chars <= 0:
        return groups
    out: list[str] = []
    for group in groups:
        if exp.speakable_len(group) <= max_chars:
            out.append(group)
            continue
        out.extend(split_long_group(group, max_chars))
    return out


def split_long_group(text: str, max_chars: int) -> list[str]:
    soft_boundaries = set(",，、 ")
    pieces: list[str] = []
    current: list[str] = []
    current_chars = 0
    last_soft_index = -1
    for ch in text:
        current.append(ch)
        if not ch.isspace():
            current_chars += 1
        if ch in soft_boundaries:
            last_soft_index = len(current)
        if current_chars < max_chars:
            continue
        if last_soft_index > 0:
            piece = "".join(current[:last_soft_index]).strip()
            rest = current[last_soft_index:]
        else:
            piece = "".join(current).strip()
            rest = []
        if piece:
            pieces.append(piece)
        current = rest
        current_chars = exp.speakable_len("".join(current))
        last_soft_index = -1
        for idx, existing in enumerate(current, start=1):
            if existing in soft_boundaries:
                last_soft_index = idx
    tail = "".join(current).strip()
    if tail:
        pieces.append(tail)
    return pieces


def merge_short_tail(groups: list[str], min_chars: int) -> list[str]:
    if len(groups) <= 1 or min_chars <= 0:
        return groups
    if exp.speakable_len(groups[-1]) >= min_chars:
        return groups
    merged = list(groups[:-2])
    merged.append(groups[-2] + groups[-1])
    return merged


def allocate_ms(chunks: list[str], target_ms: int) -> list[int]:
    total = max(1, sum(exp.speakable_len(chunk) for chunk in chunks))
    out: list[int] = []
    assigned = 0
    for idx, chunk in enumerate(chunks):
        if idx == len(chunks) - 1:
            ms = target_ms - assigned
        else:
            ms = int(round(target_ms * exp.speakable_len(chunk) / total))
            assigned += ms
        out.append(max(300, ms))
    return out


def synthesize_chunk_candidates(
    args: argparse.Namespace,
    sample: exp.SegmentSample,
    chunks: list[str],
    allocations: list[int],
    run_id: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chunk_idx, chunk in enumerate(chunks):
        for repeat in range(args.candidate_repeats):
            relpath = (
                f"experiments/chunk_select/{run_id}/"
                f"seg{sample.segment_id}-chunk{chunk_idx:02d}-r{repeat}.wav"
            )
            payload = {
                "text": chunk,
                "target_duration_sec": allocations[chunk_idx] / 1000.0,
                "max_allowed_sec": allocations[chunk_idx] / 1000.0 + 0.5,
                "voice_config": {},
                "output_relpath": relpath,
                "dubbing_meta": sample.meta.get("dubbing") or None,
            }
            started = time.time()
            response = exp.call_tts(args.ml_container, payload)
            elapsed = time.time() - started
            actual_ms = int(response.get("actual_duration_ms") or 0)
            rows.append(
                {
                    "segment_id": sample.segment_id,
                    "chunk_index": chunk_idx,
                    "repeat": repeat,
                    "expected_text": chunk,
                    "expected_chars": exp.speakable_len(chunk),
                    "target_ms": allocations[chunk_idx],
                    "actual_ms": actual_ms,
                    "drift_ms": actual_ms - allocations[chunk_idx],
                    "abs_drift_ms": abs(actual_ms - allocations[chunk_idx]),
                    "audio_relpath": response.get("audio_relpath", relpath),
                    "diagnostics": response.get("diagnostics", []),
                    "elapsed_sec": round(elapsed, 3),
                }
            )
    return rows


def chunk_score(row: dict[str, Any]) -> float:
    timing = max(0.0, 1.0 - float(row["abs_drift_ms"]) / 1200.0)
    asr = float(row.get("asr_similarity") or 0.0)
    terms = float(row.get("critical_term_recall", 1.0))
    return round(0.25 * timing + 0.50 * asr + 0.25 * terms, 6)


def select_best_combo(
    rows: list[dict[str, Any]],
    target_ms: int,
    max_fit_tempo_delta: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_chunk: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_chunk.setdefault(int(row["chunk_index"]), []).append(row)
    ordered = [sorted(by_chunk[idx], key=lambda r: float(r["candidate_score"]), reverse=True) for idx in sorted(by_chunk)]
    best_rows: list[dict[str, Any]] = []
    best_features: dict[str, Any] = {}
    best_score = -1.0
    best_feasible = False
    for combo in itertools.product(*ordered):
        features = combo_features(list(combo), target_ms)
        score = combo_score(features)
        feasible = combo_is_gate_candidate(features, max_fit_tempo_delta)
        if (feasible and not best_feasible) or (feasible == best_feasible and score > best_score):
            best_feasible = feasible
            best_score = score
            best_rows = [dict(row) for row in combo]
            best_features = features
    for row in best_rows:
        row["selection_rank"] = 1
    best_features["combo_score"] = round(best_score, 6)
    return best_rows, best_features


def combo_features(rows: list[dict[str, Any]], target_ms: int) -> dict[str, Any]:
    total_ms = sum(int(row.get("actual_ms") or 0) for row in rows)
    total_chars = sum(max(1, int(row.get("expected_chars") or 0)) for row in rows)
    weighted_asr = 0.0
    min_asr = 1.0
    for row in rows:
        chars = max(1, int(row.get("expected_chars") or 0))
        asr = float(row.get("asr_similarity") or 0.0)
        weighted_asr += asr * chars
        min_asr = min(min_asr, asr)
    weighted_asr /= max(1, total_chars)
    drift_ms = total_ms - target_ms
    raw_abs_drift_ms = abs(drift_ms)
    tempo = total_ms / target_ms if target_ms > 0 else 1.0
    observed_rates = [
        (float(row.get("expected_chars") or 0.0) / max(0.001, float(row.get("actual_ms") or 1) / 1000.0))
        for row in rows
    ]
    return {
        "raw_actual_ms": total_ms,
        "raw_drift_ms": drift_ms,
        "raw_abs_drift_ms": raw_abs_drift_ms,
        "fit_tempo": round(tempo, 6),
        "fit_tempo_delta": round(abs(tempo - 1.0), 6),
        "weighted_chunk_asr_similarity": round(weighted_asr, 6),
        "min_chunk_asr_similarity": round(min_asr, 6),
        "weighted_chunk_term_recall": round(weighted_term_recall(rows), 6),
        "min_chunk_term_recall": round(min_term_recall(rows), 6),
        "max_adjacent_rate_jump": round(max_adjacent_rate_jump(observed_rates), 6),
    }


def combo_score(features: dict[str, Any]) -> float:
    weighted_asr = float(features["weighted_chunk_asr_similarity"])
    min_asr = float(features["min_chunk_asr_similarity"])
    weighted_terms = float(features.get("weighted_chunk_term_recall", 1.0))
    min_terms = float(features.get("min_chunk_term_recall", 1.0))
    timing = max(0.0, 1.0 - float(features["raw_abs_drift_ms"]) / 2500.0)
    smooth = max(0.0, 1.0 - float(features["max_adjacent_rate_jump"]) / 0.35)
    stretch = max(0.0, 1.0 - float(features["fit_tempo_delta"]) / 0.12)
    return (
        0.36 * weighted_asr
        + 0.14 * min_asr
        + 0.18 * weighted_terms
        + 0.07 * min_terms
        + 0.15 * timing
        + 0.07 * smooth
        + 0.03 * stretch
    )


def combo_is_gate_candidate(features: dict[str, Any], max_fit_tempo_delta: float) -> bool:
    gates = loop.Gates()
    return (
        float(features["weighted_chunk_asr_similarity"]) >= gates.min_mean_asr_similarity
        and float(features["min_chunk_asr_similarity"]) >= gates.min_asr_similarity
        and float(features.get("weighted_chunk_term_recall", 1.0)) >= 0.90
        and float(features.get("min_chunk_term_recall", 1.0)) >= 0.75
        and float(features["max_adjacent_rate_jump"]) <= gates.max_adjacent_rate_jump
        and float(features["fit_tempo_delta"]) <= min(gates.max_fit_tempo_delta, max_fit_tempo_delta)
    )


def weighted_term_recall(rows: list[dict[str, Any]]) -> float:
    weighted = 0.0
    total = 0
    for row in rows:
        terms = row.get("critical_terms") or []
        if not terms:
            continue
        chars = max(1, int(row.get("expected_chars") or 0))
        weighted += float(row.get("critical_term_recall", 0.0)) * chars
        total += chars
    return weighted / total if total else 1.0


def min_term_recall(rows: list[dict[str, Any]]) -> float:
    recalls = [
        float(row.get("critical_term_recall", 0.0))
        for row in rows
        if row.get("critical_terms")
    ]
    return min(recalls) if recalls else 1.0


def max_adjacent_rate_jump(rates: list[float]) -> float:
    if len(rates) < 2:
        return 0.0
    jumps = []
    for prev, current in zip(rates, rates[1:]):
        denom = max(prev, current, 0.001)
        jumps.append(abs(current - prev) / denom)
    return max(jumps, default=0.0)


def maybe_apply_final_fit(
    args: argparse.Namespace,
    audio_relpath: str,
    target_ms: int,
    actual_ms: int,
) -> dict[str, Any]:
    if target_ms <= 0 or actual_ms <= 0:
        return {"fit_applied": False, "fit_error": "missing audio or duration"}
    drift_ms = actual_ms - target_ms
    if abs(drift_ms) <= max(0, int(args.final_fit_min_drift_ms)):
        return {
            "fit_applied": False,
            "fit_skipped": "drift_within_tolerance",
            "fit_tempo": round(actual_ms / target_ms, 6),
            "fit_drift_ms": drift_ms,
        }
    tempo = actual_ms / target_ms
    if abs(tempo - 1.0) < 0.003:
        return {"fit_applied": False, "fit_skipped": "already_close", "fit_tempo": round(tempo, 6)}
    max_delta = max(0.0, float(args.final_fit_max_delta))
    if abs(tempo - 1.0) > max_delta:
        return {
            "fit_applied": False,
            "fit_skipped": "tempo_out_of_range",
            "fit_tempo": round(tempo, 6),
        }
    return exp.apply_duration_fit(
        args.ml_container,
        audio_relpath,
        target_ms,
        actual_ms,
        min_tempo=1.0 - max_delta,
        max_tempo=1.0 + max_delta,
        fit_filter=args.final_fit_filter,
    )


def boundary_asr_eval(
    container: str,
    audio_relpath: str,
    selected: list[dict[str, Any]],
    final_ms: int,
) -> dict[str, Any]:
    source_total = sum(int(row.get("actual_ms") or 0) for row in selected)
    scale = final_ms / source_total if source_total > 0 else 1.0
    offset = 0
    chunks: list[dict[str, Any]] = []
    weighted = 0.0
    total_chars = 0
    min_similarity = 1.0
    for row in selected:
        raw_start = offset
        offset += int(row.get("actual_ms") or 0)
        start_ms = int(round(raw_start * scale))
        end_ms = int(round(offset * scale))
        hyp = loop.call_asr(container, audio_relpath, end_ms, start_ms=start_ms)
        expected = str(row.get("expected_text") or "")
        similarity = loop.similarity(expected, hyp)
        term_eval = critical_term_eval(expected, hyp)
        chars = max(1, int(row.get("expected_chars") or exp.speakable_len(expected)))
        weighted += similarity * chars
        total_chars += chars
        min_similarity = min(min_similarity, similarity)
        chunks.append(
            {
                "chunk_index": row["chunk_index"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "expected_chars": chars,
                "similarity": round(similarity, 6),
                "critical_terms": term_eval["terms"],
                "critical_term_recall": term_eval["recall"],
                "missing_critical_terms": term_eval["missing"],
                "asr_text": hyp,
            }
        )
    term_weighted, term_total = 0.0, 0
    term_mins: list[float] = []
    for chunk in chunks:
        if not chunk["critical_terms"]:
            continue
        chars = int(chunk["expected_chars"])
        term_weighted += float(chunk["critical_term_recall"]) * chars
        term_total += chars
        term_mins.append(float(chunk["critical_term_recall"]))
    return {
        "weighted_similarity": round(weighted / max(1, total_chars), 6),
        "min_similarity": round(min_similarity if chunks else 0.0, 6),
        "term_recall": round(term_weighted / term_total, 6) if term_total else 1.0,
        "min_term_recall": round(min(term_mins), 6) if term_mins else 1.0,
        "chunks": chunks,
    }


def critical_term_eval(expected: str, observed: str) -> dict[str, Any]:
    terms = critical_terms_for_text(expected)
    if not terms:
        return {"terms": [], "missing": [], "recall": 1.0}
    observed_norm = normalize_term_text(observed)
    missing = [term for term in terms if not critical_term_present(term, observed_norm)]
    recall = (len(terms) - len(missing)) / len(terms)
    return {
        "terms": terms,
        "missing": missing,
        "recall": round(recall, 6),
    }


def critical_terms_for_text(text: str) -> list[str]:
    lookup_text = text.replace("日 志", "日志")
    lower_lookup = lookup_text.lower()
    glossary = [
        "AppendEntries",
        "Append Entries",
        "追加条目",
        "Raft",
        "RPC",
        "服务器一",
        "服务器二",
        "服务器三",
        "一号服务器",
        "二号服务器",
        "三号服务器",
        "一号节点",
        "二号节点",
        "三号节点",
        "服务器",
        "领导者",
        "跟随者",
        "候选者",
        "候选人",
        "选举",
        "竞选",
        "当选",
        "投票",
        "提交",
        "请求",
        "日志条目",
        "日志",
        "任期",
        "任期号",
        "前一个日志",
        "前一个条目",
        "索引",
        "索引值",
        "下一个索引",
        "线性一致",
        "客户端",
    ]
    terms: list[str] = []
    for term in glossary:
        if term == "Raft":
            present = "raft" in lower_lookup or "拉夫特" in lookup_text
        elif term in {"服务器", "日志", "索引", "任期"}:
            present = term in lookup_text and not covered_by_more_specific_term(term, lookup_text, terms)
        else:
            present = term in lookup_text
        if present and term not in terms:
            terms.append(term)
    return terms


def covered_by_more_specific_term(term: str, text: str, existing: list[str]) -> bool:
    if term == "服务器":
        specifics = [
            "服务器一",
            "服务器二",
            "服务器三",
            "一号服务器",
            "二号服务器",
            "三号服务器",
            "一号节点",
            "二号节点",
            "三号节点",
        ]
    elif term == "日志":
        specifics = ["日志条目", "前一个日志"]
    elif term == "索引":
        specifics = ["索引值", "下一个索引"]
    elif term == "任期":
        specifics = ["任期号"]
    else:
        specifics = []
    return any(specific in text or specific in existing for specific in specifics)


def critical_term_aliases(term: str) -> list[str]:
    aliases: dict[str, list[str]] = {
        "AppendEntries": ["AppendEntries", "Append Entries", "追加条目"],
        "Append Entries": ["AppendEntries", "Append Entries", "追加条目"],
        "追加条目": ["追加条目", "AppendEntries", "Append Entries"],
        "Raft": [
            "Raft",
            "RAFT",
            "Raph",
            "Ralph",
            "Rafter",
            "Raphter",
            "Rough",
            "Ruft",
            "Rafting",
            "拉夫特",
        ],
        "服务器一": ["服务器一", "一号服务器", "一号节点"],
        "服务器二": ["服务器二", "二号服务器", "二号节点"],
        "服务器三": ["服务器三", "三号服务器", "三号节点"],
        "一号服务器": ["一号服务器", "服务器一", "一号节点"],
        "二号服务器": ["二号服务器", "服务器二", "二号节点"],
        "三号服务器": ["三号服务器", "服务器三", "三号节点"],
        "一号节点": ["一号节点", "一号服务器", "服务器一"],
        "二号节点": ["二号节点", "二号服务器", "服务器二"],
        "三号节点": ["三号节点", "三号服务器", "服务器三"],
        "候选者": ["候选者", "候选人"],
        "候选人": ["候选人", "候选者"],
        "日志条目": ["日志条目", "日志项", "条目"],
        "前一个日志": ["前一个日志", "前一个日志条目", "前一个条目"],
        "前一个条目": ["前一个条目", "前一个日志条目", "前一个日志"],
        "下一个索引": ["下一个索引", "nextindex"],
        "线性一致": ["线性一致", "linearizability"],
    }
    return aliases.get(term, [term])


def critical_term_present(term: str, observed_norm: str) -> bool:
    return any(alias_present(normalize_term_text(alias), observed_norm) for alias in critical_term_aliases(term))


def alias_present(alias_norm: str, observed_norm: str) -> bool:
    if not alias_norm:
        return False
    if alias_norm.isascii() and any("a" <= ch <= "z" for ch in alias_norm):
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(alias_norm)}(?![a-z0-9])")
        return bool(pattern.search(observed_norm))
    return alias_norm in observed_norm


def normalize_term_text(text: str) -> str:
    return (
        text.lower()
        .replace(" ", "")
        .replace("-", "")
        .replace("_", "")
        .replace("２", "2")
        .replace("１", "1")
        .replace("2", "二")
        .replace("1", "一")
    )


def concat_audio(
    container: str,
    selected: list[dict[str, Any]],
    output_relpath: str,
    target_ms: int,
    max_fit_tempo_delta: float,
    *,
    pad_shortfall: bool,
) -> dict[str, Any]:
    relpaths = [str(row["audio_relpath"]) for row in selected]
    timeline_rows = [dict(row) for row in selected]
    padding_segments: list[dict[str, Any]] = []
    total_actual = sum(int(row.get("actual_ms") or 0) for row in selected)
    should_pad = (
        pad_shortfall
        and target_ms > 0
        and total_actual > 0
        and total_actual < target_ms
        and (total_actual / target_ms) < (1.0 - max(0.0, max_fit_tempo_delta))
    )
    if should_pad:
        relpaths = []
        timeline_rows = []
        padded_total = 0
        can_pad_by_allocations = all(
            int(row.get("actual_ms") or 0) <= int(row.get("target_ms") or 0) + 120
            for row in selected
        )
        for row in selected:
            copied = dict(row)
            relpaths.append(str(row["audio_relpath"]))
            actual_ms = int(row.get("actual_ms") or 0)
            pad_ms = 0
            if can_pad_by_allocations:
                pad_ms = max(0, int(row.get("target_ms") or 0) - actual_ms)
            copied["padding_after_ms"] = pad_ms
            copied["actual_ms"] = actual_ms + pad_ms
            timeline_rows.append(copied)
            padded_total += actual_ms + pad_ms
            if pad_ms >= 80:
                silence_relpath = str(
                    Path(output_relpath).parent
                    / f"{Path(output_relpath).stem}-silence-{int(row['chunk_index']):02d}.wav"
                ).replace("\\", "/")
                create_silence(container, silence_relpath, pad_ms)
                relpaths.append(silence_relpath)
                padding_segments.append(
                    {
                        "after_chunk_index": row["chunk_index"],
                        "padding_ms": pad_ms,
                        "audio_relpath": silence_relpath,
                    }
                )
        trailing_pad_ms = max(0, target_ms - padded_total)
        if trailing_pad_ms >= 80:
            silence_relpath = str(
                Path(output_relpath).parent / f"{Path(output_relpath).stem}-silence-tail.wav"
            ).replace("\\", "/")
            create_silence(container, silence_relpath, trailing_pad_ms)
            relpaths.append(silence_relpath)
            if timeline_rows:
                timeline_rows[-1]["padding_after_ms"] = (
                    int(timeline_rows[-1].get("padding_after_ms") or 0) + trailing_pad_ms
                )
                timeline_rows[-1]["actual_ms"] = int(timeline_rows[-1]["actual_ms"]) + trailing_pad_ms
            padding_segments.append(
                {
                    "after_chunk_index": "tail",
                    "padding_ms": trailing_pad_ms,
                    "audio_relpath": silence_relpath,
                }
            )
    lines = "".join(f"file '/data/{rel}'\n" for rel in relpaths)
    script = (
        "import pathlib,sys,subprocess;"
        "lst=pathlib.Path('/tmp/chunk_select_concat.txt');"
        "lst.write_text(sys.argv[1], encoding='utf-8');"
        "out=pathlib.Path('/data')/sys.argv[2];"
        "out.parent.mkdir(parents=True, exist_ok=True);"
        "subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',str(lst),"
        "'-ac','1','-c:a','pcm_s16le',str(out)], check=True, capture_output=True)"
    )
    subprocess.run(
        ["docker", "exec", container, "python", "-c", script, lines, output_relpath],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    return {
        "padding_applied": bool(padding_segments),
        "padding_total_ms": sum(int(row["padding_ms"]) for row in padding_segments),
        "padding_segments": padding_segments,
        "timeline_rows": timeline_rows,
    }


def create_silence(container: str, relpath: str, duration_ms: int) -> None:
    script = (
        "import pathlib,subprocess,sys;"
        "out=pathlib.Path('/data')/sys.argv[1];"
        "out.parent.mkdir(parents=True, exist_ok=True);"
        "duration=max(0.001, int(sys.argv[2])/1000);"
        "subprocess.run(['ffmpeg','-y','-f','lavfi','-i','anullsrc=r=24000:cl=mono',"
        "'-t',f'{duration:.3f}','-ac','1','-c:a','pcm_s16le',str(out)],"
        "check=True,capture_output=True)"
    )
    subprocess.run(
        ["docker", "exec", container, "python", "-c", script, relpath, str(duration_ms)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
