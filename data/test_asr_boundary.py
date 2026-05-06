#!/usr/bin/env python3
"""
临时调试脚本：对 job 122 segment 0 边界区域 (9.0s-13.5s) 做 word-level Whisper 转写，
确认 end_ms padding 后末词时间戳是否准确。
输出保存到 /data/test_asr_result.txt
"""
import sys, subprocess, pathlib, json

# 先用 ffmpeg 从 vocals.wav 裁出 9.0s-13.5s 的片段
VOCALS = "/data/jobs/122/separate/vocals.wav"
CLIP   = "/data/test_clip_9to13.wav"
OUT    = "/data/test_asr_result.txt"

ret = subprocess.run(
    ["ffmpeg", "-y", "-ss", "9.0", "-i", VOCALS, "-t", "4.5",
     "-acodec", "pcm_s16le", CLIP],
    capture_output=True
)
if ret.returncode != 0:
    pathlib.Path(OUT).write_text("ffmpeg failed:\n" + ret.stderr.decode())
    sys.exit(1)

from faster_whisper import WhisperModel
model = WhisperModel("large-v3", device="auto", compute_type="auto")

lines = [
    "=== Word-level timestamps for clip 9.0s-13.5s of job122/vocals.wav ===",
    "(absolute time = clip_time + 9000ms)",
    ""
]

segs_list, info = model.transcribe(
    CLIP,
    word_timestamps=True,
    language="en",
    vad_filter=True,
    beam_size=5,
)

for seg in segs_list:
    abs_start = seg.start + 9.0
    abs_end   = seg.end   + 9.0
    lines.append(f"SEG [{abs_start:.3f}s - {abs_end:.3f}s] ({(abs_end-abs_start)*1000:.0f}ms): {seg.text.strip()}")
    for w in (seg.words or []):
        lines.append(f"  WORD [{w.start+9:.3f}s - {w.end+9:.3f}s]: {w.word!r}")

lines += [
    "",
    f"DB segment 0: start_ms=1200 end_ms=11120",
    f"  => current end_ms in ABSOLUTE time: 11.120s",
    f"  => Whisper last word should end before that if padding works",
]

pathlib.Path(OUT).write_text("\n".join(lines))
print("done, see", OUT)
