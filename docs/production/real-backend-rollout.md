# Real Backend Rollout

## Goal

Move from smoke defaults to real production backends in controlled steps.

## Stage 1: translation only

- set `TRANSLATION_PROVIDER=openai_compatible`
- fill `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`
- keep `ML_SEPARATOR_BACKEND=ffmpeg_stub`, `ML_ASR_BACKEND=mock`, `ML_TTS_BACKEND=silence`
- validate prompt stability, latency, and output length ratio

## Stage 2: ASR and diarization

- set `ML_PYTHON_EXTRAS=real`
- rebuild `ml-service`
- set `ML_ASR_BACKEND=faster_whisper`
- set `ML_VAD_BACKEND=pyannote`
- fill `PYANNOTE_AUTH_TOKEN`
- pin model choices in `config/model-manifest.example.json`

## Stage 3: separation and TTS

- set `ML_SEPARATOR_BACKEND=demucs`
- set `ML_TTS_BACKEND=indextts2`
- choose one of:
  - `INDEXTTS2_ENDPOINT`
  - `INDEXTTS2_COMMAND`
- validate resource use, output duration drift, and audio quality

## Production gates

- every backend is pinned in the model manifest
- quality regression passes on the approved sample set
- one rollback configuration is documented
- staging rollout passes before production rollout
