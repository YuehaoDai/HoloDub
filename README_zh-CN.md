# HoloDub（幻读）

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

- 使用 Go 1.22+、Gin、GORM。
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

HoloDub 目前处于 **早期设计 & 原型** 阶段：

- [x] 架构与数据模型设计完成
- [x] 说话人与音色映射抽象完成
- [ ] Go 控制面骨架（API + Worker）
- [ ] Python ML 服务骨架（FastAPI）
- [ ] 智能切分实现（ASR + VAD + 规则）
- [ ] 时长感知 TTS 集成（IndexTTS2）
- [ ] 端到端 Demo Pipeline

欢迎在架子搭好之后一起参与开发、提 Issue / PR。

---

## 🛠 技术栈

- **控制面**：Go, Gin, GORM, Redis, PostgreSQL  
- **数据面**：FastAPI, PyTorch, Demucs/UVR5, Faster-Whisper, Pyannote, IndexTTS2  
- **编排**：Docker Compose  
- **翻译**：Qwen / DeepSeek / 其他可插拔 LLM 提供方  

---

## 📜 License

Apache 2.0
