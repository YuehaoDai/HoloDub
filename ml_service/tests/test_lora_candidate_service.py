from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.indextts2_lora import ensure_lora_adapter
from app.adapters.tts import _LoraCandidatePolicy, write_silence_wav
from app.models import TTSRequest, TTSResponse
from app.services.lora_candidate import LoraCandidateService
from app.services.lora_candidate_quality import fit_audio_duration
from app.services.lora_candidate_split import SplitPlanOptions, build_split_plan, should_split
from app.storage import resolve_data_path


class _Settings:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.ffmpeg_bin = "ffmpeg"
        self.ffprobe_bin = "ffprobe"
        self.default_sample_rate = 24000
        self.default_channels = 1
        self.ml_tts_backend = "indextts2"
        self.indextts2_inline = True
        self.ml_lora_candidate_execution_enabled = False
        self.indextts2_lora_adapter_path = ""


class _FakeTTS:
    def __init__(self, settings: _Settings) -> None:
        self.settings = settings
        self.calls: list[TTSRequest] = []

    def synthesize(self, request: TTSRequest) -> TTSResponse:
        self.calls.append(request)
        path = resolve_data_path(self.settings.data_root, request.output_relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_silence_wav(
            path,
            sample_rate=self.settings.default_sample_rate,
            channels=self.settings.default_channels,
            duration_sec=max(request.target_duration_sec, 0.05),
        )
        return TTSResponse(
            audio_relpath=request.output_relpath,
            actual_duration_ms=int(request.target_duration_sec * 1000),
            diagnostics=["fake tts"],
        )


class _FakeASR:
    def transcribe_window(self, *_args, **_kwargs):
        return "default transcript", ["fake asr"]


def _candidate_request() -> TTSRequest:
    return TTSRequest(
        text="\u8fd9\u662f\u4e00\u6bb5\u8db3\u591f\u957f\u7684\u5019\u9009\u6d4b\u8bd5\u6587\u672c\uff0c\u7528\u6765\u89e6\u53d1 LoRA candidate\u3002",
        target_language="zh-CN",
        target_duration_sec=21.0,
        output_relpath="jobs/1/tts/vp0/seg.wav",
        dubbing_meta={
            "lora_candidate": {
                "enabled": True,
                "candidate_execution_enabled": True,
                "audio_selection_mode": "candidate_audio",
                "execution_mode": "candidate_fallback_v1",
                "candidate_families": ["default", "split_light_vector", "split_mix"],
                "split_planning": True,
            }
        },
    )


def _write_candidate_row(
    settings: _Settings,
    relpath: str,
    *,
    family: str = "default",
    gate_pass: bool = True,
    similarity: float = 0.992,
    abs_drift_ms: float = 12.0,
    fit_tempo_delta: float = 0.02,
    score: float = 1.0,
    join_quality: dict[str, Any] | None = None,
    split_strategy: str | None = None,
) -> dict[str, Any]:
    path = resolve_data_path(settings.data_root, relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_silence_wav(
        path,
        sample_rate=settings.default_sample_rate,
        channels=settings.default_channels,
        duration_sec=21.0,
    )
    row: dict[str, Any] = {
        "family": family,
        "audio_relpath": relpath,
        "duration_ms": 21000,
        "duration_sec": 21.0,
        "target_sec": 21.0,
        "drift_ms": abs_drift_ms,
        "abs_drift_ms": abs_drift_ms,
        "asr_text": "ok",
        "similarity": similarity,
        "critical_term_recall": 1.0,
        "latin_surface_recall": 1.0,
        "f0_range_semitones_10_90": 5.6,
        "fit_applied": True,
        "fit_tempo_delta": fit_tempo_delta,
        "join_quality": join_quality,
        "reject_reasons": [] if gate_pass else ["low_asr"],
        "risk_reasons": [],
        "gate_pass": gate_pass,
        "score": score,
    }
    if split_strategy:
        row["split_strategy"] = split_strategy
    return row


def _write_slow_complete_split_row(
    settings: _Settings,
    relpath: str,
    *,
    family: str,
    split_strategy: str,
    drift_ms: float = 3200.0,
    fit_tempo_delta: float = 0.098,
) -> dict[str, Any]:
    row = _write_candidate_row(
        settings,
        relpath,
        family=family,
        gate_pass=False,
        similarity=1.0,
        abs_drift_ms=abs(drift_ms),
        fit_tempo_delta=fit_tempo_delta,
        score=0.65,
        join_quality={"pass": True},
        split_strategy=split_strategy,
    )
    row.update(
        {
            "drift_ms": drift_ms,
            "critical_term_recall": 1.0,
            "reject_reasons": ["high_drift", "high_fit_delta"],
            "risk_reasons": [],
            "gate_pass": False,
        }
    )
    return row


def test_candidate_service_disabled_flag_falls_back_with_runtime(tmp_path: Path):
    settings = _Settings(tmp_path)
    tts = _FakeTTS(settings)
    service = LoraCandidateService(settings, tts, _FakeASR())  # type: ignore[arg-type]

    response = service.synthesize(_candidate_request())

    assert response.audio_relpath == "jobs/1/tts/vp0/seg.wav"
    assert response.candidate_result is not None
    assert response.candidate_result["fallback_reason"] == "ml_lora_candidate_execution_disabled"
    assert response.candidate_result["selected_family"] == "default"
    assert len(tts.calls) == 1


def test_candidate_service_missing_adapter_path_falls_back(tmp_path: Path):
    settings = _Settings(tmp_path)
    settings.ml_lora_candidate_execution_enabled = True
    tts = _FakeTTS(settings)
    service = LoraCandidateService(settings, tts, _FakeASR())  # type: ignore[arg-type]

    response = service.synthesize(_candidate_request())

    assert response.candidate_result is not None
    assert response.candidate_result["fallback_reason"] == "indextts2_lora_adapter_path_missing"
    assert len(tts.calls) == 1


def test_candidate_service_missing_adapter_file_falls_back_before_model_load(tmp_path: Path):
    settings = _Settings(tmp_path)
    settings.ml_lora_candidate_execution_enabled = True
    settings.indextts2_lora_adapter_path = "missing-adapter.pt"
    tts = _FakeTTS(settings)
    service = LoraCandidateService(settings, tts, _FakeASR())  # type: ignore[arg-type]

    response = service.synthesize(_candidate_request())

    assert response.candidate_result is not None
    assert response.candidate_result["fallback_reason"] == "indextts2_lora_adapter_not_found"
    assert len(tts.calls) == 1


def test_adapter_path_accepts_data_root_relative_and_repo_relative(tmp_path: Path):
    settings = _Settings(tmp_path / "data")
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]

    settings.indextts2_lora_adapter_path = "training/run/adapter.pt"
    assert service._resolve_adapter_path() == tmp_path / "data" / "training" / "run" / "adapter.pt"

    settings.indextts2_lora_adapter_path = "data/training/run/adapter.pt"
    assert service._resolve_adapter_path() == tmp_path / "data" / "training" / "run" / "adapter.pt"


def test_split_plan_uses_production_v1_limits():
    text = (
        "\u7b2c\u4e00\u53e5\u8bf4\u660e\u65e5\u5fd7\u590d\u5236\u3002" * 18
        + "\u7b2c\u4e8c\u53e5\u8bf4\u660e\u8ffd\u52a0\u6761\u76ee\u3002" * 18
    )
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    plan = build_split_plan(text, 38.0, opts)

    assert should_split(text, 38.0, opts)
    assert plan["pause_sec"] == 0.18
    assert 2 <= plan["part_count"] <= 4
    assert all(part["target_sec"] <= 16.0 + 0.001 for part in plan["parts"])


def test_should_split_can_use_density_trigger_below_legacy_threshold():
    text = "\u5b57" * 65
    legacy_opts = SplitPlanOptions(
        min_target_sec=18.0,
        min_text_chars=180,
        max_parts=4,
        max_part_sec=16.0,
        pause_sec=0.18,
    )
    density_opts = SplitPlanOptions(
        min_target_sec=18.0,
        min_text_chars=180,
        min_chars_per_sec_to_split=4.9,
        max_parts=4,
        max_part_sec=16.0,
        pause_sec=0.18,
    )

    assert not should_split(text, 12.0, legacy_opts)
    assert should_split(text, 12.0, density_opts)
    assert build_split_plan(text, 12.0, density_opts)["min_chars_per_sec_to_split"] == 4.9


def test_split_plan_flags_parts_above_safe_speech_rate():
    text = "\u8fd9\u662f\u4e00\u4e2a\u5fc5\u987b\u88ab\u538b\u7f29\u7684\u9ad8\u5bc6\u5ea6\u6d4b\u8bd5\u7247\u6bb5\uff0c" * 8
    plan = build_split_plan(
        text,
        12.0,
        SplitPlanOptions(max_parts=4, max_part_sec=16.0, max_part_chars_per_sec=5.6),
    )

    assert plan["needs_text_compression"] is True
    assert plan["speech_rate_risk_parts"]
    assert plan["max_required_chars_per_sec"] > plan["max_part_chars_per_sec"]
    assert any(part["speech_rate_risk"] for part in plan["parts"])


def test_split_plan_preserves_full_text_when_max_parts_forces_tail_merge():
    text = bytes.fromhex(
        "e982a3e4b988e68ea5e4b88be69da5e4bda0e5b0b1e58fafe4bba5e98787e794a8e69f90e7a78de689b9e9878fe58c96e79a84e5a484e79086e7ad96e795a5e38082e585b7e4bd93e69da5e8afb4efbc8ce4bda0e58fafe4bba5e8bf99e6a0b7e69da5e88083e89991efbc9ae5ae9ee99985e4b88ae68891e4bbace5b9b6e4b88de99c80e8a681e6af8fe6aca1e78ab6e68081e58f98e69bb4e983bde7ab8be58db3e8bf9be8a18ce68c81e4b985e58c96efbc8ce8808ce698afe58fafe4bba5e7ad89e588b0e58db3e5b086e59b9ee5a48de69f90e4b8aa525043e8afb7e6b182efbc8ce68896e88085e58db3e5b086e4b8bbe58aa8e58f91e98081e69f90e4b8aa525043e6b688e681afe79a84e697b6e58099efbc8ce5868de5b086e7b4afe7a7afe79a84e78ab6e68081e58f98e69bb4e4b880e5b9b6e68c81e4b985e58c96e588b0e7a381e79b98e4b88ae38082e8bf99e7a78de689b9e9878fe5a484e79086e79a84e696b9e5bc8fe68896e8aeb8e883bde5a49fe8aea9e4bda0e981bfe5858de79bb8e5bd93e4b880e983a8e58886e4b88de5bf85e8a681e79a84e68c81e4b985e58c96e6938de4bd9cefbc8ce4bb8ee8808ce68f90e58d87e695b4e4bd93e680a7e883bde38082e8808ce8bf99e4b880e782b9e4b98be68980e4bba5e887b3e585b3e9878de8a681efbc8ce6a0b9e69cace58e9fe59ba0e59ca8e4ba8ee59091e7a381e79b98e58699e585a5e695b0e68daee8bf99e4bbb6e4ba8be69cace8baabe58fafe883bde4bc9ae99d9ee5b8b8e88097e697b6efbc8ce4bba3e4bbb7e9ab98e69882e38082e58187e8aebee68891e4bbace78eb0e59ca8e8aea8e8aebae79a84e698afe4bca0e7bb9fe79a84e69cbae6a2b0e7a1ace79b98efbc8ce982a3e4b988e5bd93e4bda0e99c80e8a681e68c81e4b985e58c96e695b0e68daee38081e4b99fe5b0b1e698afe59091e7a381e79b98e69687e4bbb6e58699e585a5e58685e5aeb9e79a84e697b6e58099efbc8ce6af8fe4b880e6aca1e58699e6938de4bd9ce5a4a7e7baa6e4bc9ae88097e8b4b9e4bda0e695b4e695b43130e6afabe7a792e79a84e697b6e997b4e38082e8bf99e698afe59ba0e4b8bae69cbae6a2b0e7a1ace79b98e79a84e5b7a5e4bd9ce58e9fe79086e586b3e5ae9ae4ba86e4bda0e5bf85e9a1bbe7ad89e5be85e79b98e78987e6978be8bdacefbc8ce79bb4e588b0e79baee6a087e58699e585a5e4bd8de7bdaee5889ae5a5bde6978be8bdace588b0e7a381e5a4b4e4b88be696b9e4b8bae6ada2e28094e28094e8a681e79fa5e98193efbc8ce8bf99e7a78de69cbae6a2b0e7a1ace79b98e79a84e8bdace9809fe9809ae5b8b8e6848fe591b3e79d80e79b98e78987e5a4a7e7baa6e6af8f3130e6afabe7a792e7949ae887b3e69bb4e4b985e6898de883bde5ae8ce68890e4b880e6aca1e5ae8ce695b4e79a84e6978be8bdace38082e8808ce68385e586b5e8bf98e58fafe883bde69bb4e58aa0e7b39fe7b395efbc9ae5a682e69e9ce7a381e5a4b4e5bd93e5898de5b9b6e4b88de59ca8e6ada3e7a1aee79a84e7a381e98193e4b88aefbc8ce4bda0e7949ae887b3e8bf98e99c80e8a681e689a7e8a18ce4b880e6aca1e5afbbe98193e6938de4bd9cefbc8ce4b99fe5b0b1e698afe7a7bbe58aa8e7a381e5a4b4e88782e588b0e5afb9e5ba94e79a84e7a381e98193e4bd8de7bdaeefbc8ce8bf99e58f88e4bc9ae5b8a6e69da5e9a29de5a496e79a84e5bbb6e8bf9fe5bc80e99480e38082"
    ).decode("utf-8")

    plan = build_split_plan(
        text,
        44.9,
        SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18),
    )
    joined = "".join(part["text"] for part in plan["parts"])

    assert plan["part_count"] == 4
    assert joined == text
    assert "\u5bfb\u9053\u64cd\u4f5c" in joined
    assert joined.endswith("\u8fd9\u53c8\u4f1a\u5e26\u6765\u989d\u5916\u7684\u5ef6\u8fdf\u5f00\u9500\u3002")


def test_short_split_plan_preserves_critical_term_boundary():
    text = (
        "\u8fd9\u662f\u5176\u4e2d\u4e00\u79cd\u60c5\u51b5\uff0c"
        "\u9700\u8981\u56de\u6eda\u6574\u4e2a\u4efb\u671f\u7684\u65e5\u5fd7\u6761\u76ee\uff0c"
        "\u56e0\u4e3a\u8be5\u4efb\u671f\u5728\u9886\u5bfc\u8005\u5904\u5b8c\u5168\u7f3a\u5931\u3002"
    )

    plan = build_split_plan(
        text,
        6.9,
        SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18),
    )
    texts = [part["text"] for part in plan["parts"]]

    assert len(texts) == 2
    assert "\u65e5|\u5fd7" not in "|".join(texts)
    assert any("\u65e5\u5fd7\u6761\u76ee" in part for part in texts)
    assert texts[1].startswith("\u56e0\u4e3a\u8be5\u4efb\u671f")


def test_dense_mid_policy_forces_split_planning_below_legacy_threshold(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "\u5b57" * 73
    request.target_duration_sec = 12.58
    request.dubbing_meta["lora_candidate"]["eligibility"] = {
        "duration_eligible": False,
        "text_eligible": False,
        "dense_eligible": True,
        "reasons": ["dense_mid_segment"],
    }
    policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    assert not should_split(request.text, request.target_duration_sec, opts)
    assert service._should_split_candidate(request, policy, opts)


def test_near_long_policy_forces_split_planning_below_legacy_threshold(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "x" * 76
    request.target_duration_sec = 17.14
    request.dubbing_meta["lora_candidate"]["eligibility"] = {
        "duration_eligible": False,
        "text_eligible": False,
        "dense_eligible": False,
        "near_long_eligible": True,
        "reasons": ["near_long_dense"],
    }
    policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    assert policy.near_long_eligible is True
    assert "near_long=true" in policy.diagnostic()
    assert not should_split(request.text, request.target_duration_sec, opts)
    assert service._should_split_candidate(request, policy, opts)


def test_short_transition_policy_forces_split_planning_below_legacy_threshold(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "关于领导者选举限制这一关键机制，大家还有什么疑问吗？好的，我们就继续讲解。"
    request.target_duration_sec = 6.5
    request.dubbing_meta["lora_candidate"]["eligibility"] = {
        "duration_eligible": False,
        "text_eligible": False,
        "dense_eligible": False,
        "near_long_eligible": False,
        "short_transition_eligible": True,
        "reasons": ["short_transition_question"],
    }
    policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    assert policy.short_transition_eligible is True
    assert "short_transition=true" in policy.diagnostic()
    assert not should_split(request.text, request.target_duration_sec, opts)
    assert service._should_split_candidate(request, policy, opts)


def test_short_dense_policy_forces_split_planning_below_legacy_threshold(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "它收到客户端请求后，立即发送首个Append Entries RPC，对吧？然后呢，哦，"
    request.target_duration_sec = 7.1
    request.dubbing_meta["lora_candidate"]["eligibility"] = {
        "duration_eligible": False,
        "text_eligible": False,
        "dense_eligible": False,
        "near_long_eligible": False,
        "short_dense_eligible": True,
        "short_transition_eligible": False,
        "reasons": ["short_dense_segment"],
    }
    policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    assert policy.short_dense_eligible is True
    assert "short_dense=true" in policy.diagnostic()
    assert not should_split(request.text, request.target_duration_sec, opts)
    assert service._should_split_candidate(request, policy, opts)


def test_retry_escalation_policy_forces_split_planning_below_legacy_threshold(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "\u7136\u540e\u8fd8\u6709\u4e00\u4e2a\u5e76\u53d1\u8bf7\u6c42\uff0c\u5b83\u53ef\u80fd\u7a0d\u665a\u4e00\u70b9\u624d\u53d1\u51fa\uff0c\u8981\u6c42\u628a\u53d8\u91cfx\u7684\u503c\u8bbe\u7f6e\u4e3a2\u3002"
    request.target_duration_sec = 7.34
    request.dubbing_meta["lora_candidate"]["eligibility"] = {
        "duration_eligible": False,
        "text_eligible": False,
        "dense_eligible": False,
        "near_long_eligible": False,
        "short_dense_eligible": False,
        "short_transition_eligible": False,
        "short_term_number_eligible": False,
        "retry_escalation_eligible": True,
        "reasons": ["retry_escalation_after_default_nonconvergence"],
    }
    policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    assert policy.retry_escalation_eligible is True
    assert "retry_escalation=true" in policy.diagnostic()
    assert not should_split(request.text, request.target_duration_sec, opts)
    assert service._should_split_candidate(request, policy, opts)


def test_short_term_number_policy_forces_split_planning_below_legacy_threshold(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "x" * 34 + "\u4e94 \u53f7\u4efb\u671f"
    request.target_duration_sec = 7.52
    request.dubbing_meta["lora_candidate"]["eligibility"] = {
        "duration_eligible": False,
        "text_eligible": False,
        "dense_eligible": False,
        "near_long_eligible": False,
        "short_dense_eligible": False,
        "short_transition_eligible": False,
        "short_term_number_eligible": True,
        "reasons": ["short_term_number_anchor"],
    }
    policy = _LoraCandidatePolicy.from_meta(request.dubbing_meta)
    opts = SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18)

    assert policy.short_term_number_eligible is True
    assert "short_term_number=true" in policy.diagnostic()
    assert not should_split(request.text, request.target_duration_sec, opts)
    assert service._should_split_candidate(request, policy, opts)


def test_balanced_four_part_rescue_is_distinct_and_tighter_than_primary(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    text = (
        "\u7b2c\u4e00\u53e5\u8bf4\u660e\u65e5\u5fd7\u590d\u5236\u4e0e\u4efb\u671f\u5224\u65ad\u3002" * 8
        + "\u7b2c\u4e8c\u53e5\u8bf4\u660e\u8ffd\u52a0\u6761\u76ee\u548c\u51b2\u7a81\u68c0\u67e5\u3002" * 8
        + "\u7b2c\u4e09\u53e5\u8bf4\u660e\u9886\u5bfc\u8005\u56de\u9000\u7d22\u5f15\u3002" * 8
        + "\u7b2c\u56db\u53e5\u8bf4\u660e\u8ddf\u968f\u8005\u6062\u590d\u6b63\u786e\u72b6\u6001\u3002" * 8
    )
    primary = build_split_plan(
        text,
        40.68,
        SplitPlanOptions(max_parts=4, max_part_sec=16.0, pause_sec=0.18),
    )
    rescue = build_split_plan(
        text,
        40.68,
        SplitPlanOptions(max_parts=4, max_part_sec=10.5, pause_sec=0.18),
    )
    rescue["strategy"] = "balanced_four_part"

    assert primary["part_count"] == 3
    assert rescue["part_count"] == 4
    assert service._is_distinct_split_plan(primary, rescue)
    assert max(part["target_sec"] for part in rescue["parts"]) <= 10.5 + 1.1


def test_runtime_split_plan_uses_selected_rescue_strategy(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    primary = {
        "strategy": "automatic_clause_duration_plan",
        "parts": [{"index": 1, "text": "primary"}],
    }
    balanced = {
        "strategy": "balanced_four_part",
        "parts": [{"index": 1, "text": "balanced"}],
    }
    semantic = {
        "strategy": "semantic_two_part",
        "parts": [{"index": 1, "text": "semantic"}],
    }

    selected = {"family": "split_mix", "split_strategy": "balanced_four_part"}

    assert service._runtime_split_plan(primary, [balanced, semantic], selected) is balanced


def test_runtime_split_plan_keeps_primary_for_automatic_strategy(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    primary = {
        "strategy": "automatic_clause_duration_plan",
        "parts": [{"index": 1, "text": "primary"}],
    }
    balanced = {
        "strategy": "balanced_four_part",
        "parts": [{"index": 1, "text": "balanced"}],
    }

    selected = {"family": "split_mix", "split_strategy": "automatic"}

    assert service._runtime_split_plan(primary, [balanced], selected) is primary


def test_pause_rescues_are_distinct_even_with_same_text_parts(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    text = "\u7b2c\u4e00\u53e5\u8bf4\u660e\u65e5\u5fd7\u3002" * 12 + "\u7b2c\u4e8c\u53e5\u8bf4\u660e\u8ffd\u52a0\u6761\u76ee\u3002" * 12

    normal_pause = build_split_plan(
        text,
        27.86,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
    )
    tight_pause = build_split_plan(
        text,
        27.86,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.04),
    )
    tight_pause["strategy"] = "micro_clause_tight_pause"
    zero_pause = build_split_plan(
        text,
        27.86,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.0),
    )
    zero_pause["strategy"] = "micro_clause_zero_pause"

    assert [part["text"] for part in normal_pause["parts"]] == [part["text"] for part in tight_pause["parts"]]
    assert [part["text"] for part in normal_pause["parts"]] == [part["text"] for part in zero_pause["parts"]]
    assert normal_pause["pause_sec"] == 0.18
    assert tight_pause["pause_sec"] == 0.04
    assert zero_pause["pause_sec"] == 0.0
    assert service._is_distinct_split_plan(normal_pause, tight_pause)
    assert service._is_distinct_split_plan(tight_pause, zero_pause)


def test_concat_pause_rescue_preserves_part_targets_but_changes_assembly_pause(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    text = "\u7b2c\u4e00\u53e5\u8bf4\u660e\u65e5\u5fd7\u590d\u5236\u3002" * 12 + "\u7b2c\u4e8c\u53e5\u8bf4\u660e\u4e0b\u4e00\u4e2a\u7d22\u5f15\u3002" * 12

    normal = build_split_plan(
        text,
        27.86,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
    )
    concat_tight = build_split_plan(
        text,
        27.86,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
    )
    concat_tight["strategy"] = "micro_clause_tight_concat"
    concat_tight["planning_pause_sec"] = concat_tight["pause_sec"]
    concat_tight["pause_sec"] = 0.04

    assert [part["target_sec"] for part in normal["parts"]] == [part["target_sec"] for part in concat_tight["parts"]]
    assert concat_tight["planning_pause_sec"] == 0.18
    assert concat_tight["pause_sec"] == 0.04
    assert service._is_distinct_split_plan(normal, concat_tight)

    tight_pause = build_split_plan(
        text,
        27.86,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.04),
    )
    tight_pause["strategy"] = "micro_clause_tight_pause"

    assert [part["text"] for part in tight_pause["parts"]] == [part["text"] for part in concat_tight["parts"]]
    assert tight_pause["pause_sec"] == concat_tight["pause_sec"]
    assert [part["target_sec"] for part in tight_pause["parts"]] != [part["target_sec"] for part in concat_tight["parts"]]
    assert service._is_distinct_split_plan(tight_pause, concat_tight)


def test_concat_rescues_are_prioritized_before_pause_rescues(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]

    names = [name for name, _opts, _concat_pause in service._rescue_specs()]

    assert names.index("micro_clause_tight_concat") < names.index("micro_clause_tight_pause")
    assert names.index("micro_clause_zero_concat") < names.index("micro_clause_zero_pause")


def test_micro_clause_six_part_rescue_tightens_known_raft_log_case():
    text = bytes.fromhex(
        "e5b9b6e4b894e5898de4b880e4b8aae697a5e5bf97e4bbbbe69c9fe7ad89e4ba8e33e38082e8bf99e6aca1e696b0e79a"
        "84e8bfbde58aa0e69da1e79bae525043e68980e690bae5b8a6e79a84e5898de4b880e4b8aae697a5e5bf97e7b4a2e5bc"
        "95e580bce69c89e68980e4b88de5908ce38082e4bd86e585b3e994aee59ca8e4ba8eefbc8ce69c8de58aa1e599a8e5ae"
        "9ee99985e58f91e98081e79a84e6ada3e698afe697a5e5bf97e69da1e79baee4b8ade79a84e585b7e4bd93e58685e5ae"
        "b9e38082e69cace6aca1525043e6b688e681afe5b086e5ae8ce695b4e58c85e590abe4bb8ee696b0e68c87e5ae9ae79a"
        "84e5898de4b880e697a5e5bf97e7b4a2e5bc95e4bd8de7bdaee4b98be5908ee79a84e68980e69c89e69da1e79baee380"
        "82e59ba0e6ada4efbc8ce5afb9e4ba8ee69c8de58aa1e599a85332e8808ce8a880efbc8ce5bd93e5898de79a84e5898d"
        "e4b880e4b8aae697a5e5bf97e7b4a2e5bc95e698af3131efbc9be5ae83e4bb94e7bb86e6a380e69fa5e8afa5e4bd8de7"
        "bdaee5a484e79a84e697a5e5bf97e69da1e79baee5908ee58f91e78eb0efbc8ce4bbbbe69c9fe7bc96e58fb7e698af33"
        "efbc8ce4b88ee9a286e5afbce88085e68980e58f91e98081e79a84e4bbbbe69c9fe4bfa1e681afe5ae8ce585a8e4b880"
        "e887b4e38082e4ba8ee698afefbc8c5332e5ae9ee99985e4b88ae5b086e4bc9ae6ada3e5bc8fe68ea5e58f97e8bf99e6"
        "aca1e8bfbde58aa0e69da1e79bae525043e38082e8808ce6a0b9e68daee59bbee4ba8ce68980e8a784e5ae9ae79a84e4"
        "b8a5e6a0bce4b880e887b4e680a7e8a784e58899efbc8ce4b880e697a6e8b79fe99a8fe88085e68ea5e58f97e4ba86e8"
        "bfbde58aa0e69da1e79baeefbc8ce5b0b1e5bf85e9a1bbe7ab8be58db3e588a0e999a4e69cace59cb0e697a5e5bf97e4"
        "b8ade4bb8ee8afa5e8bfbde58aa0e4bd8de7bdaee5bc80e5a78be4b98be5908ee79a84e68980e69c89e697a2e69c89e5"
        "8685e5aeb9e38082"
    ).decode("utf-8")

    plan = build_split_plan(
        text,
        42.7,
        SplitPlanOptions(max_parts=6, max_part_sec=8.5, pause_sec=0.18),
    )

    assert plan["part_count"] == 6
    assert max(part["target_sec"] for part in plan["parts"]) < 10.0
    assert any("S2" in part["text"] for part in plan["parts"])


def test_duration_fit_rejects_over_eight_percent_without_rescue(tmp_path: Path):
    settings = _Settings(tmp_path)
    path = tmp_path / "candidate.wav"
    write_silence_wav(path, sample_rate=24000, channels=1, duration_sec=2.0)

    result = fit_audio_duration(settings, path, 1.5, max_delta=0.08)

    assert result["fit_applied"] is False
    assert result["fit_reject_reason"] == "high_fit_delta"
    assert result["fit_tempo_delta"] > 0.08


def test_high_fit_delta_is_hard_reject_not_soft_risk(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]

    row = {
        "similarity": 1.0,
        "abs_drift_ms": 12.0,
        "fit_tempo_delta": 0.09,
        "critical_term_recall": 1.0,
        "join_quality": {"pass": True},
    }

    assert service._reject_reasons(row) == ["high_fit_delta"]
    assert "high_fit_delta" not in service._risk_reasons(row)


def test_complete_but_too_slow_split_candidates_request_text_compression(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]

    candidates = [
        _write_slow_complete_split_row(
            settings,
            "jobs/1/tts/vp0/c1.wav",
            family="split_light_vector",
            split_strategy="automatic",
            fit_tempo_delta=0.118,
        ),
        _write_slow_complete_split_row(
            settings,
            "jobs/1/tts/vp0/c2.wav",
            family="split_mix",
            split_strategy="automatic",
            fit_tempo_delta=0.098,
        ),
        _write_slow_complete_split_row(
            settings,
            "jobs/1/tts/vp0/c3.wav",
            family="split_light_vector",
            split_strategy="balanced_four_part",
            fit_tempo_delta=0.115,
        ),
        _write_slow_complete_split_row(
            settings,
            "jobs/1/tts/vp0/c4.wav",
            family="split_mix",
            split_strategy="balanced_four_part",
            fit_tempo_delta=0.111,
        ),
    ]

    recommendation = service._text_compression_recommendation(candidates)

    assert recommendation is not None
    assert recommendation["needed"] is True
    assert recommendation["reason"] == "complete_high_similarity_candidates_remain_too_slow"
    assert recommendation["max_allowed_fit_delta"] == 0.05
    assert recommendation["action"] == "shorten_or_rephrase_target_text_before_next_candidate_attempt"


def test_candidate_loop_skips_rescue_strategies_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    default_relpath = service._candidate_relpath(request.output_relpath, "default")
    calls: list[tuple[str, str]] = []

    def synthesize_default(*_args, **_kwargs):
        return _write_candidate_row(
            settings,
            default_relpath,
            family="default",
            gate_pass=False,
            similarity=0.88,
            abs_drift_ms=9000.0,
            fit_tempo_delta=0.28,
            score=0.1,
        )

    def synthesize_light(_request, _plan, _controller, _cache, *, split_strategy="automatic"):
        calls.append(("split_light_vector", split_strategy))
        relpath = service._candidate_relpath(
            request.output_relpath,
            service._split_output_name("split_light_vector", split_strategy),
        )
        return _write_slow_complete_split_row(
            settings,
            relpath,
            family="split_light_vector",
            split_strategy=split_strategy,
        )

    def synthesize_mix(_request, _plan, _controller, _cache, *, split_strategy="automatic"):
        calls.append(("split_mix", split_strategy))
        relpath = service._candidate_relpath(
            request.output_relpath,
            service._split_output_name("split_mix", split_strategy),
        )
        return _write_slow_complete_split_row(
            settings,
            relpath,
            family="split_mix",
            split_strategy=split_strategy,
            fit_tempo_delta=0.099,
        )

    monkeypatch.setattr(service, "_synthesize_single_candidate", synthesize_default)
    monkeypatch.setattr(service, "_synthesize_split_light_vector", synthesize_light)
    monkeypatch.setattr(service, "_synthesize_split_mix", synthesize_mix)

    policy = SimpleNamespace(
        candidate_families=["default", "split_light_vector", "split_mix"],
        split_planning=True,
        execution_mode="candidate_fallback_v1",
        split_policy={},
    )
    response = service._run_candidates(request, policy, SimpleNamespace())  # type: ignore[arg-type]

    assert response.candidate_result is not None
    assert response.candidate_result["fallback_reason"] == "all_candidates_rejected"
    assert response.candidate_result["rescue_strategies_enabled"] is False
    assert response.candidate_result["split_plan_alternates"] == []
    assert calls == [
        ("split_light_vector", "automatic"),
        ("split_mix", "automatic"),
    ]


def test_candidate_loop_stops_opt_in_rescue_after_repeated_complete_too_slow_splits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    default_relpath = service._candidate_relpath(request.output_relpath, "default")
    calls: list[tuple[str, str]] = []

    def synthesize_default(*_args, **_kwargs):
        return _write_candidate_row(
            settings,
            default_relpath,
            family="default",
            gate_pass=False,
            similarity=0.88,
            abs_drift_ms=9000.0,
            fit_tempo_delta=0.28,
            score=0.1,
        )

    def synthesize_light(_request, _plan, _controller, _cache, *, split_strategy="automatic"):
        calls.append(("split_light_vector", split_strategy))
        relpath = service._candidate_relpath(
            request.output_relpath,
            service._split_output_name("split_light_vector", split_strategy),
        )
        return _write_slow_complete_split_row(
            settings,
            relpath,
            family="split_light_vector",
            split_strategy=split_strategy,
        )

    def synthesize_mix(_request, _plan, _controller, _cache, *, split_strategy="automatic"):
        calls.append(("split_mix", split_strategy))
        relpath = service._candidate_relpath(
            request.output_relpath,
            service._split_output_name("split_mix", split_strategy),
        )
        return _write_slow_complete_split_row(
            settings,
            relpath,
            family="split_mix",
            split_strategy=split_strategy,
            fit_tempo_delta=0.099,
        )

    monkeypatch.setattr(service, "_synthesize_single_candidate", synthesize_default)
    monkeypatch.setattr(service, "_synthesize_split_light_vector", synthesize_light)
    monkeypatch.setattr(service, "_synthesize_split_mix", synthesize_mix)

    policy = SimpleNamespace(
        candidate_families=["default", "split_light_vector", "split_mix"],
        split_planning=True,
        execution_mode="candidate_fallback_v1",
        split_policy={"enable_rescue_strategies": True},
    )
    response = service._run_candidates(request, policy, SimpleNamespace())  # type: ignore[arg-type]

    assert response.candidate_result is not None
    assert response.candidate_result["fallback_reason"] == "all_candidates_rejected"
    assert response.candidate_result["candidate_generation_stop_reason"] == "needs_text_compression_high_fit_delta"
    assert response.candidate_result["rescue_strategies_enabled"] is True
    assert response.candidate_result["text_compression_recommendation"]["needed"] is True
    assert response.candidate_result["gate_decision"]["needs_text_compression"] is True
    assert calls[0:2] == [
        ("split_light_vector", "automatic"),
        ("split_mix", "automatic"),
    ]
    assert len(calls) == 4
    rescue_strategies = {strategy for _family, strategy in calls[2:]}
    assert len(rescue_strategies) == 1
    assert "automatic" not in rescue_strategies
    assert all(strategy not in {"micro_clause_tight_concat", "micro_clause_zero_pause", "semantic_two_part"} for _family, strategy in calls)


def test_speech_rate_risk_stops_split_generation_and_requests_text_compression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    request.text = "\u8fd9\u662f\u4e00\u4e2a\u5fc5\u987b\u88ab\u538b\u7f29\u7684\u9ad8\u5bc6\u5ea6\u6d4b\u8bd5\u7247\u6bb5\uff0c" * 12
    request.target_duration_sec = 12.0
    calls: list[str] = []

    def synthesize_default(*_args, **_kwargs):
        calls.append("default")
        relpath = service._candidate_relpath(request.output_relpath, "default")
        return _write_candidate_row(
            settings,
            relpath,
            family="default",
            gate_pass=False,
            similarity=0.99,
            abs_drift_ms=1200.0,
        )

    def synthesize_split(*_args, **_kwargs):
        calls.append("split")
        raise AssertionError("split synthesis should be blocked by speech-rate risk")

    monkeypatch.setattr(service, "_synthesize_single_candidate", synthesize_default)
    monkeypatch.setattr(service, "_synthesize_split_light_vector", synthesize_split)
    monkeypatch.setattr(service, "_synthesize_split_mix", synthesize_split)

    policy = SimpleNamespace(
        candidate_families=["default", "split_light_vector", "split_mix"],
        split_planning=True,
        execution_mode="candidate_fallback_v1",
        split_policy={},
    )
    response = service._run_candidates(request, policy, SimpleNamespace())  # type: ignore[arg-type]

    assert calls == ["default"]
    assert response.candidate_result is not None
    assert response.candidate_result["fallback_reason"] == "all_candidates_rejected"
    assert response.candidate_result["candidate_generation_stop_reason"] == "needs_text_compression_speech_rate"
    assert response.candidate_result["gate_decision"]["needs_text_compression"] is True
    recommendation = response.candidate_result["text_compression_recommendation"]
    assert recommendation["reason"] == "split_part_required_speech_rate_above_safe_limit"
    assert recommendation["risk_part_indexes"]


def test_quiet_zero_pause_join_quality_rescue_allows_gate_without_strong_stop(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    row = {
        "similarity": 1.0,
        "abs_drift_ms": 18.0,
        "fit_tempo_delta": 0.043,
        "critical_term_recall": 1.0,
        "latin_surface_recall": 1.0,
        "f0_range_semitones_10_90": 5.6,
        "join_quality": {
            "pass": False,
            "reasons": ["large_edge_rms_jump"],
            "max_edge_rms_jump_db": 18.192,
            "joins": [
                {
                    "index": 1,
                    "pause_sec": 0.0,
                    "prev_tail_dbfs": -46.63,
                    "next_head_dbfs": -64.822,
                    "edge_rms_jump_db": 18.192,
                },
                {
                    "index": 2,
                    "pause_sec": 0.0,
                    "prev_tail_dbfs": -53.785,
                    "next_head_dbfs": -62.358,
                    "edge_rms_jump_db": 8.573,
                },
            ],
        },
    }

    assert service._reject_reasons(row) == []
    assert row["join_quality_rescue"]["rule"] == "quiet_zero_pause_large_edge_rms_jump"
    assert "join_quality_rescue" in service._risk_reasons(row)

    row["gate_pass"] = True
    assert not service._is_strong_gate_pass(row)


def test_quiet_zero_pause_join_quality_rescue_rejects_hot_boundary(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    row = {
        "similarity": 1.0,
        "abs_drift_ms": 10.0,
        "fit_tempo_delta": 0.02,
        "critical_term_recall": 1.0,
        "join_quality": {
            "pass": False,
            "reasons": ["large_edge_rms_jump"],
            "max_edge_rms_jump_db": 18.0,
            "joins": [
                {
                    "index": 1,
                    "pause_sec": 0.0,
                    "prev_tail_dbfs": -29.24,
                    "next_head_dbfs": -61.302,
                    "edge_rms_jump_db": 18.0,
                }
            ],
        },
    }

    assert service._join_quality_rescue(row) is None
    assert service._reject_reasons(row) == ["bad_join_quality"]


def test_quiet_zero_pause_join_quality_rescue_rejects_below_rescue_asr(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    row = {
        "similarity": 0.994,
        "abs_drift_ms": 10.0,
        "fit_tempo_delta": 0.02,
        "critical_term_recall": 1.0,
        "join_quality": {
            "pass": False,
            "reasons": ["large_edge_rms_jump"],
            "max_edge_rms_jump_db": 18.0,
            "joins": [
                {
                    "index": 1,
                    "pause_sec": 0.0,
                    "prev_tail_dbfs": -46.0,
                    "next_head_dbfs": -61.0,
                    "edge_rms_jump_db": 18.0,
                }
            ],
        },
    }

    assert service._join_quality_rescue(row) is None
    assert service._reject_reasons(row) == ["bad_join_quality"]


def test_quiet_zero_pause_join_quality_rescue_rejects_other_join_failures(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    row = {
        "similarity": 1.0,
        "abs_drift_ms": 10.0,
        "fit_tempo_delta": 0.02,
        "critical_term_recall": 1.0,
        "join_quality": {
            "pass": False,
            "reasons": ["large_edge_rms_jump", "loud_pre_join_tail"],
            "max_edge_rms_jump_db": 18.0,
            "joins": [
                {
                    "index": 1,
                    "pause_sec": 0.0,
                    "prev_tail_dbfs": -46.0,
                    "next_head_dbfs": -61.0,
                    "edge_rms_jump_db": 18.0,
                }
            ],
        },
    }

    assert service._join_quality_rescue(row) is None
    assert service._reject_reasons(row) == ["bad_join_quality"]


def test_strong_gate_pass_requires_target_quality(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    row = {
        "gate_pass": True,
        "similarity": 0.986,
        "abs_drift_ms": 299.0,
        "fit_tempo_delta": 0.05,
        "critical_term_recall": 0.95,
        "latin_surface_recall": 1.0,
        "f0_range_semitones_10_90": 4.5,
        "join_quality": {"pass": True},
    }

    assert service._is_strong_gate_pass(row)

    for key, value in [
        ("gate_pass", False),
        ("similarity", 0.984),
        ("abs_drift_ms", 301.0),
        ("fit_tempo_delta", 0.051),
        ("critical_term_recall", 0.94),
        ("latin_surface_recall", 0.99),
        ("f0_range_semitones_10_90", 4.49),
        ("join_quality", {"pass": False}),
    ]:
        candidate = dict(row)
        candidate[key] = value
        assert not service._is_strong_gate_pass(candidate), key


def test_strong_default_skips_split_candidate_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    default_relpath = service._candidate_relpath(request.output_relpath, "default")

    def synthesize_default(*_args, **_kwargs):
        return _write_candidate_row(settings, default_relpath, family="default")

    def fail_split(*_args, **_kwargs):
        raise AssertionError("split candidates should not be generated after a strong default pass")

    monkeypatch.setattr(service, "_synthesize_single_candidate", synthesize_default)
    monkeypatch.setattr(service, "_synthesize_split_light_vector", fail_split)
    monkeypatch.setattr(service, "_synthesize_split_mix", fail_split)

    policy = SimpleNamespace(
        candidate_families=["default", "split_light_vector", "split_mix"],
        split_planning=True,
        execution_mode="candidate_fallback_v1",
    )
    response = service._run_candidates(request, policy, SimpleNamespace())  # type: ignore[arg-type]

    assert response.candidate_result is not None
    assert response.candidate_result["selected_family"] == "default"
    assert response.candidate_result["split_plan"] is None
    assert response.candidate_result["candidate_generation_stop_reason"] == "default_strong_gate_pass"


def test_strong_split_light_skips_split_mix_candidate_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    request = _candidate_request()
    default_relpath = service._candidate_relpath(request.output_relpath, "default")
    light_relpath = service._candidate_relpath(request.output_relpath, "split_light_vector")

    def synthesize_default(*_args, **_kwargs):
        return _write_candidate_row(
            settings,
            default_relpath,
            family="default",
            gate_pass=False,
            similarity=0.5,
            score=0.1,
        )

    def synthesize_light(*_args, **_kwargs):
        return _write_candidate_row(
            settings,
            light_relpath,
            family="split_light_vector",
            join_quality={"pass": True},
            split_strategy="automatic",
        )

    def fail_mix(*_args, **_kwargs):
        raise AssertionError("split_mix should not be generated after a strong split_light_vector pass")

    monkeypatch.setattr(service, "_synthesize_single_candidate", synthesize_default)
    monkeypatch.setattr(service, "_synthesize_split_light_vector", synthesize_light)
    monkeypatch.setattr(service, "_synthesize_split_mix", fail_mix)

    policy = SimpleNamespace(
        candidate_families=["default", "split_light_vector", "split_mix"],
        split_planning=True,
        execution_mode="candidate_fallback_v1",
    )
    adapter_path = settings.data_root / "training" / "lora_runs" / "adapter_step80.pt"
    controller = SimpleNamespace(adapter_path=adapter_path, metadata={"step": 80, "rank": 4})
    response = service._run_candidates(request, policy, controller)  # type: ignore[arg-type]

    assert response.candidate_result is not None
    assert response.candidate_result["selected_family"] == "split_light_vector"
    assert response.candidate_result["selected_candidate"]["family"] == "split_light_vector"
    assert response.candidate_result["selected_candidate"]["fit_tempo_delta"] <= 0.05
    assert response.candidate_result["candidate_generation_stop_reason"] == "split_light_vector_strong_gate_pass"
    assert response.candidate_result["adapter"]["data_relpath"] == "training/lora_runs/adapter_step80.pt"
    assert response.candidate_result["adapter"]["metadata"]["step"] == 80


def test_candidate_generation_meta_pins_offline_duration_policy(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]

    split_meta = service._candidate_meta({"lora_candidate": {"enabled": True}}, family="split_light_vector")
    default_meta = service._candidate_meta({"lora_candidate": {"enabled": True}}, family="default")

    split_generation = split_meta["lora_candidate_generation"]
    assert split_generation["duration_control_mode"] == "embedding"
    assert split_generation["duration_control_min_sec"] == 0.05
    assert split_generation["duration_token_rate_policy"] == "piecewise"
    assert split_generation["duration_piecewise_token_rates"] == "15:23.4375,23:30,default:32.5"
    assert split_generation["max_text_tokens_per_segment"] == 600
    assert split_generation["disable_text_chunking"] is True
    assert split_generation["max_mel_headroom"] == 512
    assert default_meta["lora_candidate_generation"]["max_mel_headroom"] == 0


def test_candidate_selection_prefers_high_quality_split_mix_near_best(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    light = {
        "family": "split_light_vector",
        "gate_pass": True,
        "score": 0.991,
        "similarity": 0.993,
        "abs_drift_ms": 14.0,
        "critical_term_recall": 1.0,
    }
    mix = {
        "family": "split_mix",
        "gate_pass": True,
        "score": 0.957,
        "similarity": 0.993,
        "abs_drift_ms": 17.0,
        "critical_term_recall": 1.0,
    }

    assert service._select_candidate([light, mix]) is mix


def test_candidate_selection_does_not_prefer_split_mix_when_anchor_quality_fails(tmp_path: Path):
    settings = _Settings(tmp_path)
    service = LoraCandidateService(settings, _FakeTTS(settings), _FakeASR())  # type: ignore[arg-type]
    light = {
        "family": "split_light_vector",
        "gate_pass": True,
        "score": 0.991,
        "similarity": 0.993,
        "abs_drift_ms": 14.0,
        "critical_term_recall": 1.0,
    }
    mix = {
        "family": "split_mix",
        "gate_pass": True,
        "score": 0.957,
        "similarity": 0.993,
        "abs_drift_ms": 450.0,
        "critical_term_recall": 1.0,
    }

    assert service._select_candidate([light, mix]) is light


def test_lora_toggle_keeps_base_disabled_by_default(tmp_path: Path):
    torch = pytest.importorskip("torch")
    from torch import nn

    class Conv1D(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.weight = nn.Parameter(torch.eye(2))

        def forward(self, x):
            return x.matmul(self.weight)

    gpt = nn.Module()
    gpt.proj = Conv1D()
    tts = SimpleNamespace(gpt=gpt, device=torch.device("cpu"), use_fp16=False)
    adapter_path = tmp_path / "adapter.pt"
    torch.save(
        {
            "metadata": {"target_regex": "^proj$", "rank": 1, "alpha": 1.0},
            "modules": {
                "proj": {
                    "lora_A": torch.ones(2, 1),
                    "lora_B": torch.ones(1, 2),
                }
            },
        },
        adapter_path,
    )

    controller = ensure_lora_adapter(tts, adapter_path)
    x = torch.ones(1, 2)
    disabled_before = tts.gpt.proj(x).detach().clone()
    with controller.enabled():
        enabled = tts.gpt.proj(x).detach().clone()
    disabled_after = tts.gpt.proj(x).detach().clone()

    assert torch.allclose(disabled_before, disabled_after)
    assert not torch.allclose(disabled_before, enabled)
