from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply bounded tempo fitting to LoRA inference wavs and emit a new ASR-ready report."
    )
    parser.add_argument("--inference-report", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-report", required=True)
    parser.add_argument("--variants", default="lora", help="Comma-separated variants to fit.")
    parser.add_argument("--variant-suffix", default="fit")
    parser.add_argument("--max-tempo-delta", type=float, default=0.06)
    parser.add_argument(
        "--min-fit-drift-ms",
        type=float,
        default=0.0,
        help="Skip fitting when the source absolute drift is already at or below this threshold.",
    )
    parser.add_argument("--already-close-delta", type=float, default=0.003)
    parser.add_argument("--filter", choices=("atempo", "rubberband"), default="atempo")
    return parser.parse_args()


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return frames / float(rate)


def atempo_filter(tempo: float) -> str:
    if 0.5 <= tempo <= 2.0:
        return f"atempo={tempo:.8f}"

    parts: list[str] = []
    remaining = tempo
    while remaining > 2.0:
        parts.append("atempo=2.00000000")
        remaining /= 2.0
    while remaining < 0.5:
        parts.append("atempo=0.50000000")
        remaining /= 0.5
    parts.append(f"atempo={remaining:.8f}")
    return ",".join(parts)


def run_fit(input_path: Path, output_path: Path, tempo: float, filter_name: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if filter_name == "rubberband":
        audio_filter = f"rubberband=tempo={tempo:.8f}:pitch=1.0"
    else:
        audio_filter = atempo_filter(tempo)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(input_path),
            "-af",
            audio_filter,
            "-ar",
            "22050",
            "-ac",
            "1",
            str(output_path),
        ],
        check=True,
    )


def resolve_audio_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.exists():
        return path

    normalized = raw_path.replace("\\", "/")
    if normalized.startswith("/data/"):
        candidate = Path("data") / normalized[len("/data/") :]
        if candidate.exists():
            return candidate

    return path


def fit_row(row: dict[str, Any], output_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    source_path = resolve_audio_path(str(row["path"]))
    target_sec = float(row.get("target_sec") or 0.0)
    actual_sec = float(row.get("duration_sec") or 0.0)
    fitted_variant = f"{row['variant']}_{args.variant_suffix}"
    output_path = output_dir / f"{row['sample_id']}-{fitted_variant}.wav"

    fitted = {
        **row,
        "variant": fitted_variant,
        "source_variant": row["variant"],
        "source_path": row["path"],
        "path": str(output_path),
        "fit_filter": args.filter,
        "fit_applied": False,
        "fit_tempo": None,
        "fit_tempo_delta": None,
    }
    if target_sec <= 0 or actual_sec <= 0:
        shutil.copyfile(source_path, output_path)
        fitted["fit_skipped"] = "missing_duration"
        fitted["duration_sec"] = wav_duration_sec(output_path)
        return fitted

    drift_ms = abs((actual_sec - target_sec) * 1000.0)
    tempo = actual_sec / target_sec
    tempo_delta = abs(tempo - 1.0)
    fitted["fit_tempo"] = round(tempo, 8)
    fitted["fit_tempo_delta"] = round(tempo_delta, 8)

    if drift_ms <= args.min_fit_drift_ms:
        shutil.copyfile(source_path, output_path)
        fitted["fit_skipped"] = "drift_within_threshold"
    elif tempo_delta < args.already_close_delta:
        shutil.copyfile(source_path, output_path)
        fitted["fit_skipped"] = "already_close"
    elif tempo_delta > args.max_tempo_delta:
        shutil.copyfile(source_path, output_path)
        fitted["fit_skipped"] = "tempo_out_of_range"
    else:
        run_fit(source_path, output_path, tempo, args.filter)
        fitted["fit_applied"] = True

    fitted["duration_sec"] = wav_duration_sec(output_path)
    fitted["post_fit_drift_ms"] = round((float(fitted["duration_sec"]) - target_sec) * 1000.0, 3)
    return fitted


def main() -> int:
    args = parse_args()
    report = json.loads(Path(args.inference_report).read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = {item.strip() for item in args.variants.split(",") if item.strip()}

    rows = [fit_row(row, output_dir, args) for row in report["rows"] if row.get("variant") in variants]
    payload = {
        "source_inference_report": args.inference_report,
        "fit_policy": {
            "variants": sorted(variants),
            "variant_suffix": args.variant_suffix,
            "max_tempo_delta": args.max_tempo_delta,
            "min_fit_drift_ms": args.min_fit_drift_ms,
            "already_close_delta": args.already_close_delta,
            "filter": args.filter,
        },
        "rows": rows,
    }
    output_report = Path(args.output_report)
    output_report.parent.mkdir(parents=True, exist_ok=True)
    output_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
