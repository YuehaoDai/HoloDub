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
import wave
from array import array

from app.adapters.tts import (
    _EMO_VECTOR_LEN,
    _IDX_ANGRY,
    _IDX_CALM,
    _IDX_HAPPY,
    _IDX_SAD,
    _IDX_SURPRISED,
    _LoraCandidatePolicy,
    _blend_tokens_per_char,
    _emo_vector_from_valence_arousal,
    _find_loud_reference_start,
    _find_stable_reference_candidate,
    _has_speakable_text,
    _indextts2_duration_control_mode,
    _indextts2_duration_min_sec,
    _indextts2_duration_max_tokens,
    _indextts2_duration_token_rate,
    _indextts2_duration_tokens,
    _lora_candidate_diag_from_meta,
    _resolve_num_beams,
    _split_long_tts_text,
    _timing_groups_from_meta,
    _timing_max_allowed_ms,
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


def test_emo_vector_sum_is_capped():
    """The projected vector must stay below IndexTTS2's 0.8 ceiling.
    We default lower so speaker prompt style remains dominant."""
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
        assert _close(s, 0.35), f"valence={v} arousal={a} sum={s:.4f}"


def test_emo_vector_high_valence_high_arousal_is_happy_surprised():
    """The 'high valence + high arousal' quadrant should put mass on
    happy/surprised and no mass on the negative emotions."""
    vec = _emo_vector_from_valence_arousal(0.9, 0.9)
    assert vec[_IDX_HAPPY] > 0.25, vec
    assert vec[_IDX_SURPRISED] > 0.0, vec
    assert vec[_IDX_SAD] == 0.0
    assert vec[_IDX_ANGRY] == 0.0


def test_emo_vector_low_valence_low_arousal_is_sad():
    """The 'low valence + low arousal' quadrant should put non-trivial
    mass on sadness + calm, no mass on happy/angry."""
    vec = _emo_vector_from_valence_arousal(-0.8, 0.1)
    assert vec[_IDX_SAD] > 0.15, vec
    assert vec[_IDX_HAPPY] == 0.0
    assert vec[_IDX_ANGRY] == 0.0


def test_emo_vector_low_valence_high_arousal_is_angry():
    """The 'low valence + high arousal' quadrant maps to anger /
    surprise / fear; no mass on the positive emotions."""
    vec = _emo_vector_from_valence_arousal(-0.8, 0.9)
    assert vec[_IDX_ANGRY] > 0.15, vec
    assert vec[_IDX_HAPPY] == 0.0
    assert vec[_IDX_CALM] == 0.0


def test_emo_vector_neutral_default():
    """The (0, 0) centre point should be predominantly neutral so a
    segment without strong affect cues does not get over-emoted."""
    vec = _emo_vector_from_valence_arousal(0.0, 0.0)
    assert vec[_IDX_CALM] > 0.25, vec


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
    indextts2_num_beams = 1
    indextts2_long_num_beams = 3
    indextts2_long_quality_min_sec = 30.0
    indextts2_duration_control_mode = "off"
    indextts2_duration_token_rate = 50.0
    indextts2_duration_token_rate_policy = "constant"
    indextts2_duration_piecewise_token_rates = "15:23.4375,23:30,default:32.5"
    indextts2_duration_control_min_sec = 15.0
    data_root = "/tmp"
    default_sample_rate = 24000
    default_channels = 1
    ffmpeg_bin = "ffmpeg"
    indextts2_neutral_dubbing_max_arousal = 0.65
    indextts2_neutral_calm_vector_enabled = False
    indextts2_dubbing_emo_vector_max_sum = 0.35


def _make_adapter(settings=None):
    from app.adapters.tts import TTSAdapter

    return TTSAdapter(settings or _StubSettings())  # type: ignore[arg-type]


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


def test_resolve_prosody_kwargs_ignores_emphasis_words_for_inline_runtime():
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
    assert "emphasis_words" not in kwargs
    assert "emo_vector" not in kwargs


def test_resolve_prosody_kwargs_neutralizes_calm_lecture_segments():
    """Neutral/calm lecture plans should not force an emotion vector."""
    a = _make_adapter()
    kwargs = a._resolve_prosody_kwargs(
        {
            "emotion": {"valence": 0.3, "arousal": 0.6, "label": "calm"},
            "pacing": "normal",
        },
        text="lecture narration",
    )
    assert kwargs["use_emo_text"] is False
    assert "emo_vector" not in kwargs


def test_resolve_prosody_kwargs_applies_calm_vector_when_enabled():
    settings = _StubSettings()
    settings.indextts2_neutral_calm_vector_enabled = True
    a = _make_adapter(settings)
    kwargs = a._resolve_prosody_kwargs(
        {
            "emotion": {"valence": 0.3, "arousal": 0.6, "label": "calm"},
            "pacing": "normal",
        },
        text="lecture narration",
    )
    assert kwargs["use_emo_text"] is False
    assert len(kwargs["emo_vector"]) == 8
    assert sum(kwargs["emo_vector"]) <= settings.indextts2_dubbing_emo_vector_max_sum + 1e-9


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
    assert "emo_vector" not in kwargs


def test_resolve_num_beams_keeps_short_segments_fast():
    assert _resolve_num_beams(
        _StubSettings(),
        target_sec=5.0,
        timing_status="fits",
    ) == 1


def test_resolve_num_beams_promotes_long_or_risky_segments():
    settings = _StubSettings()
    assert _resolve_num_beams(settings, target_sec=31.0, timing_status="fits") == 3
    assert _resolve_num_beams(settings, target_sec=5.0, timing_status="too_long") == 3


def test_duration_control_mode_defaults_off():
    settings = _StubSettings()
    assert _indextts2_duration_control_mode(settings) == "off"
    settings.indextts2_duration_control_mode = "community"
    assert _indextts2_duration_control_mode(settings) == "embedding"


def test_duration_control_token_budget_keeps_target_reachable():
    settings = _StubSettings()
    settings.indextts2_duration_token_rate = 50.0
    target_tokens = _indextts2_duration_tokens(settings, 7.5)
    assert target_tokens == 375
    max_tokens = _indextts2_duration_max_tokens(
        settings,
        current_max_mel_tokens=120,
        target_duration_tokens=target_tokens,
        max_allowed_sec=8.0,
    )
    assert max_tokens >= target_tokens
    assert max_tokens <= 400


def test_duration_control_piecewise_token_rate_matches_lora_pilot():
    settings = _StubSettings()
    settings.indextts2_duration_token_rate_policy = "piecewise"
    settings.indextts2_duration_piecewise_token_rates = "15:23.4375,23:30,default:32.5"

    assert _indextts2_duration_token_rate(settings, 8.28) == 23.4375
    assert _indextts2_duration_token_rate(settings, 20.46) == 30.0
    assert _indextts2_duration_token_rate(settings, 26.35) == 32.5
    assert _indextts2_duration_tokens(settings, 20.46) == 614


# ── tokens-per-char blend floor (2026-07-21 ep183 ratchet fix) ──────────
#
# The text token budget blends the observed rate from the previous attempt
# with the prior. Before the floor, one truncated observation (audio cut by
# the mel-token cap) fed a tiny observed rate back into the budget, capping
# the next attempt even harder and pinning output near zero — the ratchet
# that stalled ep183's short dense segments (16364 family).


def test_blend_tokens_per_char_returns_prior_without_observation():
    assert _blend_tokens_per_char(None, 13.5) == 13.5
    assert _blend_tokens_per_char(0.0, 13.5) == 13.5
    assert _blend_tokens_per_char(-2.0, 13.5) == 13.5


def test_blend_tokens_per_char_blends_normal_observation():
    # 0.6 * 10.0 + 0.4 * 13.5 = 11.4 — comfortably above the floor.
    assert math.isclose(_blend_tokens_per_char(10.0, 13.5), 11.4)


def test_blend_tokens_per_char_floor_stops_ratchet():
    # Poisoned observation (e.g. 46 chars truncated to 3.633s -> 3.95 t/s):
    # raw blend = 0.6*3.95 + 0.4*13.5 = 7.77; a still-worse observation of
    # 1.0 would give 6.0 without the floor. Floor binds at 0.5*13.5 = 6.75.
    poisoned = 1.0
    blended = _blend_tokens_per_char(poisoned, 13.5)
    assert math.isclose(blended, 6.75)
    # Budget for a 46-char text must stay >= 46 * 6.75 = 310 tokens, i.e.
    # the next attempt is never starved below half the prior budget.
    assert blended * 46 >= 46 * 0.5 * 13.5


def test_blend_tokens_per_char_respects_high_observation():
    # Fast speech should still tighten the budget (that is Scheme 2's
    # purpose); the floor only stops collapse, not adaptation.
    assert math.isclose(_blend_tokens_per_char(20.0, 13.5), 0.6 * 20.0 + 0.4 * 13.5)


def test_duration_control_generation_meta_overrides_global_default():
    settings = _StubSettings()
    meta = {
        "duration_control_mode": "embedding",
        "duration_control_min_sec": 0.05,
        "duration_token_rate_policy": "piecewise",
        "duration_token_rate": 23.4375,
        "duration_piecewise_token_rates": "15:23.4375,23:30,default:32.5",
    }

    assert _indextts2_duration_control_mode(settings) == "off"
    assert _indextts2_duration_control_mode(settings, meta) == "embedding"
    assert _indextts2_duration_min_sec(settings, meta) == 0.05
    assert _indextts2_duration_token_rate(settings, 11.0, meta) == 23.4375
    assert _indextts2_duration_token_rate(settings, 22.0, meta) == 30.0
    assert _indextts2_duration_tokens(settings, 11.0, meta) == 258


def test_duration_control_unknown_token_rate_policy_uses_constant():
    settings = _StubSettings()
    settings.indextts2_duration_token_rate = 42.0
    settings.indextts2_duration_token_rate_policy = "mystery"

    assert _indextts2_duration_token_rate(settings, 20.0) == 42.0


def test_duration_control_min_sec_defaults_to_long_segments():
    settings = _StubSettings()
    assert _indextts2_duration_min_sec(settings) == 15.0


def test_timing_groups_from_meta_round_trips_matching_text():
    meta = {
        "timing": {
            "max_allowed_ms": 5300,
            "groups": [
                {"text": "one.", "target_ms": 1200},
                {"text": "two?", "target_ms": 1800},
            ],
        }
    }
    assert _timing_groups_from_meta(meta, "one. two?") == [
        ("one.", 1200),
        ("two?", 1800),
    ]
    assert _timing_max_allowed_ms(meta) == 5300


def test_timing_groups_from_meta_rejects_stale_text():
    meta = {
        "timing": {
            "groups": [
                {"text": "old text.", "target_ms": 1200},
            ],
        }
    }
    assert _timing_groups_from_meta(meta, "new text.") is None


def test_timing_groups_from_meta_ignores_bad_groups():
    meta = {
        "timing": {
            "max_allowed_ms": "bad",
            "groups": [
                {"text": "", "target_ms": 1200},
                {"text": "valid.", "target_ms": "900"},
                {"text": "invalid.", "target_ms": -1},
            ],
        }
    }
    assert _timing_groups_from_meta(meta, "valid.") == [("valid.", 900)]
    assert _timing_max_allowed_ms(meta) == 0


def test_lora_candidate_diag_from_meta_is_shadow_only():
    meta = {
        "lora_candidate": {
            "schema_version": 2,
            "enabled": True,
            "phase": "main_flow_shadow",
            "execution_mode": "shadow_only",
            "policy_version": "main_flow_shadow_v1",
            "offline_baseline_id": "episode146_full145_episode_assembly_v7",
            "candidate_execution_enabled": False,
            "audio_selection_mode": "default_audio",
            "selected_family": "default",
            "fallback_reason": "candidate_loop_shadow_only_default_audio",
            "split_planning": True,
            "candidate_families": ["default", "split_light_vector", "split_mix"],
            "split_policy": {"join_quality_required": True},
            "gate": {"max_drift_ms": 500, "max_fit_delta": 0.05},
        }
    }
    policy = _LoraCandidatePolicy.from_meta(meta)
    diag = _lora_candidate_diag_from_meta(meta)

    assert policy.present is True
    assert policy.should_execute_candidates is False
    assert "lora_candidate=main_flow_shadow" in diag
    assert "mode=shadow_only" in diag
    assert "enabled=true" in diag
    assert "split=true" in diag
    assert "selected=default" in diag
    assert "families=3" in diag
    assert "schema=2" in diag
    assert "policy=main_flow_shadow_v1" in diag
    assert "baseline=episode146_full145_episode_assembly_v7" in diag
    assert "audio=default_audio" in diag
    assert "join_required=true" in diag
    assert "max_drift_ms=500" in diag
    assert "max_fit_delta=0.05" in diag
    assert "fallback=candidate_loop_shadow_only_default_audio" in diag


def test_lora_candidate_policy_requires_candidate_audio_execution():
    meta = {
        "lora_candidate": {
            "enabled": True,
            "phase": "main_flow_candidate",
            "execution_mode": "opt_in",
            "candidate_execution_enabled": True,
            "audio_selection_mode": "candidate_audio",
            "selected_family": "split_mix",
        }
    }

    assert _LoraCandidatePolicy.from_meta(meta).should_execute_candidates is True

    meta["lora_candidate"]["audio_selection_mode"] = "default_audio"
    assert _LoraCandidatePolicy.from_meta(meta).should_execute_candidates is False


def test_find_loud_reference_start_picks_high_energy_window(tmp_path):
    path = tmp_path / "reference.wav"
    sample_rate = 1000
    samples = array(
        "h",
        [0] * sample_rate * 2
        + [9000] * sample_rate * 2
        + [0] * sample_rate,
    )
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())

    assert _find_loud_reference_start(path, window_sec=1.0, hop_sec=1.0) == 2.0


def test_find_stable_reference_candidate_avoids_loudest_window(tmp_path):
    path = tmp_path / "reference.wav"
    sample_rate = 1000
    samples = array(
        "h",
        [1200] * sample_rate * 4
        + [20000] * sample_rate * 2
        + [1400] * sample_rate * 4,
    )
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(samples.tobytes())

    candidate = _find_stable_reference_candidate([path], window_sec=2.0, hop_sec=1.0)
    assert candidate["path"] == path
    assert candidate["start_sec"] != 4.0


def test_has_speakable_text_keeps_cjk_and_ascii_terms():
    assert _has_speakable_text("三号服务器复制日志。")
    assert _has_speakable_text("AppendEntries RPC")
    assert not _has_speakable_text("  。！？., ")


def test_split_long_tts_text_keeps_short_text_intact():
    text = "AppendEntries RPC stays in one short utterance."
    assert _split_long_tts_text(text) == [text]


def test_split_long_tts_text_uses_chinese_punctuation_boundaries():
    sentence = (
        "\u9886\u5bfc\u8005\u5148\u5411\u8ddf\u968f\u8005\u53d1\u9001"
        "\u8ffd\u52a0\u6761\u76ee\u8bf7\u6c42\uff0c"
        "\u7136\u540e\u6839\u636e\u54cd\u5e94\u9010\u6b65\u8c03\u6574"
        "\u4e0b\u4e00\u4e2a\u7d22\u5f15\u3002"
    )
    text = sentence * 4

    chunks = _split_long_tts_text(text)

    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(chunk.strip()) <= 110 for chunk in chunks)


def test_split_long_tts_text_splits_long_unpunctuated_text():
    text = "server" * 40

    chunks = _split_long_tts_text(text)

    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(chunk) <= 110 for chunk in chunks)


def test_release_indextts2_drops_cached_model():
    a = _make_adapter()
    a._indextts2_model = object()
    a._indextts2_warmup_status = "ready"

    assert a.release_indextts2() is True
    assert a._indextts2_model is None
    assert a.get_indextts2_warmup_status() == "idle"
