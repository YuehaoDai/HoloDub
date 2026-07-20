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

    # Demucs can spike memory on hour-long media. Keep split mode enabled and
    # single-worker by default so it can coexist with the rest of the real stack
    # in a 16GB-ish local Docker Desktop environment.
    ml_demucs_segment_sec: float = 7.0
    ml_demucs_overlap: float = 0.1
    ml_demucs_jobs: int = 1
    ml_demucs_device: str = "auto"
    ml_demucs_model: str = "htdemucs"
    ml_demucs_threads: int = 1
    ml_demucs_chunk_sec: float = 0.0
    # Per Demucs invocation timeout. 0 keeps the historical unbounded behavior.
    # If Demucs times out after writing both expected stems, the adapter accepts
    # those files so a post-processing hang cannot block a long episode rerun.
    ml_demucs_timeout_sec: float = 0.0

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
    # Drop likely non-speech ASR hallucinations before they can reach
    # translation/TTS. This protects long chalkboard squeaks, scraping,
    # music/noise bursts and other high-energy non-voice events that Whisper
    # may otherwise render as confident-looking text.
    asr_non_speech_guard_enabled: bool = True
    asr_guard_min_duration_ms: int = 1200
    asr_guard_no_speech_prob: float = 0.62
    asr_guard_avg_logprob: float = -1.05
    asr_guard_compression_ratio: float = 2.8
    asr_guard_max_chars_per_sec: float = 24.0
    asr_guard_min_vad_overlap: float = 0.15
    asr_guard_vad_pad_ms: int = 120
    asr_guard_audio_features_enabled: bool = True
    asr_guard_zcr_threshold: float = 0.22
    asr_guard_soft_no_speech_prob: float = 0.35
    asr_guard_soft_avg_logprob: float = -0.60
    asr_guard_min_rms: float = 0.003
    pyannote_auth_token: str = ""
    pyannote_pipeline: str = "pyannote/speaker-diarization-3.1"
    pyannote_segmentation_model: str = "pyannote/segmentation-3.0"
    pyannote_segmentation_onset: float = 0.5
    pyannote_segmentation_offset: float = 0.45
    pyannote_min_speech_ms: int = 250
    pyannote_min_silence_ms: int = 300
    pyannote_inference_batch_size: int = 32
    pyannote_device: str = "auto"

    indextts2_endpoint: str = ""
    indextts2_api_key: str = ""
    indextts2_model: str = ""
    indextts2_command: str = ""
    # Main-flow LoRA candidate audio is a production opt-in behind a second
    # ML-service gate. Go may request candidate_audio, but the service still
    # falls back to default synthesis unless this flag and adapter path are set.
    ml_lora_candidate_execution_enabled: bool = False
    indextts2_lora_adapter_path: str = ""
    # The training workbench may run a deliberately tiny one-step smoke to
    # prove the ML image can see IndexTTS2, feature caches, GPU, and /data
    # outputs. Full training remains a separate explicit runner.
    ml_lora_training_smoke_enabled: bool = True
    ml_lora_training_max_smoke_steps: int = 1
    # Full adapter training can take hours and writes the official run output
    # directory. Keep it opt-in even when the smoke runner is enabled.
    ml_lora_training_full_enabled: bool = False
    # Canary/regression validation can launch TTS/ASR-heavy subprocesses and
    # writes review artifacts. Keep it behind an explicit validation runner flag.
    ml_lora_training_validation_enabled: bool = False

    # inline mode: load indextts2-inference directly inside ml-service process
    indextts2_inline: bool = False
    # path to local model checkpoints; empty = auto-download from HuggingFace
    indextts2_model_dir: str = ""
    # attention backend: "" (sdpa, default), "sage" (SageAttention), "flash" (Flash-Attn v2)
    indextts2_attn_backend: str = ""
    # auto-infer emotion vector from translated text via Qwen3 fine-tune
    indextts2_use_emo_text: bool = True
    # Greedy decoding is fast, but long lecture segments are more prone to
    # omissions. Promote long/high-risk segments to the quality beam count.
    indextts2_num_beams: int = 1
    indextts2_long_num_beams: int = 3
    indextts2_long_quality_min_sec: float = 30.0
    # Experimental community duration-control path. Default stays off because
    # upstream has not released an official stable duration API yet.
    # Supported values: off, embedding.
    indextts2_duration_control_mode: str = "off"
    # Supported values: constant, piecewise. Piecewise follows the first
    # threshold whose target seconds are greater than the request duration.
    indextts2_duration_token_rate: float = 50.0
    indextts2_duration_token_rate_policy: str = "constant"
    indextts2_duration_piecewise_token_rates: str = "15:23.4375,23:30,default:32.5"
    indextts2_duration_control_min_sec: float = 15.0
    # Structured dubbing plans are useful for expressive content, but lecture
    # narration should stay close to the speaker prompt. Neutral/calm segments
    # under this arousal are synthesized without an explicit emotion vector.
    indextts2_neutral_dubbing_max_arousal: float = 0.65
    # IndexTTS2 expects emotion vector mass <= 0.8. Keep our dubbing vectors
    # deliberately below that so speaker identity and natural lecture prosody
    # remain dominant.
    indextts2_dubbing_emo_vector_max_sum: float = 0.35
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
