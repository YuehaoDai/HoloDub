from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    adapters: dict[str, str]
    loaded_models: list[str] = Field(default_factory=list)
    model_manifest: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    tts_warmup_status: str = Field(
        default="idle",
        description="IndexTTS2 inline: idle|loading|ready|error",
    )


class SeparateRequest(BaseModel):
    input_relpath: str
    vocals_output_relpath: str
    bgm_output_relpath: str


class SeparateResponse(BaseModel):
    vocals_relpath: str
    bgm_relpath: str
    diagnostics: list[str] = Field(default_factory=list)


class WordToken(BaseModel):
    word: str
    start_ms: int
    end_ms: int
    speaker_label: str = "SPK_01"


class Segment(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    speaker_label: str = "SPK_01"
    split_reason: str = "rule"


class SmartSplitRequest(BaseModel):
    audio_relpath: str
    source_language: str = ""
    min_segment_sec: float = 2.0
    max_segment_sec: float = 15.0
    # Hard ceiling enforced in post-merge passes; no merged segment may exceed this.
    hard_max_segment_sec: float = 45.0
    # Gaps smaller than this (ms) trigger a segment merge in the close-gap post-pass.
    close_gap_ms: int = 800


class SmartSplitResponse(BaseModel):
    segments: list[Segment]
    diagnostics: list[str] = Field(default_factory=list)


class TranscribeSegmentRequest(BaseModel):
    """Re-transcribe a single time window of an existing audio file.

    Used by the segment-review UI to fix Whisper recognition errors on a
    per-segment basis without rerunning smart_split for the whole job.

    The window boundaries (start_ms, end_ms) are taken from the segment
    row that the user wants corrected; the caller is responsible for
    passing the same vocals_relpath that the original asr_smart stage
    used so the new transcript stays comparable.
    """

    audio_relpath: str
    source_language: str = ""
    start_ms: int
    end_ms: int


class TranscribeSegmentResponse(BaseModel):
    text: str
    diagnostics: list[str] = Field(default_factory=list)


class TTSRequest(BaseModel):
    text: str
    target_language: str = ""
    target_duration_sec: float
    # target + trailing silence gap; adapter uses this as the hard token ceiling.
    max_allowed_sec: float = 0.0
    voice_config: dict[str, Any] = Field(default_factory=dict)
    output_relpath: str
    # Feedback from the previous TTS attempt for adaptive token budget (scheme 2).
    # prev_actual_sec: actual audio duration produced last time.
    # prev_text_chars:  non-whitespace char count of the text used last time.
    # Both zero on the first attempt.
    prev_actual_sec: float = 0.0
    prev_text_chars: int = 0
    # OPT-204 structured prosody / emotion plan.
    # Optional dict mirroring internal/llm/dubbing_plan.go DubbingPlan shape:
    #   {
    #     "emotion": {"valence": -1..1, "arousal": 0..1, "label": "calm|excited|..."},
    #     "pacing":          "slow" | "normal" | "fast",
    #     "emphasis_words":  [str, ...],
    #     "pause_after_ms":  int 0..1000,
    #   }
    # When None or empty, the IndexTTS2 inline adapter falls back to the
    # legacy use_emo_text boolean / no prosody (the pre-OPT-204 default).
    # Backwards compatibility: existing callers that do not pass this
    # field continue to work without code changes.
    dubbing_meta: dict[str, Any] | None = None


class TTSResponse(BaseModel):
    audio_relpath: str
    actual_duration_ms: int
    diagnostics: list[str] = Field(default_factory=list)
    candidate_result: dict[str, Any] | None = None


class LoraTrainingRunRequest(BaseModel):
    train_features_relpath: str
    val_features_relpath: str
    output_dir_relpath: str
    rank: int = 4
    alpha: float = 16.0
    dropout: float = 0.05
    learning_rate: float = 0.0002
    max_steps: int = 1
    save_every: int = 1
    eval_interval: int = 1
    text_loss_weight: float = 0.2
    mel_loss_weight: float = 0.8
    duration_control: bool = True
    duration_source: str = "target"
    duration_dropout: float = 0.1
    mel_stop_loss_weight: float = 2.0
    mel_end_loss_weight: float = 1.5
    mel_end_window: int = 64
    duration_stop_bce_weight: float = 0.0
    duration_position_loss_weight: float = 0.2
    duration_position_loss_window: int = 32
    gradient_checkpointing: bool = False
    precision: str = "fp16"


class LoraTrainingRunResponse(BaseModel):
    status: str
    exit_code: int | None = None
    output_dir_relpath: str
    adapter_relpath: str | None = None
    summary_relpath: str | None = None
    metrics_relpath: str | None = None
    stdout_log_relpath: str
    stderr_log_relpath: str
    command: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class LoraCanaryRunRequest(BaseModel):
    features_relpath: str
    run_dir_relpath: str
    adapter_relpath: str
    existing_run_root_relpath: str = ""
    voice_prompt_relpath: str = "voices/test_ref.wav"
    candidate_families: str = "flat,split_light_vector,split_mix"
    spoken_text_policy: str = "technical-v3"
    duration_token_rate_policy: str = "piecewise"
    max_fit_delta: float = 0.05
    max_drift_ms: float = 500.0
    min_similarity: float = 0.97
    target_similarity: float = 0.985
    min_f0_range: float = 4.5
    target_f0_range: float = 5.4
    skip_inference: bool = False
    resume: bool = False
    sample_ids: str = ""


class LoraCanaryRunResponse(BaseModel):
    status: str
    exit_code: int | None = None
    run_dir_relpath: str
    loop_summary_relpath: str | None = None
    candidate_gate_report_relpath: str | None = None
    listening_pack_relpath: str | None = None
    quality_baseline_relpath: str | None = None
    human_review_report_relpath: str | None = None
    risk_queue_relpath: str | None = None
    stdout_log_relpath: str
    stderr_log_relpath: str
    command: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class LoraRegressionRunRequest(BaseModel):
    summary_relpath: str
    gate_report_relpath: str
    risk_queue_relpath: str = ""
    report_relpath: str
    min_count: int = 84
    max_mean_drift_ms: float = 50.0
    max_max_drift_ms: float = 300.0
    min_mean_similarity: float = 0.99
    min_min_similarity: float = 0.96
    max_join_quality_failures: int = 0
    expect_chain: list[str] = Field(default_factory=list)
    forbid_chain: list[str] = Field(default_factory=list)
    expect_risk_sample: list[str] = Field(default_factory=list)


class LoraRegressionRunResponse(BaseModel):
    status: str
    exit_code: int | None = None
    report_relpath: str
    stdout_log_relpath: str
    stderr_log_relpath: str
    command: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
