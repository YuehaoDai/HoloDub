from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit whether the active LoRA episode goal is objectively ready and human-accepted."
    )
    parser.add_argument("--shadow-audit", required=True)
    parser.add_argument("--acceptance-status", required=True)
    parser.add_argument("--listening-pack-dir", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--output")
    parser.add_argument("--check-http", action="store_true")
    parser.add_argument("--require-human-accepted", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def gate(name: str, passed: bool, value: Any, expected: Any, severity: str = "hard") -> dict[str, Any]:
    return {
        "name": name,
        "pass": bool(passed),
        "value": value,
        "expected": expected,
        "severity": severity,
    }


def resolve_existing_path(raw_path: str, repo_root: Path, data_root: Path) -> Path:
    path = Path(str(raw_path).replace("\\", "/"))
    if path.is_absolute():
        return path
    if path.exists():
        return path
    data_candidate = data_root / path
    if data_candidate.exists():
        return data_candidate
    return repo_root / path


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return 200 <= int(response.status) < 400
    except (urllib.error.URLError, TimeoutError, ValueError):
        return False


def review_manifest_priority_segments(listening_pack_dir: Path) -> set[str]:
    manifest_path = listening_pack_dir / "review_manifest.json"
    if not manifest_path.exists():
        return set()
    payload = read_json(manifest_path)
    rows = payload.get("review_rows") or []
    return {
        str(row.get("sample_id") or "")
        for row in rows
        if isinstance(row, dict) and row.get("sample_id")
    }


def build_completion_audit(
    shadow_audit: dict[str, Any],
    acceptance_status: dict[str, Any],
    listening_pack_dir: Path,
    data_root: Path,
    *,
    require_human_accepted: bool,
    check_http: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path.cwd()
    counts = shadow_audit.get("counts") or {}
    duration = shadow_audit.get("duration") or {}
    final_output_path = resolve_existing_path(
        str(duration.get("final_output_path") or ""),
        repo_root,
        data_root,
    )
    clip_count = len(list((listening_pack_dir / "clips").glob("*.m4a")))
    expected_clip_count = int(
        (acceptance_status.get("listening_pack") or {}).get("focused_clip_count") or 0
    )
    priority_segments = [
        str(item)
        for item in (acceptance_status.get("listening_pack") or {}).get("priority_segments") or []
    ]
    manifest_segments = review_manifest_priority_segments(listening_pack_dir)
    human_review = acceptance_status.get("human_review") or {}
    accepted = human_review.get("accepted")
    status_value = str(acceptance_status.get("status") or "")

    gates = [
        gate("shadow_audit_schema", shadow_audit.get("schema_version") == "holodub.lora_main_flow_shadow_audit.v1", shadow_audit.get("schema_version"), "holodub.lora_main_flow_shadow_audit.v1"),
        gate("shadow_audit_pass", shadow_audit.get("pass") is True, shadow_audit.get("pass"), True),
        gate("shadow_contract_pass", shadow_audit.get("shadow_contract_pass") is True, shadow_audit.get("shadow_contract_pass"), True),
        gate("episode_completed", (shadow_audit.get("episode") or {}).get("status") == "completed", (shadow_audit.get("episode") or {}).get("status"), "completed"),
        gate("all_segments_synthesized", int(counts.get("segments") or 0) == int(counts.get("synthesized") or -1), {"segments": counts.get("segments"), "synthesized": counts.get("synthesized")}, "segments == synthesized"),
        gate("missing_audio_zero", int(counts.get("missing_audio") or 0) == 0, counts.get("missing_audio"), 0),
        gate("final_video_exists", final_output_path.exists(), str(final_output_path), "exists"),
        gate("final_video_drift_gate", float(duration.get("final_video_abs_drift_ms") or 0) <= 100.0, duration.get("final_video_abs_drift_ms"), "<= 100ms"),
        gate("acceptance_status_schema", acceptance_status.get("schema_version") == "holodub.human_acceptance_status.v1", acceptance_status.get("schema_version"), "holodub.human_acceptance_status.v1"),
        gate("acceptance_episode_matches", int(acceptance_status.get("episode_id") or 0) == int((shadow_audit.get("episode") or {}).get("id") or 0), {"acceptance_episode_id": acceptance_status.get("episode_id"), "audit_episode_id": (shadow_audit.get("episode") or {}).get("id")}, "same episode id"),
        gate("listening_pack_index_exists", (listening_pack_dir / "index.html").exists(), str(listening_pack_dir / "index.html"), "exists"),
        gate("review_manifest_exists", (listening_pack_dir / "review_manifest.json").exists(), str(listening_pack_dir / "review_manifest.json"), "exists"),
        gate("human_checklist_exists", (listening_pack_dir / "human_acceptance_checklist.md").exists(), str(listening_pack_dir / "human_acceptance_checklist.md"), "exists"),
        gate("human_notes_template_exists", (listening_pack_dir / "human_review_notes.template.json").exists(), str(listening_pack_dir / "human_review_notes.template.json"), "exists"),
        gate("focused_clip_count_matches", clip_count == expected_clip_count and clip_count > 0, {"actual": clip_count, "expected": expected_clip_count}, "actual == expected > 0"),
        gate("priority_segments_in_review_manifest", all(item in manifest_segments for item in priority_segments), priority_segments, "all priority segments present"),
    ]
    if check_http:
        local_http_url = str((acceptance_status.get("listening_pack") or {}).get("local_http_url") or "")
        gates.append(gate("local_http_listening_pack_ok", http_ok(local_http_url), local_http_url, "HTTP 2xx/3xx", severity="review"))

    objective_gate_names = {
        "shadow_audit_schema",
        "shadow_audit_pass",
        "shadow_contract_pass",
        "episode_completed",
        "all_segments_synthesized",
        "missing_audio_zero",
        "final_video_exists",
        "final_video_drift_gate",
    }
    objective_ready = all(item["pass"] for item in gates if item["name"] in objective_gate_names)

    human_accepted = status_value in {"human_accepted", "human_accepted_with_issues"} and accepted is True
    gates.append(
        gate(
            "human_listening_accepted",
            human_accepted if require_human_accepted else status_value in {"awaiting_human_listening", "human_accepted", "human_accepted_with_issues", "human_rejected"},
            {"status": status_value, "accepted": accepted},
            "human_accepted or human_accepted_with_issues" if require_human_accepted else "known human acceptance state",
            severity="hard" if require_human_accepted else "review",
        )
    )

    hard_gates = [item for item in gates if item.get("severity") == "hard"]
    complete = all(item["pass"] for item in hard_gates)
    if require_human_accepted:
        complete = complete and human_accepted

    return {
        "schema_version": "holodub.goal_completion_audit.v1",
        "episode_id": (shadow_audit.get("episode") or {}).get("id"),
        "objective_ready": objective_ready,
        "human_accepted": human_accepted,
        "complete": complete,
        "status": status_value,
        "gates": gates,
        "artifacts": {
            "final_video": str(final_output_path),
            "listening_pack": str(listening_pack_dir / "index.html"),
            "acceptance_status": str(listening_pack_dir / "acceptance_status.json"),
        },
    }


def main() -> None:
    args = parse_args()
    shadow_path = Path(args.shadow_audit)
    status_path = Path(args.acceptance_status)
    listening_pack_dir = Path(args.listening_pack_dir)
    audit = build_completion_audit(
        read_json(shadow_path),
        read_json(status_path),
        listening_pack_dir,
        Path(args.data_root),
        require_human_accepted=args.require_human_accepted,
        check_http=args.check_http,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"complete": audit["complete"], "objective_ready": audit["objective_ready"], "human_accepted": audit["human_accepted"], "status": audit["status"]}, ensure_ascii=False))
    if not audit["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
