from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HUMAN_REVIEWED_STATUSES = {"accepted", "review_blocked"}
PENDING_REVIEW_STATUSES = {"awaiting_human_acceptance"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a 2-3 lecture MIT 6.824 batch review from acceptance and training-decision evidence."
    )
    parser.add_argument("--acceptance-status", default="data/validation/mit6824_acceptance_status.json")
    parser.add_argument("--training-decision", default="data/validation/mit6824_training_decision.json")
    parser.add_argument("--delivery-index", default="data/validation/mit6824_delivery_index.json")
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--min-reviewed", type=int, default=2)
    parser.add_argument("--output-json", default="data/validation/mit6824_batch_review.json")
    parser.add_argument("--output-md", default="data/validation/mit6824_batch_review.md")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve(root: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else root / path


def rel_to_root(path: Path | None, root: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("/", "\\")
    except ValueError:
        return str(path)


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"_invalid_json": True, "_path": str(path)}
    return payload if isinstance(payload, dict) else {}


def lecture_int(row: dict[str, Any]) -> int:
    try:
        return int(row.get("lecture") or 0)
    except (TypeError, ValueError):
        return 0


def reviewed_rows(acceptance: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in acceptance.get("lectures") or [] if isinstance(row, dict)]
    return sorted([row for row in rows if row.get("status") in HUMAN_REVIEWED_STATUSES], key=lecture_int)


def pending_review_rows(acceptance: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in acceptance.get("lectures") or [] if isinstance(row, dict)]
    return sorted([row for row in rows if row.get("status") in PENDING_REVIEW_STATUSES], key=lecture_int)


def readiness(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("acceptance_readiness")
    return value if isinstance(value, dict) else {}


def readiness_ready(row: dict[str, Any]) -> bool:
    return readiness(row).get("ready_for_human_review") is True


def latest_batch(rows: list[dict[str, Any]], batch_size: int) -> list[dict[str, Any]]:
    if batch_size <= 0:
        batch_size = 3
    return rows[-batch_size:]


def issue_family_rows(training: dict[str, Any], batch_lectures: set[int]) -> list[dict[str, Any]]:
    families = training.get("families") if isinstance(training.get("families"), list) else []
    selected: list[dict[str, Any]] = []
    for row in families:
        if not isinstance(row, dict):
            continue
        lectures = row.get("lectures") if isinstance(row.get("lectures"), list) else []
        if batch_lectures and not (set(int(x) for x in lectures if str(x).isdigit()) & batch_lectures):
            continue
        selected.append(row)
    return selected


def issue_rows(training: dict[str, Any], batch_lectures: set[int]) -> list[dict[str, Any]]:
    rows = training.get("issues") if isinstance(training.get("issues"), list) else []
    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            lecture = int(row.get("lecture") or 0)
        except (TypeError, ValueError):
            lecture = 0
        if lecture in batch_lectures:
            selected.append(row)
    return selected


def next_batch_action(
    *,
    reviewed_count: int,
    min_reviewed: int,
    training_summary: dict[str, Any],
    family_rows: list[dict[str, Any]],
) -> str:
    if reviewed_count < min_reviewed:
        return "not_due_collect_more_reviewed_lectures"
    recommendation = str(training_summary.get("recommendation") or "")
    if recommendation == "consider_course_level_adapter_training":
        return "review_course_level_adapter_training_feasibility"
    if family_rows:
        return "repair_rules_candidates_or_review_anchors_then_continue"
    return "continue_next_lectures_with_current_profile"


def build_review(
    *,
    root: Path,
    acceptance: dict[str, Any],
    training: dict[str, Any],
    delivery: dict[str, Any],
    batch_size: int,
    min_reviewed: int,
) -> dict[str, Any]:
    reviewed = reviewed_rows(acceptance)
    pending_review = pending_review_rows(acceptance)
    pending_content_ready = [row for row in pending_review if readiness_ready(row)]
    pending_content_blocked = [row for row in pending_review if readiness(row) and not readiness_ready(row)]
    batch = latest_batch(reviewed, batch_size)
    batch_lectures = {lecture_int(row) for row in batch if lecture_int(row)}
    training_summary = training.get("summary") if isinstance(training.get("summary"), dict) else {}
    families = issue_family_rows(training, batch_lectures)
    issues = issue_rows(training, batch_lectures)
    issue_counts = Counter(str(row.get("family") or "unknown") for row in issues)
    delivery_summary = delivery.get("summary") if isinstance(delivery.get("summary"), dict) else {}
    action = next_batch_action(
        reviewed_count=len(reviewed),
        min_reviewed=min_reviewed,
        training_summary=training_summary,
        family_rows=families,
    )
    return {
        "schema_version": "holodub.mit6824_batch_review.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": {
            "batch_size": batch_size,
            "min_reviewed": min_reviewed,
            "per_episode_training_allowed": False,
            "adapter_granularity": "course_or_domain_profile",
        },
        "summary": {
            "reviewed_lectures": [lecture_int(row) for row in reviewed],
            "pending_review_lectures": [lecture_int(row) for row in pending_review],
            "pending_content_ready_lectures": [lecture_int(row) for row in pending_content_ready],
            "pending_content_blocked_lectures": [lecture_int(row) for row in pending_content_blocked],
            "batch_lectures": sorted(batch_lectures),
            "reviewed_count": len(reviewed),
            "pending_review_count": len(pending_review),
            "batch_count": len(batch),
            "next_action": action,
            "training_recommendation": training_summary.get("recommendation", ""),
            "training_readiness": (training_summary.get("training_readiness") or {}).get("status", ""),
            "delivery_next_not_started": delivery_summary.get("next_not_started"),
            "delivery_review_ready": delivery_summary.get("review_ready", []),
        },
        "batch": [
            {
                "lecture": lecture_int(row),
                "title": row.get("title") or "",
                "status": row.get("status") or "",
                "human_accepted": bool(row.get("human_accepted")),
                "missing": row.get("missing") if isinstance(row.get("missing"), list) else [],
                "acceptance_dir": row.get("acceptance_dir") or "",
                "review_pack": ((row.get("artifacts") or {}).get("human_review_pack") or {}).get("path", ""),
                "final_video": ((row.get("artifacts") or {}).get("final_video") or {}).get("path", ""),
            }
            for row in batch
        ],
        "families": families,
        "issue_family_counts": dict(sorted(issue_counts.items())),
        "issues": issues[:300],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(path: Path, review: dict[str, Any]) -> None:
    summary = review.get("summary") if isinstance(review.get("summary"), dict) else {}
    lines = [
        "# MIT 6.824 Batch Review",
        "",
        f"Generated: `{review.get('generated_at')}`",
        "",
        "## Summary",
        "",
        f"- Reviewed lectures: `{summary.get('reviewed_lectures', [])}`",
        f"- Pending review lectures: `{summary.get('pending_review_lectures', [])}`",
        f"- Pending content-ready lectures: `{summary.get('pending_content_ready_lectures', [])}`",
        f"- Pending content-blocked lectures: `{summary.get('pending_content_blocked_lectures', [])}`",
        f"- Batch lectures: `{summary.get('batch_lectures', [])}`",
        f"- Next action: `{summary.get('next_action', '')}`",
        f"- Training recommendation: `{summary.get('training_recommendation', '')}`",
        f"- Training readiness: `{summary.get('training_readiness', '')}`",
        f"- Delivery next not-started: `{summary.get('delivery_next_not_started')}`",
        f"- Delivery review ready: `{summary.get('delivery_review_ready', [])}`",
        "",
        "## Batch Lectures",
        "",
        "| Lecture | Title | Status | Accepted | Review Pack | Final Video |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for row in review.get("batch") or []:
        lines.append(
            "| {lecture} | {title} | {status} | {accepted} | {review_pack} | {final_video} |".format(
                lecture=row.get("lecture", ""),
                title=str(row.get("title") or "").replace("|", "\\|"),
                status=row.get("status", ""),
                accepted=row.get("human_accepted", ""),
                review_pack=str(row.get("review_pack") or "").replace("|", "\\|"),
                final_video=str(row.get("final_video") or "").replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Failure Families",
            "",
            "| Family | Issues | Lectures | Default Action | Training Signal |",
            "| --- | ---: | --- | --- | --- |",
        ]
    )
    for row in review.get("families") or []:
        lines.append(
            f"| {row.get('family', '')} | {row.get('issue_count', '')} | {row.get('lectures', [])} | "
            f"{row.get('default_action', '')} | {row.get('training_signal', '')} |"
        )
    if not review.get("families"):
        lines.append("| none | 0 | [] | continue_current_profile | false |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = repo_root()
    acceptance_path = resolve(root, args.acceptance_status)
    training_path = resolve(root, args.training_decision)
    delivery_path = resolve(root, args.delivery_index)
    review = build_review(
        root=root,
        acceptance=read_json(acceptance_path),
        training=read_json(training_path),
        delivery=read_json(delivery_path),
        batch_size=args.batch_size,
        min_reviewed=args.min_reviewed,
    )
    output_json = resolve(root, args.output_json)
    output_md = resolve(root, args.output_md)
    if output_json is None or output_md is None:
        raise ValueError("output paths required")
    write_json(output_json, review)
    write_markdown(output_md, review)
    print(
        json.dumps(
            {
                "output_json": rel_to_root(output_json, root),
                "output_md": rel_to_root(output_md, root),
                "summary": review["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
