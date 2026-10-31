import wave
from array import array
from pathlib import Path

from app.services.lora_candidate_quality import (
    asr_similarity,
    critical_term_metrics,
    edge_rms,
    join_quality,
    normalize_asr_text,
    repair_loud_boundary_tails,
)


def _write_tail_tone_wav(path: Path, *, tail_amplitude: int = 3000, sample_rate: int = 16000) -> None:
    samples = array("h", [0] * sample_rate)
    tail_frames = int(sample_rate * 0.12)
    for index in range(sample_rate - tail_frames, sample_rate):
        samples[index] = tail_amplitude
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def _write_silence_wav(path: Path, *, sample_rate: int = 16000) -> None:
    samples = array("h", [0] * sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def test_repair_loud_boundary_tails_fades_only_hot_previous_parts(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    _write_tail_tone_wav(first)
    _write_silence_wav(second)

    before = join_quality([first, second], pause_sec=0.18)
    assert "loud_pre_join_tail" in before["reasons"]

    repaired_paths, report = repair_loud_boundary_tails(
        [first, second],
        tmp_path,
        "sample",
        enabled=True,
        fade_ms=120.0,
        max_boundary_tail_dbfs=-24.0,
    )

    assert report["enabled"] is True
    assert report["repaired_part_indices"] == [1]
    assert repaired_paths[0] != first
    assert repaired_paths[1] == second
    assert edge_rms(repaired_paths[0], 120.0)["tail_dbfs"] <= -24.0
    after = join_quality(repaired_paths, pause_sec=0.18)
    assert "loud_pre_join_tail" not in after["reasons"]


def test_asr_similarity_tolerates_slot_number_homophone() -> None:
    expected = (
        "\u5047\u8bbe\uff0c\u6211\u5e94\u8be5\u4e4b\u524d\u8bf4\u8fc7\uff0c"
        "\u5982\u679c\u670d\u52a1\u5668\u5728\u8fd9\u91cc\u53d1\u9001"
        "\u69fd\u4f4d13\u7684\u4fe1\u606f\u3002\u8fd9\u5fc5\u987b"
        "\u610f\u5473\u7740\u3002\u8be5\u670d\u52a1\u5668\u7684"
        "\u4e0b\u4e00\u4e2a\u7d22\u5f15\u9488\u5bf9\u8fd9\u4e24\u4e2a"
        "\u5176\u4ed6\u670d\u52a1\u5668\u800c\u8a00\u5747\u4e3a13\u3002"
    )
    observed = (
        "\u5047\u8bbe\u6211\u5e94\u8be5\u4e4b\u524d\u8bf4\u8fc7 "
        "\u5982\u679c\u670d\u52a1\u5668\u5728\u8fd9\u91cc "
        "\u53d1\u9001\u66f9\u9b4f\u5341\u4e09\u7684\u4fe1\u606f "
        "\u8fd9\u5fc5\u987b\u610f\u5473\u7740 \u8be5\u670d\u52a1\u5668\u7684"
        "\u4e0b\u4e00\u4e2a\u7d22\u5f15 \u9488\u5bf9\u8fd9\u4e24\u4e2a"
        "\u5176\u4ed6\u670d\u52a1\u5668\u800c\u8a00 \u5747\u4e3a\u5341\u4e09"
    )

    assert "\u69fd\u4f4d\u5341\u4e09" in normalize_asr_text(expected)
    assert "\u69fd\u4f4d\u5341\u4e09" in normalize_asr_text(observed)
    assert asr_similarity(expected, observed) >= 0.985

    metrics = critical_term_metrics(expected, observed)
    assert metrics["critical_term_recall"] == 1.0


def test_asr_similarity_tolerates_raft_homophones_and_dash_punctuation() -> None:
    expected = (
        "\u56e0\u6b64\u5f53\u4e00\u53f7\u8282\u70b9\u5b95\u673a\u3001"
        "\u53ef\u4ee5\u9009\u4e3e\u65b0\u9886\u5bfc\u8005\u65f6\uff0c"
        "\u81f3\u5c11\u4e8c\u53f7\u8282\u70b9\u6216\u4e09\u53f7\u8282\u70b9"
        "\u4e2d\u6709\u4e00\u65b9\u77e5\u6653\u5f53\u524d\u4efb\u671f\u662f\u516b\u3002"
        "\u82e5\u4ec5\u4e00\u65b9\u77e5\u6653\uff0c\u5219\u552f\u5b83\u80fd\u5f53\u9009"
        "\u2014\u2014\u56e0\u4efb\u671f\u66f4\u9ad8\uff1b"
        "\u82e5\u53cc\u65b9\u90fd\u77e5\u4e03\uff0c"
        "\u5219\u90fd\u4f1a\u5728\u516b\u53d1\u8d77\u7ade\u9009\u3001"
        "\u4e89\u53d6\u6210\u4e3a\u9886\u5bfc\u8005\u3002"
    )
    observed = (
        "\u56e0\u6b64\u5f53\u4e00\u53f7\u8282\u70b9\u5f53\u673a "
        "\u53ef\u4ee5\u9009\u4e3e\u65b0\u9886\u5bfc\u8005\u65f6 "
        "\u81f3\u5c11\u4e8c\u53f7\u8282\u70b9\u6216\u4e09\u53f7\u8282\u70b9\u4e2d "
        "\u6709\u4e00\u65b9\u77e5\u6653\u5f53\u524d\u4efb\u671f\u662f\u516b "
        "\u82e5\u4ec5\u4e00\u65b9\u77e5\u6653 \u5219\u552f\u4ed6\u80fd\u5f53\u9009 "
        "\u56e0\u4efb\u671f\u66f4\u9ad8 \u82e5\u53cc\u65b9\u90fd\u77e5\u4e03 "
        "\u5219\u90fd\u4f1a\u5728\u516b\u53d1\u8d77\u7ade\u9009 "
        "\u4e89\u53d6\u6210\u4e3a\u9886\u5bfc\u8005"
    )

    normalized = normalize_asr_text(expected)
    assert "\u2014" not in normalized
    assert "\u5f53\u673a" not in normalize_asr_text(observed)
    assert "\u4ed6" not in normalize_asr_text(observed)
    assert asr_similarity(expected, observed) >= 0.985
