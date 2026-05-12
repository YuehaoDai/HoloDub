# OPT-204 Structured Emotion / Prosody — Rollout Playbook

> Status (as of `dev-win`): code + L1 (unit tests) shipped. L2/L3
> staging require an operator with production access + a 50-segment
> human-rating panel for the L3 quality gate.

## What's in this rollout

The translate stage now has an optional path
(`TranslateWithDubbingPlan`) that emits a strict-tool `emit_dubbing_plan`
call carrying the translation plus structured prosody metadata:

```json
{
  "translation": "...",
  "emotion": {"valence": -1..1, "arousal": 0..1, "label": "calm|excited|sad|..."},
  "pacing": "slow|normal|fast",
  "emphasis_words": ["..."],
  "pause_after_ms": 0..1000
}
```

The plan is persisted on `segments.meta.dubbing` (no schema change,
the column was already JSONB) and forwarded to the ml-service TTS
adapter via `TTSRequest.dubbing_meta`. The adapter converts the
operator-facing semantic representation (label strings + 0..1 floats)
into the IndexTTS2 conditioning surface (8-element emo_vector +
emphasis_words + trailing silence via ffmpeg apad).

| Feature flag           | Default | Effect                                                                              |
| ---------------------- | ------- | ----------------------------------------------------------------------------------- |
| `DUBBING_PLAN_ENABLED` | `false` | When true, translate stage calls `TranslateWithDubbingPlan` instead of the plain-text variant. |

Backwards compatibility is total:

- Existing segments without `meta.dubbing` continue to use the legacy
  `INDEXTTS2_USE_EMO_TEXT` boolean.
- `dubbing_meta = null` in `TTSRequest` is a no-op (legacy path).
- Any failure in the strict-tool path (provider ignores `tool_choice`,
  malformed JSON, empty translation) falls back to
  `TranslateTextWithDuration` with a `WARN` log; the segment ships.

## L1 (already green on `dev-win`)

```powershell
# Go: 6 dubbing-plan parser tests
go test ./internal/llm/... -run TestDubbingPlan -count=1
go test ./internal/llm/... -run TestTranslateWithDubbingPlan -count=1

# Python: 12 prosody conversion tests
cd ml_service ; python -m pytest tests/test_tts_prosody.py -v
```

What's covered (~600 LOC of test code):

- Strict-tool schema validity (catches typos at boot)
- System prompt byte-stability (OPT-001 prefix cache invariant)
- Happy path: parse all 5 fields back from a provider response
- Optional-field handling: `emphasis_words` + `pause_after_ms` absent
- Defensive clipping: `pause_after_ms` ∈ {-50, 0, 800, 5000}
- Provider bypassing `tool_choice` (returns content) → surfaced as error
- Malformed JSON in tool args → decode error with truncated raw log
- Python: emo_vector L1-normalised, length=8, monotone clipping,
  quadrant assignments (excited / sad / angry / neutral)
- Python: emphasis words anchored to target text only, empty list omitted
- Python: missing emotion key falls back to neutral

## L2: ml-service smoke + Go end-to-end on staging

Submit a short job (60-90s) with `DUBBING_PLAN_ENABLED=true`:

```powershell
$env:DUBBING_PLAN_ENABLED = "true"
.\scripts\hot-reload.ps1
# Submit job, observe worker logs.
```

Expectations:

- `translate` stage emits one `emit_dubbing_plan` tool call per segment;
  worker log shows `OP=translate tool=emit_dubbing_plan` from the
  external-call counter.
- DB `segments.meta` for the job contains a `dubbing` sub-key with the
  five fields populated.
- TTS adapter diag string contains `prosody=plan(emotion=… pacing=…
  pause_ms=…)` (was `emo_text=…` before).
- No `translate: dubbing-plan call failed` WARN lines (consistent
  failures indicate the provider does not support strict-tool calls;
  fall back to provider whitelist e.g. qwen-plus / kimi-k2.5).

## L3: 50-segment human evaluation

The key OPT-204 question is **does the prosody actually sound better
than the legacy `use_emo_text=true` path?** This requires human ears.

Recommended panel design:

- Pull 50 segments from a representative episode (chapter opener,
  dialogue, action, monologue, chapter closer — 10 each).
- Generate TTS audio twice per segment: once with
  `DUBBING_PLAN_ENABLED=false`, once with `true`.
- Blind-pairwise rate each pair on three axes:
  1. **Emotion fit**: does the spoken emotion match the source's
     intent? (1-5)
  2. **Emphasis correctness**: are the stressed words the right ones?
     (1-5)
  3. **Pacing naturalness**: does the rate feel like a real speaker?
     (1-5)
- Goal (per plan §5): emotion fit ≥ 80% match, emphasis correctness
  ≥ 70%.

Capture the per-axis means + standard deviation in
`docs/opt-204/l3-eval-results.json` so the next prosody iteration has
a baseline to beat.

## L4: default ON

Once L3 passes, flip the default and document the trade-off in the
release notes:

```powershell
# Operator action — flip the .env.example default:
DUBBING_PLAN_ENABLED=true
```

Per-job override stays available via Job.config (future PR — out of
scope here; the env flag is the unit of rollout).

## Rollback

```powershell
$env:DUBBING_PLAN_ENABLED = "false"
.\scripts\hot-reload.ps1
```

Worker picks up on the next stage lease (< 30s). Persisted
`meta.dubbing` data stays in the DB but is silently ignored by the
legacy TTS path; if you need to PURGE it, the down-migration comment
in `migrations/012_segment_dubbing_meta_down.sql` has the SQL one-liner.

## Observability

| Signal | Where |
| --- | --- |
| Tool call attempts | `holodub_external_calls_total{operation="translate", model="…", status="ok\|retryable\|permanent"}` |
| Cost (Δ vs legacy) | `holodub_llm_cost_total_usd{operation="translate"}` (≈ +5-10% per segment from the extra schema tokens) |
| Fallback rate | grep `translate: dubbing-plan call failed` in worker log |
| Per-segment prosody used | `seg.meta.dubbing` (JSONB query) |
| TTS adapter diag | grep `prosody=plan` vs `emo_text=` in worker `tts_duration` stage logs |

## Known limitations

- The `valence/arousal → emo_vector` mapping is a hand-tuned heuristic
  (quadrant split at arousal=0.5). The L3 evaluation will tell us
  whether a learned map is justified.
- IndexTTS2 versions that do not accept `emphasis_words` as a kwarg
  will reject the call; the adapter's outer try/except catches this
  and falls back to legacy. If consistent fallback is observed, pin a
  newer IndexTTS2 version in `ml_service/pyproject.toml`.
- `pause_after_ms` is applied as ffmpeg `apad` on the synthesized
  audio; segments that already have a long trailing silence in the
  source will end up with the silence stacked. The merge stage's
  per-segment clip-to-slot fixes this in practice but is worth
  watching during L3.
