// Package llm — OPT-204 structured emotion / prosody output.
//
// The pre-OPT-204 translate path returns a plain text string and a
// boolean `use_emo_text` flag is passed verbatim to IndexTTS2; the
// flag enables a generic "speak with feeling" mode that ignores the
// actual content of the segment. OPT-204 replaces the boolean with a
// structured `DubbingPlan` emitted by the translator LLM in the same
// turn as the translation, threaded through to IndexTTS2 as
// `emo_vector + emphasis_tokens + pause_after_ms` so the TTS adapter
// can stress the right words at the right rate.
//
// The translator LLM emits the plan via a strict tool call
// (`emit_dubbing_plan`) so the schema is enforced at the provider
// level — content-mode hallucinations cannot smuggle ad-hoc fields
// through. Falling back to the plain-text path is a one-line config
// flip (`DUBBING_PLAN_ENABLED=false`); the legacy translate path stays
// fully functional during the rollout, the same way OPT-201's
// SegmentAgentEnabled gates the agent-vs-legacy retry loop.
package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

// DubbingPlan is the structured output the translator LLM emits when
// OPT-204 is enabled. The semantic axes are intentionally a subset of
// the IndexTTS2 conditioning surface (emotion vector + emphasis token
// IDs + post-utterance silence); fields beyond that surface would be
// dead weight from the LLM's perspective.
//
// JSON tags match the strict tool schema (see dubbingPlanSchema below).
// Field ordering matters for byte-stable prompt prefix caching: keep
// `translation` first because the agent's downstream consumers
// (RetranslateWithConstraint history, seg.TargetText) read it most.
type DubbingPlan struct {
	// Translation is the target-language text the TTS adapter speaks.
	// Always non-empty when the call returns no error.
	Translation string `json:"translation"`

	// Emotion is the structured affect signal. Valence is signed (-1
	// = strongly negative, +1 = strongly positive); arousal is unsigned
	// (0 = monotone, 1 = highly energetic). Label is the operator-
	// facing tag used for dashboards; the TTS adapter consumes the
	// numeric pair, not the label.
	Emotion DubbingEmotion `json:"emotion"`

	// Pacing is the relative speaking rate. The TTS adapter maps
	// "slow" → 0.85×, "normal" → 1.0×, "fast" → 1.15×; values outside
	// the enum are rejected at the schema level. Pacing is a relative
	// rate, NOT an absolute words-per-minute — duration is still
	// constrained per-segment.
	Pacing string `json:"pacing"`

	// EmphasisWords is the list of target-language words to stress
	// when speaking. Each entry must appear verbatim in `Translation`;
	// the TTS adapter substring-matches to locate the position. Empty
	// list is fine (no special emphasis).
	EmphasisWords []string `json:"emphasis_words,omitempty"`

	// PauseAfterMs is the silence appended after the segment audio
	// (0..1000 ms). Used for breath / sentence-end pacing; per-segment
	// budget already includes the trailing gap so this stays small.
	// Values > 1000 are clipped at the schema level.
	PauseAfterMs int `json:"pause_after_ms,omitempty"`
}

// DubbingEmotion is the affect tuple. Kept as a separate type so other
// pieces of the pipeline (chapter judge, episode judge) can later
// share the same shape without duplicating field definitions.
type DubbingEmotion struct {
	Valence float64 `json:"valence"`
	Arousal float64 `json:"arousal"`
	Label   string  `json:"label"`
}

// dubbingPlanSchema is the strict tool schema for emit_dubbing_plan.
// All numeric ranges are enforced by the provider; the schema is
// marshalled once at init() so a typo crashes at boot rather than on
// first request — a critical translation path must never silently
// reject schema-malformed responses.
var dubbingPlanSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"translation": map[string]any{
			"type":        "string",
			"description": "The target-language text the TTS adapter will speak. Required.",
		},
		"emotion": map[string]any{
			"type": "object",
			"properties": map[string]any{
				"valence": map[string]any{
					"type":        "number",
					"minimum":     -1.0,
					"maximum":     1.0,
					"description": "Affect polarity. -1 = strongly negative; +1 = strongly positive; 0 = neutral.",
				},
				"arousal": map[string]any{
					"type":        "number",
					"minimum":     0.0,
					"maximum":     1.0,
					"description": "Affect energy. 0 = monotone; 1 = highly energetic.",
				},
				"label": map[string]any{
					"type":        "string",
					"description": "Operator-facing tag (calm|excited|sad|angry|tender|surprised|neutral). Used for dashboards only.",
				},
			},
			"required":             []string{"valence", "arousal", "label"},
			"additionalProperties": false,
		},
		"pacing": map[string]any{
			"type": "string",
			"enum": []string{"slow", "normal", "fast"},
			"description": "Relative speaking rate (slow=0.85x, normal=1.0x, fast=1.15x).",
		},
		"emphasis_words": map[string]any{
			"type": "array",
			"items": map[string]any{"type": "string"},
			"description": "Target-language words to stress when speaking. Each must appear verbatim in `translation`.",
		},
		"pause_after_ms": map[string]any{
			"type":        "integer",
			"minimum":     0,
			"maximum":     1000,
			"description": "Silence appended after the segment audio (ms). 0..1000.",
		},
	},
	"required":             []string{"translation", "emotion", "pacing"},
	"additionalProperties": false,
})

// dubbingPlanSystemPrompt returns the byte-stable system prompt used by
// emit_dubbing_plan. Per OPT-001-followup-1 the per-segment values
// (duration, char limit, source text) live in the user message; this
// system prompt depends only on per-job constants (target language,
// rate, episode summary) so prefix cache reuse stays high.
func dubbingPlanSystemPrompt(targetLanguage string, rate float64, translationSummary string) string {
	var sb strings.Builder
	sb.WriteString("You translate subtitle segments for dubbing and ALSO emit prosody / emotion metadata.\n")
	sb.WriteString("Call the emit_dubbing_plan tool with the structured result.\n\n")
	sb.WriteString("Translation rules:\n")
	sb.WriteString("- Preserve meaning faithfully.\n")
	sb.WriteString(fmt.Sprintf("- Keep length appropriate for the speaking rate (≈ %.1f chars/sec in %s).\n", rate, targetLanguage))
	sb.WriteString("- Match register and tone of the source.\n\n")
	sb.WriteString("Emotion guidance:\n")
	sb.WriteString("- valence: -1 (strongly negative) … +1 (strongly positive); 0 = neutral.\n")
	sb.WriteString("- arousal: 0 (monotone) … 1 (highly energetic).\n")
	sb.WriteString("- label: one of calm | excited | sad | angry | tender | surprised | neutral.\n\n")
	sb.WriteString("Pacing guidance:\n")
	sb.WriteString("- slow: deliberate, contemplative content.\n")
	sb.WriteString("- normal: default for most dialogue.\n")
	sb.WriteString("- fast: action, excitement, time pressure.\n\n")
	sb.WriteString("Emphasis guidance:\n")
	sb.WriteString("- Mark 0..3 words per segment for stress. Each MUST appear verbatim in `translation`.\n")
	sb.WriteString("- Skip when no word stands out.\n\n")
	sb.WriteString("Pause guidance:\n")
	sb.WriteString("- Use pause_after_ms for sentence breaks / breath. Default 0; rarely above 500.\n\n")
	if translationSummary != "" {
		sb.WriteString("[Episode reference — terminology / style guide]\n")
		sb.WriteString(translationSummary)
		sb.WriteString("\n[End of reference]\n")
	}
	return sb.String()
}

// TranslateWithDubbingPlan is the OPT-204 strict-tool variant of
// TranslateTextWithDuration. Returns the full DubbingPlan; callers
// that only need the translation can pluck plan.Translation. Returns
// an error (no fallback) when the provider response cannot be parsed
// — fallback to the plain-text path is the caller's decision.
//
// Costs roughly the same as the plain-text translate call (one chat
// completion, slightly larger output payload). Token cost increase
// is bounded by the schema (≈ 30 extra output tokens per segment).
//
// Backwards compatibility: this is a NEW method, not a modification
// of TranslateTextWithDuration; the existing call sites stay on the
// plain-text path until the feature flag flips them over.
func (c *Client) TranslateWithDubbingPlan(
	ctx context.Context,
	sourceLanguage, targetLanguage, text string,
	targetSec float64,
	charsPerSecHint float64,
	contextBefore []ContextSegment,
	translationSummary string,
) (DubbingPlan, error) {
	if c.baseURL == "" || c.apiKey == "" || c.model == "" {
		return DubbingPlan{}, errors.New("TranslateWithDubbingPlan: OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL are required")
	}
	rate := charsPerSec(targetLanguage)
	if charsPerSecHint > 0 {
		rate = charsPerSecHint
	}
	systemPrompt := dubbingPlanSystemPrompt(targetLanguage, rate, translationSummary)

	var userMsg strings.Builder
	userMsg.WriteString("[Per-segment constraints]\n")
	fmt.Fprintf(&userMsg, "Segment duration: %.1f seconds.\n", targetSec)
	if len(contextBefore) > 0 {
		userMsg.WriteString("\n[Preceding segments — for terminology and style reference]\n")
		for i, seg := range contextBefore {
			label := fmt.Sprintf("-%d", len(contextBefore)-i)
			fmt.Fprintf(&userMsg, "(%s) %s: %s\n     %s: %s\n", label, sourceLanguage, seg.SrcText, targetLanguage, seg.TgtText)
		}
		userMsg.WriteString("\n[Segment to translate now]\n")
	}
	fmt.Fprintf(&userMsg, "Source language: %s\nText: %s", sourceLanguage, text)

	payload := chatCompletionRequest{
		Model:       c.model,
		Temperature: c.temperature,
		Messages: []chatMessage{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: userMsg.String()},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_dubbing_plan",
				Description: "Submit the translation plus structured emotion / pacing / emphasis metadata for this segment.",
				Parameters:  dubbingPlanSchema,
			},
		}},
		ToolChoice: forceToolChoice("emit_dubbing_plan"),
	}

	rawArgs, err := c.doChatTool(ctx, OpTranslate, payload, "emit_dubbing_plan")
	if err != nil {
		return DubbingPlan{}, fmt.Errorf("emit_dubbing_plan tool call: %w", err)
	}
	if rawArgs == "" {
		return DubbingPlan{}, errors.New("emit_dubbing_plan: provider returned no tool call args")
	}
	var plan DubbingPlan
	if err := json.Unmarshal([]byte(rawArgs), &plan); err != nil {
		return DubbingPlan{}, fmt.Errorf("emit_dubbing_plan: decode tool args: %w (raw=%q)", err, truncForLog(rawArgs, 200))
	}
	if strings.TrimSpace(plan.Translation) == "" {
		return DubbingPlan{}, errors.New("emit_dubbing_plan: provider returned empty translation field")
	}
	// Defensive clipping: pause_after_ms beyond 1000 should be rejected
	// by the schema, but providers do occasionally violate enums. Clip
	// rather than fail because shipping a slightly truncated pause is
	// strictly better than failing the segment.
	if plan.PauseAfterMs < 0 {
		plan.PauseAfterMs = 0
	}
	if plan.PauseAfterMs > 1000 {
		plan.PauseAfterMs = 1000
	}
	return plan, nil
}

// truncForLog returns s clipped to maxLen (with "..." suffix if
// truncated). Used in error messages so a 50KB raw response doesn't
// flood logs when the provider returns gibberish.
func truncForLog(s string, maxLen int) string {
	if len(s) <= maxLen {
		return s
	}
	return s[:maxLen] + "..."
}
