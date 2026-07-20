from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lora_asr_eval import critical_term_metrics, similarity  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recompute critical-term metrics in existing ASR reports.")
    parser.add_argument("--report", action="append", default=[], help="ASR report path. May be repeated.")
    return parser.parse_args()


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def recompute_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_variant.setdefault(str(row["variant"]), []).append(row)

    summary: dict[str, Any] = {}
    for variant, values in by_variant.items():
        sims = [float(row["similarity"]) for row in values if row.get("similarity") is not None]
        terms = [
            float(row["critical_term_recall"])
            for row in values
            if row.get("critical_term_recall") is not None
        ]
        latin = [
            float(row["latin_surface_recall"])
            for row in values
            if row.get("latin_surface_recall") is not None
        ]
        summary[variant] = {
            "count": len(values),
            "mean_similarity": mean(sims),
            "min_similarity": min(sims) if sims else None,
            "mean_critical_term_recall": mean(terms),
            "min_critical_term_recall": min(terms) if terms else None,
            "mean_latin_surface_recall": mean(latin),
            "min_latin_surface_recall": min(latin) if latin else None,
        }
    return summary


def rescore_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for row in payload.get("rows", []):
        if row.get("expected_text") is not None and row.get("asr_text") is not None:
            expected = str(row["expected_text"])
            asr_text = str(row["asr_text"])
            row = {
                **row,
                "similarity": similarity(expected, asr_text),
                **critical_term_metrics(expected, asr_text),
            }
        rows.append(row)
    payload["rows"] = rows
    payload["summary"] = recompute_summary(rows)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return payload["summary"]


def main() -> int:
    args = parse_args()
    for raw in args.report:
        path = Path(raw)
        summary = rescore_report(path)
        print(json.dumps({"report": str(path), "summary": summary}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
