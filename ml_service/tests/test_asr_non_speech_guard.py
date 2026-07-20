import math
import shutil
import wave
from pathlib import Path

from app.adapters.asr import (
    WhisperSegmentMeta,
    _classify_asr_segment,
    _speech_overlap_ratio,
    _text_hallucination_reason,
)
from app.adapters.vad import SpeechSpan
from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_root=tmp_path,
        asr_non_speech_guard_enabled=True,
        asr_guard_audio_features_enabled=False,
    )


def _write_tone(path: Path, frequency_hz: float, duration_sec: float = 2.0) -> None:
    sample_rate = 16000
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for i in range(int(sample_rate * duration_sec)):
            sample = 0.25 * math.sin(2 * math.pi * frequency_hz * i / sample_rate)
            frames.extend(int(sample * 32767).to_bytes(2, "little", signed=True))
        wav.writeframes(frames)


def _audio_settings(tmp_path: Path) -> Settings:
    if shutil.which("ffmpeg") is None:
        import pytest

        pytest.skip("ffmpeg is required for audio feature guard tests")
    return Settings(
        data_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        asr_non_speech_guard_enabled=True,
        asr_guard_audio_features_enabled=True,
    )


def test_guard_drops_high_no_speech_prob(tmp_path: Path) -> None:
    decision = _classify_asr_segment(
        _settings(tmp_path),
        tmp_path / "missing.wav",
        WhisperSegmentMeta(
            start_ms=0,
            end_ms=5000,
            text="thank you thank you thank you",
            avg_logprob=-0.8,
            no_speech_prob=0.95,
            compression_ratio=1.1,
        ),
        [],
    )

    assert decision.drop
    assert decision.reason.startswith("no_speech_prob")


def test_guard_keeps_confident_text_despite_high_no_speech_prob(tmp_path: Path) -> None:
    decision = _classify_asr_segment(
        _settings(tmp_path),
        tmp_path / "missing.wav",
        WhisperSegmentMeta(
            start_ms=0,
            end_ms=3220,
            text="一号节点在此处甚至根本没有任何日志条目",
            avg_logprob=-0.08,
            no_speech_prob=0.69,
            compression_ratio=1.29,
        ),
        [],
    )

    assert not decision.drop


def test_guard_keeps_normal_speech_like_segment(tmp_path: Path) -> None:
    decision = _classify_asr_segment(
        _settings(tmp_path),
        tmp_path / "missing.wav",
        WhisperSegmentMeta(
            start_ms=0,
            end_ms=4500,
            text="Today we are going to discuss cache consistency.",
            avg_logprob=-0.12,
            no_speech_prob=0.02,
            compression_ratio=1.05,
        ),
        [],
    )

    assert not decision.drop


def test_audio_guard_drops_high_zcr_soft_suspicious_segment(tmp_path: Path) -> None:
    audio_path = tmp_path / "chalk.wav"
    _write_tone(audio_path, 3800)

    decision = _classify_asr_segment(
        _audio_settings(tmp_path),
        audio_path,
        WhisperSegmentMeta(
            start_ms=0,
            end_ms=2000,
            text="thank you",
            avg_logprob=-0.65,
            no_speech_prob=0.40,
            compression_ratio=1.0,
        ),
        [],
    )

    assert decision.drop
    assert decision.reason.startswith("high_zcr")


def test_audio_guard_keeps_low_zcr_soft_suspicious_segment(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    _write_tone(audio_path, 440)

    decision = _classify_asr_segment(
        _audio_settings(tmp_path),
        audio_path,
        WhisperSegmentMeta(
            start_ms=0,
            end_ms=2000,
            text="today we continue",
            avg_logprob=-0.65,
            no_speech_prob=0.40,
            compression_ratio=1.0,
        ),
        [],
    )

    assert not decision.drop


def test_guard_drops_when_pyannote_overlap_is_too_low(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    decision = _classify_asr_segment(
        settings,
        tmp_path / "missing.wav",
        WhisperSegmentMeta(
            start_ms=10_000,
            end_ms=16_000,
            text="the system continues with this example",
            avg_logprob=-0.1,
            no_speech_prob=0.01,
            compression_ratio=1.0,
        ),
        [SpeechSpan(start_ms=0, end_ms=5000)],
    )

    assert decision.drop
    assert decision.reason.startswith("vad_overlap_low")


def test_speech_overlap_ratio_allows_boundary_padding() -> None:
    overlap = _speech_overlap_ratio(
        start_ms=1000,
        end_ms=2000,
        spans=[SpeechSpan(start_ms=2050, end_ms=3000)],
        pad_ms=100,
    )

    assert overlap > 0


def test_text_hallucination_detects_repetition() -> None:
    assert _text_hallucination_reason("aaaaaaa aaa", 5000, 24.0) == "repeated_chars"
    assert _text_hallucination_reason(
        "the the the the the the", 5000, 24.0
    ) in {"low_char_diversity", "repeated_tokens"}
