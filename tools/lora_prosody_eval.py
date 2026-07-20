from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import torch
import torchaudio
from torchaudio.functional import detect_pitch_frequency


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure pitch/energy dynamics for LoRA TTS samples.")
    parser.add_argument("--reports", required=True, help="Comma-separated inference or ASR reports.")
    parser.add_argument("--output", required=True)
    parser.add_argument("--features", default="")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--reference-wav", default="")
    parser.add_argument("--include-teacher", action="store_true")
    parser.add_argument("--frame-sec", type=float, default=0.05)
    parser.add_argument("--pitch-frame-sec", type=float, default=0.02)
    parser.add_argument("--active-dbfs", type=float, default=-55.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
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
    if text.startswith("/data/"):
        return data_root / text.removeprefix("/data/")
    if text.startswith(f"{data_root.as_posix().rstrip('/')}/"):
        return Path(text)
    path = Path(raw_path)
    if path.is_absolute():
        return path
    if Path(text).exists():
        return Path(text)
    return data_root / text


def quantile(values: torch.Tensor, q: float) -> float:
    if values.numel() == 0:
        return 0.0
    return float(torch.quantile(values.float(), q).item())


def semitone_range(low_hz: float, high_hz: float) -> float:
    if low_hz <= 0 or high_hz <= 0:
        return 0.0
    return 12.0 * math.log2(high_hz / low_hz)


def load_mono(path: Path) -> tuple[torch.Tensor, int]:
    wav, sample_rate = torchaudio.load(str(path))
    if wav.ndim == 2 and wav.size(0) > 1:
        wav = wav.mean(dim=0, keepdim=True)
    return wav, int(sample_rate)


def rms_dbfs(wav: torch.Tensor, sample_rate: int, frame_sec: float) -> torch.Tensor:
    frame = max(1, int(round(sample_rate * frame_sec)))
    samples = wav[0]
    if samples.numel() < frame:
        return torch.empty(0)
    frames = samples.unfold(0, frame, frame)
    rms = frames.pow(2).mean(dim=1).sqrt().clamp_min(1e-9)
    return 20.0 * torch.log10(rms)


def audio_metrics(path: Path, args: argparse.Namespace) -> dict[str, Any]:
    wav, sample_rate = load_mono(path)
    duration_sec = wav.size(-1) / float(sample_rate)
    pitch_wav = wav
    if sample_rate != args.sample_rate:
        pitch_wav = torchaudio.functional.resample(wav, sample_rate, args.sample_rate)

    f0 = detect_pitch_frequency(
        pitch_wav,
        args.sample_rate,
        frame_time=args.pitch_frame_sec,
        win_length=30,
        freq_low=60,
        freq_high=500,
    )[0]
    voiced = f0[(f0 >= 60) & (f0 <= 500)].float()
    db = rms_dbfs(wav, sample_rate, args.frame_sec)
    active = db[db >= args.active_dbfs].float()

    metrics: dict[str, Any] = {
        "duration_sec": duration_sec,
        "voiced_frame_count": int(voiced.numel()),
        "rms_frame_count": int(db.numel()),
    }
    if db.numel() > 0:
        silent_ratio = float((db < args.active_dbfs).float().mean().item())
        metrics["silence_ratio"] = silent_ratio
    if active.numel() > 0:
        rms_p10 = quantile(active, 0.10)
        rms_p90 = quantile(active, 0.90)
        metrics.update(
            {
                "rms_dbfs_mean": float(active.mean().item()),
                "rms_dbfs_std": float(active.std(unbiased=False).item()),
                "rms_dbfs_p10": rms_p10,
                "rms_dbfs_p90": rms_p90,
                "rms_dbfs_range_10_90": rms_p90 - rms_p10,
            }
        )
    if voiced.numel() > 5:
        median = float(voiced.median().item())
        f0_p10 = quantile(voiced, 0.10)
        f0_p90 = quantile(voiced, 0.90)
        semitones = 12.0 * torch.log2(voiced / max(median, 1e-6))
        metrics.update(
            {
                "f0_hz_mean": float(voiced.mean().item()),
                "f0_hz_median": median,
                "f0_hz_std": float(voiced.std(unbiased=False).item()),
                "f0_hz_p10": f0_p10,
                "f0_hz_p90": f0_p90,
                "f0_range_semitones_10_90": semitone_range(f0_p10, f0_p90),
                "f0_std_semitones": float(semitones.std(unbiased=False).item()),
            }
        )
    return metrics


def report_rows(report_path: Path, data_root: Path) -> list[dict[str, Any]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    rows = payload.get("rows", [])
    out: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("path"):
            continue
        out.append(
            {
                "kind": "report",
                "report": str(report_path),
                "sample_id": row.get("sample_id"),
                "variant": row.get("variant"),
                "group": f"{report_path.parent.name}:{row.get('variant')}",
                "path": str(resolve_path(row["path"], data_root)),
                "target_sec": row.get("target_sec"),
                "duration_sec_reported": row.get("duration_sec"),
                "similarity": row.get("similarity"),
            }
        )
    return out


def teacher_rows(features_path: Path, data_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in read_jsonl(features_path):
        relpath = row.get("audio_relpath") or row.get("audio_path")
        if not relpath:
            continue
        rows.append(
            {
                "kind": "teacher",
                "report": "",
                "sample_id": row.get("sample_id"),
                "variant": "teacher",
                "group": "teacher",
                "path": str(resolve_path(relpath, data_root)),
                "target_sec": float(row.get("target_ms") or 0) / 1000.0,
                "duration_sec_reported": float(row.get("audio_ms") or 0) / 1000.0,
                "similarity": None,
            }
        )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_variant.setdefault(str(row.get("group") or row["variant"]), []).append(row)

    summary: dict[str, Any] = {}
    fields = [
        "f0_range_semitones_10_90",
        "f0_std_semitones",
        "rms_dbfs_range_10_90",
        "rms_dbfs_std",
        "silence_ratio",
    ]
    for variant, values in by_variant.items():
        item: dict[str, Any] = {"count": len(values)}
        for field in fields:
            nums = [float(row[field]) for row in values if row.get(field) is not None]
            if nums:
                item[f"mean_{field}"] = sum(nums) / len(nums)
                item[f"min_{field}"] = min(nums)
                item[f"max_{field}"] = max(nums)
        summary[variant] = item
    return summary


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    rows: list[dict[str, Any]] = []
    for raw_report in args.reports.split(","):
        report_path = Path(raw_report.strip())
        if report_path:
            rows.extend(report_rows(report_path, data_root))
    if args.include_teacher and args.features:
        rows.extend(teacher_rows(Path(args.features), data_root))
    if args.reference_wav:
        rows.append(
            {
                "kind": "reference",
                "report": "",
                "sample_id": "reference",
                "variant": "reference",
                "group": "reference",
                "path": str(resolve_path(args.reference_wav, data_root)),
                "target_sec": None,
                "duration_sec_reported": None,
                "similarity": None,
            }
        )

    measured: list[dict[str, Any]] = []
    for row in rows:
        path = Path(row["path"])
        if not path.exists():
            measured.append({**row, "error": "missing_audio"})
            continue
        try:
            measured.append({**row, **audio_metrics(path, args)})
        except Exception as exc:  # noqa: BLE001
            measured.append({**row, "error": f"{type(exc).__name__}: {exc}"})

    payload = {
        "reports": [item.strip() for item in args.reports.split(",") if item.strip()],
        "features": args.features,
        "reference_wav": args.reference_wav,
        "rows": measured,
        "summary": summarize([row for row in measured if "error" not in row]),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
