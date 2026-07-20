from __future__ import annotations

import wave
from array import array
from pathlib import Path

from tests.quality import dub_asr_validation as v


def write_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = array("h", samples)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(data.tobytes())


def test_expected_text_prefers_spoken_text() -> None:
    row = {
        "target_text": "服务器S3被选为leader",
        "meta": {"tts_spoken_text": "三号服务器被选为leader"},
    }
    assert v.expected_text_for_segment(row) == ("三号服务器被选为leader", "tts_spoken_text")


def test_analyze_results_flags_audio_and_text_quality(tmp_path: Path) -> None:
    write_wav(tmp_path / "jobs/1/tts.wav", [32767] * 200)
    rows = [
        {
            "segment_id": 1,
            "target_text": "S1拥有日志",
            "meta": {"voice_config_source": "global_default_fallback"},
            "judge_score": 0.5,
            "judge_meta": {"verdict": "retry"},
        }
    ]
    asr_results = [
        {
            "ok": True,
            "label": "c01_s0001",
            "segment_id": 1,
            "chapter_ordinal": 1,
            "segment_ordinal": 1,
            "audio_relpath": "jobs/1/tts.wav",
            "expected_text": "S1拥有日志",
            "asr_text": "\u4e00\u7d22\u9a6c\u91cc\u5148\u4ee4",
        }
    ]

    analyzed = v.analyze_results(rows=rows, asr_results=asr_results, data_root=tmp_path)
    flags = analyzed[0]["flags"]

    assert any(flag.startswith("weird_asr_terms") for flag in flags)
    assert any(flag.startswith("low_similarity") for flag in flags)
    assert any(flag.startswith("expected_mixed_tokens") for flag in flags)
    assert any(flag.startswith("audio_clipping") for flag in flags)
    assert "judge_retry" in flags
    assert "global_default_voice" in flags


def test_retry_verdict_is_flagged_even_with_high_score(tmp_path: Path) -> None:
    write_wav(tmp_path / "jobs/1/high-score-retry.wav", [0] * 200)
    rows = [
        {
            "segment_id": 9,
            "target_text": "leader appends entries",
            "meta": {},
            "judge_score": 0.9,
            "judge_meta": {"verdict": "retry"},
        }
    ]
    asr_results = [
        {
            "ok": True,
            "label": "c01_s0009",
            "segment_id": 9,
            "chapter_ordinal": 1,
            "segment_ordinal": 9,
            "audio_relpath": "jobs/1/high-score-retry.wav",
            "expected_text": "leader appends entries",
            "asr_text": "leader appends entries",
        }
    ]

    analyzed = v.analyze_results(rows=rows, asr_results=asr_results, data_root=tmp_path)

    assert "judge_retry" in analyzed[0]["flags"]


def test_summarize_and_gate_counts_failures(tmp_path: Path) -> None:
    write_wav(tmp_path / "jobs/1/a.wav", [0] * 200)
    rows = [
        {
            "segment_id": 1,
            "target_text": "hello",
            "meta": {},
            "judge_score": None,
            "judge_meta": {},
        }
    ]
    asr_results = [
        {
            "ok": True,
            "label": "c01_s0001",
            "segment_id": 1,
            "chapter_ordinal": 1,
            "segment_ordinal": 1,
            "audio_relpath": "jobs/1/a.wav",
            "expected_text": "hello",
            "asr_text": "",
        }
    ]

    analyzed = v.analyze_results(rows=rows, asr_results=asr_results, data_root=tmp_path)
    summary = v.summarize(analyzed)

    assert summary["empty_asr_count"] == 1
    assert summary["low_similarity_count"] == 1

    class Args:
        max_low_similarity = 0

    gate = v.gate_summary(summary, Args())
    assert gate["pass"] is False
