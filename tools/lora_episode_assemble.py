from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_OVERLAYS_PER_PASS = 30
BREATH_MARGIN_MS = 300
SAMPLE_RATE = 24000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assemble a full episode from a LoRA candidate_gate_report plus "
            "fallback TTS rows, without writing production DB state."
        )
    )
    parser.add_argument("--timeline", required=True, help="Episode segment timeline JSONL.")
    parser.add_argument("--chapters", required=True, help="Episode chapters manifest JSON.")
    parser.add_argument(
        "--candidate-report",
        action="append",
        required=True,
        help="Selected candidate_gate_report JSON. Repeat to merge rescue reports; later reports override duplicate sample_ids.",
    )
    parser.add_argument("--features", required=True)
    parser.add_argument("--rejected", default="")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--episode-id", type=int, default=146)
    parser.add_argument("--voice-key", default="vp0")
    parser.add_argument("--no-video", action="store_true")
    parser.add_argument("--measure-loudness", action="store_true")
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def path_from_any(raw: str | None, data_root: Path) -> Path | None:
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    repo_path = REPO_ROOT / path
    if repo_path.exists():
        return repo_path
    return data_root / path


def rel_to_repo(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def format_sec(ms: int | float) -> str:
    return f"{float(ms) / 1000.0:.3f}"


def run_cmd(cmd: list[str], *, label: str) -> None:
    print(f"[episode-assemble] {label}: {' '.join(cmd[:8])}{' ...' if len(cmd) > 8 else ''}")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout or "").splitlines()[-80:])
        raise RuntimeError(f"{label} failed with exit {proc.returncode}\n{tail}")


def probe_duration_ms(ffprobe: str, path: Path) -> int:
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
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ffprobe failed for {path}")
    return int(round(float(proc.stdout.strip()) * 1000))


def loudnorm_measure(ffmpeg: str, path: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-23:TP=-1:LRA=7:print_format=json",
            "-f",
            "null",
            "-",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {"error": "\n".join((proc.stderr or "").splitlines()[-20:])}
    stderr = proc.stderr or ""
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end < start:
        return {"error": "loudnorm JSON block not found"}
    try:
        return json.loads(stderr[start : end + 1])
    except json.JSONDecodeError as exc:
        return {"error": f"parse loudnorm JSON: {exc}"}


def clip_and_fade_filter(actual_ms: int, max_ms: int) -> str:
    clip = actual_ms
    if max_ms > 0 and max_ms < clip:
        clip = max_ms
    if clip <= 80:
        return f"aresample={SAMPLE_RATE},atrim=duration={clip / 1000.0:.3f},"
    fade = 0.03
    fade_out_start = max(0.0, clip / 1000.0 - fade)
    return (
        f"aresample={SAMPLE_RATE},atrim=duration={clip / 1000.0:.3f},"
        f"afade=t=in:st=0:d={fade:.2f},"
        f"afade=t=out:st={fade_out_start:.2f}:d={fade:.2f},"
    )


def build_voice_mix(filter_parts: list[str], labels: list[str], out_label: str) -> list[str]:
    if len(labels) == 1:
        filter_parts.append(f"{labels[0]}volume=1.0[{out_label}]")
    else:
        filter_parts.append(
            f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:"
            f"dropout_transition=0:normalize=0,volume=1.0[{out_label}]"
        )
    return filter_parts


def build_voice_chunk(
    ffmpeg: str,
    out_path: Path,
    duration_ms: int,
    overlays: list[dict[str, Any]],
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration_sec = format_sec(duration_ms)
    args = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-t",
        duration_sec,
        "-i",
        f"anullsrc=r={SAMPLE_RATE}:cl=mono",
    ]
    filter_parts: list[str] = []
    labels: list[str] = []
    for idx, overlay in enumerate(overlays):
        args.extend(["-i", str(overlay["abs_path"])])
        label = f"s{idx}"
        filter_parts.append(
            f"[{idx + 1}:a]"
            f"{clip_and_fade_filter(int(overlay['duration_ms']), int(overlay['max_duration_ms']))}"
            f"adelay={int(overlay['delay_ms'])}|{int(overlay['delay_ms'])},"
            f"alimiter=limit=0.95[{label}]"
        )
        labels.append(f"[{label}]")
    filter_parts = build_voice_mix(filter_parts, labels, "vout")
    filter_parts.append(
        "[0:a][vout]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95,apad=whole_dur={duration_sec},"
        f"atrim=duration={duration_sec},asetpts=N/SR/TB,aresample=24000[mix]"
    )
    args.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[mix]",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            str(out_path),
        ]
    )
    run_cmd(args, label=f"voice_chunk {out_path.name}")


def merge_voice_chunks(ffmpeg: str, out_path: Path, duration_ms: int, chunk_paths: list[Path]) -> None:
    if len(chunk_paths) == 1:
        shutil.copyfile(chunk_paths[0], out_path)
        return
    args = [ffmpeg, "-y"]
    labels: list[str] = []
    for idx, path in enumerate(chunk_paths):
        args.extend(["-i", str(path)])
        labels.append(f"[{idx}:a]")
    filter_arg = (
        f"{''.join(labels)}amix=inputs={len(chunk_paths)}:duration=first:"
        f"dropout_transition=0:normalize=0,alimiter=limit=0.95,"
        f"apad=whole_dur={format_sec(duration_ms)},"
        f"atrim=duration={format_sec(duration_ms)},asetpts=N/SR/TB[mix]"
    )
    args.extend(
        [
            "-filter_complex",
            filter_arg,
            "-map",
            "[mix]",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            str(out_path),
        ]
    )
    run_cmd(args, label=f"merge_voice_chunks {out_path.name}")


def apply_bgm(ffmpeg: str, voice_path: Path, bgm_path: Path | None, out_path: Path, duration_ms: int) -> None:
    duration_sec = format_sec(duration_ms)
    if bgm_path is None:
        args = [
            ffmpeg,
            "-y",
            "-i",
            str(voice_path),
            "-af",
            f"aresample={SAMPLE_RATE},apad=whole_dur={duration_sec},atrim=duration={duration_sec},asetpts=N/SR/TB",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            str(out_path),
        ]
        run_cmd(args, label=f"duration_lock {out_path.name}")
        return
    args = [
        ffmpeg,
        "-y",
        "-i",
        str(voice_path),
        "-i",
        str(bgm_path),
        "-filter_complex",
        (
            f"[0:a]aresample={SAMPLE_RATE},apad=whole_dur={duration_sec},"
            f"atrim=duration={duration_sec},asetpts=N/SR/TB,asplit=2[sc][mixin];"
            f"[1:a]aresample={SAMPLE_RATE},apad=whole_dur={duration_sec},"
            f"atrim=duration={duration_sec},volume=0.75,alimiter=limit=0.95[bgm];"
            "[bgm][sc]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=250[duckedbgm];"
            "[mixin][duckedbgm]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            f"alimiter=limit=0.95,apad=whole_dur={duration_sec},"
            f"atrim=duration={duration_sec},asetpts=N/SR/TB[mix]"
        ),
        "-map",
        "[mix]",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        str(out_path),
    ]
    run_cmd(args, label=f"apply_bgm {out_path.name}")


def render_dub_track(
    ffmpeg: str,
    out_path: Path,
    duration_ms: int,
    bgm_path: Path | None,
    overlays: list[dict[str, Any]],
) -> None:
    if not overlays:
        args = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-t",
            format_sec(duration_ms),
            "-i",
            f"anullsrc=r={SAMPLE_RATE}:cl=mono",
            "-ar",
            str(SAMPLE_RATE),
            "-ac",
            "1",
            str(out_path),
        ]
        run_cmd(args, label=f"silence_track {out_path.name}")
        return

    tmp_paths: list[Path] = []
    try:
        for offset in range(0, len(overlays), MAX_OVERLAYS_PER_PASS):
            chunk = overlays[offset : offset + MAX_OVERLAYS_PER_PASS]
            chunk_path = out_path.with_name(f"_{out_path.stem}_voice_{offset:03d}.wav")
            build_voice_chunk(ffmpeg, chunk_path, duration_ms, chunk)
            tmp_paths.append(chunk_path)
        voice_path = out_path.with_name(f"_{out_path.stem}_voice_merged.wav")
        tmp_paths.append(voice_path)
        merge_voice_chunks(ffmpeg, voice_path, duration_ms, tmp_paths[:-1])
        apply_bgm(ffmpeg, voice_path, bgm_path, out_path, duration_ms)
    finally:
        for path in tmp_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def mux_video(ffmpeg: str, video_path: Path, audio_path: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        str(out_path),
    ]
    run_cmd(args, label=f"mux {out_path.name}")


def concat_videos(ffmpeg: str, inputs: list[Path], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path = out_path.with_suffix(".concat.txt")
    concat_path.write_text(
        "\n".join(f"file '{str(path.resolve()).replace(chr(39), chr(39) + '\\\\' + chr(39) + chr(39))}'" for path in inputs)
        + "\n",
        encoding="utf-8",
    )
    args = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-c",
        "copy",
        str(out_path),
    ]
    try:
        run_cmd(args, label=f"concat {out_path.name}")
    except RuntimeError:
        args = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            str(out_path),
        ]
        run_cmd(args, label=f"concat_reencode {out_path.name}")


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "max": max(values),
        "min": min(values),
    }


def selected_rows_by_sample(candidate_reports: list[tuple[Path, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for report_path, candidate_report in candidate_reports:
        for row in candidate_report.get("rows", []):
            sample_id = str(row.get("sample_id") or "")
            if sample_id:
                enriched = dict(row)
                enriched["_episode_assembly_candidate_report"] = str(report_path)
                rows[sample_id] = enriched
    return rows


def rejected_reason(row: dict[str, Any] | None) -> str | None:
    if not row:
        return None
    reason = row.get("reason") or row.get("reject_reason")
    if reason:
        return str(reason)
    reasons = row.get("rejection_reasons")
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])
    return None


def lora_assembly_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    similarities = [float(row["similarity"]) for row in rows if row.get("similarity") is not None]
    term_recalls = [
        float(row["critical_term_recall"])
        for row in rows
        if row.get("critical_term_recall") is not None
    ]
    f0_ranges = [
        float(row["f0_range_semitones_10_90"])
        for row in rows
        if row.get("f0_range_semitones_10_90") is not None
    ]
    tempo_deltas = [
        float(row["fit_tempo_delta"])
        for row in rows
        if row.get("fit_tempo_delta") is not None
    ]
    return {
        "abs_drift_ms": summarize([float(row["abs_drift_ms"]) for row in rows]),
        "similarity": summarize(similarities),
        "critical_term_recall": summarize(term_recalls),
        "f0_range_semitones_10_90": summarize(f0_ranges),
        "fit_tempo_delta": summarize(tempo_deltas),
        "join_quality_fail_count": sum(
            1 for row in rows if row.get("join_quality_pass") is False
        ),
        "candidate_label_counts": dict(Counter(str(row.get("candidate_label") or "") for row in rows)),
    }


def chapter_rows(chapters_manifest: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in chapters_manifest.get("chapters", []):
        ordinal = int(row.get("ordinal") or 0)
        if ordinal:
            out[ordinal] = row
    return out


def main() -> None:
    args = parse_args()
    data_root = path_from_any(args.data_root, REPO_ROOT)
    assert data_root is not None
    run_dir = path_from_any(args.run_dir, REPO_ROOT)
    assert run_dir is not None
    run_dir.mkdir(parents=True, exist_ok=True)

    timeline = read_jsonl(Path(args.timeline))
    chapters_manifest = read_json(Path(args.chapters))
    chapters = chapter_rows(chapters_manifest)
    candidate_reports = [(Path(path), read_json(Path(path))) for path in args.candidate_report]
    selected_by_sample = selected_rows_by_sample(candidate_reports)
    accepted_ids = {str(row.get("sample_id")) for row in read_jsonl(Path(args.features))}
    rejected_rows = {str(row.get("sample_id")): row for row in read_jsonl(Path(args.rejected))} if args.rejected else {}

    timeline = [row for row in timeline if int(row.get("episode_id") or 0) == args.episode_id]
    timeline.sort(key=lambda row: (int(row.get("chapter_ordinal") or 0), int(row.get("ordinal") or 0)))
    by_chapter: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in timeline:
        by_chapter[int(row.get("chapter_ordinal") or 0)].append(row)

    output_chapters = run_dir / "chapters" / args.voice_key
    output_final = run_dir / "output" / args.voice_key / "final.mp4"
    output_final_audio = run_dir / "output" / args.voice_key / "final_audio.wav"
    output_chapters.mkdir(parents=True, exist_ok=True)

    all_segment_rows: list[dict[str, Any]] = []
    chapter_outputs: list[Path] = []
    chapter_audio_outputs: list[Path] = []

    for ordinal in sorted(by_chapter):
        rows = by_chapter[ordinal]
        if not rows:
            continue
        chapter = chapters.get(ordinal, {})
        duration_ms = int(chapter.get("duration_ms") or 0)
        if duration_ms <= 0:
            duration_ms = max(int(row.get("end_ms") or 0) for row in rows)
        source_video = path_from_any(f"episodes/{args.episode_id}/chapters/source/ch{ordinal:02d}.mp4", data_root)
        source_bgm = path_from_any(f"episodes/{args.episode_id}/chapters/source/ch{ordinal:02d}.bgm.wav", data_root)
        if source_bgm and source_bgm.exists():
            duration_ms = max(duration_ms, probe_duration_ms(args.ffprobe, source_bgm))

        next_start_by_id: dict[int, int] = {}
        for idx, row in enumerate(rows[:-1]):
            next_start_by_id[int(row["segment_id"])] = int(rows[idx + 1].get("start_ms") or 0)

        overlays: list[dict[str, Any]] = []
        for row in rows:
            sample_id = f"seg{int(row['segment_id'])}"
            selected = selected_by_sample.get(sample_id)
            source_kind = "lora_selected" if selected else "fallback_existing_tts"
            if selected:
                audio_path = path_from_any(str(selected.get("selected_path") or selected.get("path") or ""), data_root)
                duration_ms_row = int(round(float(selected.get("duration_sec") or 0.0) * 1000))
                abs_drift_ms = float(selected.get("abs_drift_ms") or 0.0)
                candidate_label = str(selected.get("candidate_label") or "")
                similarity = selected.get("similarity")
                critical_term_recall = selected.get("critical_term_recall")
                f0_range = selected.get("f0_range_semitones_10_90")
                fit_tempo_delta = selected.get("fit_tempo_delta")
                join_quality = selected.get("join_quality") if isinstance(selected.get("join_quality"), dict) else {}
                join_quality_pass = join_quality.get("pass") if join_quality else None
            else:
                audio_path = path_from_any(str(row.get("tts_audio_rel_path") or ""), data_root)
                duration_ms_row = int(row.get("tts_duration_ms") or 0)
                target_ms = int(row.get("original_duration_ms") or 0)
                abs_drift_ms = abs(duration_ms_row - target_ms)
                candidate_label = "existing_tts_fallback"
                similarity = None
                critical_term_recall = None
                f0_range = None
                fit_tempo_delta = None
                join_quality_pass = None

            audio_exists = bool(audio_path and audio_path.exists())
            start_ms = int(row.get("start_ms") or 0)
            end_ms = int(row.get("end_ms") or start_ms)
            target_ms = int(row.get("original_duration_ms") or max(0, end_ms - start_ms))
            max_duration_ms = 0
            next_start = next_start_by_id.get(int(row["segment_id"]))
            if next_start is not None:
                slot_ms = next_start - start_ms - BREATH_MARGIN_MS
                if slot_ms > 0:
                    max_duration_ms = slot_ms
            clipped_ms = max(0, duration_ms_row - max_duration_ms) if max_duration_ms > 0 else 0
            rejected = rejected_rows.get(sample_id)
            reject_reason = rejected_reason(rejected)
            segment_summary = {
                "sample_id": sample_id,
                "segment_id": int(row["segment_id"]),
                "job_id": int(row["job_id"]),
                "chapter_ordinal": ordinal,
                "ordinal": int(row.get("ordinal") or 0),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "target_ms": target_ms,
                "audio_duration_ms": duration_ms_row,
                "abs_drift_ms": abs_drift_ms,
                "source_kind": source_kind,
                "candidate_label": candidate_label,
                "candidate_label_chain": selected.get("candidate_label_chain") if selected else None,
                "selected_candidate_report": selected.get("_episode_assembly_candidate_report") if selected else None,
                "accepted_manifest": sample_id in accepted_ids,
                "reject_reason": reject_reason,
                "rejection_reasons": rejected.get("rejection_reasons") if rejected else None,
                "similarity": similarity,
                "critical_term_recall": critical_term_recall,
                "f0_range_semitones_10_90": f0_range,
                "fit_tempo_delta": fit_tempo_delta,
                "join_quality_pass": join_quality_pass,
                "audio_path": str(audio_path) if audio_path else "",
                "audio_exists": audio_exists,
                "max_duration_ms": max_duration_ms,
                "clipped_ms": clipped_ms,
            }
            all_segment_rows.append(segment_summary)
            if audio_exists and duration_ms_row > 0:
                overlays.append(
                    {
                        "abs_path": audio_path,
                        "delay_ms": start_ms,
                        "duration_ms": duration_ms_row,
                        "max_duration_ms": max_duration_ms,
                    }
                )

        dub_path = output_chapters / f"ch{ordinal:02d}.dub_track.wav"
        if not dub_path.exists():
            render_dub_track(args.ffmpeg, dub_path, duration_ms, source_bgm if source_bgm and source_bgm.exists() else None, overlays)
        else:
            print(f"[episode-assemble] reuse {dub_path}")
        chapter_audio_outputs.append(dub_path)
        if not args.no_video and source_video and source_video.exists():
            chapter_mp4 = output_chapters / f"ch{ordinal:02d}.mp4"
            if not chapter_mp4.exists():
                mux_video(args.ffmpeg, source_video, dub_path, chapter_mp4)
            else:
                print(f"[episode-assemble] reuse {chapter_mp4}")
            chapter_outputs.append(chapter_mp4)

    if chapter_audio_outputs:
        concat_audio_inputs = chapter_audio_outputs
        output_final_audio.parent.mkdir(parents=True, exist_ok=True)
        if len(concat_audio_inputs) == 1:
            shutil.copyfile(concat_audio_inputs[0], output_final_audio)
        else:
            args_ff = [args.ffmpeg, "-y"]
            labels: list[str] = []
            for idx, path in enumerate(concat_audio_inputs):
                args_ff.extend(["-i", str(path)])
                labels.append(f"[{idx}:a]")
            args_ff.extend(
                [
                    "-filter_complex",
                    f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[a]",
                    "-map",
                    "[a]",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "1",
                    str(output_final_audio),
                ]
            )
            run_cmd(args_ff, label="concat final_audio.wav")

    if chapter_outputs and not args.no_video:
        concat_videos(args.ffmpeg, chapter_outputs, output_final)

    segments_by_kind = Counter(row["source_kind"] for row in all_segment_rows)
    clip_rows = [row for row in all_segment_rows if int(row.get("clipped_ms") or 0) > 0]
    missing_audio = [row for row in all_segment_rows if not row.get("audio_exists")]
    fallback_rows = [row for row in all_segment_rows if row["source_kind"] == "fallback_existing_tts"]
    lora_rows = [row for row in all_segment_rows if row["source_kind"] == "lora_selected"]

    final_audio_duration_ms = probe_duration_ms(args.ffprobe, output_final_audio) if output_final_audio.exists() else 0
    final_video_duration_ms = probe_duration_ms(args.ffprobe, output_final) if output_final.exists() else 0
    expected_duration_ms = int(chapters_manifest.get("total_duration_ms") or sum(int(chapters[k].get("duration_ms") or 0) for k in chapters))

    summary: dict[str, Any] = {
        "schema_version": "holodub.lora_episode_assembly.v1",
        "episode_id": args.episode_id,
        "run_dir": rel_to_repo(run_dir),
        "candidate_reports": [str(path) for path in args.candidate_report],
        "timeline": str(args.timeline),
        "chapters_manifest": str(args.chapters),
        "outputs": {
            "final_audio": rel_to_repo(output_final_audio) if output_final_audio.exists() else "",
            "final_video": rel_to_repo(output_final) if output_final.exists() else "",
            "chapters_dir": rel_to_repo(output_chapters),
        },
        "coverage": {
            "timeline_segments": len(all_segment_rows),
            "accepted_feature_rows": len(accepted_ids),
            "lora_selected_segments": segments_by_kind["lora_selected"],
            "fallback_existing_tts_segments": segments_by_kind["fallback_existing_tts"],
            "missing_audio_segments": len(missing_audio),
            "manifest_rejected_fallback_segments": sum(1 for row in fallback_rows if row.get("reject_reason")),
        },
        "duration": {
            "expected_duration_ms": expected_duration_ms,
            "final_audio_duration_ms": final_audio_duration_ms,
            "final_video_duration_ms": final_video_duration_ms,
            "final_audio_abs_drift_ms": abs(final_audio_duration_ms - expected_duration_ms) if final_audio_duration_ms else None,
            "final_video_abs_drift_ms": abs(final_video_duration_ms - expected_duration_ms) if final_video_duration_ms else None,
        },
        "lora_selected_metrics": lora_assembly_metrics(lora_rows),
        "input_candidate_report_summaries": [
            {
                "path": str(path),
                "selected": report.get("summary", {}).get("selected", {}),
                "candidate_count": len(report.get("rows", [])),
            }
            for path, report in candidate_reports
        ],
        "fallback_existing_tts_metrics": {
            "abs_drift_ms": summarize([float(row["abs_drift_ms"]) for row in fallback_rows]),
            "reject_reason_counts": dict(Counter(str(row.get("reject_reason") or "not_in_feature_manifest") for row in fallback_rows)),
        },
        "complete_timeline_metrics": {
            "abs_drift_ms": summarize([float(row["abs_drift_ms"]) for row in all_segment_rows]),
            "clip_count": len(clip_rows),
            "clip_total_ms": sum(int(row.get("clipped_ms") or 0) for row in clip_rows),
            "clip_max_ms": max([int(row.get("clipped_ms") or 0) for row in clip_rows] or [0]),
            "clip_rows": [
                {
                    "sample_id": row["sample_id"],
                    "source_kind": row["source_kind"],
                    "candidate_label": row["candidate_label"],
                    "clipped_ms": row["clipped_ms"],
                    "max_duration_ms": row["max_duration_ms"],
                    "audio_duration_ms": row["audio_duration_ms"],
                }
                for row in sorted(clip_rows, key=lambda item: int(item.get("clipped_ms") or 0), reverse=True)[:25]
            ],
        },
        "missing_audio": missing_audio,
    }
    if args.measure_loudness and output_final_audio.exists():
        summary["final_audio_loudnorm"] = loudnorm_measure(args.ffmpeg, output_final_audio)

    write_json(run_dir / "episode_result_summary.json", summary)
    write_json(run_dir / "episode_segment_lineage.json", {"segments": all_segment_rows})
    print(json.dumps({"summary": rel_to_repo(run_dir / "episode_result_summary.json"), "final_video": summary["outputs"]["final_video"], "final_audio": summary["outputs"]["final_audio"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
