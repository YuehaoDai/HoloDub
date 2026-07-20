from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract follow-up rescue sample IDs from one or more LoRA canary loop summaries."
    )
    parser.add_argument("--summary", action="append", required=True, help="Path to loop_summary.json.")
    parser.add_argument("--output", required=True, help="Output text file, one sample ID per line.")
    parser.add_argument("--no-no-selection", dest="include_no_selection", action="store_false", default=True)
    parser.add_argument("--no-low-asr", dest="include_low_asr", action="store_false", default=True)
    parser.add_argument(
        "--no-low-critical-terms",
        dest="include_low_critical_terms",
        action="store_false",
        default=True,
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def add_reason(
    reasons_by_sample: dict[str, list[dict[str, Any]]],
    sample_id: Any,
    reason: str,
    summary_path: Path,
    payload: dict[str, Any],
) -> None:
    if sample_id is None:
        return
    sample = str(sample_id)
    reasons_by_sample.setdefault(sample, []).append(
        {
            "reason": reason,
            "summary": str(summary_path),
            **payload,
        }
    )


def collect(args: argparse.Namespace) -> dict[str, list[dict[str, Any]]]:
    reasons_by_sample: dict[str, list[dict[str, Any]]] = {}
    for summary_arg in args.summary:
        summary_path = Path(summary_arg)
        summary = load_json(summary_path)

        if args.include_no_selection:
            for row in summary.get("no_selection_samples", []) or []:
                add_reason(
                    reasons_by_sample,
                    row.get("sample_id"),
                    "no_selection",
                    summary_path,
                    {"no_selection_reason": row.get("reason")},
                )

        if args.include_low_asr:
            for row in summary.get("low_asr_samples", []) or []:
                add_reason(
                    reasons_by_sample,
                    row.get("sample_id"),
                    "low_asr",
                    summary_path,
                    {
                        "candidate_label": row.get("candidate_label"),
                        "similarity": row.get("similarity"),
                    },
                )

        if args.include_low_critical_terms:
            for row in summary.get("low_critical_term_samples", []) or []:
                add_reason(
                    reasons_by_sample,
                    row.get("sample_id"),
                    "low_critical_terms",
                    summary_path,
                    {
                        "candidate_label": row.get("candidate_label"),
                        "critical_term_recall": row.get("critical_term_recall"),
                        "critical_terms_missing": row.get("critical_terms_missing"),
                    },
                )
    return reasons_by_sample


def main() -> int:
    args = parse_args()
    reasons_by_sample = collect(args)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    sample_ids = sorted(reasons_by_sample)
    output.write_text("\n".join(sample_ids) + ("\n" if sample_ids else ""), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "count": len(sample_ids),
                "sample_ids": sample_ids,
                "reasons_by_sample": reasons_by_sample,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
