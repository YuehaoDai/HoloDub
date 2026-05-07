"""Unit tests for the per-segment ASR re-transcription path.

These tests exercise ASRAdapter.transcribe_window with the mock backend
so they run without GPU / faster-whisper / network access.  The mock
adapter reads a side-car ``<audio>.words.json`` to produce deterministic
output, which is enough to verify the ffmpeg trim + adapter dispatch +
temporary-file cleanup contract.

The route-level integration test (``transcribe_segment`` HTTP handler)
is exercised end-to-end in the post-deploy verification step rather
than here because it requires the full FastAPI dependency injection
(services container, gpu_guard, etc.).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.adapters.asr import ASRAdapter
from app.config import Settings
from app.services.model_registry import ModelRegistry


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(),
    reason="ffmpeg / ffprobe not available in this environment",
)


def _generate_silence_wav(path: Path, duration_sec: float = 5.0, sample_rate: int = 16000) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl=mono",
            "-t",
            f"{duration_sec:.3f}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _write_words_sidecar(audio_path: Path, words: list[dict]) -> Path:
    sidecar = audio_path.with_suffix(audio_path.suffix + ".words.json")
    sidecar.write_text(json.dumps({"words": words}), encoding="utf-8")
    return sidecar


def _mock_adapter(tmp_path: Path) -> ASRAdapter:
    settings = Settings(data_root=tmp_path, ml_asr_backend="mock")
    return ASRAdapter(settings, ModelRegistry())


def test_transcribe_window_rejects_inverted_range(tmp_path: Path) -> None:
    adapter = _mock_adapter(tmp_path)
    audio = tmp_path / "vocals.wav"
    _generate_silence_wav(audio)
    with pytest.raises(ValueError):
        adapter.transcribe_window(audio, source_language="", start_ms=2000, end_ms=2000)
    with pytest.raises(ValueError):
        adapter.transcribe_window(audio, source_language="", start_ms=2000, end_ms=1000)


def test_transcribe_window_returns_text_and_cleans_tempfile(tmp_path: Path) -> None:
    adapter = _mock_adapter(tmp_path)
    audio = tmp_path / "vocals.wav"
    _generate_silence_wav(audio, duration_sec=4.0)
    # The mock adapter consults audio_path.with_suffix("...words.json"),
    # but the temp clip will not have a side-car so it falls back to the
    # "audio.stem" single-word path.  Both behaviours are valid for this
    # contract test — we only require non-empty text output and clean
    # temporary-file handling.

    text, diagnostics = adapter.transcribe_window(
        audio, source_language="", start_ms=500, end_ms=2500
    )
    assert isinstance(text, str)
    assert text != ""
    assert any("window=500..2500ms" in d for d in diagnostics)
    assert any(d.startswith("asr backend=mock") for d in diagnostics)

    # No leftover .wav files in the system temp dir attributable to this run.
    # We cannot easily assert the OS-wide tempdir is empty, but we can verify
    # that the adapter does not leave files inside the test workspace.
    leftovers = [p for p in tmp_path.glob("**/*.wav") if p != audio]
    assert leftovers == [], f"unexpected leftover wavs: {leftovers}"


def test_transcribe_window_respects_time_window(tmp_path: Path) -> None:
    """Verify the trimmed clip duration matches end_ms - start_ms (within
    a small tolerance).  We reach into a custom adapter that intercepts
    the temp clip before it is consumed so the test can probe its length.
    """
    adapter = _mock_adapter(tmp_path)
    audio = tmp_path / "vocals.wav"
    _generate_silence_wav(audio, duration_sec=10.0)

    captured: list[Path] = []
    real_run = subprocess.run

    def capture_then_run(args, *a, **kw):  # type: ignore[no-untyped-def]
        result = real_run(args, *a, **kw)
        if isinstance(args, list) and len(args) >= 2 and args[0] == "ffmpeg":
            out = Path(args[-1])
            if out.exists():
                # Copy the clip aside so we can probe it after the adapter cleans up.
                copy = tmp_path / f"clip-{len(captured)}.wav"
                shutil.copyfile(out, copy)
                captured.append(copy)
        return result

    import app.adapters.asr as asr_mod

    asr_mod.subprocess.run = capture_then_run  # type: ignore[attr-defined]
    try:
        adapter.transcribe_window(
            audio, source_language="", start_ms=2000, end_ms=4500
        )
    finally:
        asr_mod.subprocess.run = real_run  # type: ignore[attr-defined]

    assert captured, "ffmpeg trim was not invoked"
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(captured[0]),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(probe.stdout.strip())
    # Requested 2.5 s window — accept ±0.2 s slack for ffmpeg framing rounding.
    assert 2.3 <= duration <= 2.7, f"unexpected clip duration {duration:.3f} s"
