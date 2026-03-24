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
    # vad_filter uses Silero-VAD to skip silence, significantly reducing peak VRAM
    # usage on long audio files. Strongly recommended for videos > 30 minutes.
    faster_whisper_vad_filter: bool = True
    # beam_size=1 (greedy) uses ~5x less VRAM than the default beam_size=5.
    # Accuracy drops slightly but is usually acceptable for dubbing ASR.
    faster_whisper_beam_size: int = 1
    pyannote_auth_token: str = ""
    pyannote_pipeline: str = "pyannote/speaker-diarization-3.1"

    indextts2_endpoint: str = ""
    indextts2_api_key: str = ""
    indextts2_model: str = ""
    indextts2_command: str = ""

    # inline mode: load indextts2-inference directly inside ml-service process
    indextts2_inline: bool = False
    # path to local model checkpoints; empty = auto-download from HuggingFace
    indextts2_model_dir: str = ""
    # attention backend: "" (sdpa, default), "sage" (SageAttention), "flash" (Flash-Attn v2)
    indextts2_attn_backend: str = ""
    # auto-infer emotion vector from translated text via Qwen3 fine-tune
    indextts2_use_emo_text: bool = True
    # fallback spk_audio_prompt when no VoiceProfile is bound to the speaker
    indextts2_default_voice_relpath: str = ""

    # edge-tts voice name; see `edge-tts --list-voices` for options.
    # zh-CN-XiaoxiaoNeural is a natural female Mandarin voice.
    edge_tts_voice: str = "zh-CN-XiaoxiaoNeural"

    default_sample_rate: int = 24000
    default_channels: int = 1
    model_manifest_path: Path = Path("/app/config/model-manifest.example.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
