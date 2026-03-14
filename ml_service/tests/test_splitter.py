from app.adapters.asr import segment_words
from app.models import WordToken


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_words(pairs: list[tuple[str, int, int]]) -> list[WordToken]:
    return [WordToken(word=w, start_ms=s, end_ms=e) for w, s, e in pairs]


# ---------------------------------------------------------------------------
# Short word-count must NOT trigger a split — remains one segment
# ---------------------------------------------------------------------------

def test_no_split_when_word_count_below_minimum():
    """Fewer than 5 words: no punctuation or silence split allowed."""
    words = _make_words([
        ("Hello",   0,    400),
        ("world.",  420,  900),   # ends with '.', but only 2 words total
        ("Next",    1800, 2100),  # gap=900 ms ≥ 800 ms, still only 4 words
        ("line",    2110, 2500),
    ])
    segments = segment_words(words, min_segment_sec=0.5, max_segment_sec=10.0)

    assert len(segments) == 1
    assert segments[0].split_reason == "end"


# ---------------------------------------------------------------------------
# Sentence-ending punctuation splits when word-count ≥ 5 on BOTH sides
# (post-merge step would re-absorb a trailing <5-word segment)
# ---------------------------------------------------------------------------

def test_splits_on_sentence_ending_punctuation_with_enough_words():
    """≥5 words + sentence-ending punctuation → split, both halves kept."""
    words = _make_words([
        ("This",      0,    300),
        ("is",        320,  500),
        ("a",         520,  600),
        ("complete",  620,  900),
        ("sentence.", 920,  1400),   # 5th word, ends sentence
        ("And",       1420, 1700),
        ("this",      1720, 1900),
        ("is",        1920, 2100),
        ("another",   2120, 2400),
        ("one",       2420, 2700),
    ])
    segments = segment_words(words, min_segment_sec=1.0, max_segment_sec=10.0)

    assert len(segments) == 2
    assert segments[0].split_reason == "punctuation"
    assert "sentence" in segments[0].text
    assert "another" in segments[1].text
    assert segments[1].split_reason == "end"


# ---------------------------------------------------------------------------
# Silence gap ≥ 800 ms + both halves have ≥ 5 words → split
# ---------------------------------------------------------------------------

def test_splits_on_long_silence_gap():
    """Gap ≥ 800 ms with 5+ words on both sides → two segments."""
    words = _make_words([
        ("One",   0,    300),
        ("two",   320,  600),
        ("three", 620,  900),
        ("four",  920,  1200),
        ("five",  1220, 1500),   # 5 words; gap to next = 900 ms ≥ 800 ms
        ("six",   2400, 2700),   # gap = 2400-1500 = 900 ms
        ("seven", 2720, 3000),
        ("eight", 3020, 3300),
        ("nine",  3320, 3600),
        ("ten",   3620, 3900),
    ])
    segments = segment_words(words, min_segment_sec=1.0, max_segment_sec=10.0)

    assert len(segments) == 2
    assert segments[0].split_reason == "silence_gap"
    assert segments[1].split_reason == "end"


# ---------------------------------------------------------------------------
# Gap < 800 ms must NOT split (old threshold was 500 ms)
# ---------------------------------------------------------------------------

def test_no_split_on_short_silence_gap():
    """Gap of 700 ms (< 800 ms) should not trigger a split."""
    words = _make_words([
        ("One",   0,    300),
        ("two",   320,  600),
        ("three", 620,  900),
        ("four",  920,  1200),
        ("five",  1220, 1500),
        ("six",   2200, 2500),   # gap = 700 ms < 800 ms
        ("seven", 2520, 2800),
    ])
    segments = segment_words(words, min_segment_sec=1.0, max_segment_sec=10.0)

    assert len(segments) == 1
    assert segments[0].split_reason == "end"


# ---------------------------------------------------------------------------
# Comma / semicolon must NOT trigger a split
# ---------------------------------------------------------------------------

def test_comma_does_not_split():
    """Words ending with comma or semicolon are not sentence boundaries."""
    words = _make_words([
        ("Well,",  0,    400),
        ("this",   420,  700),
        ("is",     720,  900),
        ("fine,",  920,  1200),
        ("right",  1220, 1500),
        ("here",   1520, 1800),
    ])
    segments = segment_words(words, min_segment_sec=1.0, max_segment_sec=10.0)

    assert len(segments) == 1


# ---------------------------------------------------------------------------
# max_duration forces a split regardless of word count
# ---------------------------------------------------------------------------

def test_max_duration_forces_split():
    """max_duration split fires even without punctuation or a silence gap."""
    words = _make_words([
        ("one",   0,    500),
        ("two",   510,  1000),
        ("three", 1010, 1500),
        ("four",  1510, 2000),
        ("five",  2010, 2500),   # duration = 2500 ms == max → split here
        ("six",   2510, 3000),
        ("seven", 3010, 3500),
        ("eight", 3510, 4000),
        ("nine",  4010, 4500),
        ("ten",   4510, 5000),
    ])
    segments = segment_words(words, min_segment_sec=1.0, max_segment_sec=2.5)

    assert len(segments) >= 2
    assert any(s.split_reason == "max_duration" for s in segments[:-1])
