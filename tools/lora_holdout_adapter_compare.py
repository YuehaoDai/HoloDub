from __future__ import annotations

import argparse
import html
import json
import shutil
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare raw LoRA adapter inference on a fixed holdout set.")
    parser.add_argument("--features", required=True, help="JSONL feature rows used for inference.")
    parser.add_argument("--run", action="append", required=True, help="Run label and report as label=path.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--episode-segments", default="", help="Optional episode_segments.json for current final audio.")
    parser.add_argument("--data-root", default="data")
    parser.add_argument(
        "--variant-filter",
        default="",
        help="Optional comma-separated variants to include from each report. Empty includes every row with sample_id/path.",
    )
    parser.add_argument("--fit-warning-threshold", type=float, default=0.05)
    parser.add_argument("--fit-hard-threshold", type=float, default=0.05)
    parser.add_argument("--fit-severe-threshold", type=float, default=0.08)
    return parser.parse_args()


def parse_label_path(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError(f"expected label=path, got {value!r}")
    label, raw = value.split("=", 1)
    label = label.strip()
    if not label:
        raise ValueError(f"empty run label in {value!r}")
    return label, Path(raw)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def sample_id(row: dict[str, Any]) -> str:
    raw = row.get("sample_id")
    if raw:
        return str(raw)
    segment_id = row.get("segment_id")
    return f"seg{segment_id}" if segment_id not in (None, "") else ""


def resolve_path(raw: str, data_root: Path) -> Path:
    path = Path(str(raw))
    if path.exists():
        return path
    normalized = str(raw).replace("\\", "/")
    if normalized.startswith("/data/"):
        candidate = data_root / normalized[len("/data/") :]
        if candidate.exists():
            return candidate
    candidate = data_root / normalized
    if candidate.exists():
        return candidate
    return path


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[idx]


def rounded(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def duration_metrics(row: dict[str, Any]) -> dict[str, Any]:
    target_sec = float(row.get("target_sec") or 0.0)
    duration_sec = float(row.get("duration_sec") or 0.0)
    drift_ms = (duration_sec - target_sec) * 1000.0 if target_sec > 0 and duration_sec > 0 else None
    fit_delta = abs(duration_sec / target_sec - 1.0) if target_sec > 0 and duration_sec > 0 else None
    join_quality = row.get("join_quality") if isinstance(row.get("join_quality"), dict) else {}
    duration_padding = row.get("duration_padding") if isinstance(row.get("duration_padding"), dict) else {}
    return {
        "duration_sec": rounded(duration_sec, 6),
        "target_sec": rounded(target_sec, 6),
        "drift_ms": rounded(drift_ms, 3),
        "abs_drift_ms": rounded(abs(drift_ms), 3) if drift_ms is not None else None,
        "fit_tempo_delta": rounded(fit_delta, 6),
        "join_pass": join_quality.get("pass") if join_quality else None,
        "join_reasons": join_quality.get("reasons") or [],
        "extra_pause_sec": rounded(duration_padding.get("extra_pause_sec"), 6)
        if duration_padding.get("extra_pause_sec") is not None
        else None,
    }


def summarize(rows: list[dict[str, Any]], warning: float, hard: float, severe: float) -> dict[str, Any]:
    abs_drifts = [float(row["abs_drift_ms"]) for row in rows if row.get("abs_drift_ms") is not None]
    fit_deltas = [float(row["fit_tempo_delta"]) for row in rows if row.get("fit_tempo_delta") is not None]
    join_rows = [row for row in rows if row.get("join_pass") is not None]
    return {
        "count": len(rows),
        "mean_abs_drift_ms": rounded(statistics.fmean(abs_drifts), 3) if abs_drifts else None,
        "p50_abs_drift_ms": rounded(percentile(abs_drifts, 0.5), 3),
        "p90_abs_drift_ms": rounded(percentile(abs_drifts, 0.9), 3),
        "max_abs_drift_ms": rounded(max(abs_drifts), 3) if abs_drifts else None,
        "mean_fit_tempo_delta": rounded(statistics.fmean(fit_deltas), 6) if fit_deltas else None,
        "p90_fit_tempo_delta": rounded(percentile(fit_deltas, 0.9), 6),
        "max_fit_tempo_delta": rounded(max(fit_deltas), 6) if fit_deltas else None,
        "fit_gt_warning_count": sum(1 for value in fit_deltas if value > warning),
        "fit_gt_hard_count": sum(1 for value in fit_deltas if value > hard),
        "fit_gt_severe_count": sum(1 for value in fit_deltas if value > severe),
        "join_pass_count": sum(1 for row in join_rows if row.get("join_pass") is True),
        "join_checked_count": len(join_rows),
    }


def load_current_audio(episode_segments: Path | None) -> dict[str, dict[str, Any]]:
    if episode_segments is None or not episode_segments.exists():
        return {}
    rows = read_json(episode_segments)
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = sample_id(row)
        if not sid:
            continue
        out[sid] = row
    return out


def copy_audio(source: Path, dest: Path) -> str | None:
    if not source.exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)
    return dest.as_posix()


def render_html(payload: dict[str, Any], output_dir: Path) -> None:
    pack_dir = output_dir / "listening_pack"
    lines = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'>",
        "<title>LoRA Holdout Adapter Compare</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;background:#f7f8fb;color:#17202a}",
        "table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d9dee7}",
        "th,td{border-bottom:1px solid #e7ebf1;padding:8px;vertical-align:top;text-align:left}",
        "th{background:#eef2f7;font-size:12px;color:#4c596a}",
        ".text{max-width:520px;line-height:1.45}.metric{font-variant-numeric:tabular-nums;white-space:nowrap}",
        "audio{width:220px}.bad{color:#a33}.ok{color:#237a3b}",
        "</style></head><body>",
        "<h1>LoRA Holdout Adapter Compare</h1>",
        f"<p>Generated at {html.escape(str(payload.get('generated_at')))}. Production hard fit: {payload['thresholds']['fit_hard_threshold']:.0%}; severe marker: {payload['thresholds']['fit_severe_threshold']:.0%}.</p>",
        "<h2>Summary</h2><table><tr><th>Run</th><th>Mean abs drift</th><th>P90 abs drift</th><th>Max abs drift</th><th>Mean fit delta</th><th>P90 fit delta</th><th>&gt;5% hard</th><th>&gt;8% severe</th><th>Join pass</th></tr>",
    ]
    for label, summary in payload["summary_by_run"].items():
        lines.append(
            "<tr>"
            f"<td>{html.escape(label)}</td>"
            f"<td class='metric'>{summary.get('mean_abs_drift_ms')}</td>"
            f"<td class='metric'>{summary.get('p90_abs_drift_ms')}</td>"
            f"<td class='metric'>{summary.get('max_abs_drift_ms')}</td>"
            f"<td class='metric'>{summary.get('mean_fit_tempo_delta')}</td>"
            f"<td class='metric'>{summary.get('p90_fit_tempo_delta')}</td>"
            f"<td class='metric'>{summary.get('fit_gt_hard_count')}</td>"
            f"<td class='metric'>{summary.get('fit_gt_severe_count')}</td>"
            f"<td class='metric'>{summary.get('join_pass_count')}/{summary.get('join_checked_count')}</td>"
            "</tr>"
        )
    lines.append("</table><h2>Samples</h2><table><tr><th>Sample</th><th>Text</th><th>Current final</th>")
    for run in payload["runs"]:
        lines.append(f"<th>{html.escape(run['label'])}</th>")
    lines.append("</tr>")
    for row in payload["rows"]:
        lines.append("<tr>")
        lines.append(
            f"<td><b>{html.escape(row['sample_id'])}</b><br><span class='metric'>target {row.get('target_sec')}s</span></td>"
        )
        lines.append(f"<td class='text'>{html.escape(row.get('text') or '')}</td>")
        current = row.get("current_final") or {}
        if current.get("audio"):
            lines.append(f"<td><audio controls src='{html.escape(current['audio'])}'></audio></td>")
        else:
            lines.append("<td>missing</td>")
        for run in payload["runs"]:
            item = row.get("runs", {}).get(run["label"], {})
            audio = item.get("audio")
            fit_delta = item.get("fit_tempo_delta")
            cls = "bad" if isinstance(fit_delta, (int, float)) and fit_delta > payload["thresholds"]["fit_warning_threshold"] else "ok"
            if audio:
                lines.append(
                    "<td>"
                    f"<audio controls src='{html.escape(audio)}'></audio>"
                    f"<div class='metric {cls}'>dur {item.get('duration_sec')}s / drift {item.get('drift_ms')}ms / ratio {item.get('fit_tempo_delta')}</div>"
                    f"<div class='metric'>join {item.get('join_pass')} / pad {item.get('extra_pause_sec')}s</div>"
                    f"<div class='meta'>{html.escape(', '.join(str(reason) for reason in item.get('join_reasons') or []))}</div>"
                    "</td>"
                )
            else:
                lines.append("<td>missing</td>")
        lines.append("</tr>")
    lines.append("</table></body></html>")
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "index.html").write_text("\n".join(lines), encoding="utf-8")


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# LoRA Holdout Adapter Compare",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "| Run | Mean abs drift ms | P90 abs drift ms | Max abs drift ms | Mean fit delta | P90 fit delta | >5% hard | >8% severe |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, summary in payload["summary_by_run"].items():
        lines.append(
            f"| {label} | {summary.get('mean_abs_drift_ms')} | {summary.get('p90_abs_drift_ms')} | "
            f"{summary.get('max_abs_drift_ms')} | {summary.get('mean_fit_tempo_delta')} | "
            f"{summary.get('p90_fit_tempo_delta')} | {summary.get('fit_gt_hard_count')} | {summary.get('fit_gt_severe_count')} |"
        )
    lines.append("")
    lines.append("| Sample | Target s | Best fit delta | Notes |")
    lines.append("| --- | ---: | ---: | --- |")
    for row in payload["rows"]:
        best = row.get("best_by_fit_delta") or {}
        notes = []
        for label, item in (row.get("runs") or {}).items():
            notes.append(f"{label}: fit {item.get('fit_tempo_delta')}, drift {item.get('drift_ms')}ms")
        lines.append(
            f"| {row['sample_id']} | {row.get('target_sec')} | {best.get('label')}={best.get('fit_tempo_delta')} | "
            + "; ".join(notes)
            + " |"
        )
    lines.append("")
    lines.append(f"Listening pack: `{payload['listening_pack']}`")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_root = Path(args.data_root)
    features = {str(row["sample_id"]): row for row in read_jsonl(Path(args.features))}
    variant_filter = {item.strip() for item in args.variant_filter.split(",") if item.strip()}
    runs: list[dict[str, Any]] = []
    for raw in args.run:
        label, report_path = parse_label_path(raw)
        report = read_json(report_path)
        rows_by_id = {
            str(row.get("sample_id")): row
            for row in report.get("rows", [])
            if isinstance(row, dict)
            and row.get("sample_id")
            and row.get("path")
            and (not variant_filter or str(row.get("variant")) in variant_filter)
        }
        runs.append({"label": label, "report": str(report_path), "rows_by_id": rows_by_id})

    current_rows = load_current_audio(Path(args.episode_segments) if args.episode_segments else None)
    audio_dir = output_dir / "listening_pack" / "audio"
    out_rows: list[dict[str, Any]] = []
    by_run_for_summary: dict[str, list[dict[str, Any]]] = {run["label"]: [] for run in runs}
    for sid, feature in features.items():
        target_sec = float(feature.get("target_ms") or 0) / 1000.0
        row_out: dict[str, Any] = {
            "sample_id": sid,
            "target_sec": rounded(target_sec, 3),
            "text": feature.get("text"),
            "runs": {},
        }
        current = current_rows.get(sid) or {}
        current_rel = current.get("tts_audio_rel_path") or current.get("audio_relpath")
        if current_rel:
            copied = copy_audio(resolve_path(str(current_rel), data_root), audio_dir / f"{sid}-current-final.wav")
            row_out["current_final"] = {"audio": f"audio/{Path(copied).name}"} if copied else {"audio": None}
        best: dict[str, Any] | None = None
        for run in runs:
            label = run["label"]
            infer_row = run["rows_by_id"].get(sid)
            if not infer_row:
                row_out["runs"][label] = {"missing": True}
                continue
            metrics = duration_metrics(infer_row)
            source_path = resolve_path(str(infer_row.get("path")), data_root)
            copied = copy_audio(source_path, audio_dir / f"{sid}-{label}.wav")
            item = {**metrics, "audio": f"audio/{Path(copied).name}" if copied else None}
            row_out["runs"][label] = item
            by_run_for_summary[label].append(metrics)
            if item.get("fit_tempo_delta") is not None and (
                best is None or float(item["fit_tempo_delta"]) < float(best["fit_tempo_delta"])
            ):
                best = {"label": label, "fit_tempo_delta": item["fit_tempo_delta"]}
        row_out["best_by_fit_delta"] = best
        out_rows.append(row_out)

    payload = {
        "schema_version": "holodub.lora_holdout_adapter_compare.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "features": args.features,
        "thresholds": {
            "fit_warning_threshold": args.fit_warning_threshold,
            "fit_hard_threshold": args.fit_hard_threshold,
            "fit_severe_threshold": args.fit_severe_threshold,
        },
        "runs": [{"label": run["label"], "report": run["report"]} for run in runs],
        "summary_by_run": {
            label: summarize(rows, args.fit_warning_threshold, args.fit_hard_threshold, args.fit_severe_threshold)
            for label, rows in by_run_for_summary.items()
        },
        "rows": out_rows,
        "listening_pack": str(output_dir / "listening_pack" / "index.html"),
    }
    (output_dir / "duration_compare.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "duration_compare.md").write_text(render_markdown(payload), encoding="utf-8")
    render_html(payload, output_dir)
    print(json.dumps({"output": str(output_dir), "listening_pack": payload["listening_pack"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
