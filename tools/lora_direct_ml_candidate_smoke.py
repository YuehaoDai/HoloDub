from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_TEXT = (
    "\u8fd9\u4e2a Append Entries RPC \u8fd8\u5305\u542b\u4e24\u4e2a\u81f3\u5173"
    "\u91cd\u8981\u7684\u5b57\u6bb5\uff0c\u5206\u522b\u662f\u524d\u4e00"
    "\u65e5\u5fd7\u7d22\u5f15\u5b57\u6bb5\u4ee5\u53ca\u524d\u4e00"
    "\u65e5\u5fd7\u4efb\u671f\u5b57\u6bb5\u3002"
    "\u8fd9\u4e24\u4e2a\u5b57\u6bb5\u7684\u4f5c\u7528\u662f\u786e"
    "\u4fdd\u9886\u5bfc\u8005\u548c\u8ddf\u968f\u8005\u7684\u65e5"
    "\u5fd7\u5728\u540c\u4e00\u4e2a\u4f4d\u7f6e\u4e0a\u4fdd\u6301"
    "\u4e00\u81f4\u3002"
    "\u5982\u679c\u4e00\u53f7\u8282\u70b9\u53d1\u73b0\u524d\u4e00"
    "\u6761\u65e5\u5fd7\u5e76\u4e0d\u5339\u914d\uff0c\u5b83\u5c31"
    "\u4f1a\u62d2\u7edd\u8fd9\u6b21\u8ffd\u52a0\u8bf7\u6c42\uff0c"
    "\u968f\u540e\u9886\u5bfc\u8005\u4f1a\u5c1d\u8bd5\u66f4\u65e9"
    "\u7684\u4f4d\u7f6e\uff0c\u76f4\u5230\u627e\u5230\u53cc\u65b9"
    "\u5171\u540c\u8ba4\u53ef\u7684\u65e5\u5fd7\u524d\u7f00\u3002"
)
DEFAULT_TARGET_DURATION_SEC = 22.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a direct ML-service /tts/run LoRA candidate smoke and write a compact summary."
    )
    parser.add_argument("--ml-url", default="http://127.0.0.1:8000/tts/run")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--text-file", default="")
    parser.add_argument("--feature-jsonl", default="")
    parser.add_argument("--sample-id", default="")
    parser.add_argument("--target-duration-sec", type=float, default=0.0)
    parser.add_argument("--max-allowed-sec", type=float, default=0.0)
    parser.add_argument("--voice-sample-relpath", default="voices/test_ref.wav")
    parser.add_argument(
        "--output-relpath",
        default="training/lora_runs/main_flow_candidate_smoke/direct_ml_candidate_smoke.wav",
    )
    parser.add_argument(
        "--summary",
        default="data/training/lora_runs/main_flow_candidate_smoke/direct_ml_candidate_smoke_summary.json",
    )
    parser.add_argument("--candidate-families", default="split_light_vector,split_mix")
    parser.add_argument(
        "--dense-eligible",
        action="store_true",
        help="Mark the request as dense-risk eligible so sub-18s smoke samples still exercise split planning.",
    )
    parser.add_argument("--timeout-sec", type=float, default=900.0)
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Exit successfully even if ML falls back instead of producing candidate audio.",
    )
    return parser.parse_args()


def candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": candidate.get("family"),
        "split_strategy": candidate.get("split_strategy"),
        "split_part_count": candidate.get("split_part_count"),
        "gate_pass": candidate.get("gate_pass"),
        "score": candidate.get("score"),
        "duration_ms": candidate.get("duration_ms"),
        "drift_ms": candidate.get("drift_ms"),
        "similarity": candidate.get("similarity"),
        "critical_term_recall": candidate.get("critical_term_recall"),
        "latin_surface_recall": candidate.get("latin_surface_recall"),
        "reject_reasons": candidate.get("reject_reasons") or [],
        "risk_reasons": candidate.get("risk_reasons") or [],
        "target_sec": candidate.get("target_sec"),
        "duration_sec": candidate.get("duration_sec"),
        "abs_drift_ms": candidate.get("abs_drift_ms"),
        "fit_applied": candidate.get("fit_applied"),
        "fit_tempo_delta": candidate.get("fit_tempo_delta"),
        "fit_reject_reason": candidate.get("fit_reject_reason"),
        "pre_fit_duration_sec": candidate.get("pre_fit_duration_sec"),
        "pre_fit_drift_ms": candidate.get("pre_fit_drift_ms"),
        "join_quality": candidate.get("join_quality"),
        "source_parts": candidate.get("source_parts") or [],
        "selected_part_families": candidate.get("selected_part_families") or [],
        "diagnostics": candidate.get("diagnostics") or [],
    }


def read_feature_row(path: Path, sample_id: str) -> dict[str, Any]:
    selected: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            if not sample_id or str(row.get("sample_id") or "") == sample_id:
                selected = row
                break
    if selected is None:
        raise ValueError(f"sample_id={sample_id!r} not found in {path}")
    return selected


def request_text_and_target(args: argparse.Namespace) -> tuple[str, float]:
    if args.feature_jsonl:
        row = read_feature_row(Path(args.feature_jsonl), args.sample_id)
        text = str(row.get("text") or "").strip()
        target_sec = float(row.get("target_ms") or row.get("audio_ms") or 0.0) / 1000.0
        if not text:
            raise ValueError(f"feature row {row.get('sample_id')!r} has empty text")
        if target_sec <= 0:
            raise ValueError(f"feature row {row.get('sample_id')!r} has no positive target_ms/audio_ms")
        return text, target_sec
    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8").strip()
    target_sec = args.target_duration_sec if args.target_duration_sec > 0 else DEFAULT_TARGET_DURATION_SEC
    return text, target_sec


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    text, feature_target_sec = request_text_and_target(args)
    target_sec = args.target_duration_sec if args.target_duration_sec > 0 else feature_target_sec
    max_allowed_sec = args.max_allowed_sec if args.max_allowed_sec > 0 else target_sec + 0.8
    families = [item.strip() for item in args.candidate_families.split(",") if item.strip()]
    dense_eligible = bool(getattr(args, "dense_eligible", False))
    return {
        "text": text,
        "target_language": "zh-CN",
        "target_duration_sec": target_sec,
        "max_allowed_sec": max_allowed_sec,
        "voice_config": {"sample_relpaths": [args.voice_sample_relpath]},
        "output_relpath": args.output_relpath,
        "dubbing_meta": {
            "lora_candidate": {
                "schema_version": 1,
                "enabled": True,
                "phase": "main_flow_opt_in_smoke",
                "execution_mode": "ml_direct_smoke",
                "candidate_execution_enabled": True,
                "audio_selection_mode": "candidate_audio",
                "policy_version": "stage3_smoke_v1",
                "offline_baseline_id": "canary84_full84_regen_v1",
                "split_planning": True,
                "eligibility": {
                    "dense_eligible": dense_eligible,
                    "reasons": ["direct_smoke_dense_eligible"] if dense_eligible else [],
                },
                "candidate_families": families,
                "split_policy": {"join_quality_required": True},
                "gate": {"max_drift_ms": 500, "max_fit_delta": 0.05},
            }
        },
    }


def post_json(url: str, payload: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_sec) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ML service returned a non-object JSON response")
    return data


def summarize(result: dict[str, Any], elapsed_sec: float) -> dict[str, Any]:
    runtime = result.get("candidate_result") if isinstance(result.get("candidate_result"), dict) else {}
    split_plan = runtime.get("split_plan") if isinstance(runtime.get("split_plan"), dict) else {}
    summary = {
        "elapsed_sec": round(elapsed_sec, 1),
        "audio_relpath": result.get("audio_relpath"),
        "actual_duration_ms": result.get("actual_duration_ms"),
        "diagnostics": result.get("diagnostics") or [],
        "selected_family": runtime.get("selected_family"),
        "selected_split_strategy": runtime.get("selected_split_strategy"),
        "audio_selection_mode": runtime.get("audio_selection_mode"),
        "fallback_reason": runtime.get("fallback_reason"),
        "gate_decision": runtime.get("gate_decision"),
        "split_parts": len(split_plan.get("parts") or []),
        "candidates": [
            candidate_summary(candidate)
            for candidate in runtime.get("candidates", [])
            if isinstance(candidate, dict)
        ],
    }
    summary["validation_errors"] = validate_summary(summary)
    summary["ok"] = not summary["validation_errors"]
    return summary


def validate_summary(summary: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if summary.get("audio_selection_mode") != "candidate_audio":
        errors.append("audio_selection_mode_not_candidate_audio")
    if not summary.get("selected_family"):
        errors.append("missing_selected_family")
    if summary.get("fallback_reason"):
        errors.append(f"fallback:{summary.get('fallback_reason')}")
    if not summary.get("candidates"):
        errors.append("missing_candidate_records")
    return errors


def main() -> int:
    args = parse_args()
    payload = build_payload(args)
    start = time.time()
    result = post_json(args.ml_url, payload, args.timeout_sec)
    summary = summarize(result, time.time() - start)
    output = Path(args.summary)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if summary["validation_errors"] and not args.allow_fallback:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
