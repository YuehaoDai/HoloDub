from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an HTML A/B listening pack for LoRA TTS candidates.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--features", default="")
    parser.add_argument("--data-root", default="data")
    parser.add_argument(
        "--report",
        action="append",
        default=[],
        help="Candidate report as label=path. May be repeated.",
    )
    parser.add_argument("--include-teacher", action="store_true")
    parser.add_argument(
        "--only-report-samples",
        action="store_true",
        help="When --include-teacher is set, include teacher rows only for samples present in candidate reports.",
    )
    parser.add_argument("--sample-ids", default="", help="Comma-separated sample IDs to include.")
    parser.add_argument("--sample-ids-file", default="", help="Text file with one sample ID per line to include.")
    parser.add_argument("--reference-wav", default="")
    parser.add_argument("--title", default="HoloDub LoRA Listening Pack")
    parser.add_argument(
        "--review-notes",
        action="append",
        default=[],
        help="JSON file with listening-review notes. May be repeated.",
    )
    return parser.parse_args()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path:
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def resolve_path(raw_path: str | Path, data_root: Path) -> Path:
    text = str(raw_path).replace("\\", "/")
    local_data_root = Path("data")
    if text.startswith("/data/"):
        return data_root / text.removeprefix("/data/")
    if text.startswith("data/"):
        return data_root / text.removeprefix("data/")
    if text.startswith(f"{data_root.as_posix().rstrip('/')}/"):
        return Path(text)
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if Path(text).exists():
        return Path(text)
    if not data_root.exists() and local_data_root.exists():
        return local_data_root / text
    return data_root / text


def safe_name(name: str) -> str:
    return SAFE_NAME_RE.sub("_", name).strip("._") or "item"


def parse_report_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"--report must be label=path, got {value!r}")
    label, raw_path = value.split("=", 1)
    label = label.strip()
    if not label:
        raise ValueError(f"--report label is empty: {value!r}")
    return label, Path(raw_path.strip())


def load_sample_filter(args: argparse.Namespace) -> set[str]:
    sample_ids: set[str] = set()
    if args.sample_ids:
        sample_ids.update(item.strip() for item in args.sample_ids.split(",") if item.strip())
    if args.sample_ids_file:
        path = Path(args.sample_ids_file)
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                sample_ids.add(line)
    return sample_ids


def load_review_notes(paths: list[str]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for value in paths:
        if not value:
            continue
        payload = json.loads(Path(value).read_text(encoding="utf-8"))
        current = payload.get("notes", []) if isinstance(payload, dict) else payload
        if not isinstance(current, list):
            raise ValueError(f"review notes must be a list or {{'notes': [...]}}: {value}")
        for note in current:
            if not isinstance(note, dict):
                continue
            if note.get("sample_id") is None:
                continue
            notes.append(note)
    return notes


def report_rows(label: str, report_path: Path, data_root: Path) -> list[dict[str, Any]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    decisions = {
        str(decision.get("sample_id")): decision
        for decision in payload.get("decisions", [])
        if decision.get("sample_id") is not None
    }
    rows: list[dict[str, Any]] = []
    for row in payload.get("rows", []):
        if not row.get("path"):
            continue
        sample_id = row.get("sample_id")
        decision = decisions.get(str(sample_id)) or {}
        duration_sec = float(row.get("duration_sec") or 0.0)
        target_sec = float(row.get("target_sec") or 0.0)
        rows.append(
            {
                "sample_id": sample_id,
                "label": label,
                "candidate_label": row.get("candidate_label"),
                "upstream_candidate_label": row.get("upstream_candidate_label"),
                "candidate_label_chain": row.get("candidate_label_chain") or [],
                "gate_decision": decision,
                "listening_override_applied": row.get("listening_override_applied")
                or decision.get("listening_override_applied"),
                "listening_override": row.get("listening_override") or decision.get("listening_override"),
                "reject_reasons": row.get("reject_reasons")
                or decision.get("chosen_reject_reasons")
                or [],
                "source_path": str(resolve_path(row["path"], data_root)),
                "duration_sec": duration_sec,
                "target_sec": target_sec,
                "drift_ms": (duration_sec - target_sec) * 1000.0 if target_sec > 0 else None,
                "similarity": row.get("similarity"),
                "critical_term_recall": row.get("critical_term_recall"),
                "critical_terms_missing": row.get("critical_terms_missing") or [],
                "latin_surface_recall": row.get("latin_surface_recall"),
                "variant": row.get("variant"),
                "fit_tempo_delta": row.get("fit_tempo_delta"),
                "fit_applied": row.get("fit_applied"),
            }
        )
    return rows


def teacher_rows(features_path: Path, data_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_jsonl(features_path):
        relpath = row.get("audio_relpath") or row.get("audio_path")
        if not relpath:
            continue
        duration_sec = float(row.get("audio_ms") or 0) / 1000.0
        target_sec = float(row.get("target_ms") or 0) / 1000.0
        rows.append(
            {
                "sample_id": row.get("sample_id"),
                "label": "teacher",
                "source_path": str(resolve_path(relpath, data_root)),
                "duration_sec": duration_sec,
                "target_sec": target_sec,
                "drift_ms": (duration_sec - target_sec) * 1000.0 if target_sec > 0 else None,
                "similarity": None,
                "critical_term_recall": None,
                "critical_terms_missing": [],
                "latin_surface_recall": None,
                "variant": "teacher",
                "fit_tempo_delta": None,
                "fit_applied": False,
            }
        )
    return rows


def feature_meta(features_path: Path | None) -> dict[str, dict[str, Any]]:
    if not features_path or not features_path.exists():
        return {}
    meta: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(features_path):
        sample_id = row.get("sample_id")
        if sample_id is None:
            continue
        spoken = str(row.get("text") or "")
        display = str(row.get("display_text") or spoken)
        meta[str(sample_id)] = {
            "display_text": display,
            "spoken_text": spoken,
            "spoken_text_policy": row.get("spoken_text_policy"),
            "spoken_text_rewrites": row.get("spoken_text_rewrites") or [],
        }
    return meta


def copy_audio(row: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    sample_id = safe_name(str(row["sample_id"]))
    label = safe_name(str(row["label"]))
    source_path = Path(row["source_path"])
    target = output_dir / "audio" / sample_id / f"{label}{source_path.suffix.lower() or '.wav'}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_path, target)
    return {**row, "pack_path": str(target), "href": target.relative_to(output_dir).as_posix()}


def fmt_ms(value: Any) -> str:
    if value is None:
        return ""
    return f"{float(value):+.0f} ms"


def fmt_float(value: Any, digits: int = 4) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"


def note_is_row_targeted(note: dict[str, Any]) -> bool:
    return bool(note.get("label") or note.get("candidate_label"))


def row_matches_note(row: dict[str, Any], note: dict[str, Any]) -> bool:
    labels = {
        str(value)
        for value in (
            row.get("label"),
            row.get("candidate_label"),
            row.get("upstream_candidate_label"),
            *(row.get("candidate_label_chain") or []),
        )
        if value is not None
    }
    if note.get("label") is not None and str(note["label"]) not in labels:
        return False
    if note.get("candidate_label") is not None and str(note["candidate_label"]) not in labels:
        return False
    return True


def render_note(note: dict[str, Any]) -> str:
    severity = safe_name(str(note.get("severity") or "review"))
    title = str(note.get("title") or "Review note")
    body = str(note.get("body") or "")
    return (
        f"<div class='note note-{html.escape(severity)}'>"
        f"<strong>{html.escape(title)}</strong>"
        f"{'<p>' + html.escape(body) + '</p>' if body else ''}"
        "</div>"
    )


def render_auto_review(row: dict[str, Any]) -> str:
    if not row.get("listening_override_applied"):
        return ""
    override = row.get("listening_override") or {}
    decision = row.get("gate_decision") or {}
    reasons = row.get("reject_reasons") or decision.get("chosen_reject_reasons") or []
    reason_text = str(override.get("reason") or "human listening override")
    candidates = decision.get("candidates") or []
    selected_labels = {
        str(value)
        for value in (
            row.get("candidate_label"),
            row.get("upstream_candidate_label"),
            *(row.get("candidate_label_chain") or []),
        )
        if value is not None
    }
    runner_up = None
    for candidate in candidates:
        labels = {str(candidate.get("label"))}
        labels.update(str(value) for value in candidate.get("candidate_label_chain") or [] if value is not None)
        if not labels & selected_labels:
            runner_up = candidate
            break

    details = [
        f"reason: {reason_text}",
        f"covered: {', '.join(str(item) for item in reasons) if reasons else 'none'}",
    ]
    if runner_up:
        runner_label = runner_up.get("label")
        runner_drift = fmt_ms(runner_up.get("abs_drift_ms"))
        runner_sim = fmt_float(runner_up.get("similarity"))
        runner_f0 = fmt_float(runner_up.get("f0_range_semitones_10_90"), 2)
        details.append(
            f"runner-up: {runner_label}, drift {runner_drift}, ASR {runner_sim}, F0 {runner_f0}"
        )
    return render_note(
        {
            "severity": "high",
            "title": "Listening override applied",
            "body": "; ".join(details),
        }
    )


def render_html(
    title: str,
    rows: list[dict[str, Any]],
    reference: dict[str, Any] | None,
    samples: dict[str, dict[str, Any]],
    review_notes: list[dict[str, Any]],
) -> str:
    by_sample: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_sample.setdefault(str(row["sample_id"]), []).append(row)
    notes_by_sample: dict[str, list[dict[str, Any]]] = {}
    for note in review_notes:
        notes_by_sample.setdefault(str(note["sample_id"]), []).append(note)

    parts = [
        "<!doctype html>",
        "<meta charset='utf-8'>",
        f"<title>{html.escape(title)}</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;line-height:1.35;color:#18202a;background:#f7f8fa}",
        "h1{font-size:22px;margin:0 0 16px}",
        "h2{font-size:16px;margin:28px 0 10px}",
        "table{width:100%;border-collapse:collapse;background:white;border:1px solid #d8dde6}",
        "th,td{border-bottom:1px solid #e3e7ee;padding:8px;text-align:left;vertical-align:middle;font-size:13px}",
        "th{background:#eef2f7;color:#3a4656}",
        "audio{width:320px;max-width:36vw}",
        ".meta{color:#5b6675;font-size:12px}",
        ".textblock{background:#fff;border:1px solid #d8dde6;padding:8px 10px;margin:0 0 10px;font-size:13px}",
        ".textblock p{margin:4px 0}",
        ".textlabel{display:inline-block;color:#5b6675;min-width:72px}",
        ".pill{display:inline-block;border:1px solid #c7d0dc;border-radius:999px;padding:1px 7px;background:#f9fbfd}",
        ".subpill{display:inline-block;margin-top:4px;color:#5b6675;font-size:11px}",
        ".note{background:#fff7ed;border:1px solid #fb923c;border-left:4px solid #f97316;padding:8px 10px;margin:8px 0 10px;font-size:13px}",
        ".note p{margin:4px 0 0}",
        ".note-high strong,.note-review strong{color:#9a3412}",
        "</style>",
        f"<h1>{html.escape(title)}</h1>",
        "<p class='meta'>建议横向听同一个 sample 的不同候选，重点关注：句尾是否被吞、技术词是否清楚、音调是否太平、是否过度表演。</p>",
    ]
    parts[-1] = (
        "<p class='meta'>建议横向听同一个 sample 的不同候选，重点关注："
        "句尾是否被吞、技术词是否清楚、音调是否太平、是否过度表演。</p>"
    )
    parts[-1] = (
        "<p class='meta'>Compare candidates for the same sample. Focus on clipped endings, "
        "technical term clarity, pitch variation, and over-acting.</p>"
    )
    if reference:
        parts.extend(
            [
                "<h2>Voice Reference</h2>",
                f"<p class='meta'>{html.escape(reference['source_path'])}</p>",
                f"<audio controls src='{html.escape(reference['href'])}'></audio>",
            ]
        )

    for sample_id in sorted(by_sample):
        parts.append(f"<h2>{html.escape(sample_id)}</h2>")
        sample_notes = [
            note for note in notes_by_sample.get(sample_id, []) if not note_is_row_targeted(note)
        ]
        for note in sample_notes:
            parts.append(render_note(note))
        meta = samples.get(sample_id)
        if meta:
            display_text = str(meta.get("display_text") or "")
            spoken_text = str(meta.get("spoken_text") or "")
            rewrite_count = len(meta.get("spoken_text_rewrites") or [])
            parts.append("<div class='textblock'>")
            if display_text:
                parts.append(f"<p><span class='textlabel'>Display</span>{html.escape(display_text)}</p>")
            if spoken_text and spoken_text != display_text:
                parts.append(f"<p><span class='textlabel'>TTS input</span>{html.escape(spoken_text)}</p>")
            if rewrite_count:
                policy = html.escape(str(meta.get("spoken_text_policy") or ""))
                parts.append(f"<p class='meta'>spoken-text policy: {policy}, rewrites: {rewrite_count}</p>")
            parts.append("</div>")
        parts.append(
            "<table><thead><tr><th>Candidate</th><th>Audio</th><th>Drift</th><th>ASR Sim</th>"
            "<th>Terms</th><th>Missing Terms</th><th>Fit</th><th>Review</th><th>Source</th></tr></thead><tbody>"
        )
        for row in by_sample[sample_id]:
            fit = ""
            if row.get("fit_tempo_delta") is not None:
                fit = f"{float(row['fit_tempo_delta']) * 100:.1f}%"
                if row.get("fit_applied"):
                    fit += " applied"
            missing_terms = ", ".join(str(term) for term in row.get("critical_terms_missing") or [])
            label = str(row["label"])
            candidate_label = row.get("candidate_label")
            upstream_label = row.get("upstream_candidate_label")
            candidate_label_chain = [str(value) for value in row.get("candidate_label_chain") or [] if value]
            row_notes = [
                note
                for note in notes_by_sample.get(sample_id, [])
                if note_is_row_targeted(note) and row_matches_note(row, note)
            ]
            candidate_bits = [f"<span class='pill'>{html.escape(label)}</span>"]
            if candidate_label is not None and str(candidate_label) != label:
                candidate_bits.append(f"<span class='subpill'>candidate: {html.escape(str(candidate_label))}</span>")
            if (
                upstream_label is not None
                and str(upstream_label) != label
                and str(upstream_label) != str(candidate_label)
            ):
                candidate_bits.append(f"<span class='subpill'>upstream: {html.escape(str(upstream_label))}</span>")
            display_chain = [item for item in candidate_label_chain if item != label]
            if display_chain and row_notes:
                candidate_bits.append(f"<span class='subpill'>lineage: {html.escape(' > '.join(display_chain))}</span>")
            review = "".join(render_note(note) for note in row_notes)
            review = render_auto_review(row) + review
            parts.append(
                "<tr>"
                f"<td>{''.join(candidate_bits)}</td>"
                f"<td><audio controls preload='none' src='{html.escape(str(row['href']))}'></audio></td>"
                f"<td>{html.escape(fmt_ms(row.get('drift_ms')))}</td>"
                f"<td>{html.escape(fmt_float(row.get('similarity')))}</td>"
                f"<td>{html.escape(fmt_float(row.get('critical_term_recall')))}</td>"
                f"<td class='meta'>{html.escape(missing_terms)}</td>"
                f"<td>{html.escape(fit)}</td>"
                f"<td>{review}</td>"
                f"<td class='meta'>{html.escape(str(row['source_path']))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    return "\n".join(parts) + "\n"


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root)
    sample_filter = load_sample_filter(args)

    rows: list[dict[str, Any]] = []
    report_sample_ids: set[str] = set()
    for value in args.report:
        label, report_path = parse_report_arg(value)
        current_rows = report_rows(label, report_path, data_root)
        if sample_filter:
            current_rows = [row for row in current_rows if str(row.get("sample_id")) in sample_filter]
        rows.extend(current_rows)
        report_sample_ids.update(str(row["sample_id"]) for row in current_rows if row.get("sample_id") is not None)
    if args.include_teacher and args.features:
        teacher = teacher_rows(Path(args.features), data_root)
        if sample_filter:
            teacher = [row for row in teacher if str(row.get("sample_id")) in sample_filter]
        if args.only_report_samples:
            teacher = [row for row in teacher if str(row.get("sample_id")) in report_sample_ids]
        rows.extend(teacher)

    copied = [copy_audio(row, output_dir) for row in rows]
    reference: dict[str, Any] | None = None
    if args.reference_wav:
        reference = {
            "sample_id": "reference",
            "label": "reference",
            "source_path": str(resolve_path(args.reference_wav, data_root)),
            "duration_sec": None,
            "target_sec": None,
            "drift_ms": None,
            "similarity": None,
            "critical_term_recall": None,
            "critical_terms_missing": [],
            "latin_surface_recall": None,
            "variant": "reference",
            "fit_tempo_delta": None,
            "fit_applied": False,
        }
        reference = copy_audio(reference, output_dir)

    samples = feature_meta(Path(args.features)) if args.features else {}
    if sample_filter:
        samples = {sample_id: meta for sample_id, meta in samples.items() if sample_id in sample_filter}
    review_notes = load_review_notes(args.review_notes)
    if sample_filter:
        review_notes = [note for note in review_notes if str(note.get("sample_id")) in sample_filter]
    manifest = {
        "title": args.title,
        "rows": copied,
        "reference": reference,
        "samples": samples,
        "review_notes": review_notes,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "index.html").write_text(
        render_html(args.title, copied, reference, samples, review_notes),
        encoding="utf-8",
    )
    print(json.dumps({"output_dir": str(output_dir), "index": str(output_dir / "index.html"), "rows": len(copied)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
