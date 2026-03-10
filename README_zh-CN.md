# HoloDub（幻读）

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
> 不止翻译台词，而是重现整场表演。

HoloDub 是一个 **面向创作者、自托管的视频翻译与配音工具箱**。

它主要服务于这样一群人：

- 想把 YouTube / B 站 / TikTok 视频搬运到其他语种平台的 UP / 博主  
- 做剪辑、二创、混剪的个人创作者、小团队  
- 想在本地 GPU 主机或云服务器上，用自己的模型做“私有译制”的字幕组 / Studio  

HoloDub 的目标不是“翻完字幕 + 随便套一条 AI 配音”，  
而是围绕整条时间轴，**重构音轨**，尽量接近「原生配音」的观看体验：

> 核心能力：**智能语义切分 + 时长感知 TTS（Duration-aware TTS）**

---

## ✨ 主要特性

### 🎬 时间轴优先的配音工作流

- **智能语义切分（Smart Semantic Split）**
  - 基于 Whisper 的 **单词级时间戳 ASR**。
  - 结合 Pyannote 的 VAD 静音检测。
  - 按“语义 + 停顿”拆分片段，而不是机械按固定时长切。
  - 可配置片段时长窗口（例如 2–15 秒 / 段）。

- **时长感知 TTS（Duration-aware TTS, IndexTTS2）**
  - 每个片段记录「原始时长」，作为 TTS 的硬约束输入。
  - 利用 IndexTTS2 的时长控制能力，使合成语音的长度尽量贴近原片段。
  - 必要时配合 `atempo` 等轻量时间拉伸手段，保证「音画尽量对齐」。

### 🗣 多说话人 + 自定义音色

- **说话人聚类（Speaker Diarization）**
  - 对整条人声做说话人识别和聚类（例如 `SPK_01`、`SPK_02`）。
  - 每个切分片段都会绑定一个说话人 ID。

- **自定义音色配置（Voice Profiles）**
  - `sample` 模式：  
    上传若干段参考音频，即可做 zero-shot 声纹克隆。
  - `checkpoint` 模式：  
    直接加载已有的 SoVITS / IndexTTS 风格检查点  
    （`.pth/.ckpt + .index + config`）。
  - 支持统一管理模型路径、说话人 ID、语种标签等元数据。

- **说话人与音色映射（Speaker → Voice Mapping）**
  - 面向每条视频任务（Job）：  
    为 `SPK_01 / SPK_02 / …` 分别选择不同的 Voice Profile。
  - 一个视频里，可以让主持人、嘉宾、旁白用完全不同的音色。
  - 修改映射后，可以只对受影响的片段重新生成配音。

### 🚀 创作者友好 & 自托管

- **想跑在哪儿就跑在哪儿**
  - 本地带 GPU 的桌面机 / NAS。
  - 云服务器上的单卡 / 双卡实例。
  - 默认不依赖任何第三方 SaaS。

- **默认单节点架构**
  - 简单的“一机版”拓扑：
    - Go 控制面（API + Worker）
    - Python ML 服务（GPU 推理）
    - PostgreSQL + Redis
  - 全部通过 Docker Compose 拉起来即可。

- **数据都在自己手里**
  - 视频 / 音频 / 文本 / 模型统一存放在 `/data` 目录。
  - 数据库只保存 **相对路径**（例如 `jobs/101/input.mp4`），迁移和备份更简单。

---

## 🧱 架构概览（简版）

> 不看这一段也可以用；  
> 想二开 / 魔改时，这一段能帮你快速搞清楚结构。

### 控制面（Go）

- 使用 Go 1.25+、Gin、GORM。
- 驱动任务在以下阶段流转：

  `media` → `separate` → `asr_smart` → `translate` → `tts_duration` → `merge`

- 在 PostgreSQL 中维护：
  - `jobs`：每条视频一个 Job。
  - `voice_profiles`：音色配置。
  - `speakers`：任务内的逻辑说话人。
  - `speaker_voice_bindings`：说话人与音色绑定关系。
  - `segments`：切分片段（翻译 & TTS 的最小单位）。

- 基于 Redis 做任务队列：
  - Job 级阶段任务（如 `job:123:stage:asr_smart`）。
  - 需要的时候可细化到 Segment 级别的 TTS 子任务。

- 对接 LLM（Qwen / DeepSeek 等）做翻译：
  - Prompt 会对「配音友好、长度接近」做约束，尽量避免译文过长或过短。

### 数据面（Python / GPU）

- 使用 Python 3.10+、FastAPI、PyTorch。
- 提供全局 GPU 锁 / 信号量，避免 OOM。
- 使用模型注册表管理：
  - Demucs / UVR5：人声 / 伴奏分离。
  - Faster-Whisper：ASR + 单词级时间戳。
  - Pyannote：VAD + 说话人识别。
  - IndexTTS2：时长感知 TTS。

- 通过 HTTP 提供接口，例如：
  - `POST /asr/smart_split`：
    - 输入：`audio_path`（相对路径）、最小/最大片段时长。
    - 输出：包含 `start_ms` / `end_ms` / `text` / `speaker_label` / `split_reason` 的片段列表。
  - `POST /tts/run`：
    - 输入：译文、`target_duration_sec`、`voice_config`、`output_relpath`。
    - 输出：音频保存路径 + 实际生成时长。

### 存储与路径约定

- 宿主机 `./data` 挂载为所有容器内的 `/data`。
- 数据库只保存相对路径（例如 `jobs/101/input.mp4`）。
- 应用运行时通过 `DATA_ROOT` 环境变量组装绝对路径。

---

## 📊 数据模型要点

- **jobs**：一条完整视频任务的生命周期。
- **voice_profiles**：抽象“音色配置”的元信息。
- **speakers**：单个任务内部的逻辑说话人（`SPK_01`、`SPK_02` 等）。
- **speaker_voice_bindings**：`speaker_id` → `voice_profile_id` 的映射。
- **segments**：
  - 包含 `start_ms` / `end_ms` / `original_duration_ms`。
  - `src_text`（ASR 文本）与 `tgt_text`（翻译文本）。
  - `tts_audio_path` / `tts_duration_ms`。
  - `split_reason`（通过标点、静音、最大时长等规则切分）。

---

## 🚦 当前状态

- [x] 架构与数据模型设计完成
- [x] 说话人与音色映射抽象完成
- [x] Go 控制面（API + Worker）
- [x] Python ML 服务（FastAPI）
- [x] 烟测模式端到端（mock / ffmpeg_stub / silence）
- [x] **真实 ASR**：Faster-Whisper large-v3，GPU 加速，词级时间戳
- [x] **真实翻译**：OpenAI 兼容接口（已测 qwen-turbo / DeepSeek）
- [x] **真实 TTS**：Edge-TTS（微软，免费，中文自然语音，含时长对齐）
- [x] GPU 直通（NVIDIA Container Toolkit + Docker Compose `deploy.devices`）
- [ ] 真实人声 / 伴奏分离（Demucs，需 `ML_PYTHON_EXTRAS=real`）
- [ ] 说话人分离（Pyannote，需 HuggingFace token）
- [ ] 时长感知 TTS 集成（IndexTTS2，零样本声纹克隆）

欢迎一起参与开发、提 Issue / PR。

---

## ▶ 运行当前原型

### 前置条件

- **Docker Desktop** 已安装并运行（Windows 使用 WSL2 后端）
- NVIDIA GPU 用户需额外安装 **NVIDIA Container Toolkit**（Docker Desktop 自带 nvidia runtime 支持时可跳过）

### 模式一：烟测（无需 GPU / API Key）

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

默认配置为 mock 后端，无需任何外部依赖，可验证整条流水线。

```powershell
# 提交测试任务
$body = '{"input_relpath":"input-smoke.mp4","target_language":"zh","auto_start":true}'
Invoke-RestMethod -Uri "http://127.0.0.1:8080/jobs" -Method POST -ContentType "application/json" -Body $body
```

打开 **http://localhost:8080/ui/** 查看进度，输出在 `data/jobs/<id>/output/final.mp4`。

### 模式二：接入真实后端（推荐）

#### 第一步：翻译（只需 API Key）

在 `.env` 中填写：

```env
TRANSLATION_PROVIDER=openai_compatible
OPENAI_BASE_URL=https://api.deepseek.com/v1   # 或阿里云 / OpenAI 等任意兼容接口
OPENAI_API_KEY=sk-xxxxxx
OPENAI_MODEL=deepseek-chat
```

推荐 **DeepSeek**（国内可访问、极低价格、中文效果好）。

#### 第二步：真实 ASR + TTS（需要 GPU）

在 `.env` 中修改：

```env
ML_PYTHON_EXTRAS=real
ML_ASR_BACKEND=faster_whisper
FASTER_WHISPER_MODEL=large-v3     # 推荐；显存 < 6GB 可用 medium
ML_TTS_BACKEND=edge_tts           # 免费，无需 API key，支持多种中文音色
```

重建 ml-service 镜像（首次约 10 分钟，主要是下载 PyTorch）：

```powershell
docker compose build ml-service
docker compose --env-file .env up -d
```

> ⚠️ **重要**：务必用 `--env-file .env` 或确保环境变量 `COMPOSE_ENV_FILE` 未被设置为 `.env.example`，否则容器会读取旧配置。

#### 第三步：Whisper 模型缓存（避免每次重启重新下载）

首次运行时，ml-service 会自动从 HuggingFace 下载 Whisper 模型（large-v3 约 3GB）。  
为避免每次重启容器都重新下载，`docker-compose.yml` 已将 `./hf-cache` 挂载到容器的 HuggingFace 缓存目录：

```
./hf-cache:/root/.cache/huggingface
```

模型一旦下载到 `hf-cache/`，后续重启无需重复下载。

#### 可选：说话人分离（Pyannote）

1. 在 HuggingFace 上接受 [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1) 的许可协议
2. 生成 Access Token：https://hf.co/settings/tokens
3. 在 `.env` 中填写：

```env
ML_VAD_BACKEND=pyannote
PYANNOTE_AUTH_TOKEN=hf_xxxxxx
```

### 常用命令

```powershell
docker compose ps                         # 查看服务状态
docker compose logs -f worker             # 查看 worker 日志
docker compose logs -f ml-service         # 查看 ML 服务日志（含模型加载进度）
docker compose down                       # 停止并移除容器
docker compose --env-file .env up -d      # 显式指定 .env 重启（推荐）
```

### Edge-TTS 音色选项

默认音色为 `zh-CN-XiaoxiaoNeural`（女声）。可在 `.env` 中设置 `EDGE_TTS_VOICE` 更换：

| 音色 | 风格 |
|------|------|
| `zh-CN-XiaoxiaoNeural` | 女声，自然对话（默认） |
| `zh-CN-YunxiNeural` | 男声，新闻播报 |
| `zh-CN-YunjianNeural` | 男声，激昂有力 |
| `zh-TW-HsiaoChenNeural` | 台湾女声 |

### .env 配置说明

| 变量 | 烟测默认 | 真实后端 |
|------|----------|----------|
| `TRANSLATION_PROVIDER` | `mock` | `openai_compatible` |
| `ML_ASR_BACKEND` | `mock` | `faster_whisper` |
| `ML_TTS_BACKEND` | `silence` | `edge_tts` |
| `ML_VAD_BACKEND` | `none` | `pyannote`（可选）|
| `ML_SEPARATOR_BACKEND` | `ffmpeg_stub` | `demucs`（可选）|
| `ML_PYTHON_EXTRAS` | 空 | `real` |

---

## 🛠 技术栈

- **控制面**：Go, Gin, GORM, Redis, PostgreSQL  
- **数据面**：FastAPI, PyTorch, Demucs/UVR5, Faster-Whisper, Pyannote, IndexTTS2  
- **编排**：Docker Compose  
- **翻译**：Qwen / DeepSeek / 其他可插拔 LLM 提供方  

---

## 📜 License

Apache 2.0
