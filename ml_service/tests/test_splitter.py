from app.adapters.asr import segment_words
from app.models import WordToken


def test_segment_words_splits_on_gap_and_punctuation():
    words = [
        WordToken(word="Hello", start_ms=0, end_ms=400),
        WordToken(word="world.", start_ms=420, end_ms=900),
        WordToken(word="Next", start_ms=1800, end_ms=2100),
        WordToken(word="line", start_ms=2110, end_ms=2500),
    ]

    segments = segment_words(words, min_segment_sec=0.5, max_segment_sec=5.0)

    assert len(segments) == 2
    assert segments[0].text == "Hello world."
    assert segments[0].split_reason == "punctuation"
    assert segments[1].split_reason == "end"
