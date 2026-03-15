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

  <p>
    <a href="README_zh-CN.md">📖 中文文档</a>
  </p>

</div>

> Dub the whole performance, not just the words.

## 🎬 Demo — English → Chinese (IndexTTS2, RTX 5080)

Full 79-minute MIT 6.824 Distributed Systems lecture (626 segments). Dubbed with zero-shot voice cloning, no training required.

**Watch on Bilibili**: [HoloDub 配音演示 — MIT 6.824 分布式系统课程（英→中）](https://www.bilibili.com/video/BV1vrwszEELd/)

Pipeline: `Faster-Whisper large-v3` (ASR) → `Qwen-turbo` (translation) → `IndexTTS2 FP16` (zero-shot TTS) · Total time: ~52 min on RTX 5080 (79-min video)

---

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
- [x] **Real TTS (interim)**: Edge-TTS (Microsoft, free, Chinese voice, atempo duration alignment)
- [x] GPU passthrough (NVIDIA Container Toolkit + Docker Compose `deploy.devices`)
- [x] **IndexTTS2 inline integration**: `indextts2-inference` inside `ml-service`, zero-shot voice cloning
- [x] **IndexTTS2 real inference validated**: English → Chinese, RTF 1.47x (RTX 5080)
- [x] **asyncio blocking fixed**: all ML routes use `run_in_executor`; healthz stays live during GPU inference
- [x] **Duration-aware translation**: first-pass translation prompt carries character budget derived from segment duration; Kimi-k2.5 re-translation triggered when TTS audio overflows trailing silence gap
- [x] **Drift-based retranslation (≤6%)**: re-translate when `|actual − target| / target > 6%`; up to 10 attempts with feedback (previous text, duration, drift) injected into prompt; stops when within threshold or max attempts reached
- [x] **atempo removed**: duration alignment now uses gap-borrowing (overflow into trailing silence) + re-translation loop; no speed artifacts
- [x] **ASR segmentation improved**: sentence-boundary-only splits, 5-word minimum, 800 ms silence threshold, short-segment post-merge; default min 4 s / max 20 s
- [x] **amix volume bug fixed**: `normalize=0` on all amix filters; previously audio was divided by segment count (–38 dB for 81-segment videos)
- [x] **Batched ffmpeg merge**: segments processed in groups of 30 to avoid `filter_complex` hang (O(N²) parser) on long videos; merge stage lease re-queue on conflict (no task loss)
- [x] **Per-segment TTS persistence**: each segment's result is written to DB immediately; timeout/retry only re-processes unfinished segments
- [x] **Parallel TTS**: worker sends up to `TTS_CONCURRENCY` (default 2) segments concurrently; ml-service `GPU_CONCURRENCY=2` allows 2 parallel GPU inferences for higher throughput
- [x] **Full 79-minute video validated**: 626 segments, English → Chinese, complete pipeline end-to-end
- [ ] Real vocal / BGM separation (Demucs, requires `ML_PYTHON_EXTRAS=real`)
- [ ] Speaker diarization (Pyannote, requires HuggingFace token)
- [x] **BigVGAN CUDA kernel on Blackwell / sm_120**: switched to `nvidia/cuda:12.8.0-devel` image + patched unused `#include <cuda_profiler_api.h>` + pre-compiled `.so` baked into the image layer; RTX 5080 now uses the native CUDA kernel
- [ ] IndexTTS2 eager model pre-loading at startup (cold-start ~6 min on RTX 5080; see Known Issues)
- [ ] Use a Chinese reference audio clip for better prosody alignment (current default uses English lecture audio)

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

### Edge-TTS voice options (interim)

> Edge-TTS is a **temporary TTS solution** to validate the full pipeline before IndexTTS2 is wired in.  
> It does not support zero-shot voice cloning; all speakers use the same preset voice.

Default is `zh-CN-XiaoxiaoNeural` (female, conversational). Override with `EDGE_TTS_VOICE` in `.env`:

| Voice | Style |
|-------|-------|
| `zh-CN-XiaoxiaoNeural` | Female, natural dialogue (default) |
| `zh-CN-YunxiNeural` | Male, news-reading |
| `zh-CN-YunjianNeural` | Male, energetic |
| `zh-TW-HsiaoChenNeural` | Female, Traditional Chinese |

---

## 🎙 IndexTTS2 — zero-shot dubbing (available now)

[IndexTTS2](https://github.com/index-tts/index-tts) (Bilibili, Apache 2.0) is the primary TTS backend HoloDub is built for. The `indextts2-inference` package is now **integrated and tested** inside `ml-service`.

### Why IndexTTS2

**1. Duration control** — the most critical feature for dubbing  
- `max_mel_tokens` is estimated from the **translated text length** (empirical: ~50 AR tokens/sec, ~13.5 tokens/Chinese char) with 5 % headroom — not from the target duration.  Using text length as the primary constraint prevents the model from generating slow prosody to fill an over-generous budget.
- `max_allowed_sec = target_sec + gap_to_next_segment` is passed as a hard ceiling so audio never exceeds the available silence window.
- If generated audio overflows into the trailing silence gap: accepted silently (natural pacing).
- If overflow exceeds the gap (would overlap next segment): Kimi-k2.5 re-translates with a tighter character budget (up to `RETRANSLATION_MAX_ATTEMPTS` times).
- **No `atempo` time-stretching** — duration is aligned through re-translation and natural gap borrowing, not speed artifacts.

**2. Zero-shot voice cloning**  
- 3–10 s of reference audio clones any speaker's voice, no training required.
- Integrates with HoloDub's **Voice Profile** system: upload a reference clip, bind it to `SPK_01` or `SPK_02`, and each speaker gets their own cloned voice.

**3. Emotion-aware synthesis**  
- Timbre and emotion are disentangled: use Speaker A's voice to convey Speaker B's emotion.
- `use_emo_text=true` (default): the Qwen3 fine-tune infers an 8-dim emotion vector automatically from the translated text — angry lines sound angry without manual annotation.
- Additional control: explicit emotion reference audio (`emo_audio_prompt`) or direct 8-dim vector.

### Enabling IndexTTS2 inline mode

**Requirements**: GPU + `ML_PYTHON_EXTRAS=real` image already built.

```env
# .env
ML_TTS_BACKEND=indextts2
INDEXTTS2_INLINE=true

# optional: use local checkpoints instead of HuggingFace auto-download
INDEXTTS2_MODEL_DIR=/data/models/indextts2

# optional: attention backend (default sdpa; sage/flash for Ampere/Hopper)
INDEXTTS2_ATTN_BACKEND=

# emotion auto-detection from translated text (recommended)
INDEXTTS2_USE_EMO_TEXT=true

# fallback reference voice when no VoiceProfile is bound to a speaker
INDEXTTS2_DEFAULT_VOICE_RELPATH=voices/default.wav
```

Restart the stack (no rebuild needed):

```powershell
docker compose --env-file .env up -d
```

**First run**: the IndexTTS2 model (~3–5 GB) is downloaded automatically from HuggingFace to `./hf-cache` and cached for subsequent runs.

### Setting up voice profiles for zero-shot cloning

```powershell
# 1. Upload a reference audio clip (3-10 s, WAV preferred)
#    Place it under data/voices/ so the container can access it via /data/voices/

# 2. Create a VoiceProfile via API
$body = @{
    name = "Host Voice"
    mode = "sample"
    language = "zh"
    sample_relpaths = @("voices/host_ref.wav")
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8080/voice-profiles" -Method Post -ContentType "application/json" -Body $body

# 3. Bind SPK_01 in a job to this profile
$body = @{ bindings = @(@{ speaker_label = "SPK_01"; voice_profile_id = <profile_id> }) } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:8080/jobs/<job_id>/bindings" -Method Put -ContentType "application/json" -Body $body
```

### Alternative connection modes (no local GPU required)

**HTTP mode** — run IndexTTS2 as a sidecar service:
```env
ML_TTS_BACKEND=indextts2
INDEXTTS2_ENDPOINT=http://your-indextts2-service:8000/tts
```

**Command mode** — call a local script:
```env
ML_TTS_BACKEND=indextts2
INDEXTTS2_COMMAND=python run_tts.py --text "{text}" --duration "{duration}" --output "{output}"
```

### Remaining work

- [ ] `emo_audio_prompt` support for cross-speaker emotion transfer
- [ ] Per-segment emotion override via API (currently auto-detected from text)
- [ ] Chinese reference audio for better prosody (current default is English; causes subtle rhythm mismatch)

---

## 🎬 Demo run — validated results

Full demo available on Bilibili: [BV1vrwszEELd](https://www.bilibili.com/video/BV1vrwszEELd/)

### 60-second clip (9 segments)

**Input**: MIT 6.824 Distributed Systems lecture, English, 60 s  
**Pipeline**: Faster-Whisper large-v3 → Qwen-turbo → IndexTTS2 (zero-shot)  
**Hardware**: RTX 5080, Docker + WSL2

| Phase | Time |
|-------|------|
| IndexTTS2 cold-start (first job) | **~6 minutes** |
| Single segment inference (warm) | **3.6 s** (GPT 1.06s + S2Mel 0.70s + BigVGAN 0.10s) |
| Full 60-second job | **~7 minutes** |
| Real-time factor (RTF) | **1.47×** per segment |

### Full 79-minute lecture (626 segments)

**Input**: MIT 6.824 full lecture video, English, 79 min, 946 MB  
**Pipeline**: same as above  
**Segmentation**: 626 segments, avg 6.9 s each (min 4 s, max 21.5 s)

| Phase | Approximate time |
|-------|-----------------|
| ASR (Faster-Whisper large-v3) | ~8 min |
| Translation (Qwen-turbo, 626 segments) | ~12 min |
| TTS (IndexTTS2, warm after cold-start) | ~36 min |
| Merge (batched ffmpeg, 21 chunks × 30 segs) | ~5 min |
| **Total** | **~60 min** |

**Timing accuracy**: avg drift < 1 % across all 626 segments; Kimi-k2.5 re-translation triggered on 3 segments where overflow exceeded trailing gap.

**10-minute clip (test_10min.mp4)**: ~81 segments; drift-based retranslation (≤6% target, up to 10 attempts) keeps most segments within 5%; high-drift filter surfaces remaining outliers for manual review.

---

## ⚠️ Known Issues & Help Wanted

These are open problems found during real hardware testing. Contributions welcome!

### 1. ~~BigVGAN CUDA kernel fails on Blackwell / RTX 50-series (sm_120)~~ — RESOLVED ✅

**Original symptom**: `nvcc` was absent from the `nvidia/cuda:12.8.0-runtime` base image, so the BigVGAN anti-aliasing CUDA extension could not be JIT-compiled at runtime.

**Fix applied** (`docker/ml.Dockerfile`):

1. **Switch base image** from `nvidia/cuda:12.8.0-runtime-ubuntu22.04` to **`nvidia/cuda:12.8.0-devel-ubuntu22.04`** — the devel variant bundles the full CUDA compiler toolchain (`nvcc`, all headers including `cuda_runtime.h`, `cusparse.h`, etc.). Image size increases ~1 GB but the build is reliable.

2. **Patch unused header** — BigVGAN's `.cu` source includes `<cuda_profiler_api.h>` which is absent from even the devel headers for CUDA 12.8. The header is never actually called, so it is removed via `sed` in the Dockerfile:
   ```dockerfile
   RUN find /usr/local/lib/python3.11/dist-packages/indextts -name "*.cu" \
        -exec sed -i 's|#include <cuda_profiler_api.h>|// removed (unused)|g' {} \;
   ```

3. **Pre-compile and cache** — `docker/precompile_bigvgan.py` is executed during `docker build`, compiling the extension for architectures `7.5;8.0;8.6;8.9+PTX;12.0` and storing the resulting `.so` in the image layer. Every container start loads the cached kernel instantly.

**Result on RTX 5080**: BigVGAN now uses the native CUDA kernel. Combined with `use_fp16=True` on IndexTTS2, TTS throughput improved **~2.5× vs FP32 + torch fallback** (3.08 s/segment vs ~7.7 s/segment on the 626-segment 79-minute benchmark).

### 2. IndexTTS2 cold-start (mitigated)

**Symptom**: Loading all components (GPT 3.3 GB, S2Mel 1.1 GB, BigVGAN, wav2vec2bert, CAMP++, NeMo text processor) takes **~6 minutes** on RTX 5080.

**Mitigation (implemented)**:
- IndexTTS2 is now **eager-loaded** at `ml-service` startup when `INDEXTTS2_INLINE=true` and `ML_TTS_BACKEND=indextts2`, so the first TTS request no longer blocks for 6 minutes.
- `/healthz` returns `tts_warmup_status`: `idle` | `loading` | `ready` | `error`.
- Go API exposes `GET /ml-health` (proxies to ml-service) for the UI.
- The Web UI shows a "TTS 模型预热中…" banner while `tts_warmup_status === "loading"`.

### 3. HuggingFace XetHub large-file download stalls in Docker

**Symptom**: `snapshot_download` of IndexTTS2 via the XetHub protocol (`hf-xet`) stalls on 2–3 large blobs (`.incomplete` files stop growing).

**Workaround** (included in `data/dl_gpt.py`): set `HF_HUB_DISABLE_XET=1` and use a `requests`-based streaming download instead:

```python
import os, requests
os.environ["HF_HUB_DISABLE_XET"] = "1"
from huggingface_hub import snapshot_download
snapshot_download("IndexTeam/IndexTTS-2", token=os.environ.get("HF_TOKEN"))
```

Or directly download the stuck file with:

```python
import requests, os
url = "https://huggingface.co/IndexTeam/IndexTTS-2/resolve/main/gpt.pth"
headers = {"Authorization": f"Bearer {os.environ['HF_TOKEN']}"}
with open("gpt.pth", "wb") as f:
    for chunk in requests.get(url, headers=headers, stream=True).iter_content(8*1024*1024):
        f.write(chunk)
```

---

### .env quick reference

| Variable | Smoke default | Real backends |
|----------|---------------|---------------|
| `TRANSLATION_PROVIDER` | `mock` | `openai_compatible` |
| `ML_ASR_BACKEND` | `mock` | `faster_whisper` |
| `ML_TTS_BACKEND` | `silence` | `edge_tts` → `indextts2` |
| `ML_VAD_BACKEND` | `none` | `pyannote` (optional) |
| `ML_SEPARATOR_BACKEND` | `ffmpeg_stub` | `demucs` (optional) |
| `ML_PYTHON_EXTRAS` | _(empty)_ | `real` |
| `INDEXTTS2_INLINE` | `false` | `true` (when `ML_TTS_BACKEND=indextts2`) |
| `INDEXTTS2_MODEL_DIR` | _(empty, auto-download)_ | `/data/models/indextts2` |
| `INDEXTTS2_USE_EMO_TEXT` | `false` | `true` (needs Qwen3 emo model) |
| `INDEXTTS2_DEFAULT_VOICE_RELPATH` | _(empty)_ | `voices/ref.wav` — **use a Chinese clip** for best results |
| `RETRANSLATION_ENABLED` | `true` | `true` / `false` |
| `RETRANSLATION_MODEL` | `kimi-k2.5` | any model on same `OPENAI_BASE_URL` |
| `RETRANSLATION_DRIFT_THRESHOLD` | `0.06` | max allowed drift (6%); triggers re-translation if exceeded |
| `RETRANSLATION_MAX_ATTEMPTS` | `10` | max re-translation attempts per segment |
| `TTS_CONCURRENCY` | `2` | parallel TTS requests from worker |
| `GPU_CONCURRENCY` | `2` | parallel GPU inferences in ml-service (requires ~16 GB VRAM) |
| `STAGE_TIMEOUT_SECONDS` | `14400` | raise for very long videos |

---

## 🛠 Tech stack

- **Control plane**: Go, Gin, GORM, Redis, PostgreSQL  
- **ML service**: FastAPI, PyTorch, Demucs/UVR5, Faster-Whisper, Pyannote, IndexTTS2  
- **Orchestration**: Docker Compose  
- **Translation**: Qwen / DeepSeek / pluggable LLM providers  
- **Web UI** (Phase 1): Vue 3 + Vite + Tailwind CSS — dark sidebar, segment drift review, inline TTS edit & re-synthesize  

---

## 🖥 Web UI — Segment Review & Fine-tuning

The operator UI (`/ui/`) has been rebuilt as a Vue 3 SPA with an **Open WebUI-style dark sidebar** layout.

![Segment fine-tuning interface](assets/精调界面截图.png)

*Segment table with drift badges, inline edit, and high-drift filter — review and refine translations per segment.*

### Phase 1 features (current branch: `feature/ui-segment-review`)

| Feature | Description |
|---------|-------------|
| **Job sidebar** | Real-time job list with status badges, auto-refreshes every 10s |
| **Segment table** | All segments with source text, translated text, and drift % badge |
| **Drift badges** | Green < 5 % · Yellow 5–15 % · Red > 15 % |
| **Audio playback** | Inline `<audio>` player per synthesized segment (lazy-loads blob) |
| **Inline edit** | Click Edit → modify translated text → Save or Save + Re-synthesize |
| **Filter / sort** | Filter by All / High-drift / Unsynthesized · Sort by ordinal or drift % |
| **Re-merge** | Trigger merge stage retry after editing to regenerate `final.mp4` |

### To build the UI locally

```bash
cd ui
npm install
npm run build
# Outputs to internal/ui/static/ (picked up by go:embed)
```

For development with hot-reload:
```bash
cd ui
npm install
npm run dev
# Vite dev server at http://localhost:5173/
# API calls proxy to the Go server (configure VITE_API_BASE if needed)
```

### Planned Phase 2 (backlog)

- **Original audio comparison**: Side-by-side playback of original vocal clip vs. TTS output (`GET /jobs/:id/audio/:ordinal` cut from `vocals.wav`)
- **Batch re-synthesize**: Multi-select segments with checkboxes + batch Rerun button
- **Keyboard shortcuts**: `J`/`K` to navigate segments, `Space` to play, `E` to edit
- **Waveform visualization**: WaveSurfer.js integration for precise audio inspection
- **Segment quality tagging**: Mark segments as good / bad / skip, persisted to segment meta
- **Speaker binding UI**: Assign voice profiles to speakers directly from the segment table

---

## 🛠 Tech stack

- **Control plane**: Go, Gin, GORM, Redis, PostgreSQL  
- **ML service**: FastAPI, PyTorch, Demucs/UVR5, Faster-Whisper, Pyannote, IndexTTS2  
- **Orchestration**: Docker Compose  
- **Translation**: Qwen / DeepSeek / pluggable LLM providers  
- **Web UI**: Vue 3, Vite, Tailwind CSS  

---

## 📜 License

Apache 2.0
