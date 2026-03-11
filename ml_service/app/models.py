from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    adapters: dict[str, str]
    loaded_models: list[str] = Field(default_factory=list)
    model_manifest: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


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


class SmartSplitResponse(BaseModel):
    segments: list[Segment]
    diagnostics: list[str] = Field(default_factory=list)


class TTSRequest(BaseModel):
    text: str
    target_duration_sec: float
    # target + trailing silence gap; adapter uses this as the hard token ceiling.
    max_allowed_sec: float = 0.0
    voice_config: dict[str, Any] = Field(default_factory=dict)
    output_relpath: str


class TTSResponse(BaseModel):
    audio_relpath: str
    actual_duration_ms: int
    diagnostics: list[str] = Field(default_factory=list)
