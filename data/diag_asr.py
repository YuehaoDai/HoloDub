#!/usr/bin/env python3
"""
单段 ASR 诊断：取 job 122 的 vocals.wav，
对 segment 0 (start=1200ms, end=11120ms) + 后续 2000ms，
用 faster_whisper 做 word-level 转写，输出结果到 /data/diag_result.txt
"""
import subprocess
import pathlib
import sys
import os

VOCALS  = "/data/jobs/122/separate/vocals.wav"
CLIP    = "/data/diag_clip.wav"
RESULT  = "/data/diag_result.txt"

# Extract: from 0s to 14s (covers seg0 1.2s-11.12s + 2.88s padding)
ret = subprocess.run(
    ["ffmpeg", "-y", "-ss", "0", "-i", VOCALS, "-t", "14.0",
     "-acodec", "pcm_s16le", CLIP],
    capture_output=True
)
if ret.returncode != 0:
    pathlib.Path(RESULT).write_text("ffmpeg failed:\n" + ret.stderr.decode(errors="replace"))
    sys.exit(1)

try:
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="auto", compute_type="auto")
    segs_iter, info = model.transcribe(
        CLIP,
        word_timestamps=True,
        language="en",
        vad_filter=True,
        beam_size=5,
    )

    lines = [
        "=== Word-level timestamps: 0-14s of job122/vocals.wav ===",
        f"Detected language: {info.language}",
        "",
        "DB info:",
        "  Segment 0: start=1200ms, end=11120ms",
        "  Segment 1: start=12990ms",
        "  NOTE: raw whisper end (before 500ms pad) would be ~10620ms",
        "",
        "Whisper output (absolute timestamps in this clip = same as vocals.wav):",
    ]
    for seg in segs_iter:
        lines.append(f"\nSEG [{seg.start*1000:.0f}ms - {seg.end*1000:.0f}ms]: {seg.text.strip()}")
        for w in (seg.words or []):
            marker = " <-- LAST WORD?" if w.word.strip().lower().startswith("where") else ""
            lines.append(f"  WORD [{w.start*1000:.0f}ms - {w.end*1000:.0f}ms]: {repr(w.word)}{marker}")

    lines += [
        "",
        "=== Key check: does 'where' end before 11120ms? ===",
        "If the last 'where' word ends well before 11120ms, padding is sufficient.",
        "If it ends after 10620ms but before 11120ms, padding already helps.",
        "If it ends after 11120ms, we need more padding.",
    ]
    pathlib.Path(RESULT).write_text("\n".join(lines))
except Exception as e:
    pathlib.Path(RESULT).write_text(f"Error: {e}")

print("Done. Result at", RESULT)
