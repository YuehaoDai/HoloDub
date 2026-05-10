// Package llm — OPT-402 episode-level glossary extraction.
//
// ExtractEpisodeGlossary asks an LLM to produce a canonical term sheet
// from the FULL ASR text of an episode, plus a short "reference card"
// that downstream chapter-level translate calls inject into their system
// prompt for cross-chapter coherence (term consistency, register, named
// entities). The function is non-blocking on the pipeline: a glossary
// failure is logged but does not fail the episode, and translate falls
// back to the legacy no-glossary path when Episode.Glossary is empty.
//
// Why a separate file: glossary has a stable contract independent of the
// translate / review / judge prompt families and uses its own model
// (cfg.GlossaryModel, default qwen-turbo). Keeping it isolated mirrors
// the Judge layout and lets future OPTs (chapter-level glossary refresh,
// cross-episode term reuse) extend without touching client.go.
package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"holodub/internal/observability"
)

// GlossaryEntry is one canonical (source -> target) translation pair the
// downstream translator MUST follow verbatim. Note is an optional context
// hint (e.g. "speaker name", "product name", "technical term").
type GlossaryEntry struct {
	Source string `json:"source"`
	Target string `json:"target"`
	Note   string `json:"note,omitempty"`
}

// SpeakerHint captures one identified speaker in the episode for the
// chapter-level translator + TTS layers. VoiceRegister is a brief tag
// like "narrator/calm" / "host/energetic" / "interviewee/measured" — kept
// free-form because OPT-402 is observe-only on the speaker pipeline.
type SpeakerHint struct {
	Label         string `json:"label"`         // ASR-level label (SPK_01, …)
	DisplayName   string `json:"display_name"`  // human-friendly name when known
	VoiceRegister string `json:"voice_register"` // tone / register description
}

// GlossaryResult is the structured output the LLM produces. ReferenceCard
// is a short markdown-formatted prose block (genre, topic, register,
// named entities) injected verbatim into translate prompts as the
// translation summary.
type GlossaryResult struct {
	Glossary      []GlossaryEntry `json:"glossary"`
	Speakers      []SpeakerHint   `json:"speakers"`
	ReferenceCard string          `json:"reference_card_md"`
}

// glossaryToolSchema is the strict JSON Schema sent to the LLM. As with
// judge / review the schema is marshalled at init() so any typo crashes
// immediately rather than failing on first request. Numbers / arrays are
// kept simple — providers like DashScope accept the OpenAI shape, but
// nested 'oneOf' / pattern constraints are unevenly supported.
var glossaryToolSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"glossary": map[string]any{
			"type":        "array",
			"description": "Canonical term sheet. Include named entities, technical terms, recurring catchphrases. Skip generic words.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"source": map[string]any{"type": "string"},
					"target": map[string]any{"type": "string"},
					"note":   map[string]any{"type": "string"},
				},
				"required":             []string{"source", "target"},
				"additionalProperties": false,
			},
		},
		"speakers": map[string]any{
			"type":        "array",
			"description": "One entry per distinct speaker label. Empty if speakers are not separable from the transcript.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"label":          map[string]any{"type": "string"},
					"display_name":   map[string]any{"type": "string"},
					"voice_register": map[string]any{"type": "string"},
				},
				"required":             []string{"label"},
				"additionalProperties": false,
			},
		},
		"reference_card_md": map[string]any{
			"type":        "string",
			"description": "Short markdown summary (≤300 words): genre/topic, key named entities, register/tone. Used verbatim in chapter translate prompts.",
		},
	},
	"required":             []string{"glossary", "speakers", "reference_card_md"},
	"additionalProperties": false,
})

func glossarySystemPrompt(srcLang, tgtLang string) string {
	return fmt.Sprintf(
		"You are a senior dubbing localisation editor. Given the FULL ASR transcript of one episode in %s, "+
			"produce a canonical term sheet that downstream chapter translators MUST follow for consistency. "+
			"Call the emit_episode_glossary function — do NOT respond with prose.\n\n"+
			"[Glossary inclusion criteria]\n"+
			"- Named entities: people, products, organisations, places, projects, fictional names.\n"+
			"- Technical / domain terms with established translations (e.g. algorithm names, jargon).\n"+
			"- Catchphrases or recurring expressions that should stay consistent.\n"+
			"- SKIP generic words, common verbs, and one-off mentions — the term sheet should be short and high-signal.\n\n"+
			"[Translation pairs]\n"+
			"- Provide the canonical %s rendering of each %s term.\n"+
			"- Proper nouns that are conventionally NOT translated (e.g. 'Raft', 'MapReduce') MUST appear with target == source so the translator never paraphrases them.\n\n"+
			"[Speakers]\n"+
			"- Identify distinct speakers if the transcript shows turn boundaries or addresses by name. "+
			"Otherwise return an empty array.\n\n"+
			"[Reference card]\n"+
			"- ≤300 words of markdown describing genre, topic, register, key named entities. "+
			"Aim for content the translator can scan in 10 seconds.",
		srcLang, tgtLang, srcLang,
	)
}

// ExtractEpisodeGlossary derives the canonical episode-level term sheet
// + speaker hints + reference card from the FULL ASR text. The result is
// stored on the Episode row and injected into per-chapter translate
// calls (see stage_glossary_extract.go and stage_tts.go).
//
// Behaviour contract:
//   - Returns (zero-value, nil) when GlossaryEnabled is false OR asrFullText
//     is blank — caller should treat this as "no glossary, fall back to
//     legacy translate path".
//   - Returns (zero-value, error) on LLM/network/parse failure — caller
//     SHOULD log and continue (the pipeline is more important than the
//     glossary). A strict-parse-failed metric is emitted on schema breach.
//   - Uses cfg.GlossaryModel (recommended: qwen-turbo) so the high-volume
//     translate calls keep the more capable kimi-k2 / k2.5 models. When
//     GlossaryModel is empty, falls back to c.model.
func (c *Client) ExtractEpisodeGlossary(ctx context.Context, asrFullText, srcLang, tgtLang string) (GlossaryResult, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return GlossaryResult{}, errors.New("glossary requires OPENAI_BASE_URL and OPENAI_API_KEY")
	}
	if strings.TrimSpace(asrFullText) == "" {
		// Nothing to extract; not an error.
		return GlossaryResult{}, nil
	}

	model := c.glossaryModel
	if model == "" {
		model = c.model
	}
	if model == "" {
		return GlossaryResult{}, errors.New("glossary requires GLOSSARY_MODEL or OPENAI_MODEL")
	}

	var userMsg strings.Builder
	userMsg.WriteString("[Episode ASR transcript - source language: ")
	userMsg.WriteString(srcLang)
	userMsg.WriteString("]\n")
	userMsg.WriteString(asrFullText)
	userMsg.WriteString("\n[End of transcript]")

	payload := chatCompletionRequest{
		Model:       model,
		Temperature: 0.1, // canonical glossary should be near-deterministic
		Messages: []chatMessage{
			{Role: "system", Content: glossarySystemPrompt(srcLang, tgtLang)},
			{Role: "user", Content: userMsg.String()},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_episode_glossary",
				Description: "Submit the canonical glossary, speaker hints and reference card for one episode.",
				Parameters:  glossaryToolSchema,
			},
		}},
		ToolChoice: forceToolChoice("emit_episode_glossary"),
	}

	rawArgs, err := c.doChatTool(ctx, OpGlossary, payload, "emit_episode_glossary")
	if err != nil {
		return GlossaryResult{}, fmt.Errorf("glossary tool call: %w", err)
	}
	if rawArgs == "" {
		observability.IncLLMStrictParseFailed(OpGlossary)
		return GlossaryResult{}, errors.New("glossary: no tool call in response")
	}

	var result GlossaryResult
	if err := json.Unmarshal([]byte(rawArgs), &result); err != nil {
		observability.IncLLMStrictParseFailed(OpGlossary)
		return GlossaryResult{}, fmt.Errorf("glossary: parse tool args: %w (raw: %.200s)", err, rawArgs)
	}
	return result, nil
}
