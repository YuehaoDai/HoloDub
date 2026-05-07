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
    # Whisper systematically underestimates the end timestamp of the last word in
    # each segment (the trailing phonemes are often cut off by 200-400 ms).
    # This padding is added to every segment's end_ms BEFORE the close-gap merge
    # pass so that merging decisions reflect the true post-padding boundaries.
    # Japanese trailing vowels (延長音) can extend 500+ ms; default is conservative.
    asr_end_pad_ms: int = 500
    asr_end_pad_min_gap_ms: int = 80
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
    # BigVGAN fused-anti-alias-activation custom CUDA kernel.
    # Disabled by default because the inline JIT compilation path
    # (``cpp_extension.load`` invoked from inside the warm-up thread)
    # has been observed to hang indefinitely on RTX-50-class (sm_120)
    # GPUs with PyTorch 2.x + CUDA 12.8, even though the same nvcc
    # command runs to completion when invoked from a plain shell. The
    # PyTorch fallback path (use_cuda_kernel=False) produces identical
    # audio with only a small inference-time speed cost.
    indextts2_use_cuda_kernel: bool = False

    default_sample_rate: int = 24000
    default_channels: int = 1
    model_manifest_path: Path = Path("/app/config/model-manifest.example.json")

    # 0 = unlimited (matches historical behaviour). Set to a positive
    # integer to cap how many heavy models stay resident at once; the
    # least-recently-used entry is evicted when the cap is reached.
    model_registry_max_models: int = 0


@lru_cache
def get_settings() -> Settings:
    return Settings()
