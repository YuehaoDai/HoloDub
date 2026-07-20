from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_DECISIONS = {"accept", "accept_with_issues", "reject"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record a human listening decision into acceptance_status.json."
    )
    parser.add_argument("--status", required=True, help="Path to acceptance_status.json.")
    parser.add_argument("--notes", required=True, help="Path to filled human review notes JSON.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_notes(status: dict[str, Any], notes: dict[str, Any]) -> None:
    if notes.get("schema_version") != "holodub.human_review_notes.v1":
        raise ValueError("notes schema_version must be holodub.human_review_notes.v1")
    if status.get("schema_version") != "holodub.human_acceptance_status.v1":
        raise ValueError("status schema_version must be holodub.human_acceptance_status.v1")
    if int(notes.get("episode_id") or 0) != int(status.get("episode_id") or 0):
        raise ValueError("notes episode_id does not match acceptance status")

    decision = str(notes.get("decision") or "").strip()
    if decision not in VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(VALID_DECISIONS)}, got {decision!r}")

    priority = notes.get("priority_segments")
    if not isinstance(priority, dict):
        raise ValueError("priority_segments must be an object")
    expected = status.get("listening_pack", {}).get("priority_segments") or []
    missing = [sample_id for sample_id in expected if sample_id not in priority]
    if missing:
        raise ValueError(f"priority_segments missing required ids: {missing}")

    if decision == "accept":
        blocking = notes.get("blocking_segments") or []
        if blocking:
            raise ValueError("accept decision cannot include blocking_segments")


def normalize_review_notes(notes: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(notes)
    reviewed_at = str(out.get("reviewed_at") or "").strip()
    if not reviewed_at:
        out["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    out["decision"] = str(out.get("decision") or "").strip()
    return out


def apply_human_acceptance(status: dict[str, Any], notes: dict[str, Any]) -> dict[str, Any]:
    validate_notes(status, notes)
    normalized = normalize_review_notes(notes)
    decision = normalized["decision"]

    updated = deepcopy(status)
    updated["status"] = {
        "accept": "human_accepted",
        "accept_with_issues": "human_accepted_with_issues",
        "reject": "human_rejected",
    }[decision]
    updated["human_review"] = {
        "accepted": decision in {"accept", "accept_with_issues"},
        "decision": decision,
        "reviewed_at": normalized.get("reviewed_at"),
        "reviewer": normalized.get("reviewer"),
        "notes": normalized,
    }
    return updated


def main() -> None:
    args = parse_args()
    status_path = Path(args.status)
    notes_path = Path(args.notes)
    status = read_json(status_path)
    notes = read_json(notes_path)
    updated = apply_human_acceptance(status, notes)
    if not args.dry_run:
        write_json(status_path, updated)
    print(json.dumps({"status": updated["status"], "accepted": updated["human_review"]["accepted"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
