from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a main-flow episode run for LoRA candidate shadow provenance."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--segments-json", help="Local JSON export with segments or {segments:[...]}.")
    source.add_argument("--episode-id", type=int, help="Episode id to fetch from the HoloDub API.")
    parser.add_argument("--api-base", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--min-target-sec", type=float, default=18.0)
    parser.add_argument("--min-text-chars", type=int, default=180)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--require-completed", action="store_true")
    parser.add_argument("--max-final-video-drift-ms", type=float, default=100.0)
    parser.set_defaults(expect_split_planning=True)
    parser.add_argument("--expect-split-planning", dest="expect_split_planning", action="store_true")
    parser.add_argument("--no-expect-split-planning", dest="expect_split_planning", action="store_false")
    return parser.parse_args()


def fetch_json(api_base: str, path: str, api_key: str = "") -> dict[str, Any]:
    url = api_base.rstrip("/") + path
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


def load_episode_from_api(
    api_base: str,
    episode_id: int,
    api_key: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    episode = fetch_json(api_base, f"/episodes/{episode_id}", api_key)
    chapters_payload = fetch_json(api_base, f"/episodes/{episode_id}/chapters", api_key)
    chapters = chapters_payload.get("chapters") or []
    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        job_id = chapter.get("id")
        if not job_id:
            continue
        payload = fetch_json(api_base, f"/jobs/{job_id}/segments", api_key)
        for segment in payload.get("segments") or []:
            if isinstance(segment, dict):
                segment = dict(segment)
                segment.setdefault("chapter_ordinal", chapter.get("chapter_ordinal"))
                segment.setdefault("chapter_id", job_id)
                rows.append(segment)
    return episode, [row for row in chapters if isinstance(row, dict)], rows


def load_segments_from_file(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("segments") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def count_non_ws(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


def target_sec(row: dict[str, Any]) -> float:
    duration_ms = row.get("original_duration_ms")
    if duration_ms in (None, 0):
        start_ms = int(row.get("start_ms") or 0)
        end_ms = int(row.get("end_ms") or 0)
        duration_ms = max(0, end_ms - start_ms)
    return float(duration_ms or 0) / 1000.0


def segment_text(row: dict[str, Any]) -> str:
    return str(row.get("tgt_text") or row.get("target_text") or row.get("src_text") or row.get("source_text") or "")


def shadow_meta(row: dict[str, Any]) -> dict[str, Any] | None:
    meta = row.get("meta")
    if not isinstance(meta, dict):
        return None
    raw = meta.get("tts_lora_candidate")
    if isinstance(raw, dict):
        return raw
    return None


def is_synthesized(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").lower() == "synthesized"


def eligible(row: dict[str, Any], min_target_sec: float, min_text_chars: int) -> tuple[bool, bool, bool]:
    dur_ok = min_target_sec <= 0 or target_sec(row) >= min_target_sec
    text_ok = min_text_chars <= 0 or count_non_ws(segment_text(row)) >= min_text_chars
    return dur_ok or text_ok, dur_ok, text_ok


def check_segment(
    row: dict[str, Any],
    min_target_sec: float,
    min_text_chars: int,
    expect_split_planning: bool,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    sid = str(row.get("id") or row.get("sample_id") or row.get("ordinal") or "?")
    if not is_synthesized(row):
        return issues

    is_eligible, dur_ok, text_ok = eligible(row, min_target_sec, min_text_chars)
    candidate = shadow_meta(row)
    if candidate is None:
        return [
            {
                "sample_id": sid,
                "severity": "fail",
                "reason": "missing_tts_lora_candidate",
                "eligible": is_eligible,
                "target_sec": target_sec(row),
                "text_chars": count_non_ws(segment_text(row)),
            }
        ]

    def fail(reason: str, value: Any, expected: Any) -> None:
        issues.append(
            {
                "sample_id": sid,
                "severity": "fail",
                "reason": reason,
                "value": value,
                "expected": expected,
                "eligible": is_eligible,
                "target_sec": target_sec(row),
                "text_chars": count_non_ws(segment_text(row)),
            }
        )

    if candidate.get("schema_version") != 2:
        fail("schema_version_not_2", candidate.get("schema_version"), 2)
    if candidate.get("phase") != "main_flow_shadow":
        fail("phase_not_main_flow_shadow", candidate.get("phase"), "main_flow_shadow")
    if candidate.get("candidate_execution_enabled") is not False:
        fail("candidate_execution_not_disabled", candidate.get("candidate_execution_enabled"), False)
    if candidate.get("audio_selection_mode") != "default_audio":
        fail("audio_selection_not_default", candidate.get("audio_selection_mode"), "default_audio")
    if candidate.get("selected_family") != "default":
        fail("selected_family_not_default", candidate.get("selected_family"), "default")
    if candidate.get("enabled") is not is_eligible:
        fail("eligibility_mismatch", candidate.get("enabled"), is_eligible)

    split_planning = bool(candidate.get("split_planning"))
    if is_eligible and expect_split_planning and not split_planning:
        fail("eligible_without_split_planning", split_planning, True)
    if not is_eligible and split_planning:
        fail("ineligible_with_split_planning", split_planning, False)

    families = candidate.get("candidate_families") or []
    if not isinstance(families, list):
        fail("candidate_families_not_list", families, "list")
        families = []
    if "split_light_vector_10" in families:
        fail("tempo_rescue_10_in_production_families", families, "comparison_only")
    if is_eligible and split_planning:
        for family in ("split_light_vector", "split_mix"):
            if family not in families:
                fail("missing_split_family", families, family)
    if not is_eligible and families != ["default"]:
        fail("short_segment_families_changed", families, ["default"])

    eligibility = candidate.get("eligibility")
    if isinstance(eligibility, dict):
        if bool(eligibility.get("duration_eligible")) != dur_ok:
            fail("duration_eligibility_mismatch", eligibility.get("duration_eligible"), dur_ok)
        if bool(eligibility.get("text_eligible")) != text_ok:
            fail("text_eligibility_mismatch", eligibility.get("text_eligible"), text_ok)
    else:
        fail("missing_eligibility_block", eligibility, "dict")

    return issues


def media_duration_ms(ffprobe: str, path: Path) -> int | None:
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return int(round(float(proc.stdout.strip()) * 1000.0))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * pct))
    return ordered[max(0, min(len(ordered) - 1, idx))]


def metric_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "max": None, "p95": None}
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "max": max(values),
        "p95": percentile(values, 0.95),
    }


def expected_episode_duration_ms(
    episode: dict[str, Any] | None,
    chapters: list[dict[str, Any]] | None,
    segments: list[dict[str, Any]],
) -> int:
    if episode:
        value = int(episode.get("duration_ms") or 0)
        if value > 0:
            return value
    chapter_end = max((int(row.get("chapter_end_ms") or 0) for row in chapters or []), default=0)
    if chapter_end > 0:
        return chapter_end
    return max((int(row.get("end_ms") or 0) for row in segments), default=0)


def final_output_path(data_root: str, episode: dict[str, Any] | None) -> Path | None:
    if not data_root or not episode:
        return None
    rel = episode.get("output_relpath")
    if not rel:
        return None
    return Path(data_root) / str(rel)


def objective_gate(name: str, passed: bool, value: Any, threshold: Any) -> dict[str, Any]:
    return {
        "name": name,
        "pass": bool(passed),
        "value": value,
        "threshold": threshold,
    }


def build_audit(
    segments: list[dict[str, Any]],
    min_target_sec: float = 18.0,
    min_text_chars: int = 180,
    expect_split_planning: bool = True,
    episode: dict[str, Any] | None = None,
    chapters: list[dict[str, Any]] | None = None,
    data_root: str = "",
    ffprobe: str = "ffprobe",
    require_completed: bool = False,
    max_final_video_drift_ms: float = 100.0,
) -> dict[str, Any]:
    synthesized = [row for row in segments if is_synthesized(row)]
    non_skipped = [row for row in segments if str(row.get("status") or "").lower() != "skipped"]
    eligible_rows = [row for row in synthesized if eligible(row, min_target_sec, min_text_chars)[0]]
    short_rows = [row for row in synthesized if not eligible(row, min_target_sec, min_text_chars)[0]]
    with_meta = [row for row in synthesized if shadow_meta(row) is not None]
    missing_audio = [
        row for row in non_skipped
        if str(row.get("tts_audio_path") or "") == "" or int(row.get("tts_duration_ms") or 0) <= 0
    ]
    segment_drifts = [
        abs(float(row.get("tts_duration_ms") or 0) - target_sec(row) * 1000.0)
        for row in synthesized
        if int(row.get("tts_duration_ms") or 0) > 0
    ]
    issues: list[dict[str, Any]] = []
    for row in synthesized:
        issues.extend(check_segment(row, min_target_sec, min_text_chars, expect_split_planning))

    expected_ms = expected_episode_duration_ms(episode, chapters, segments)
    output_path = final_output_path(data_root, episode)
    output_exists = bool(output_path and output_path.exists())
    final_video_ms = media_duration_ms(ffprobe, output_path) if output_exists and output_path else None
    final_video_drift_ms = abs(final_video_ms - expected_ms) if final_video_ms is not None and expected_ms > 0 else None
    chapter_status_counts: dict[str, int] = {}
    for chapter in chapters or []:
        status = str(chapter.get("status") or "")
        chapter_status_counts[status] = chapter_status_counts.get(status, 0) + 1
    objective_gates = [
        objective_gate("shadow_contract", len(issues) == 0, len(issues), 0),
        objective_gate("all_non_skipped_audio_present", len(missing_audio) == 0, len(missing_audio), 0),
    ]
    if episode:
        objective_gates.append(
            objective_gate(
                "episode_completed",
                (not require_completed) or str(episode.get("status") or "") == "completed",
                episode.get("status"),
                "completed" if require_completed else "not required",
            )
        )
        objective_gates.append(
            objective_gate(
                "chapters_completed",
                (not require_completed)
                or all(str(ch.get("status") or "") == "completed" for ch in chapters or []),
                chapter_status_counts,
                "all completed" if require_completed else "not required",
            )
        )
        objective_gates.append(
            objective_gate(
                "final_output_exists",
                (not require_completed) or output_exists,
                str(output_path) if output_path else None,
                "exists" if require_completed else "not required",
            )
        )
    if require_completed and expected_ms > 0:
        objective_gates.append(
            objective_gate(
                "final_video_drift",
                final_video_drift_ms is not None and final_video_drift_ms <= max_final_video_drift_ms,
                final_video_drift_ms,
                f"<= {max_final_video_drift_ms}ms",
            )
        )

    return {
        "schema_version": "holodub.lora_main_flow_shadow_audit.v1",
        "pass": all(item["pass"] for item in objective_gates),
        "shadow_contract_pass": len(issues) == 0,
        "counts": {
            "segments": len(segments),
            "synthesized": len(synthesized),
            "non_skipped": len(non_skipped),
            "duration_or_text_eligible": len(eligible_rows),
            "short_or_low_risk": len(short_rows),
            "with_tts_lora_candidate": len(with_meta),
            "missing_audio": len(missing_audio),
            "issues": len(issues),
        },
        "thresholds": {
            "min_target_sec": min_target_sec,
            "min_text_chars": min_text_chars,
            "expect_split_planning": expect_split_planning,
            "require_completed": require_completed,
            "max_final_video_drift_ms": max_final_video_drift_ms,
        },
        "episode": {
            "id": episode.get("id"),
            "name": episode.get("name"),
            "status": episode.get("status"),
            "duration_ms": episode.get("duration_ms"),
            "output_relpath": episode.get("output_relpath"),
            "chapters_manifest_rel_path": episode.get("chapters_manifest_rel_path"),
        } if episode else None,
        "chapter_status_counts": chapter_status_counts,
        "duration": {
            "expected_duration_ms": expected_ms,
            "final_output_path": str(output_path) if output_path else None,
            "final_output_exists": output_exists,
            "final_video_duration_ms": final_video_ms,
            "final_video_abs_drift_ms": final_video_drift_ms,
            "segment_abs_drift_ms": metric_summary(segment_drifts),
        },
        "objective_gates": objective_gates,
        "issues": issues,
        "missing_audio": [
            {
                "id": row.get("id"),
                "chapter_ordinal": row.get("chapter_ordinal"),
                "ordinal": row.get("ordinal"),
                "status": row.get("status"),
                "tts_audio_path": row.get("tts_audio_path"),
                "tts_duration_ms": row.get("tts_duration_ms"),
            }
            for row in missing_audio[:100]
        ],
    }


def render_markdown(audit: dict[str, Any]) -> str:
    counts = audit.get("counts") or {}
    duration = audit.get("duration") or {}
    episode = audit.get("episode") or {}
    lines = [
        "# HoloDub LoRA Main-Flow Shadow Audit",
        "",
        f"- Pass: {audit.get('pass')}",
        f"- Shadow contract pass: {audit.get('shadow_contract_pass')}",
        f"- Episode: {episode.get('id')} {episode.get('name') or ''}",
        f"- Episode status: {episode.get('status')}",
        f"- Segments: {counts.get('segments')}",
        f"- Synthesized: {counts.get('synthesized')}",
        f"- Missing audio: {counts.get('missing_audio')}",
        f"- Eligible: {counts.get('duration_or_text_eligible')}",
        f"- Short / low-risk: {counts.get('short_or_low_risk')}",
        f"- With provenance: {counts.get('with_tts_lora_candidate')}",
        f"- Issues: {counts.get('issues')}",
        f"- Final output: {duration.get('final_output_path')}",
        f"- Final video drift: {duration.get('final_video_abs_drift_ms')}ms",
        "",
        "## Objective Gates",
        "",
    ]
    for item in audit.get("objective_gates") or []:
        status = "PASS" if item.get("pass") else "FAIL"
        lines.append(f"- {status} `{item.get('name')}`: {item.get('value')} ({item.get('threshold')})")
    segment_drift = duration.get("segment_abs_drift_ms") or {}
    lines.extend(
        [
            "",
            "## Segment Drift",
            "",
            f"- Count: {segment_drift.get('count')}",
            f"- Mean: {segment_drift.get('mean')}ms",
            f"- P95: {segment_drift.get('p95')}ms",
            f"- Max: {segment_drift.get('max')}ms",
            "",
            "## Issues",
            "",
        ]
    )
    for issue in audit.get("issues") or []:
        value = issue.get("value")
        expected = issue.get("expected")
        lines.append(
            f"- {issue.get('sample_id')}: {issue.get('reason')}, value={value}, "
            f"expected={expected}, target={issue.get('target_sec')}s, chars={issue.get('text_chars')}"
        )
    if not audit.get("issues"):
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    episode: dict[str, Any] | None = None
    chapters: list[dict[str, Any]] | None = None
    if args.segments_json:
        segments = load_segments_from_file(Path(args.segments_json))
        source = str(Path(args.segments_json))
    else:
        episode, chapters, segments = load_episode_from_api(args.api_base, int(args.episode_id), args.api_key)
        source = f"{args.api_base.rstrip('/')}/episodes/{args.episode_id}"

    audit = build_audit(
        segments,
        min_target_sec=args.min_target_sec,
        min_text_chars=args.min_text_chars,
        expect_split_planning=args.expect_split_planning,
        episode=episode,
        chapters=chapters,
        data_root=args.data_root,
        ffprobe=args.ffprobe,
        require_completed=args.require_completed,
        max_final_video_drift_ms=args.max_final_video_drift_ms,
    )
    audit["source"] = source
    if args.output_dir:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "main_flow_shadow_audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (out_dir / "main_flow_shadow_audit.md").write_text(render_markdown(audit), encoding="utf-8")
    print(json.dumps({"pass": audit["pass"], **audit["counts"]}, ensure_ascii=False))
    if not audit["pass"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
