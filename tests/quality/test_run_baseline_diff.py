"""Unit tests for tests/quality/run_baseline_diff.py.

L1 verification for PR-14: the diff harness itself is the thing being
tested, NOT a job. Real video runs happen out-of-band on staging.

Run with:  python -m pytest tests/quality/test_run_baseline_diff.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


# The script lives next to this test file. We load it as a module
# directly so we don't have to install the tests/quality directory as
# a package.
_SCRIPT_PATH = Path(__file__).resolve().parent / "run_baseline_diff.py"
_spec = importlib.util.spec_from_file_location("run_baseline_diff", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["run_baseline_diff"] = mod
_spec.loader.exec_module(mod)


# ---------- _percentile ----------

def test_percentile_single_value() -> None:
    assert mod._percentile([3.14], 0.95) == 3.14


def test_percentile_empty() -> None:
    assert mod._percentile([], 0.5) == 0.0


def test_percentile_interpolates() -> None:
    # 5 evenly spaced samples; p=0.95 should land between 4 and 5.
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    got = mod._percentile(samples, 0.95)
    # rank = 0.95 * 4 = 3.8  -> 0.8 of the way from 4 to 5 = 4.8
    assert got == pytest.approx(4.8)


def test_percentile_p50_equals_median() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert mod._percentile(samples, 0.5) == pytest.approx(3.0)


# ---------- diff_against_baseline ----------

def _baseline(**overrides: float) -> dict:
    base = {
        "captured_at": "2026-05-01T00:00:00Z",
        "job_id": 100,
        "metrics": {
            "drift_p50_sec": 0.10,
            "drift_p95_sec": 0.50,
            "max_segment_drift_sec": 0.80,
            "judge_score_mean": 0.90,
            "cost_usd_total": 1.00,
            "wall_time_sec": 600,
        },
    }
    base["metrics"].update(overrides)
    return base


def _current(**overrides: float) -> dict:
    return _baseline(**overrides) | {"job_id": 999, "captured_at": "2026-05-12T00:00:00Z"}


def test_diff_pass_when_metrics_match() -> None:
    report = mod.diff_against_baseline(_baseline(), _current())
    assert report["overall_status"] == "PASS"
    # All metrics should report either PASS (regression == 0) — no WARN.
    statuses = {d["status"] for d in report["diffs"]}
    assert statuses <= {"PASS"}


def test_diff_fail_on_cost_blowup() -> None:
    # 30% cost increase blows the +20% gate.
    report = mod.diff_against_baseline(_baseline(), _current(cost_usd_total=1.30))
    assert report["overall_status"] == "FAIL"
    cost_row = next(d for d in report["diffs"] if d["metric"] == "cost_usd_total")
    assert cost_row["status"] == "FAIL"
    assert cost_row["regression_pct"] == pytest.approx(30.0, abs=0.1)


def test_diff_warn_on_minor_drift_regression() -> None:
    # 10% drift p95 increase: above 0 but below +20% gate → WARN.
    report = mod.diff_against_baseline(_baseline(), _current(drift_p95_sec=0.55))
    assert report["overall_status"] == "PASS"
    drift_row = next(d for d in report["diffs"] if d["metric"] == "drift_p95_sec")
    assert drift_row["status"] == "WARN"


def test_diff_fail_on_judge_drop() -> None:
    # judge score is "higher is better"; -10% drop blows the -5% gate.
    report = mod.diff_against_baseline(_baseline(), _current(judge_score_mean=0.81))
    assert report["overall_status"] == "FAIL"
    judge_row = next(d for d in report["diffs"] if d["metric"] == "judge_score_mean")
    assert judge_row["status"] == "FAIL"


def test_diff_pass_when_judge_improves() -> None:
    # Higher judge score is GOOD; regression direction should be negative.
    report = mod.diff_against_baseline(_baseline(), _current(judge_score_mean=0.95))
    judge_row = next(d for d in report["diffs"] if d["metric"] == "judge_score_mean")
    assert judge_row["status"] == "PASS"
    # judge improved by 5.56% → regression_pct ≈ -5.56
    assert judge_row["regression_pct"] < 0


def test_diff_handles_missing_baseline_metric() -> None:
    # Baseline pre-OPT-001 has cost = null. Current has 0.50. The row
    # should be "n/a", NOT fail.
    b = _baseline()
    b["metrics"]["cost_usd_total"] = None  # mimic old baseline schema
    report = mod.diff_against_baseline(b, _current(cost_usd_total=0.50))
    cost_row = next(d for d in report["diffs"] if d["metric"] == "cost_usd_total")
    assert cost_row["status"] == "n/a"
    # Overall PASS as long as everything else is fine.
    assert report["overall_status"] == "PASS"


def test_diff_handles_zero_baseline_metric() -> None:
    # Zero baseline can't be a denominator. Tag n/a.
    b = _baseline(wall_time_sec=0)
    report = mod.diff_against_baseline(b, _current(wall_time_sec=300))
    row = next(d for d in report["diffs"] if d["metric"] == "wall_time_sec")
    assert row["status"] == "n/a"


def test_diff_fail_aggregates_across_metrics() -> None:
    # Two FAIL metrics → overall FAIL.
    report = mod.diff_against_baseline(
        _baseline(),
        _current(cost_usd_total=1.30, wall_time_sec=800),
    )
    fails = [d for d in report["diffs"] if d["status"] == "FAIL"]
    assert {d["metric"] for d in fails} == {"cost_usd_total", "wall_time_sec"}
    assert report["overall_status"] == "FAIL"


# ---------- realistic-shape fixture ----------

# ---------- normalize_legacy_baseline ----------

def test_normalize_legacy_pre_p0_shape() -> None:
    """The on-disk baseline-pre-p0.json shape:
      - segments.drift = [{ordinal, target_ms, tts_ms, drift_ratio}, ...]
      - wall_clock.total_sec
      - cost_usd = null
    Expect we project per-segment drifts into seconds and derive p50/p95
    from them, not from the legacy ratio field."""
    legacy = {
        "version": "pre-OPT-001",
        "captured_at": "2026-05-10T02:32:08Z",
        "job_id": 124,
        "test_video": "test_60s.mp4",
        "wall_clock": {"total_sec": 559.7},
        "segments": {
            "drift": [
                {"ordinal": 0, "target_ms": 7500, "tts_ms": 7337},
                {"ordinal": 1, "target_ms": 4940, "tts_ms": 5421},
                {"ordinal": 2, "target_ms": 20540, "tts_ms": 19820},
                {"ordinal": 3, "target_ms": 14800, "tts_ms": 14793},
                {"ordinal": 4, "target_ms": 4803, "tts_ms": 4667},
            ],
        },
        "cost_usd": None,
    }
    normalized = mod.normalize_legacy_baseline(legacy)
    m = normalized["metrics"]
    # drifts in sec: 0.163, 0.481, 0.720, 0.007, 0.136
    assert m["drift_p50_sec"] == pytest.approx(0.163, abs=0.001)
    assert m["max_segment_drift_sec"] == pytest.approx(0.720, abs=0.001)
    assert m["wall_time_sec"] == 559
    assert m["cost_usd_total"] is None
    assert m["judge_score_mean"] is None


def test_normalize_legacy_post_p0_10min_shape() -> None:
    """baseline-post-p0-10min-final.json shape:
      - job.total_wall_sec (no top-level wall_clock)
      - opt002_long_video_validation.judge_avg_score
      - no per-segment drift array
    """
    legacy = {
        "version": "post-OPT",
        "captured_at": "2026-05-10T05:08:46Z",
        "job": {"id": 131, "total_wall_sec": 2365.26},
        "opt002_long_video_validation": {"judge_avg_score": 0.994},
    }
    normalized = mod.normalize_legacy_baseline(legacy)
    m = normalized["metrics"]
    assert normalized["job_id"] == 131
    assert m["wall_time_sec"] == 2365
    assert m["judge_score_mean"] == pytest.approx(0.994)
    assert m["drift_p50_sec"] is None  # n/a path; differ tolerates


def test_normalize_legacy_is_idempotent() -> None:
    """A current report (with `metrics`) passes through unchanged."""
    fresh = {
        "captured_at": "now",
        "job_id": 999,
        "metrics": {
            "drift_p50_sec": 0.10,
            "wall_time_sec": 600,
        },
    }
    assert mod.normalize_legacy_baseline(fresh) is fresh


def test_normalize_then_diff_pre_p0_against_self_is_pass() -> None:
    legacy = {
        "version": "pre-OPT-001",
        "captured_at": "2026-05-10T02:32:08Z",
        "job_id": 124,
        "wall_clock": {"total_sec": 559.7},
        "segments": {
            "drift": [
                {"ordinal": 0, "target_ms": 7500, "tts_ms": 7337},
                {"ordinal": 1, "target_ms": 4940, "tts_ms": 5421},
            ],
        },
        "cost_usd": None,
    }
    normalized = mod.normalize_legacy_baseline(legacy)
    report = mod.diff_against_baseline(normalized, dict(normalized))
    # Self-vs-self: every present metric is PASS, missing ones n/a.
    assert report["overall_status"] == "PASS"


# ---------- on-disk baselines integration test ----------

def test_real_baselines_load_and_normalize() -> None:
    """Every baseline JSON in the repo must be loadable by the
    harness; this is the L1 'don't ship a broken adapter' gate."""
    root = Path(__file__).resolve().parent
    files = [
        "baseline-pre-p0.json",
        "baseline-post-p0-10min-final.json",
        "opt402-79min-episode-139.json",
    ]
    for name in files:
        path = root / name
        if not path.exists():
            pytest.skip(f"{name} missing — operator hasn't captured it yet")
        # `utf-8-sig` tolerates a leading BOM (PowerShell's default
        # for Out-File). opt402-79min-episode-139.json was captured
        # this way; the harness must read it without erroring.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        normalized = mod.normalize_legacy_baseline(payload)
        # Must always at least carry a metrics dict (possibly with
        # None values — that's fine, the differ marks those n/a).
        assert "metrics" in normalized
        assert isinstance(normalized["metrics"], dict)


def test_diff_against_pre_p0_baseline_shape(tmp_path: Path) -> None:
    """Stage a baseline JSON that mimics the on-disk schema (with
    extra keys we don't read) and confirm we tolerate it."""
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps({
            "version": "pre-OPT-001",
            "captured_at": "2026-05-10T02:32:08Z",
            "job_id": 124,
            # Lots of extra keys the script does NOT read:
            "test_video": "test_60s.mp4",
            "stages": {"media": 0.015},
            "wall_clock": {"total_sec": 559.7},
            "segments": {"drift_p50": 0.028, "drift_p95": 0.097},
            # The only keys the differ actually reads:
            "metrics": {
                "drift_p50_sec": 0.028,
                "drift_p95_sec": 0.097,
                "cost_usd_total": 0.0,
                "wall_time_sec": 560,
            },
        }),
        encoding="utf-8",
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    current = {
        "captured_at": "now",
        "job_id": 200,
        "metrics": {
            "drift_p50_sec": 0.030,  # +7%, WARN not FAIL
            "drift_p95_sec": 0.100,  # +3%, WARN not FAIL
            "cost_usd_total": 0.0,   # both zero → n/a
            "wall_time_sec": 500,    # -10%, PASS (got faster)
        },
    }
    report = mod.diff_against_baseline(baseline, current)
    assert report["overall_status"] == "PASS"
