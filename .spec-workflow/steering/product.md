# HoloDub — Product Vision

## Elevator Pitch
HoloDub（幻读）is a self-hosted video translation & AI dubbing toolkit that rebuilds audio tracks around the original timeline using smart semantic splitting + duration-aware TTS, so dubbed audio stays perfectly in sync with the video.

## Target Users
- YouTube / Bilibili channel owners
- Clip / highlights makers
- Fansub groups
- Small studios that want everything on their own GPU box

## Core Value Proposition
Instead of just "translating subtitles and dropping a generic voice on top", HoloDub uses:
1. **Smart semantic splitting** — Whisper ASR with word-level timestamps, VAD-assisted pause detection, splits by meaning not fixed-length chunks
2. **Duration-aware TTS** — IndexTTS2 uses original segment duration as hard constraint
3. **Timeline-first merge** — audio track rebuilds around original timing

## Key Differentiators
- Zero-shot voice cloning (no training required per speaker)
- Multi-speaker diarization with custom voice profiles
- Long video support (79+ min) via Episode → Chapter → Segment three-tier architecture
- Closed-loop rework engine: three-level Judge (Segment/Chapter/Episode) → automatic retry/ensemble/broadcast
- LLM-driven chapterization (semantic boundary detection, not just silence-based)

## Current Maturity
- Core pipeline: ✅ End-to-end 79-min validation (626 segments, MIT 6.824 lecture)
- Long video architecture: ✅ Episode/Chapter/Segment three-tier complete
- Quality control: ✅ Three-level LLM Judge + closed-loop rework engine
- ML gaps: ❌ Demucs (human voice separation stub), ❌ Pyannote (speaker diarization stub)
- Observability: ⚠️ Basic Prometheus metrics, no OTEL tracing, no agent transcript storage
- Production readiness: ⚠️ Single-worker deployment, no multi-tenancy

## Immediate Priorities (from TaskMaster)
1. OPT-101: OpenTelemetry GenAI semconv + cost USD trace (observability)
2. Demucs real voice separation (ML capability)
3. Pyannote real speaker diarization (ML capability)
4. OPT-201/202/204 operational validation (code-complete, needs staging)