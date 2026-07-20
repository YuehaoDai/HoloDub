from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select baseline or LoRA inference variants using duration and ASR gates."
    )
    parser.add_argument("--asr-report", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-lora-target-sec", type=float, default=15.0)
    parser.add_argument("--min-drift-gain-ms", type=float, default=250.0)
    parser.add_argument("--max-similarity-drop", type=float, default=0.015)
    parser.add_argument("--min-lora-similarity", type=float, default=0.95)
    parser.add_argument("--short-max-lora-drift-ms", type=float, default=500.0)
    return parser.parse_args()


def abs_drift_ms(row: dict[str, Any]) -> float:
    return abs((float(row["duration_sec"]) - float(row["target_sec"])) * 1000.0)


def signed_drift_ms(row: dict[str, Any]) -> float:
    return (float(row["duration_sec"]) - float(row["target_sec"])) * 1000.0


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def choose_variant(
    baseline: dict[str, Any],
    lora: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    target_sec = float(lora["target_sec"])
    baseline_abs = abs_drift_ms(baseline)
    lora_abs = abs_drift_ms(lora)
    drift_gain = baseline_abs - lora_abs
    sim_drop = float(baseline["similarity"]) - float(lora["similarity"])

    if float(lora["similarity"]) < args.min_lora_similarity:
        reasons.append(f"reject_lora_low_similarity:{float(lora['similarity']):.4f}")
        return "baseline", reasons
    if sim_drop > args.max_similarity_drop:
        reasons.append(f"reject_lora_similarity_drop:{sim_drop:.4f}")
        return "baseline", reasons
    if target_sec < args.min_lora_target_sec and lora_abs > args.short_max_lora_drift_ms:
        reasons.append(f"reject_lora_short_overstretch:{lora_abs:.0f}ms")
        return "baseline", reasons
    if drift_gain >= args.min_drift_gain_ms:
        reasons.append(f"use_lora_drift_gain:{drift_gain:.0f}ms")
        return "lora", reasons
    if target_sec >= args.min_lora_target_sec and float(lora["similarity"]) > float(baseline["similarity"]):
        reasons.append("use_lora_similarity_gain")
        return "lora", reasons

    reasons.append(f"use_baseline_no_gain:{drift_gain:.0f}ms")
    return "baseline", reasons


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "mean_abs_drift_ms": None,
            "max_abs_drift_ms": None,
            "mean_similarity": None,
            "min_similarity": None,
        }
    abs_drifts = [abs_drift_ms(row) for row in rows]
    sims = [float(row["similarity"]) for row in rows]
    return {
        "count": len(rows),
        "mean_abs_drift_ms": mean(abs_drifts),
        "max_abs_drift_ms": max(abs_drifts),
        "mean_similarity": mean(sims),
        "min_similarity": min(sims),
    }


def main() -> int:
    args = parse_args()
    report = json.loads(Path(args.asr_report).read_text(encoding="utf-8"))
    by_id: dict[str, dict[str, dict[str, Any]]] = {}
    for row in report["rows"]:
        by_id.setdefault(row["sample_id"], {})[row["variant"]] = row

    selected: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    lora_rows: list[dict[str, Any]] = []

    for sample_id in sorted(by_id):
        variants = by_id[sample_id]
        baseline = variants.get("baseline")
        lora = variants.get("lora")
        if baseline is None or lora is None:
            continue
        baseline_rows.append(baseline)
        lora_rows.append(lora)
        choice, reasons = choose_variant(baseline, lora, args)
        chosen = lora if choice == "lora" else baseline
        selected.append(chosen)
        decisions.append(
            {
                "sample_id": sample_id,
                "choice": choice,
                "reasons": reasons,
                "baseline_abs_drift_ms": abs_drift_ms(baseline),
                "lora_abs_drift_ms": abs_drift_ms(lora),
                "baseline_signed_drift_ms": signed_drift_ms(baseline),
                "lora_signed_drift_ms": signed_drift_ms(lora),
                "baseline_similarity": baseline["similarity"],
                "lora_similarity": lora["similarity"],
                "chosen_path": chosen["path"],
            }
        )

    payload = {
        "asr_report": args.asr_report,
        "policy": {
            "min_lora_target_sec": args.min_lora_target_sec,
            "min_drift_gain_ms": args.min_drift_gain_ms,
            "max_similarity_drop": args.max_similarity_drop,
            "min_lora_similarity": args.min_lora_similarity,
            "short_max_lora_drift_ms": args.short_max_lora_drift_ms,
        },
        "decisions": decisions,
        "summary": {
            "baseline": summarize(baseline_rows),
            "lora": summarize(lora_rows),
            "selected": summarize(selected),
        },
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
