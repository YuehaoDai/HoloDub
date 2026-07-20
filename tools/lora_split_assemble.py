from __future__ import annotations

import argparse
import json
import math
import subprocess
import wave
from array import array
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assemble split LoRA TTS part wavs into ASR/gate-ready whole-sample reports."
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument(
        "--parts-report",
        action="append",
        required=True,
        help="Part inference/ASR report. May be repeated; later reports override same sample_id parts.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-report", required=True)
    parser.add_argument("--variant", default="split_gatefit")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--sample-ids", default="")
    parser.add_argument("--sample-rate", type=int, default=22050)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--join-window-ms", type=float, default=120.0)
    parser.add_argument(
        "--duration-padding-policy",
        choices=("fixed", "distribute_silence"),
        default="fixed",
        help="Optionally absorb short assembled duration with bounded silence between split parts.",
    )
    parser.add_argument(
        "--max-extra-pause-sec",
        type=float,
        default=0.0,
        help="Maximum extra silence added per join when --duration-padding-policy=distribute_silence.",
    )
    parser.add_argument("--max-edge-rms-jump-db", type=float, default=16.0)
    parser.add_argument(
        "--max-boundary-tail-dbfs",
        type=float,
        default=-24.0,
        help="Fail a join when the previous part is still this loud at its tail; less negative is louder.",
    )
    parser.add_argument(
        "--repair-loud-boundary-tails",
        action="store_true",
        help="Apply a short tail fade to split parts that would otherwise fail the loud-tail join gate.",
    )
    parser.add_argument("--boundary-tail-fade-ms", type=float, default=120.0)
    parser.add_argument("--max-total-drift-ms", type=float, default=500.0)
    return parser.parse_args()


def resolve_audio_path(raw_path: str, data_root: Path) -> Path:
    text = str(raw_path).replace("\\", "/")
    if text.startswith("/data/"):
        return data_root / text.removeprefix("/data/")
    path = Path(text)
    if path.exists() or path.is_absolute():
        return path
    return data_root / text


def wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def read_wav_samples(path: Path) -> tuple[array, int]:
    with wave.open(str(path), "rb") as handle:
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit PCM wav for RMS check, got sample width {width}: {path}")
    samples = array("h")
    samples.frombytes(frames)
    return samples, rate


def fade_tail(path: Path, output_path: Path, fade_ms: float) -> Path:
    with wave.open(str(path), "rb") as handle:
        params = handle.getparams()
        frames = handle.readframes(handle.getnframes())
    if params.sampwidth != 2:
        raise ValueError(f"expected 16-bit PCM wav for tail fade, got sample width {params.sampwidth}: {path}")
    samples = array("h")
    samples.frombytes(frames)
    channels = max(1, int(params.nchannels))
    fade_frames = max(1, int(params.framerate * max(1.0, fade_ms) / 1000.0))
    fade_samples = min(len(samples), fade_frames * channels)
    start = len(samples) - fade_samples
    frame_count = max(1, fade_samples // channels)
    for frame in range(frame_count):
        gain = max(0.0, 1.0 - (frame + 1) / frame_count)
        base = start + frame * channels
        for channel in range(channels):
            idx = base + channel
            if idx < len(samples):
                samples[idx] = int(samples[idx] * gain)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as handle:
        handle.setparams(params)
        handle.writeframes(samples.tobytes())
    return output_path


def rms_dbfs(samples: array) -> float:
    if not samples:
        return -120.0
    total = sum(float(sample) * float(sample) for sample in samples)
    rms = math.sqrt(total / len(samples))
    if rms <= 0:
        return -120.0
    return max(-120.0, 20.0 * math.log10(rms / 32768.0))


def edge_rms(path: Path, window_ms: float) -> dict[str, float]:
    samples, rate = read_wav_samples(path)
    window = max(1, int(rate * max(window_ms, 1.0) / 1000.0))
    return {
        "head_dbfs": round(rms_dbfs(samples[:window]), 3),
        "tail_dbfs": round(rms_dbfs(samples[-window:]), 3),
    }


def run_concat(
    part_paths: list[Path],
    output_path: Path,
    pause_secs: list[float],
    sample_rate: int,
    channels: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-y", "-v", "error"]
    filter_inputs: list[str] = []
    input_index = 0
    for index, path in enumerate(part_paths):
        command.extend(["-i", str(path)])
        filter_inputs.append(f"[{input_index}:a]")
        input_index += 1
        if index < len(part_paths) - 1 and pause_secs[index] > 0:
            command.extend(
                ["-f", "lavfi", "-t", f"{pause_secs[index]:.6f}", "-i", f"anullsrc=r={sample_rate}:cl=mono"]
            )
            filter_inputs.append(f"[{input_index}:a]")
            input_index += 1
    command.extend(
        [
            "-filter_complex",
            "".join(filter_inputs) + f"concat=n={len(filter_inputs)}:v=0:a=1",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            str(output_path),
        ]
    )
    subprocess.run(command, check=True)


def part_rows_by_id(reports: list[dict[str, Any]], data_root: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for report in reports:
        for row in report.get("rows", []):
            if not row.get("sample_id") or not row.get("path"):
                continue
            rows[str(row["sample_id"])] = {
                **row,
                "resolved_path": str(resolve_audio_path(str(row["path"]), data_root)),
            }
    return rows


def repair_loud_boundary_tails(
    paths: list[Path],
    output_dir: Path,
    sample_id: str,
    args: argparse.Namespace,
) -> tuple[list[Path], dict[str, Any]]:
    if not args.repair_loud_boundary_tails or len(paths) < 2 or args.boundary_tail_fade_ms <= 0:
        return paths, {"enabled": False, "repaired_part_indices": []}
    repaired = list(paths)
    repairs: list[dict[str, Any]] = []
    for index, path in enumerate(paths[:-1]):
        edge = edge_rms(path, args.join_window_ms)
        tail_dbfs = float(edge["tail_dbfs"])
        if tail_dbfs <= float(args.max_boundary_tail_dbfs):
            continue
        target = output_dir / "_boundary_repair" / f"{sample_id}_p{index + 1}_tailfade.wav"
        repaired[index] = fade_tail(path, target, args.boundary_tail_fade_ms)
        repaired_edge = edge_rms(repaired[index], args.join_window_ms)
        repairs.append(
            {
                "part_index": index + 1,
                "source_path": str(path),
                "repaired_path": str(repaired[index]),
                "fade_ms": args.boundary_tail_fade_ms,
                "tail_dbfs_before": edge["tail_dbfs"],
                "tail_dbfs_after": repaired_edge["tail_dbfs"],
            }
        )
    return repaired, {
        "enabled": True,
        "fade_ms": args.boundary_tail_fade_ms,
        "repaired_part_indices": [item["part_index"] for item in repairs],
        "repairs": repairs,
    }


def selected_plan_rows(plan: dict[str, Any], sample_ids: set[str]) -> list[dict[str, Any]]:
    rows = plan.get("planned", [])
    if not sample_ids:
        return rows
    return [row for row in rows if str(row.get("sample_id")) in sample_ids]


def assemble_row(
    plan_row: dict[str, Any],
    part_rows: dict[str, dict[str, Any]],
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    sample_id = str(plan_row["sample_id"])
    parts = sorted(plan_row.get("parts", []), key=lambda item: int(item.get("index") or 0))
    missing = [part["sample_id"] for part in parts if str(part["sample_id"]) not in part_rows]
    if missing:
        raise ValueError(f"{sample_id}: missing part rows in report: {missing}")

    rows = [part_rows[str(part["sample_id"])] for part in parts]
    source_paths = [Path(str(row["resolved_path"])) for row in rows]
    output_path = output_dir / f"{sample_id}-{args.variant}.wav"
    pause_sec = float(plan_row.get("pause_sec") or 0.0)
    base_pause_secs = [pause_sec for _ in range(max(0, len(source_paths) - 1))]
    target_sec = float(plan_row["target_sec"])
    part_duration_sec = sum(wav_duration_sec(path) for path in source_paths)
    fixed_duration_sec = part_duration_sec + sum(base_pause_secs)
    extra_pause_sec = 0.0
    unapplied_padding_sec = 0.0
    pause_secs = list(base_pause_secs)
    if (
        args.duration_padding_policy == "distribute_silence"
        and pause_secs
        and args.max_extra_pause_sec > 0
        and target_sec > fixed_duration_sec
    ):
        missing_sec = target_sec - fixed_duration_sec
        extra_per_join = min(float(args.max_extra_pause_sec), missing_sec / len(pause_secs))
        pause_secs = [pause + extra_per_join for pause in pause_secs]
        extra_pause_sec = extra_per_join * len(pause_secs)
        unapplied_padding_sec = max(0.0, missing_sec - extra_pause_sec)
    paths, boundary_repair = repair_loud_boundary_tails(source_paths, output_dir, sample_id, args)
    run_concat(paths, output_path, pause_secs, args.sample_rate, args.channels)

    duration_sec = wav_duration_sec(output_path)
    edges = [edge_rms(path, args.join_window_ms) for path in paths]
    joins: list[dict[str, Any]] = []
    for index in range(len(edges) - 1):
        jump = abs(float(edges[index]["tail_dbfs"]) - float(edges[index + 1]["head_dbfs"]))
        joins.append(
            {
                "index": index + 1,
                "prev_tail_dbfs": edges[index]["tail_dbfs"],
                "next_head_dbfs": edges[index + 1]["head_dbfs"],
                "edge_rms_jump_db": round(jump, 3),
                "pause_sec": round(pause_secs[index], 6),
                "base_pause_sec": pause_sec,
            }
        )
    max_jump = max((float(join["edge_rms_jump_db"]) for join in joins), default=0.0)
    drift_ms = (duration_sec - target_sec) * 1000.0
    max_fit_delta = max((float(row.get("fit_tempo_delta") or 0.0) for row in rows), default=0.0)
    quality_reasons: list[str] = []
    if abs(drift_ms) > args.max_total_drift_ms:
        quality_reasons.append("high_total_drift")
    if any(float(join["pause_sec"]) <= 0.02 for join in joins) and max_jump > args.max_edge_rms_jump_db:
        quality_reasons.append("large_edge_rms_jump")
    loud_tails = [
        join for join in joins if float(join["prev_tail_dbfs"]) > float(args.max_boundary_tail_dbfs)
    ]
    if loud_tails:
        quality_reasons.append("loud_pre_join_tail")

    return {
        "sample_id": sample_id,
        "variant": args.variant,
        "path": str(output_path),
        "duration_sec": duration_sec,
        "target_sec": target_sec,
        "teacher_sec": target_sec,
        "source_variant": "split",
        "source_parts": [row["path"] for row in rows],
        "source_part_sample_ids": [row["sample_id"] for row in rows],
        "fit_filter": "atempo",
        "fit_applied": any(bool(row.get("fit_applied")) for row in rows),
        "fit_tempo_delta": round(max_fit_delta, 8),
        "post_fit_drift_ms": round(drift_ms, 3),
        "split_parts": len(rows),
        "split_pause_sec": pause_sec,
        "split_pause_secs": [round(value, 6) for value in pause_secs],
        "duration_padding_policy": args.duration_padding_policy,
        "duration_padding": {
            "base_duration_sec": round(fixed_duration_sec, 6),
            "extra_pause_sec": round(extra_pause_sec, 6),
            "unapplied_padding_sec": round(unapplied_padding_sec, 6),
            "max_extra_pause_sec": args.max_extra_pause_sec,
        },
        "boundary_repair": boundary_repair,
        "split_strategy": "automatic_clause_duration_plan",
        "join_window_ms": args.join_window_ms,
        "join_quality": {
            "pass": not quality_reasons,
            "reasons": quality_reasons,
            "max_edge_rms_jump_db": round(max_jump, 3),
            "max_boundary_tail_dbfs": args.max_boundary_tail_dbfs,
            "joins": joins,
        },
    }


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    parts_reports = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.parts_report]
    sample_ids = {item.strip() for item in args.sample_ids.split(",") if item.strip()}
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows_by_id = part_rows_by_id(parts_reports, data_root)
    assembled = [assemble_row(row, rows_by_id, output_dir, args) for row in selected_plan_rows(plan, sample_ids)]
    payload = {
        "plan": args.plan,
        "parts_reports": args.parts_report,
        "policy": {
            "variant": args.variant,
            "sample_rate": args.sample_rate,
            "channels": args.channels,
            "join_window_ms": args.join_window_ms,
            "max_edge_rms_jump_db": args.max_edge_rms_jump_db,
            "max_boundary_tail_dbfs": args.max_boundary_tail_dbfs,
            "max_total_drift_ms": args.max_total_drift_ms,
            "duration_padding_policy": args.duration_padding_policy,
            "max_extra_pause_sec": args.max_extra_pause_sec,
        },
        "rows": assembled,
    }
    output_report = Path(args.output_report)
    output_report.parent.mkdir(parents=True, exist_ok=True)
    output_report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({"assembled": len(assembled), "output_report": str(output_report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
