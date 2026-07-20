from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


HARD_REJECTION_REASONS = {
    "bad_join_quality",
    "high_drift",
    "high_fit_delta",
    "low_asr",
    "low_term_recall",
    "tail_truncation",
}
SOFT_FALLBACK_REASONS = {"flat_f0"}
AUTO_HARD_REJECTION_REASONS = HARD_REJECTION_REASONS


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Select the best LoRA TTS candidate per sample.")
    parser.add_argument("--candidate", action="append", default=[], help="Candidate as label=asr_report.json")
    parser.add_argument("--prosody-report", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--selected-dir", default="")
    parser.add_argument("--max-drift-ms", type=float, default=500.0)
    parser.add_argument("--target-mean-drift-ms", type=float, default=300.0)
    parser.add_argument("--min-similarity", type=float, default=0.97)
    parser.add_argument("--target-similarity", type=float, default=0.985)
    parser.add_argument("--min-f0-range", type=float, default=4.5)
    parser.add_argument("--target-f0-range", type=float, default=5.4)
    parser.add_argument("--min-critical-term-recall", type=float, default=0.8)
    parser.add_argument("--target-critical-term-recall", type=float, default=0.95)
    parser.add_argument(
        "--min-tail-recall",
        type=float,
        default=0.0,
        help="Reject candidates whose final spoken tail is missing from the ASR tail window. 0 disables.",
    )
    parser.add_argument("--max-fit-delta", type=float, default=0.05)
    parser.add_argument("--allow-failed-join-quality", action="store_true")
    parser.add_argument(
        "--listening-overrides",
        action="append",
        default=[],
        help="JSON file with human listening overrides. May be repeated.",
    )
    return parser.parse_args()


def parse_candidate(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"--candidate must be label=path, got {value!r}")
    label, raw_path = value.split("=", 1)
    label = label.strip()
    if not label:
        raise ValueError(f"empty candidate label: {value!r}")
    return label, Path(raw_path.strip())


def load_listening_overrides(paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    overrides: dict[str, list[dict[str, Any]]] = {}
    for value in paths:
        if not value:
            continue
        payload = json.loads(Path(value).read_text(encoding="utf-8"))
        current = payload.get("overrides", []) if isinstance(payload, dict) else payload
        if not isinstance(current, list):
            raise ValueError(f"listening overrides must be a list or {{'overrides': [...]}}: {value}")
        for override in current:
            if not isinstance(override, dict):
                continue
            sample_id = override.get("sample_id")
            if sample_id is None:
                continue
            action = str(override.get("action") or "select")
            if action != "select":
                continue
            overrides.setdefault(str(sample_id), []).append(override)
    return overrides


def abs_drift_ms(row: dict[str, Any]) -> float:
    return abs((float(row["duration_sec"]) - float(row["target_sec"])) * 1000.0)


def signed_drift_ms(row: dict[str, Any]) -> float:
    return (float(row["duration_sec"]) - float(row["target_sec"])) * 1000.0


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def normalize_path(path: str) -> str:
    return str(Path(path.replace("/data/", "data/"))).replace("\\", "/").lower()


def load_prosody(path: Path) -> dict[str, dict[str, Any]]:
    if not path or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    by_path: dict[str, dict[str, Any]] = {}
    for row in payload.get("rows", []):
        if row.get("path"):
            by_path[normalize_path(str(row["path"]))] = row
    return by_path


def normalize_spoken_tail_text(value: Any) -> str:
    text = str(value or "").lower()
    chars: list[str] = []
    for char in text:
        if char.isalnum() or "\u4e00" <= char <= "\u9fff":
            chars.append(char)
    return "".join(chars)


def lcs_len(left: str, right: str) -> int:
    if not left or not right:
        return 0
    prev = [0] * (len(right) + 1)
    for lch in left:
        curr = [0] * (len(right) + 1)
        for idx, rch in enumerate(right, start=1):
            if lch == rch:
                curr[idx] = prev[idx - 1] + 1
            else:
                curr[idx] = max(prev[idx], curr[idx - 1])
        prev = curr
    return prev[-1]


def tail_completeness(expected: Any, observed: Any, tail_len: int = 4) -> dict[str, Any]:
    expected_norm = normalize_spoken_tail_text(expected)
    observed_norm = normalize_spoken_tail_text(observed)
    if len(expected_norm) < tail_len or not observed_norm:
        return {"tail_recall": None, "tail_expected": "", "tail_observed_window": ""}
    expected_tail = expected_norm[-tail_len:]
    observed_window = observed_norm[-max(12, tail_len * 4) :]
    recall = lcs_len(expected_tail, observed_window) / float(len(expected_tail))
    return {
        "tail_recall": recall,
        "tail_expected": expected_tail,
        "tail_observed_window": observed_window,
    }


def load_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    prosody_by_path = load_prosody(Path(args.prosody_report)) if args.prosody_report else {}
    rows: list[dict[str, Any]] = []
    for raw in args.candidate:
        label, report_path = parse_candidate(raw)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            path_key = normalize_path(str(row.get("path") or ""))
            prosody = prosody_by_path.get(path_key, {})
            upstream_candidate_label = row.get("candidate_label")
            candidate_label_chain: list[str] = []
            for value in (
                label,
                upstream_candidate_label,
                row.get("upstream_candidate_label"),
                *(row.get("candidate_label_chain") or []),
            ):
                if value is None:
                    continue
                text = str(value)
                if text and text not in candidate_label_chain:
                    candidate_label_chain.append(text)
            tail = tail_completeness(row.get("expected_text") or row.get("text"), row.get("asr_text"))
            rows.append(
                {
                    **row,
                    **tail,
                    "candidate_label": label,
                    "upstream_candidate_label": upstream_candidate_label,
                    "candidate_label_chain": candidate_label_chain,
                    "candidate_report": str(report_path),
                    "abs_drift_ms": abs_drift_ms(row),
                    "signed_drift_ms": signed_drift_ms(row),
                    "f0_range_semitones_10_90": prosody.get("f0_range_semitones_10_90"),
                    "f0_std_semitones": prosody.get("f0_std_semitones"),
                    "rms_dbfs_range_10_90": prosody.get("rms_dbfs_range_10_90"),
                    "silence_ratio": prosody.get("silence_ratio"),
                }
            )
    return rows


def rejection_reasons(row: dict[str, Any], args: argparse.Namespace) -> list[str]:
    reasons: list[str] = []
    if float(row.get("similarity") or 0.0) < args.min_similarity:
        reasons.append("low_asr")
    if float(row["abs_drift_ms"]) > args.max_drift_ms:
        reasons.append("high_drift")
    fit_delta = row.get("fit_tempo_delta")
    if fit_delta is not None and float(fit_delta) > args.max_fit_delta:
        reasons.append("high_fit_delta")
    f0_range = row.get("f0_range_semitones_10_90")
    if f0_range is not None and float(f0_range) < args.min_f0_range:
        reasons.append("flat_f0")
    term_recall = row.get("critical_term_recall")
    if term_recall is not None and float(term_recall) < args.min_critical_term_recall:
        reasons.append("low_term_recall")
    tail_recall = row.get("tail_recall")
    if args.min_tail_recall > 0 and tail_recall is not None and float(tail_recall) < args.min_tail_recall:
        reasons.append("tail_truncation")
    join_quality = row.get("join_quality")
    if (
        join_quality is not None
        and not args.allow_failed_join_quality
        and not bool(join_quality.get("pass"))
    ):
        reasons.append("bad_join_quality")
    return reasons


def candidate_score(row: dict[str, Any], args: argparse.Namespace) -> float:
    similarity = float(row.get("similarity") or 0.0)
    drift = float(row["abs_drift_ms"])
    f0_range = float(row.get("f0_range_semitones_10_90") or 0.0)
    fit_delta = float(row.get("fit_tempo_delta") or 0.0)
    term_recall = row.get("critical_term_recall")

    drift_score = max(0.0, 1.0 - drift / max(args.max_drift_ms, 1.0))
    asr_score = min(1.05, similarity / max(args.target_similarity, 1e-6))
    f0_score = min(1.15, f0_range / max(args.target_f0_range, 1e-6)) if f0_range > 0 else 0.6
    term_score = (
        min(1.05, float(term_recall) / max(args.target_critical_term_recall, 1e-6))
        if term_recall is not None
        else 1.0
    )
    fit_penalty = min(0.4, fit_delta * 3.0)
    return 0.28 * drift_score + 0.42 * asr_score + 0.15 * f0_score + 0.12 * term_score - 0.03 * fit_penalty


def fallback_quality_key(row: dict[str, Any], args: argparse.Namespace) -> tuple[float, ...]:
    similarity = float(row.get("similarity") or 0.0)
    drift = float(row["abs_drift_ms"])
    f0_range = row.get("f0_range_semitones_10_90")
    fit_delta = float(row.get("fit_tempo_delta") or 0.0)
    term_recall = row.get("critical_term_recall")
    latin_recall = row.get("latin_surface_recall")

    term_score = float(term_recall) if term_recall is not None else 1.0
    latin_score = float(latin_recall) if latin_recall is not None else 1.0
    f0_score = float(f0_range) if f0_range is not None else args.min_f0_range
    return (
        1.0 if similarity >= args.min_similarity else 0.0,
        1.0 if term_recall is None or term_score >= args.min_critical_term_recall else 0.0,
        similarity,
        term_score,
        latin_score,
        1.0 if f0_range is None or f0_score >= args.min_f0_range else 0.0,
        -drift,
        -fit_delta,
        float(row["score"]),
    )


def row_label_set(row: dict[str, Any]) -> set[str]:
    labels: set[str] = set()
    for value in (
        row.get("candidate_label"),
        row.get("upstream_candidate_label"),
        *(row.get("candidate_label_chain") or []),
    ):
        if value is not None:
            labels.add(str(value))
    return labels


def listening_override_match(row: dict[str, Any], override: dict[str, Any]) -> bool:
    target = override.get("candidate_label") or override.get("label")
    return target is not None and str(target) in row_label_set(row)


def select_listening_override(
    sample_id: str,
    annotated: list[dict[str, Any]],
    overrides: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    for override in overrides.get(sample_id, []):
        matches = [row for row in annotated if listening_override_match(row, override)]
        if not matches:
            target = override.get("candidate_label") or override.get("label")
            raise SystemExit(f"listening override target not found for {sample_id}: {target}")
        chosen = max(matches, key=lambda row: row["score"])
        reject_reasons = set(chosen["reject_reasons"])
        hard = sorted(reject_reasons & HARD_REJECTION_REASONS)
        if hard:
            target = override.get("candidate_label") or override.get("label")
            raise SystemExit(
                f"listening override target {target} for {sample_id} has hard rejection reasons: {hard}"
            )
        unsupported = sorted(reject_reasons - SOFT_FALLBACK_REASONS)
        if unsupported:
            target = override.get("candidate_label") or override.get("label")
            raise SystemExit(
                f"listening override target {target} for {sample_id} has unsupported rejection reasons: {unsupported}"
            )
        return {**chosen, "listening_override_applied": True, "listening_override": override}
    return None


def eligible_fallback_pool(annotated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in annotated:
        reasons = set(row.get("reject_reasons") or [])
        if reasons & AUTO_HARD_REJECTION_REASONS:
            continue
        unsupported = reasons - SOFT_FALLBACK_REASONS
        if unsupported:
            continue
        rows.append(row)
    return rows


def decision_candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "label": row["candidate_label"],
            "upstream_candidate_label": row.get("upstream_candidate_label"),
            "candidate_label_chain": row.get("candidate_label_chain") or [],
            "score": row["score"],
            "reject_reasons": row["reject_reasons"],
            "path": row.get("path"),
            "selected_path": row.get("selected_path"),
            "selected_source_path": row.get("selected_source_path"),
            "variant": row.get("variant"),
            "duration_sec": row.get("duration_sec"),
            "target_sec": row.get("target_sec"),
            "abs_drift_ms": row["abs_drift_ms"],
            "signed_drift_ms": row.get("signed_drift_ms"),
            "similarity": row.get("similarity"),
            "critical_term_recall": row.get("critical_term_recall"),
            "critical_terms_missing": row.get("critical_terms_missing"),
            "latin_surface_recall": row.get("latin_surface_recall"),
            "tail_recall": row.get("tail_recall"),
            "tail_expected": row.get("tail_expected"),
            "tail_observed_window": row.get("tail_observed_window"),
            "f0_range_semitones_10_90": row.get("f0_range_semitones_10_90"),
            "fit_tempo_delta": row.get("fit_tempo_delta"),
            "join_quality_pass": (
                row.get("join_quality", {}).get("pass")
                if row.get("join_quality") is not None
                else None
            ),
            "join_quality_reasons": (
                row.get("join_quality", {}).get("reasons", [])
                if row.get("join_quality") is not None
                else []
            ),
        }
        for row in sorted(rows, key=lambda item: item["score"], reverse=True)
    ]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"count": 0}
    drifts = [float(row["abs_drift_ms"]) for row in rows]
    sims = [float(row.get("similarity") or 0.0) for row in rows]
    f0 = [float(row["f0_range_semitones_10_90"]) for row in rows if row.get("f0_range_semitones_10_90") is not None]
    fit = [float(row["fit_tempo_delta"]) for row in rows if row.get("fit_tempo_delta") is not None]
    terms = [float(row["critical_term_recall"]) for row in rows if row.get("critical_term_recall") is not None]
    latin = [float(row["latin_surface_recall"]) for row in rows if row.get("latin_surface_recall") is not None]
    join_quality_fail_count = sum(
        1 for row in rows if row.get("join_quality") is not None and not bool(row["join_quality"].get("pass"))
    )
    listening_override_count = sum(1 for row in rows if bool(row.get("listening_override_applied")))
    return {
        "count": len(rows),
        "mean_abs_drift_ms": mean(drifts),
        "max_abs_drift_ms": max(drifts),
        "mean_similarity": mean(sims),
        "min_similarity": min(sims),
        "mean_f0_range_semitones_10_90": mean(f0),
        "min_f0_range_semitones_10_90": min(f0) if f0 else None,
        "max_fit_tempo_delta": max(fit) if fit else None,
        "mean_critical_term_recall": mean(terms),
        "min_critical_term_recall": min(terms) if terms else None,
        "mean_latin_surface_recall": mean(latin),
        "min_latin_surface_recall": min(latin) if latin else None,
        "join_quality_fail_count": join_quality_fail_count,
        "listening_override_count": listening_override_count,
    }


def copy_selected_audio(rows: list[dict[str, Any]], selected_dir: Path) -> list[dict[str, Any]]:
    selected_dir.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, Any]] = []
    for row in rows:
        source = Path(str(row["path"]).replace("/data/", "data/"))
        if not source.exists():
            source = Path(str(row["path"]))
        suffix = source.suffix or ".wav"
        target = selected_dir / f"{row['sample_id']}-{row['candidate_label']}{suffix}"
        shutil.copyfile(source, target)
        copied.append({**row, "selected_source_path": row["path"], "path": str(target), "selected_path": str(target)})
    return copied


def main() -> int:
    args = parse_args()
    rows = load_candidates(args)
    listening_overrides = load_listening_overrides(args.listening_overrides)
    by_sample: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_sample.setdefault(str(row["sample_id"]), []).append(row)

    decisions: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for sample_id in sorted(by_sample):
        candidates = by_sample[sample_id]
        annotated = []
        for row in candidates:
            reasons = rejection_reasons(row, args)
            score = candidate_score(row, args)
            annotated.append({**row, "reject_reasons": reasons, "score": score})
        override_chosen = select_listening_override(sample_id, annotated, listening_overrides)
        if override_chosen is not None:
            selected.append(override_chosen)
            decisions.append(
                {
                    "sample_id": sample_id,
                    "choice": override_chosen["candidate_label"],
                    "fallback_used": True,
                    "fallback_tier": "listening_override",
                    "listening_override_applied": True,
                    "listening_override": override_chosen.get("listening_override"),
                    "chosen_score": override_chosen["score"],
                    "chosen_abs_drift_ms": override_chosen["abs_drift_ms"],
                    "chosen_similarity": override_chosen.get("similarity"),
                    "chosen_critical_term_recall": override_chosen.get("critical_term_recall"),
                    "chosen_critical_terms_missing": override_chosen.get("critical_terms_missing"),
                    "chosen_f0_range_semitones_10_90": override_chosen.get("f0_range_semitones_10_90"),
                    "chosen_fit_tempo_delta": override_chosen.get("fit_tempo_delta"),
                    "chosen_reject_reasons": override_chosen.get("reject_reasons", []),
                    "chosen_path": override_chosen.get("path"),
                    "candidates": decision_candidate_rows(annotated),
                }
            )
            continue
        viable = [row for row in annotated if not row["reject_reasons"]]
        fallback_pool = eligible_fallback_pool(annotated)
        within_drift_pool = fallback_pool
        if viable:
            pool = viable
            fallback_tier = "strict"
        else:
            pool = within_drift_pool
            fallback_tier = "best_within_drift"
        if not pool:
            no_selection_reason = (
                "all_candidates_failed_hard_gate"
                if not fallback_pool
                else "all_candidates_failed_strict_gate"
            )
            decisions.append(
                {
                    "sample_id": sample_id,
                    "choice": None,
                    "fallback_used": True,
                    "no_selection": True,
                    "no_selection_reason": no_selection_reason,
                    "candidates": decision_candidate_rows(annotated),
                }
            )
            continue
        if viable:
            chosen = max(pool, key=lambda row: row["score"])
        else:
            chosen = max(pool, key=lambda row: fallback_quality_key(row, args))
        selected.append(chosen)
        decisions.append(
            {
                "sample_id": sample_id,
                "choice": chosen["candidate_label"],
                "fallback_used": not bool(viable),
                "fallback_tier": fallback_tier,
                "chosen_score": chosen["score"],
                "chosen_abs_drift_ms": chosen["abs_drift_ms"],
                "chosen_similarity": chosen.get("similarity"),
                "chosen_critical_term_recall": chosen.get("critical_term_recall"),
                "chosen_critical_terms_missing": chosen.get("critical_terms_missing"),
                "chosen_f0_range_semitones_10_90": chosen.get("f0_range_semitones_10_90"),
                "chosen_fit_tempo_delta": chosen.get("fit_tempo_delta"),
                "chosen_path": chosen.get("path"),
                "candidates": decision_candidate_rows(annotated),
            }
        )

    if args.selected_dir:
        selected = copy_selected_audio(selected, Path(args.selected_dir))

    by_label: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_label.setdefault(str(row["candidate_label"]), []).append(row)

    payload = {
        "policy": {
            "max_drift_ms": args.max_drift_ms,
            "target_mean_drift_ms": args.target_mean_drift_ms,
            "min_similarity": args.min_similarity,
            "target_similarity": args.target_similarity,
            "min_f0_range": args.min_f0_range,
            "target_f0_range": args.target_f0_range,
            "min_critical_term_recall": args.min_critical_term_recall,
            "target_critical_term_recall": args.target_critical_term_recall,
            "max_fit_delta": args.max_fit_delta,
            "allow_failed_join_quality": args.allow_failed_join_quality,
            "listening_overrides": list(args.listening_overrides),
        },
        "summary": {
            "selected": summarize(selected),
            "candidates": {label: summarize(label_rows) for label, label_rows in sorted(by_label.items())},
        },
        "rows": selected,
        "decisions": decisions,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
