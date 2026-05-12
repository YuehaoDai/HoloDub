"""Three-tier baseline regression for the Quality Mainline 1-Quarter
rollout (Phase 5 / PR-14).

Operator workflow per the plan §6:

  1. Run a single video through `dev-win` and capture a "current" report
     via this script's `--mode collect`.
  2. Compare against the on-disk baseline JSON (60s / 10min / 79min)
     via `--mode diff`.
  3. Investigate any metric that regressed > 20% (testing-and-rollout.mdc §7).

This script ONLY reads. It does NOT submit jobs, so it is safe to run
from a developer workstation against a staging API. The actual job
submission stays operator-driven.

Compared to the existing run_regression.py:
  - run_regression.py:  pass/fail gate (DOES the job meet absolute
                        thresholds? — used by CI smoke).
  - run_baseline_diff.py (this file):  delta report (DID metrics get
                                       worse vs the captured baseline?
                                       — used by quarter-level rollouts).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import urllib.request
from pathlib import Path
from typing import Any


# Per-metric thresholds for the regression gate. Each entry maps
# `metric_name → max_relative_regression`. A regression is the ratio
# (current / baseline - 1) for higher-is-worse metrics (drift, cost,
# wall_time) and (baseline / current - 1) for higher-is-better
# (judge_score). When the ratio exceeds the threshold the metric is
# tagged FAIL in the output.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "drift_p50_sec": 0.20,           # +20% drift p50 fails
    "drift_p95_sec": 0.20,           # +20% drift p95 fails
    "max_segment_drift_sec": 0.20,   # +20% worst-segment drift fails
    "cost_usd_total": 0.20,          # +20% cost fails
    "wall_time_sec": 0.20,           # +20% wall time fails
    "judge_score_mean": 0.05,        # -5% judge score fails (tighter)
}

# Whether higher is worse (True) or better (False) for the regression
# direction. drift / cost / wall time = higher is worse; judge score =
# higher is better.
HIGHER_IS_WORSE: dict[str, bool] = {
    "drift_p50_sec": True,
    "drift_p95_sec": True,
    "max_segment_drift_sec": True,
    "cost_usd_total": True,
    "wall_time_sec": True,
    "judge_score_mean": False,
}


def fetch_json(url: str, timeout_sec: int = 30) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout_sec) as response:
        return json.loads(response.read().decode("utf-8"))


def collect_metrics_from_job(api_base_url: str, job_id: int) -> dict[str, Any]:
    """Pull a completed job from the API and compute the metrics we
    care about for the quarter-level regression. Returns the shape a
    baseline JSON file should have."""
    job = fetch_json(f"{api_base_url.rstrip('/')}/jobs/{job_id}")

    segments = job.get("segments", []) or []
    drift_seconds: list[float] = []
    judge_scores: list[float] = []
    max_segment_drift_sec = 0.0
    for seg in segments:
        orig_ms = int(seg.get("original_duration_ms") or 0)
        actual_ms = int(seg.get("tts_duration_ms") or 0)
        drift_sec = abs(actual_ms - orig_ms) / 1000.0
        drift_seconds.append(drift_sec)
        max_segment_drift_sec = max(max_segment_drift_sec, drift_sec)
        if seg.get("judge_score") is not None:
            judge_scores.append(float(seg["judge_score"]))

    drift_p50 = statistics.median(drift_seconds) if drift_seconds else 0.0
    drift_p95 = _percentile(drift_seconds, 0.95) if drift_seconds else 0.0
    judge_mean = statistics.fmean(judge_scores) if judge_scores else 0.0

    # Wall time + cost come from the job-level fields populated by the
    # OPT-407 cost ledger. Both default to 0 if missing (the baseline
    # JSON may also have these as 0 if the job ran before cost
    # tracking shipped — the diff path tolerates that).
    return {
        "schema_version": 1,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(),
        "job_id": job_id,
        "source_video": job.get("input_relpath", ""),
        "source_language": job.get("source_language", ""),
        "target_language": job.get("target_language", ""),
        "segment_count": len(segments),
        "metrics": {
            "drift_p50_sec": round(drift_p50, 4),
            "drift_p95_sec": round(drift_p95, 4),
            "max_segment_drift_sec": round(max_segment_drift_sec, 4),
            "judge_score_mean": round(judge_mean, 4),
            "cost_usd_total": round(float(job.get("accumulated_cost_usd") or 0.0), 4),
            "wall_time_sec": int(job.get("processing_seconds") or 0),
        },
    }


def _percentile(samples: list[float], p: float) -> float:
    """Linear-interpolation percentile (Numpy-compatible). Stdlib's
    statistics.quantiles works for fixed cut points but is awkward for
    arbitrary p; this is small enough to inline."""
    if not samples:
        return 0.0
    if len(samples) == 1:
        return samples[0]
    sorted_samples = sorted(samples)
    rank = p * (len(sorted_samples) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = rank - lo
    return sorted_samples[lo] * (1 - frac) + sorted_samples[hi] * frac


def normalize_legacy_baseline(payload: dict[str, Any]) -> dict[str, Any]:
    """Older baselines (baseline-pre-p0.json, baseline-post-p0-10min-*.json,
    opt402-79min-episode-139.json) were captured before this harness
    existed and use a different shape: drift lives under
    `segments.drift_p50` / `segments.drift_p95`, wall time under
    `wall_clock.total_sec`, etc.

    This function projects those into the schema the differ expects so
    the on-disk files can be used as baselines as-is. It is a NO-OP
    when the input already has `metrics` (i.e. was produced by
    `collect_metrics_from_job`)."""
    if "metrics" in payload and isinstance(payload["metrics"], dict):
        return payload

    metrics: dict[str, Any] = {}
    segments = payload.get("segments", {})
    if isinstance(segments, dict):
        # drift_p50/p95 in the legacy file are ratios; convert to
        # seconds by multiplying through the target duration when we
        # have per-segment data. Otherwise leave as-is and let the
        # comparison be n/a — the harness tolerates that.
        drift_ratios_to_sec = None
        drift_entries = segments.get("drift")
        if isinstance(drift_entries, list) and drift_entries:
            # Compute drift in seconds per entry, then re-derive
            # p50/p95 in seconds. This is more faithful than scaling
            # the legacy ratio by a single number.
            drift_seconds: list[float] = []
            for entry in drift_entries:
                target_ms = float(entry.get("target_ms") or 0)
                tts_ms = float(entry.get("tts_ms") or 0)
                drift_seconds.append(abs(tts_ms - target_ms) / 1000.0)
            metrics["drift_p50_sec"] = round(statistics.median(drift_seconds), 4)
            metrics["drift_p95_sec"] = round(_percentile(drift_seconds, 0.95), 4)
            metrics["max_segment_drift_sec"] = round(max(drift_seconds), 4)
            drift_ratios_to_sec = True
        if drift_ratios_to_sec is None:
            # No per-segment data → mark missing so diff returns n/a.
            metrics.setdefault("drift_p50_sec", None)
            metrics.setdefault("drift_p95_sec", None)
            metrics.setdefault("max_segment_drift_sec", None)

    wall_clock = payload.get("wall_clock", {})
    if isinstance(wall_clock, dict) and wall_clock.get("total_sec") is not None:
        metrics["wall_time_sec"] = int(wall_clock["total_sec"])
    elif payload.get("job", {}).get("total_wall_sec") is not None:
        # baseline-post-p0-10min-final.json shape
        metrics["wall_time_sec"] = int(payload["job"]["total_wall_sec"])
    else:
        metrics.setdefault("wall_time_sec", None)

    # cost / judge score are not consistently present in legacy files;
    # tolerate absence.
    cost = payload.get("cost_usd")
    metrics["cost_usd_total"] = float(cost) if cost is not None else None
    judge_avg = payload.get("opt002_long_video_validation", {}).get("judge_avg_score")
    metrics["judge_score_mean"] = float(judge_avg) if judge_avg is not None else None

    return {
        "schema_version": 1,
        "captured_at": payload.get("captured_at"),
        "job_id": payload.get("job_id") or payload.get("job", {}).get("id"),
        "source_video": payload.get("test_video") or payload.get("source_video_relpath", ""),
        "source_language": payload.get("source_language", ""),
        "target_language": payload.get("target_language", ""),
        "metrics": metrics,
    }


def diff_against_baseline(
    baseline: dict[str, Any],
    current: dict[str, Any],
    thresholds: dict[str, float] = DEFAULT_THRESHOLDS,
) -> dict[str, Any]:
    """Compute per-metric deltas and pass/fail tags. Returns a single
    dict suitable for JSON-dumping into a PR comment.

    A missing metric on either side is tagged `n/a` (no comparison,
    no failure) so the script tolerates baselines captured before a
    given metric existed.
    """
    b_metrics = baseline.get("metrics", {}) or {}
    c_metrics = current.get("metrics", {}) or {}
    diffs: list[dict[str, Any]] = []
    any_fail = False

    all_keys = sorted(set(b_metrics) | set(c_metrics))
    for key in all_keys:
        bv = b_metrics.get(key)
        cv = c_metrics.get(key)
        if bv is None or cv is None or bv == 0:
            diffs.append({"metric": key, "baseline": bv, "current": cv, "status": "n/a"})
            continue
        # Regression is computed in the "higher-is-worse" direction.
        if HIGHER_IS_WORSE.get(key, True):
            regression = (cv - bv) / bv
        else:
            regression = (bv - cv) / bv
        threshold = thresholds.get(key, 0.20)
        status = "PASS"
        if regression > threshold:
            status = "FAIL"
            any_fail = True
        elif regression > 0:
            # Got worse but within tolerance — surface so reviewers
            # see the trend.
            status = "WARN"
        diffs.append({
            "metric": key,
            "baseline": bv,
            "current": cv,
            "regression_pct": round(regression * 100, 2),
            "threshold_pct": round(threshold * 100, 2),
            "status": status,
        })

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "baseline_captured_at": baseline.get("captured_at"),
        "current_captured_at": current.get("captured_at"),
        "baseline_job_id": baseline.get("job_id"),
        "current_job_id": current.get("job_id"),
        "overall_status": "FAIL" if any_fail else "PASS",
        "diffs": diffs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Three-tier baseline regression for the quality mainline.",
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_collect = sub.add_parser(
        "collect",
        help="Pull a completed job's metrics and write a baseline-shaped JSON.",
    )
    p_collect.add_argument("--api-base-url", required=True)
    p_collect.add_argument("--job-id", type=int, required=True)
    p_collect.add_argument(
        "--output",
        required=True,
        help="Path to write the resulting JSON. Overwrites if exists.",
    )

    p_diff = sub.add_parser(
        "diff",
        help="Diff a current report against an on-disk baseline; exit nonzero on FAIL.",
    )
    p_diff.add_argument("--baseline", required=True, help="Path to baseline JSON.")
    p_diff.add_argument(
        "--current",
        required=False,
        help="Path to current JSON. If omitted, --api-base-url + --job-id must be set.",
    )
    p_diff.add_argument("--api-base-url")
    p_diff.add_argument("--job-id", type=int)
    p_diff.add_argument("--output", help="Optional; write the diff report here too.")

    args = parser.parse_args()

    if args.mode == "collect":
        report = collect_metrics_from_job(args.api_base_url, args.job_id)
        Path(args.output).write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote baseline-shaped report to {args.output}")
        return 0

    if args.mode == "diff":
        # `utf-8-sig` tolerates the BOM PowerShell tends to write into
        # JSON files (see opt402-79min-episode-139.json captured by
        # Out-File). Regular UTF-8 reads also work via this codec.
        raw_baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8-sig"))
        baseline = normalize_legacy_baseline(raw_baseline)
        if args.current:
            current = normalize_legacy_baseline(
                json.loads(Path(args.current).read_text(encoding="utf-8-sig"))
            )
        else:
            if not args.api_base_url or args.job_id is None:
                parser.error("--current OR (--api-base-url + --job-id) is required for diff mode")
            current = collect_metrics_from_job(args.api_base_url, args.job_id)
        report = diff_against_baseline(baseline, current)
        out = json.dumps(report, indent=2, ensure_ascii=False)
        print(out)
        if args.output:
            Path(args.output).write_text(out, encoding="utf-8")
        return 0 if report["overall_status"] == "PASS" else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
