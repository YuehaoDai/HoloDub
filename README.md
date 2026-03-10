# HoloDub (幻读)

<div align="center">

  <img src="./header.svg" alt="HoloDub Logo" width="100%">

  <h3>Holographic Audio Dubbing with Perfect Sync</h3>
  <p>全息音频配音 · 智能语义切分 · 时长精准对齐</p>

  <p>
    <a href="https://golang.org/">
      <img src="https://img.shields.io/badge/Go-1.25+-00ADD8?style=flat&logo=go&logoColor=white" alt="Go Version">
    </a>
    <a href="https://www.python.org/">
      <img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python Version">
    </a>
    <a href="#">
      <img src="https://img.shields.io/badge/Model-IndexTTS2-FF6F00?style=flat" alt="IndexTTS2">
    </a>
    <a href="https://www.apache.org/licenses/LICENSE-2.0">
      <img src="https://img.shields.io/badge/License-Apache%202.0-green.svg" alt="License">
    </a>
  </p>

</div>

> Dub the whole performance, not just the words.

HoloDub is a **self-hosted video translation & dubbing toolkit for creators**.

It is designed for people who need to re-voice videos for platforms like **YouTube, Bilibili, TikTok** and beyond:
- channel owners,
- clip / highlights makers,
- fansub groups,
- small studios that want to keep everything on their own GPU box or cloud instance.

Instead of just “translating subtitles and dropping a generic voice on top”, HoloDub rebuilds the audio track **around the original timeline** using:

> **Smart semantic splitting + duration-aware TTS**, so dubbed audio stays in sync with the video.

---

## ✨ Features at a glance

### 🎬 Timeline-first dubbing

- **Smart semantic split**
  - Whisper-based ASR with **word-level timestamps**.
  - VAD-assisted pause detection (Pyannote).
  - Splits by **meaning and natural pauses**, not fixed-length chunks.
  - Configurable segment length window (e.g. 2–15 seconds).

- **Duration-aware TTS (IndexTTS2)**
  - Every segment carries its **original duration** as metadata.
  - IndexTTS2 uses this as a hard constraint: generated speech length ≈ original segment length.
  - Optional `atempo` / light time-stretching as fallback when needed.

### 🗣 Multi-speaker & custom voices

- **Speaker diarization**
  - Automatically clusters different speakers (e.g. `SPK_01`, `SPK_02`, …).
  - Each segment is tagged with a speaker ID.

- **Custom voice profiles**
  - `sample` mode  
    Upload 1–N reference clips and use zero-shot voice cloning.
  - `checkpoint` mode  
    Load your own SoVITS / IndexTTS-style checkpoints  
    (`.pth/.ckpt + .index + config`).
  - Language tags, internal speaker IDs and model paths are stored centrally.

- **Speaker → voice mapping**
  - For each job, map `SPK_01 / SPK_02 / …` to different voice profiles.
  - Use different timbres in one video (host, guest, narrator…).
  - Change mappings and re-run only the affected segments.

### 🚀 Creator-friendly & self-hosted

- **Run it where you want**
  - On your own GPU PC at home.
  - On a single cloud GPU instance.
  - No external SaaS required by default.

- **Single-node by default**
  - Simple “one-box” layout:
    - Go control plane (API + worker)
    - Python ML service (GPU)
    - PostgreSQL + Redis
  - All wired together with Docker Compose.

- **Keep your data**
  - Videos, audio, transcripts and models live under a shared `/data` volume.
  - Database stores **relative paths** only (easier to move / backup).

---

## 🧱 Architecture (short version)

You don’t need to understand all of this to *use* HoloDub —  
but if you want to hack on it, here’s the rough picture.

### Control plane (Go)

- Go 1.25+, Gin, GORM.
- Drives jobs through stages:

  `media` → `separate` → `asr_smart` → `translate` → `tts_duration` → `merge`

- Stores:
  - `jobs` (one per video),
  - `voice_profiles` (custom voices),
  - `speakers` (logical speakers per job),
  - `speaker_voice_bindings` (who uses which voice),
  - `segments` (time-aligned pieces).

- Uses Redis as task queue:
  - Job-level stage tasks (e.g. `job:123:stage:asr_smart`).
  - Optionally segment-level TTS tasks.

- Talks to an LLM (Qwen / DeepSeek / etc.) for translation:
  - Prompts tuned for **dubbing-friendly, length-aware** output.

### Data plane (Python / GPU)

- Python 3.10+, FastAPI, PyTorch.
- Global GPU lock / semaphore to avoid OOM.
- Model registry with simple LRU-style caching:
  - Demucs / UVR5 — vocal & BGM separation.
  - Faster-Whisper — ASR with word-level timestamps.
  - Pyannote.audio — VAD and (optional) diarization.
  - IndexTTS2 — duration-aware TTS.

- HTTP endpoints (examples):
  - `POST /asr/smart_split`  
    → segments with `start_ms`, `end_ms`, `text`, `speaker_label`, `split_reason`.
  - `POST /tts/run`  
    → takes `text` + `target_duration_sec` + `voice_config` + `output_relpath`,  
    → returns saved audio path + actual duration.

### Shared storage

- Host `./data` mounted as `/data` in all containers.
- DB only stores **relative paths** (e.g. `jobs/101/input.mp4`).
- Apps resolve absolute paths using `DATA_ROOT`.

---

## 🗂 Data model highlights

- **jobs**
  - One row per video.
  - Tracks status, current stage, progress, config JSON, IO paths, errors, retries, heartbeat.

- **voice_profiles**
  - Describes how to synthesize a voice:
    - sample-based or checkpoint-based,
    - paths to models / indexes / configs,
    - language tags, internal speaker IDs.

- **speakers**
  - Per-job logical speakers generated by diarization  
    (e.g. `SPK_01` is “the host”, `SPK_02` is “the guest”).

- **speaker_voice_bindings**
  - Maps `speaker_id` → `voice_profile_id`.  
    This is where you say: *“for this job, SPK_01 uses Voice A, SPK_02 uses Voice B.”*

- **segments**
  - Minimal units for translation & TTS.
  - Carry:
    - `start_ms`, `end_ms`
    - `original_duration_ms` (generated column)
    - `src_text`, `tgt_text`
    - `tts_audio_path`, `tts_duration_ms`
    - `split_reason` (by punctuation, silence, max-duration, …)

---

## 📊 Status

- [x] Architecture & schema design
- [x] Speaker / voice mapping model
- [x] Go control plane (API + worker)
- [x] Python ML service (FastAPI)
- [x] Smoke-mode end-to-end pipeline (`mock` / `ffmpeg_stub` / `silence`)
- [x] **Real ASR**: Faster-Whisper large-v3, GPU-accelerated, word-level timestamps
- [x] **Real translation**: OpenAI-compatible API (tested with qwen-turbo / DeepSeek)
- [x] **Real TTS**: Edge-TTS (Microsoft, free, natural Chinese voice, with duration alignment)
- [x] GPU passthrough (NVIDIA Container Toolkit + Docker Compose `deploy.devices`)
- [ ] Real vocal / BGM separation (Demucs, requires `ML_PYTHON_EXTRAS=real`)
- [ ] Speaker diarization (Pyannote, requires HuggingFace token)
- [ ] Duration-aware TTS (IndexTTS2, zero-shot voice cloning)

If you’re interested in hacking on it, PRs and discussions are very welcome.

---

## ▶ Running

### Prerequisites

- **Docker Desktop** installed and running (Windows: WSL2 backend)
- NVIDIA GPU users: **NVIDIA Container Toolkit** (Docker Desktop includes nvidia runtime support)

### Mode 1: Smoke run (no GPU / API key needed)

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

Default config uses mock backends — no external dependencies. Verify the full pipeline end to end.

```powershell
# Submit a test job
$body = '{"input_relpath":"input-smoke.mp4","target_language":"zh","auto_start":true}'
Invoke-RestMethod -Uri "http://127.0.0.1:8080/jobs" -Method POST -ContentType "application/json" -Body $body
```

Open **http://localhost:8080/ui/** to track progress. Output lands at `data/jobs/<id>/output/final.mp4`.

### Mode 2: Real backends (recommended)

#### Step 1 — Translation (API key only)

```env
TRANSLATION_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://api.deepseek.com/v1   # or any OpenAI-compatible endpoint
OPENAI_API_KEY=sk-xxxxxx
OPENAI_MODEL=deepseek-chat
```

**DeepSeek** is recommended: accessible globally, very cheap, excellent Chinese quality.

#### Step 2 — Real ASR + TTS (GPU required)

```env
ML_PYTHON_EXTRAS=real
ML_ASR_BACKEND=faster_whisper
FASTER_WHISPER_MODEL=large-v3   # recommended; use medium if VRAM < 6 GB
ML_TTS_BACKEND=edge_tts         # free, no API key, multiple Chinese voices
```

Rebuild the ML service image (first time ~10 min, mostly PyTorch download):

```powershell
docker compose build ml-service
docker compose --env-file .env up -d
```

> ⚠️ **Important**: always use `--env-file .env` or make sure the shell variable `COMPOSE_ENV_FILE` is not set to `.env.example`. If it is set, containers will silently use the example config instead of yours.

#### Step 3 — Whisper model cache (avoid re-downloading on restart)

On first run, `ml-service` downloads the Whisper model from HuggingFace (~3 GB for large-v3).  
`docker-compose.yml` mounts `./hf-cache` into the container so the model persists across restarts:

```
./hf-cache:/root/.cache/huggingface
```

Once downloaded, subsequent restarts load from disk immediately.

#### Optional — Speaker diarization (Pyannote)

1. Accept the license for [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) on HuggingFace.
2. Create a Read token at https://hf.co/settings/tokens.
3. Set in `.env`:

```env
ML_VAD_BACKEND=pyannote
PYANNOTE_AUTH_TOKEN=hf_xxxxxx
```

### Common commands

```powershell
docker compose ps                         # service status
docker compose logs -f worker             # tail worker logs
docker compose logs -f ml-service         # ML service logs (model load progress etc.)
docker compose down                       # stop and remove containers
docker compose --env-file .env up -d      # restart with explicit env file (recommended)
```

### Edge-TTS voice options

Default is `zh-CN-XiaoxiaoNeural` (female, conversational). Override with `EDGE_TTS_VOICE` in `.env`:

| Voice | Style |
|-------|-------|
| `zh-CN-XiaoxiaoNeural` | Female, natural dialogue (default) |
| `zh-CN-YunxiNeural` | Male, news-reading |
| `zh-CN-YunjianNeural` | Male, energetic |
| `zh-TW-HsiaoChenNeural` | Female, Traditional Chinese |

### .env quick reference

| Variable | Smoke default | Real backends |
|----------|---------------|---------------|
| `TRANSLATION_PROVIDER` | `mock` | `openai_compatible` |
| `ML_ASR_BACKEND` | `mock` | `faster_whisper` |
| `ML_TTS_BACKEND` | `silence` | `edge_tts` |
| `ML_VAD_BACKEND` | `none` | `pyannote` (optional) |
| `ML_SEPARATOR_BACKEND` | `ffmpeg_stub` | `demucs` (optional) |
| `ML_PYTHON_EXTRAS` | _(empty)_ | `real` |

---

## 🛠 Tech stack

- **Control plane**: Go, Gin, GORM, Redis, PostgreSQL  
- **ML service**: FastAPI, PyTorch, Demucs/UVR5, Faster-Whisper, Pyannote, IndexTTS2  
- **Orchestration**: Docker Compose  
- **Translation**: Qwen / DeepSeek / pluggable LLM providers  

---

## 📜 License

Apache 2.0
