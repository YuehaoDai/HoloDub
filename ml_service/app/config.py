from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_root: Path = Path("/data")
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    gpu_concurrency: int = 1

    ml_separator_backend: str = "ffmpeg_stub"
    ml_asr_backend: str = "mock"
    ml_vad_backend: str = "none"
    ml_tts_backend: str = "silence"

    faster_whisper_model: str = "small"
    pyannote_auth_token: str = ""
    pyannote_pipeline: str = "pyannote/speaker-diarization-3.1"

    indextts2_endpoint: str = ""
    indextts2_api_key: str = ""
    indextts2_model: str = ""
    indextts2_command: str = ""

    # edge-tts voice name; see `edge-tts --list-voices` for options.
    # zh-CN-XiaoxiaoNeural is a natural female Mandarin voice.
    edge_tts_voice: str = "zh-CN-XiaoxiaoNeural"

    default_sample_rate: int = 24000
    default_channels: int = 1
    model_manifest_path: Path = Path("/app/config/model-manifest.example.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
