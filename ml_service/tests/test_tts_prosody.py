"""Unit tests for the OPT-204 TTS adapter prosody conversion.

These tests target the pure helper functions
(`_emo_vector_from_valence_arousal` and `TTSAdapter._resolve_prosody_kwargs`)
so we can iterate on the (valence, arousal) → emo_vector mapping
without spinning up an actual IndexTTS2 instance (which is GPU-bound
and ~10s to load).

The larger end-to-end check — that an IndexTTS2 inline call accepts
emo_vector / emphasis_words kwargs without raising — lives in PR-13's
L2 staging script (`scripts/opt204-smoke.ps1`), NOT in this Python
test suite. CI containers do not carry the GPU runtime.
"""

from __future__ import annotations

import math

from app.adapters.tts import (
    _EMO_VECTOR_LEN,
    _IDX_ANGRY,
    _IDX_EXCITED,
    _IDX_HAPPY,
    _IDX_NEUTRAL,
    _IDX_SAD,
    _emo_vector_from_valence_arousal,
)


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    """Tolerant equality for the normalised-vector tests; the mapping
    uses linear interpolation so exact-match would be brittle if we
    later tune coefficients."""
    return math.isclose(a, b, abs_tol=tol)


def test_emo_vector_length_is_eight():
    """Sanity: IndexTTS2's emo_vector convention is exactly 8 elements.
    A drift here breaks the conditioning surface silently — the model
    accepts mis-sized vectors and produces unexpected emoting."""
    vec = _emo_vector_from_valence_arousal(0.0, 0.5)
    assert len(vec) == _EMO_VECTOR_LEN
    assert _EMO_VECTOR_LEN == 8


def test_emo_vector_is_l1_normalised():
    """Across the (valence, arousal) cube the projected vector MUST
    sum to ~1.0 so IndexTTS2 treats it as a probability distribution.
    Sampling the four quadrants + boundaries catches any branch where
    the normalisation got dropped."""
    samples = [
        (0.8, 0.9),
        (0.8, 0.1),
        (-0.8, 0.9),
        (-0.8, 0.1),
        (0.0, 0.5),
        (0.0, 0.0),
        (1.0, 1.0),
        (-1.0, 0.0),
    ]
    for v, a in samples:
        vec = _emo_vector_from_valence_arousal(v, a)
        s = sum(vec)
        assert _close(s, 1.0), f"valence={v} arousal={a} sum={s:.4f}"


def test_emo_vector_high_valence_high_arousal_is_excited():
    """The 'high valence + high arousal' quadrant should put non-trivial
    mass on excited/happy and no mass on the negative emotions."""
    vec = _emo_vector_from_valence_arousal(0.9, 0.9)
    assert vec[_IDX_EXCITED] > 0.3, vec
    assert vec[_IDX_HAPPY] > 0.2, vec
    assert vec[_IDX_SAD] == 0.0
    assert vec[_IDX_ANGRY] == 0.0


def test_emo_vector_low_valence_low_arousal_is_sad():
    """The 'low valence + low arousal' quadrant should put non-trivial
    mass on sadness + a touch of neutral, no mass on excited/angry."""
    vec = _emo_vector_from_valence_arousal(-0.8, 0.1)
    assert vec[_IDX_SAD] > 0.3, vec
    assert vec[_IDX_EXCITED] == 0.0
    assert vec[_IDX_ANGRY] == 0.0


def test_emo_vector_low_valence_high_arousal_is_angry():
    """The 'low valence + high arousal' quadrant maps to anger /
    surprise / fear; no mass on the positive emotions."""
    vec = _emo_vector_from_valence_arousal(-0.8, 0.9)
    assert vec[_IDX_ANGRY] > 0.3, vec
    assert vec[_IDX_HAPPY] == 0.0
    assert vec[_IDX_EXCITED] == 0.0


def test_emo_vector_neutral_default():
    """The (0, 0) centre point should be predominantly neutral so a
    segment without strong affect cues does not get over-emoted."""
    vec = _emo_vector_from_valence_arousal(0.0, 0.0)
    assert vec[_IDX_NEUTRAL] > 0.5, vec


def test_emo_vector_clipping():
    """Valence outside [-1,1] and arousal outside [0,1] must be clipped
    rather than producing NaNs or negative emo_vector entries. The
    translator schema enforces these ranges but production providers
    occasionally violate enums under load, so a defensive clip is
    cheap insurance."""
    vec_a = _emo_vector_from_valence_arousal(5.0, -10.0)
    vec_b = _emo_vector_from_valence_arousal(1.0, 0.0)
    assert vec_a == vec_b, "Out-of-range inputs must clip identically to in-range edges"
    for x in vec_a:
        assert x >= 0
        assert not math.isnan(x)


def test_emo_vector_step_at_arousal_threshold_is_bounded():
    """The MVP mapping uses arousal=0.5 as a hard quadrant split, so a
    step IS expected at the threshold. This test documents the upper
    bound on that step (per-emotion mass change ≤ 0.7) so a future
    "smooth the boundary" refactor has a regression guard.

    OPT-204 PR-13's L3 evaluation (50-sample human rating) will tell
    us whether the audible step is annoying enough to warrant the
    sigmoid blend. If yes, this bound should drop to ~0.2 and the
    mapping should be reworked; if no, this stays as-is and we keep
    the simpler heuristic."""
    v = 0.5
    a_lo = _emo_vector_from_valence_arousal(v, 0.49)
    a_hi = _emo_vector_from_valence_arousal(v, 0.51)
    max_delta = max(abs(a - b) for a, b in zip(a_lo, a_hi, strict=True))
    assert max_delta <= 0.7, f"step={max_delta:.3f} larger than MVP tolerance at arousal=0.5"


# ----------------------------------------------------------------------
# TTSAdapter._resolve_prosody_kwargs is a small helper that returns the
# infer kwargs dict for a given dubbing_meta. It does not require any
# IndexTTS2 model, so we can test it directly via a minimal adapter
# stub.
# ----------------------------------------------------------------------


class _StubSettings:
    """Minimal Settings stub satisfying TTSAdapter.__init__."""

    ml_tts_backend = "indextts2"
    indextts2_inline = True
    indextts2_use_emo_text = False
    data_root = "/tmp"
    default_sample_rate = 24000
    default_channels = 1
    ffmpeg_bin = "ffmpeg"


def _make_adapter():
    from app.adapters.tts import TTSAdapter

    return TTSAdapter(_StubSettings())  # type: ignore[arg-type]


def test_resolve_prosody_kwargs_disables_emo_text():
    """When dubbing_meta is present, use_emo_text MUST be set False —
    stacking emo_vector + emo_text produces double-emoting."""
    a = _make_adapter()
    kwargs = a._resolve_prosody_kwargs(
        {"emotion": {"valence": 0.5, "arousal": 0.7, "label": "excited"},
         "pacing": "normal"},
        text="Hello world",
    )
    assert kwargs["use_emo_text"] is False
    assert "emo_vector" in kwargs
    assert len(kwargs["emo_vector"]) == 8


def test_resolve_prosody_kwargs_only_anchored_emphasis_words():
    """Emphasis words that do NOT appear in the target text must be
    stripped — the model can't anchor on a token it never sees."""
    a = _make_adapter()
    kwargs = a._resolve_prosody_kwargs(
        {
            "emotion": {"valence": 0.0, "arousal": 0.0, "label": "neutral"},
            "pacing": "normal",
            "emphasis_words": ["hello", "world", "missing_token"],
        },
        text="hello world",
    )
    assert "emphasis_words" in kwargs
    assert kwargs["emphasis_words"] == ["hello", "world"]


def test_resolve_prosody_kwargs_skips_empty_emphasis_list():
    """An empty emphasis_words list must NOT produce an empty kwarg
    (IndexTTS2 versions that don't accept the kwarg would otherwise
    reject the call). Better to omit than to send []."""
    a = _make_adapter()
    kwargs = a._resolve_prosody_kwargs(
        {
            "emotion": {"valence": 0.0, "arousal": 0.0, "label": "neutral"},
            "pacing": "slow",
            "emphasis_words": [],
        },
        text="anything",
    )
    assert "emphasis_words" not in kwargs


def test_resolve_prosody_kwargs_missing_emotion_falls_back_to_neutral():
    """Defensive: a malformed dubbing_meta missing the emotion key must
    still produce a valid emo_vector (centred on neutral). The
    upstream try/except in _run_indextts2_inline catches outright
    failures; this branch keeps soft-malformed inputs working."""
    a = _make_adapter()
    kwargs = a._resolve_prosody_kwargs(
        {"pacing": "normal"},
        text="hello",
    )
    # Without emotion the mapping defaults to (valence=0, arousal=0) →
    # predominantly neutral.
    assert kwargs["use_emo_text"] is False
    assert sum(kwargs["emo_vector"]) > 0.99  # L1 normalised
