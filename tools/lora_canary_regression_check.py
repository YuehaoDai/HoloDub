from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assert LoRA canary selected metrics and anchor decisions.")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--gate-report", required=True)
    parser.add_argument("--risk-queue", default="")
    parser.add_argument("--min-count", type=int, default=84)
    parser.add_argument("--max-mean-drift-ms", type=float, default=50.0)
    parser.add_argument("--max-max-drift-ms", type=float, default=300.0)
    parser.add_argument("--min-mean-similarity", type=float, default=0.99)
    parser.add_argument("--min-min-similarity", type=float, default=0.96)
    parser.add_argument("--max-join-quality-failures", type=int, default=0)
    parser.add_argument(
        "--expect-chain",
        action="append",
        default=[],
        help="Anchor expectation as sample_id=label that must appear in selected label chain.",
    )
    parser.add_argument(
        "--forbid-chain",
        action="append",
        default=[],
        help="Anchor expectation as sample_id=label that must not appear in selected label chain.",
    )
    parser.add_argument(
        "--expect-risk-sample",
        action="append",
        default=[],
        help="Sample ID that must appear in risk_queue.json.",
    )
    return parser.parse_args()


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_summary_or_gate(summary_path: str | Path, gate: dict[str, Any]) -> dict[str, Any]:
    path = Path(summary_path)
    if path.exists():
        return read_json(path)
    selected = gate.get("summary", {}).get("selected")
    if isinstance(selected, dict):
        return {"summary": {"selected": selected}, "source": "candidate_gate_report"}
    raise FileNotFoundError(f"summary not found and gate report has no selected summary: {path}")


def parse_pair(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise ValueError(f"expected sample_id=label, got {value!r}")
    sample_id, label = value.split("=", 1)
    return sample_id.strip(), label.strip()


def selected_metrics(summary: dict[str, Any]) -> dict[str, Any]:
    metrics = summary.get("selected_metrics")
    if isinstance(metrics, dict):
        return metrics
    metrics = summary.get("summary", {}).get("selected")
    if isinstance(metrics, dict):
        return metrics
    raise ValueError("summary does not contain selected metrics")


def label_chain(row: dict[str, Any]) -> list[str]:
    chain: list[str] = []
    for value in (
        row.get("candidate_label"),
        row.get("upstream_candidate_label"),
        *(row.get("candidate_label_chain") or []),
    ):
        if value is None:
            continue
        text = str(value)
        if text and text not in chain:
            chain.append(text)
    return chain


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> int:
    args = parse_args()
    gate = read_json(args.gate_report)
    summary = read_summary_or_gate(args.summary, gate)
    metrics = selected_metrics(summary)

    checks = [
        (int(metrics.get("count") or 0) >= args.min_count, f"count {metrics.get('count')} < {args.min_count}"),
        (
            float(metrics.get("mean_abs_drift_ms") or 0.0) < args.max_mean_drift_ms,
            f"mean_abs_drift_ms {metrics.get('mean_abs_drift_ms')} >= {args.max_mean_drift_ms}",
        ),
        (
            float(metrics.get("max_abs_drift_ms") or 0.0) < args.max_max_drift_ms,
            f"max_abs_drift_ms {metrics.get('max_abs_drift_ms')} >= {args.max_max_drift_ms}",
        ),
        (
            float(metrics.get("mean_similarity") or 0.0) >= args.min_mean_similarity,
            f"mean_similarity {metrics.get('mean_similarity')} < {args.min_mean_similarity}",
        ),
        (
            float(metrics.get("min_similarity") or 0.0) >= args.min_min_similarity,
            f"min_similarity {metrics.get('min_similarity')} < {args.min_min_similarity}",
        ),
        (
            int(metrics.get("join_quality_fail_count") or 0) <= args.max_join_quality_failures,
            f"join_quality_fail_count {metrics.get('join_quality_fail_count')} > {args.max_join_quality_failures}",
        ),
    ]
    for ok, message in checks:
        if not ok:
            fail(message)

    rows = {
        str(row.get("sample_id")): row
        for row in gate.get("rows", [])
        if row.get("sample_id") is not None
    }
    for raw in args.expect_chain:
        sample_id, expected = parse_pair(raw)
        chain = label_chain(rows.get(sample_id, {}))
        if expected not in chain:
            fail(f"{sample_id} label chain {chain} does not contain {expected!r}")
    for raw in args.forbid_chain:
        sample_id, forbidden = parse_pair(raw)
        chain = label_chain(rows.get(sample_id, {}))
        if forbidden in chain:
            fail(f"{sample_id} label chain {chain} contains forbidden {forbidden!r}")

    if args.risk_queue and args.expect_risk_sample:
        risk = read_json(args.risk_queue)
        sample_ids = {str(sample_id) for sample_id in risk.get("sample_ids", [])}
        for sample_id in args.expect_risk_sample:
            if sample_id not in sample_ids:
                fail(f"{sample_id} missing from risk queue")

    print(json.dumps({"ok": True, "selected_metrics": metrics}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
