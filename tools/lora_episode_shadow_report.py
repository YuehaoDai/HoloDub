from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sample_id(row: dict[str, Any]) -> str:
    return str(row.get("sample_id") or "")


def count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter(str(row.get(key) or "") for row in rows)
    counts.pop("", None)
    return dict(sorted(counts.items()))


def count_rejection_reasons(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        for key in ("rejection_reasons", "reject_reasons"):
            raw = row.get(key)
            if isinstance(raw, list):
                for reason in raw:
                    if reason:
                        counts[str(reason)] += 1
                break
        else:
            reason = row.get("reject_reason") or row.get("reason")
            if reason:
                counts[str(reason)] += 1
    return dict(sorted(counts.items()))


def selected_decisions(candidate_report: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = candidate_report.get("decisions")
    return decisions if isinstance(decisions, list) else []


def selected_metrics(
    combined_summary: dict[str, Any] | None,
    candidate_report: dict[str, Any],
) -> dict[str, Any]:
    if combined_summary and isinstance(combined_summary.get("selected_metrics"), dict):
        return combined_summary["selected_metrics"]
    decisions = selected_decisions(candidate_report)
    if not decisions:
        return {"count": 0}
    drifts = [float(d.get("chosen_abs_drift_ms") or 0.0) for d in decisions]
    sims = [float(d.get("chosen_similarity") or 0.0) for d in decisions]
    f0s = [
        float(d.get("chosen_f0_range_semitones_10_90") or 0.0)
        for d in decisions
        if d.get("chosen_f0_range_semitones_10_90") is not None
    ]
    return {
        "count": len(decisions),
        "mean_abs_drift_ms": sum(drifts) / len(drifts),
        "max_abs_drift_ms": max(drifts),
        "mean_similarity": sum(sims) / len(sims),
        "min_similarity": min(sims),
        "mean_f0_range_semitones_10_90": sum(f0s) / len(f0s) if f0s else None,
        "min_f0_range_semitones_10_90": min(f0s) if f0s else None,
    }


def risk_summary(risk_queue: dict[str, Any] | None) -> dict[str, Any]:
    if not risk_queue:
        return {"count": 0, "reason_counts": {}}
    entries = risk_queue.get("entries")
    if not isinstance(entries, list):
        return {"count": int(risk_queue.get("count") or 0), "reason_counts": {}}
    reasons: Counter[str] = Counter()
    fixed_regression_ids = {"seg10569", "seg10686", "seg10634"}
    fixed_present: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("sample_id") or "")
        if sid in fixed_regression_ids:
            fixed_present.append(sid)
        for reason in entry.get("reasons") or []:
            reasons[str(reason)] += 1
    return {
        "count": len(entries),
        "reason_counts": dict(sorted(reasons.items())),
        "fixed_regression_ids_present": sorted(set(fixed_present)),
    }


def human_review_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {}
    out: dict[str, Any] = {}
    for key, value in sorted(report.items()):
        if isinstance(value, list):
            out[f"{key}_count"] = len(value)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an offline episode-level LoRA shadow coverage report."
    )
    parser.add_argument("--corpus-dir", required=True)
    parser.add_argument("--feature-manifest", required=True)
    parser.add_argument("--candidate-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--combined-summary")
    parser.add_argument("--human-review-report")
    parser.add_argument("--risk-queue")
    parser.add_argument("--top-uncovered", type=int, default=16)
    args = parser.parse_args()

    corpus_dir = Path(args.corpus_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    corpus_manifest = load_json(corpus_dir / "manifest.json")
    feature_manifest = load_json(Path(args.feature_manifest))
    candidate_report = load_json(Path(args.candidate_report))
    combined_summary = load_json(Path(args.combined_summary)) if args.combined_summary else None
    human_review = load_json(Path(args.human_review_report)) if args.human_review_report else None
    risk_queue = load_json(Path(args.risk_queue)) if args.risk_queue else None

    corpus_rows = load_jsonl(corpus_dir / str(corpus_manifest.get("files", {}).get("all", "all.jsonl")))
    manifest_reject_rows = load_jsonl(
        corpus_dir / str(corpus_manifest.get("files", {}).get("rejected", "rejected.jsonl"))
    )
    corpus_by_id = {sample_id(row): row for row in corpus_rows if sample_id(row)}
    feature_rejections = feature_manifest.get("rejections") or []
    feature_rejection_by_id = {
        str(row.get("sample_id") or ""): row
        for row in feature_rejections
        if isinstance(row, dict) and row.get("sample_id")
    }
    decisions = selected_decisions(candidate_report)
    selected_ids = {str(d.get("sample_id") or "") for d in decisions if d.get("sample_id")}
    corpus_ids = set(corpus_by_id)
    feature_uncovered_ids = sorted(corpus_ids - selected_ids)

    uncovered_rows: list[dict[str, Any]] = []
    for sid in feature_uncovered_ids:
        row = corpus_by_id.get(sid, {})
        rejection = feature_rejection_by_id.get(sid, {})
        uncovered_rows.append(
            {
                "sample_id": sid,
                "reason": rejection.get("reason") or "not_in_candidate_report",
                "split": rejection.get("split") or row.get("split"),
                "target_ms": row.get("target_ms"),
                "audio_ms": row.get("audio_ms"),
                "abs_drift_ms": row.get("abs_drift_ms"),
                "chars": row.get("chars"),
                "text_preview": str(row.get("text") or "")[:120],
            }
        )
    uncovered_rows.sort(
        key=lambda row: (
            -(int(row.get("target_ms") or 0)),
            str(row.get("sample_id") or ""),
        )
    )

    split_stats = feature_manifest.get("splits") or {}
    feature_accepted = sum(
        int(info.get("accepted") or 0)
        for info in split_stats.values()
        if isinstance(info, dict)
    )

    summary = {
        "schema_version": "holodub.lora_episode_shadow_report.v1",
        "inputs": {
            "corpus_dir": str(corpus_dir),
            "feature_manifest": str(Path(args.feature_manifest)),
            "candidate_report": str(Path(args.candidate_report)),
            "combined_summary": str(Path(args.combined_summary)) if args.combined_summary else None,
            "human_review_report": str(Path(args.human_review_report)) if args.human_review_report else None,
            "risk_queue": str(Path(args.risk_queue)) if args.risk_queue else None,
        },
        "episode_ids": corpus_manifest.get("episode_ids") or [],
        "corpus": {
            "accepted_rows": len(corpus_rows),
            "manifest_accepted": corpus_manifest.get("stats", {}).get("accepted"),
            "manifest_rejected": corpus_manifest.get("stats", {}).get("rejected"),
            "manifest_reject_rows": len(manifest_reject_rows),
            "manifest_reject_reasons": count_rejection_reasons(manifest_reject_rows),
        },
        "feature_coverage": {
            "feature_accepted": feature_accepted,
            "candidate_selected": len(selected_ids),
            "feature_rejected_before_limit": len(feature_rejections),
            "corpus_accepted_uncovered": len(feature_uncovered_ids),
            "coverage_ratio_of_corpus_accepted": (
                round(len(selected_ids) / len(corpus_rows), 6) if corpus_rows else 0
            ),
            "feature_rejection_reasons": count_by(feature_rejections, "reason"),
        },
        "selected_metrics": selected_metrics(combined_summary, candidate_report),
        "risk_queue": risk_summary(risk_queue),
        "human_review": human_review_summary(human_review),
        "uncovered_top": uncovered_rows[: args.top_uncovered],
        "blocked_for_full_episode_candidate_loop": len(feature_uncovered_ids) > 0,
        "recommended_next_steps": [
            "Run split-first feature extraction or candidate generation for feature-overlength accepted corpus rows.",
            "Keep main-flow LoRA candidate strategy in opt-in shadow until uncovered long rows have provenance.",
            "Run one real episode shadow with LORA_CANDIDATE_LOOP_ENABLED=true and LORA_SPLIT_PLANNING_ENABLED=true after the overlength report is reviewed.",
        ],
    }

    summary_path = output_dir / "episode_shadow_summary.json"
    report_path = output_dir / "episode_shadow_report.md"
    ids_path = output_dir / "episode_shadow_uncovered_ids.txt"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    ids_path.write_text("\n".join(feature_uncovered_ids) + ("\n" if feature_uncovered_ids else ""), encoding="utf-8")
    report_path.write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "report": str(report_path)}, ensure_ascii=False))


def render_markdown(summary: dict[str, Any]) -> str:
    metrics = summary.get("selected_metrics") or {}
    coverage = summary.get("feature_coverage") or {}
    risk = summary.get("risk_queue") or {}
    lines = [
        "# HoloDub LoRA Episode Shadow Report",
        "",
        "## Status",
        "",
        f"- Episode ids: {summary.get('episode_ids')}",
        f"- Corpus accepted rows: {summary.get('corpus', {}).get('accepted_rows')}",
        f"- Candidate selected rows: {coverage.get('candidate_selected')}",
        f"- Corpus accepted uncovered rows: {coverage.get('corpus_accepted_uncovered')}",
        f"- Coverage ratio: {coverage.get('coverage_ratio_of_corpus_accepted')}",
        f"- Blocked for full candidate loop: {summary.get('blocked_for_full_episode_candidate_loop')}",
        "",
        "## Selected Metrics",
        "",
        f"- Count: {metrics.get('count')}",
        f"- Mean abs drift ms: {metrics.get('mean_abs_drift_ms')}",
        f"- Max abs drift ms: {metrics.get('max_abs_drift_ms')}",
        f"- Mean ASR similarity: {metrics.get('mean_similarity')}",
        f"- Min ASR similarity: {metrics.get('min_similarity')}",
        f"- Mean F0 range: {metrics.get('mean_f0_range_semitones_10_90')}",
        f"- Min F0 range: {metrics.get('min_f0_range_semitones_10_90')}",
        "",
        "## Coverage Gaps",
        "",
        f"- Feature accepted: {coverage.get('feature_accepted')}",
        f"- Feature rejected before limit: {coverage.get('feature_rejected_before_limit')}",
        f"- Feature rejection reasons: {coverage.get('feature_rejection_reasons')}",
        "",
        "## Risk Queue",
        "",
        f"- Count: {risk.get('count')}",
        f"- Reason counts: {risk.get('reason_counts')}",
        f"- Fixed regression ids present: {risk.get('fixed_regression_ids_present')}",
        "",
        "## Top Uncovered Rows",
        "",
    ]
    for row in summary.get("uncovered_top") or []:
        lines.append(
            "- {sample_id}: {reason}, target_ms={target_ms}, chars={chars}, split={split}".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## Recommended Next Steps",
            "",
        ]
    )
    for step in summary.get("recommended_next_steps") or []:
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
