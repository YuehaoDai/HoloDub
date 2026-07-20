from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
ML_SERVICE_ROOT = REPO_ROOT / "ml_service"
if str(ML_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(ML_SERVICE_ROOT))

from app.services.lora_candidate import LoraCandidateService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a saved LoRA candidate runtime with the current ML-service gate."
    )
    parser.add_argument("--runtime-json", required=True)
    parser.add_argument("--output", default="")
    parser.add_argument("--pretty", action="store_true")
    return parser.parse_args()


def load_json(path: str) -> Any:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def extract_runtime(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        return {"candidates": payload}
    if not isinstance(payload, dict):
        raise SystemExit("runtime JSON must be an object or candidate list")
    if isinstance(payload.get("candidate_result"), dict):
        return payload["candidate_result"]
    if isinstance(payload.get("tts_lora_candidate_runtime"), dict):
        return payload["tts_lora_candidate_runtime"]
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("tts_lora_candidate_runtime"), dict):
        return meta["tts_lora_candidate_runtime"]
    if isinstance(payload.get("runtime"), dict):
        return payload["runtime"]
    if isinstance(payload.get("candidates"), list):
        return payload
    raise SystemExit("runtime JSON does not contain a candidates array")


def gate_service() -> LoraCandidateService:
    return LoraCandidateService(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )  # type: ignore[arg-type]


def replay_candidate(service: LoraCandidateService, candidate: dict[str, Any]) -> dict[str, Any]:
    row = copy.deepcopy(candidate)
    original_reject_reasons = list(row.get("reject_reasons") or [])
    original_gate_pass = bool(row.get("gate_pass"))
    row["reject_reasons"] = service._reject_reasons(row)
    row["risk_reasons"] = service._risk_reasons(row)
    row["gate_pass"] = not row["reject_reasons"]
    row["score"] = service._candidate_score(row)
    row["strong_gate_pass"] = service._is_strong_gate_pass(row)
    row["gate_replay"] = {
        "original_gate_pass": original_gate_pass,
        "original_reject_reasons": original_reject_reasons,
        "changed": original_gate_pass != bool(row["gate_pass"])
        or original_reject_reasons != list(row["reject_reasons"]),
    }
    return row


def candidate_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "family": row.get("family"),
        "split_strategy": row.get("split_strategy"),
        "audio_relpath": row.get("audio_relpath"),
        "duration_ms": row.get("duration_ms"),
        "drift_ms": row.get("drift_ms"),
        "abs_drift_ms": row.get("abs_drift_ms"),
        "similarity": row.get("similarity"),
        "critical_term_recall": row.get("critical_term_recall"),
        "latin_surface_recall": row.get("latin_surface_recall"),
        "fit_tempo_delta": row.get("fit_tempo_delta"),
        "gate_pass": row.get("gate_pass"),
        "strong_gate_pass": row.get("strong_gate_pass"),
        "score": row.get("score"),
        "reject_reasons": row.get("reject_reasons") or [],
        "risk_reasons": row.get("risk_reasons") or [],
        "join_quality_rescue": row.get("join_quality_rescue"),
        "gate_replay": row.get("gate_replay") or {},
    }


def replay_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    candidates = [row for row in (runtime.get("candidates") or []) if isinstance(row, dict)]
    service = gate_service()
    replayed = [replay_candidate(service, row) for row in candidates]
    selected = service._select_candidate(replayed) if replayed else {}
    selected_gate_pass = bool(selected.get("gate_pass"))
    changed = any(bool(row.get("gate_replay", {}).get("changed")) for row in replayed)
    changed = changed or runtime.get("selected_family") != selected.get("family")
    changed = changed or bool(runtime.get("fallback_reason")) == selected_gate_pass
    return {
        "schema_version": "holodub.lora_candidate_runtime_replay.v1",
        "input_selected_family": runtime.get("selected_family"),
        "input_audio_selection_mode": runtime.get("audio_selection_mode"),
        "input_fallback_reason": runtime.get("fallback_reason") or "",
        "replayed_selected_family": selected.get("family"),
        "replayed_selected_split_strategy": selected.get("split_strategy"),
        "replayed_audio_selection_mode": "candidate_audio" if selected_gate_pass else "default_audio",
        "replayed_fallback_reason": "" if selected_gate_pass else "all_candidates_rejected",
        "changed": changed,
        "candidate_count": len(replayed),
        "gate_pass_count": sum(1 for row in replayed if row.get("gate_pass")),
        "join_quality_rescue_count": sum(
            1
            for row in replayed
            if isinstance(row.get("join_quality_rescue"), dict)
            and row["join_quality_rescue"].get("applied")
        ),
        "selected_candidate": candidate_summary(selected) if selected else None,
        "candidates": [candidate_summary(row) for row in replayed],
    }


def main() -> None:
    args = parse_args()
    report = replay_runtime(extract_runtime(load_json(args.runtime_json)))
    text = json.dumps(report, ensure_ascii=False, indent=2 if args.pretty else None)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
