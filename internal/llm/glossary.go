// Package llm — OPT-402 episode-level glossary extraction +
// OPT-405 LLM-driven chapterization.
//
// ExtractEpisodeGlossary asks an LLM to produce, in ONE tool call, four
// artefacts that downstream stages of the dubbing pipeline depend on:
//
//  1. A canonical term sheet (glossary) so per-chapter translators stay
//     consistent on named entities, technical terms, recurring catchphrases.
//  2. Speaker hints so the speaker-binding UI can pre-populate.
//  3. A short reference card (≤300 words markdown) injected verbatim into
//     translate prompts as the translation summary.
//  4. (OPT-405) Chapter cuts derived from SEMANTIC theme transitions, not
//     silence + duration heuristics. The model receives every ASR segment
//     indexed with [N] timestamps and is instructed to:
//       - put theme completeness above duration balance
//       - cut on natural pivots ("ok let's move on", "next we'll talk about")
//       - prefer 5–45min chapters as a SOFT range, NEVER force-cut a
//         coherent argument and NEVER pad an unrelated topic
//
// Why all four in one call: they share the same context (full ASR text)
// and the same expensive prompt-prefix (~50–100k tokens for a 1h video).
// Issuing them as a single tool call halves the LLM cost and guarantees
// the chapter titles + reference card describe the same set of themes.
//
// Failure mode contract: the call is STRICTLY non-blocking on the
// pipeline. Any error logs a warning and the stage returns nil — the
// downstream translate path falls back to the legacy no-glossary mode
// AND the chapterize stage falls back to the deterministic DP algorithm
// (internal/chapterize/algo.go), so a transient LLM outage never fails
// an episode and never breaks the legacy fixed-DP path.
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

// ChapterCut is one OPT-405 LLM-derived chapter boundary. Indices refer
// to the segment list as the model RECEIVED it (0-based, contiguous,
// matching the [N] tags in the user prompt), so the caller can map them
// straight back to its own []models.Segment slice without any name
// resolution.
//
// The pair must satisfy 0 <= StartSegmentIdx <= EndSegmentIdx and chapters
// must collectively cover [0, len(segments)-1] with no overlap or gap.
// stage_chapterize validates these invariants and falls back to DP on
// breach (so a malformed LLM response never produces broken chapter
// boundaries downstream).
type ChapterCut struct {
	StartSegmentIdx int    `json:"start_segment_idx"`
	EndSegmentIdx   int    `json:"end_segment_idx"`
	TitleSource     string `json:"title_source"`
	TitleTranslated string `json:"title_translated"`
	SummaryMD       string `json:"summary_md"`
}

// GlossaryResult is the structured output the LLM produces. ReferenceCard
// is a short markdown-formatted prose block (genre, topic, register,
// named entities) injected verbatim into translate prompts as the
// translation summary. Chapters (OPT-405) is the LLM-driven semantic
// chapter plan; empty when chapter-driven mode is disabled OR when the
// model declined to chapterize a short transcript.
type GlossaryResult struct {
	Glossary      []GlossaryEntry `json:"glossary"`
	Speakers      []SpeakerHint   `json:"speakers"`
	ReferenceCard string          `json:"reference_card_md"`
	Chapters      []ChapterCut    `json:"chapters,omitempty"`
}

// glossaryToolSchema is the strict JSON Schema sent to the LLM. As with
// judge / review the schema is marshalled at init() so any typo crashes
// immediately rather than failing on first request. Numbers / arrays are
// kept simple — providers like DashScope accept the OpenAI shape, but
// nested 'oneOf' / pattern constraints are unevenly supported.
//
// OPT-405 added the chapters[] array. The chapter cuts are OPTIONAL at
// the schema level (so a transcript too short to chapterize can return
// chapters=[]), but when present they MUST cover every segment index
// without gaps/overlap; stage_chapterize.go validates this on the Go
// side and falls back to the deterministic DP algorithm on any breach.
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
		"chapters": map[string]any{
			"type": "array",
			"description": "OPT-405 SEMANTIC chapter SLICES. Each entry describes one CONTIGUOUS slice of the timeline; together the slices PARTITION the segment list into non-overlapping consecutive ranges that cover [0, total_segments-1] EXACTLY ONCE. " +
				"This is NOT a tagging / categorisation task: every entry has a UNIQUE [start_segment_idx, end_segment_idx] window, " +
				"chapter N+1's start_segment_idx MUST equal chapter N's end_segment_idx + 1, " +
				"and you should NEVER repeat the same window across multiple chapters. " +
				"Example for 200 segments split into 3 themes: " +
				"[{start:0,end:79,title:'Theme A',...}, {start:80,end:139,title:'Theme B',...}, {start:140,end:199,title:'Theme C',...}]. " +
				"PUT THEME COMPLETENESS ABOVE DURATION BALANCE: each chapter must finish a coherent argument before the next begins. " +
				"Use [] only when the transcript is too short or has no clear theme transitions; the pipeline will fall back to a single chapter or to the silence-based DP cuts.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"start_segment_idx": map[string]any{"type": "integer", "minimum": 0, "description": "0-based inclusive index of the FIRST segment in this chapter. MUST equal the previous chapter's end_segment_idx + 1 (or 0 for the very first chapter)."},
					"end_segment_idx":   map[string]any{"type": "integer", "minimum": 0, "description": "0-based inclusive index of the LAST segment in this chapter. MUST be < total_segments and >= start_segment_idx."},
					"title_source":      map[string]any{"type": "string", "description": "Concise title in the SOURCE language (≤80 chars), e.g. 'Course Logistics & Grading'."},
					"title_translated":  map[string]any{"type": "string", "description": "Concise title in the TARGET language (≤80 chars), e.g. '课程结构与评分'."},
					"summary_md":        map[string]any{"type": "string", "description": "1–2 sentence markdown summary of what this chapter covers, in the TARGET language."},
				},
				"required":             []string{"start_segment_idx", "end_segment_idx", "title_source", "title_translated", "summary_md"},
				"additionalProperties": false,
			},
		},
	},
	"required":             []string{"glossary", "speakers", "reference_card_md"},
	"additionalProperties": false,
})

// glossarySystemPrompt assembles the OPT-402+OPT-405 unified system prompt.
//
// chapterizeEnabled toggles the chapterization section. When false the
// model is told to leave chapters=[] (the schema allows this — it's the
// 1-chapter / DP-fallback path). Keeping the conditional inside the
// prompt instead of in two separate prompts saves a duplicate string and,
// more importantly, keeps the rest of the prompt byte-stable for cache
// purposes (OPT-001 friendly).
func glossarySystemPrompt(srcLang, tgtLang string, chapterizeEnabled bool) string {
	chapterizationBlock := ""
	if chapterizeEnabled {
		chapterizationBlock = fmt.Sprintf(
			"\n[Chapterization (OPT-405) — semantic SLICING, NOT tagging]\n"+
				"The user message contains EVERY ASR segment tagged with [idx] and a timestamp. "+
				"Decide where to SLICE the episode into 1..N chapter PIECES along the timeline, then return one entry per piece in the chapters[] array.\n"+
				"\n"+
				"This is a PARTITION task, NOT a categorisation task:\n"+
				"  - Each chapter describes one CONTIGUOUS time window of the episode.\n"+
				"  - The N chapters together cover the full timeline EXACTLY ONCE — no overlap, no gap, no repetition.\n"+
				"  - Chapter N+1's start_segment_idx is ALWAYS chapter N's end_segment_idx + 1.\n"+
				"  - DO NOT emit multiple chapters that all span [0, total-1] with different titles — that is wrong.\n"+
				"\n"+
				"GOOD example for 200 segments split into 3 themes:\n"+
				"  [{start_segment_idx: 0,   end_segment_idx: 79,  title: 'Theme A', ...},\n"+
				"   {start_segment_idx: 80,  end_segment_idx: 139, title: 'Theme B', ...},\n"+
				"   {start_segment_idx: 140, end_segment_idx: 199, title: 'Theme C', ...}]\n"+
				"\n"+
				"BAD example (DO NOT do this — every chapter spans the full range):\n"+
				"  [{start_segment_idx: 0, end_segment_idx: 199, title: 'Aspect 1', ...},\n"+
				"   {start_segment_idx: 0, end_segment_idx: 199, title: 'Aspect 2', ...}]\n"+
				"\n"+
				"PRINCIPLES for choosing CUT POSITIONS (in priority order):\n"+
				"1. THEME COMPLETENESS IS THE TOP PRIORITY. A chapter MUST cover one coherent theme from beginning to end. "+
				"Never end a chapter mid-argument and spill the conclusion into the next chapter. "+
				"Never start a chapter halfway through a topic that the previous chapter introduced.\n"+
				"2. CUT ON NATURAL PIVOTS. The speaker usually telegraphs theme changes — phrases like \"OK, let's move on\", "+
				"\"next we'll talk about X\", \"any questions? ... alright, going to Y\", explicit topic re-orientation, "+
				"demo→theory transitions, or a clear summary-then-new-topic pattern. Place the cut JUST AFTER such pivots.\n"+
				"3. LENGTH IS A SOFT GUIDE, NOT A CONSTRAINT. Long-form content (lectures, podcasts, talks) usually has "+
				"natural themes in the 5–45 minute range. Treat that as a soft hint:\n"+
				"   - Do NOT force-cut a coherent 50-minute argument into two halves just to hit a balance target.\n"+
				"   - Do NOT pad a 3-minute aside into the next theme just because it would otherwise be a short chapter — "+
				"merge it with the adjacent theme it actually belongs to.\n"+
				"   - 1 chapter is acceptable when the entire episode is genuinely one theme.\n"+
				"4. USE THE REFERENCE CARD. The reference_card_md you produce describes the macro-themes of the episode. "+
				"The chapters you cut should map onto those macro-themes (typically 2–6 of them per hour of content).\n"+
				"\n"+
				"OUTPUT REQUIREMENTS:\n"+
				"- Indices are 0-based and INCLUSIVE on both ends.\n"+
				"- The first chapter MUST start at index 0.\n"+
				"- The last chapter MUST end at index (total_segments - 1).\n"+
				"- Chapter ordering is left-to-right along the timeline; do NOT skip, repeat, or overlap windows.\n"+
				"- title_source: ≤80 chars, %s, descriptive of THE THEME (not literal first words).\n"+
				"- title_translated: ≤80 chars, %s.\n"+
				"- summary_md: 1–2 sentences, %s, describing what the chapter covers.\n"+
				"- Return [] for chapters ONLY if the transcript truly has no theme structure (very rare); the pipeline "+
				"will then fall back to silence-based cuts.\n",
			srcLang, tgtLang, tgtLang,
		)
	}

	return fmt.Sprintf(
		"You are a senior dubbing localisation editor. Given the FULL ASR transcript of one episode in %s, "+
			"produce a canonical term sheet that downstream chapter translators MUST follow for consistency, "+
			"plus speaker hints, a short reference card, and a SEMANTIC chapter plan. "+
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
			"Aim for content the translator can scan in 10 seconds."+
			"%s",
		srcLang, tgtLang, srcLang, chapterizationBlock,
	)
}

// EpisodeSegment is the input shape ExtractEpisodeGlossary expects: ONE
// entry per ASR segment in chronological order. The caller (pipeline /
// stage_glossary_extract.go) is expected to flatten every chapter's
// segments into a single contiguous slice before invoking us.
//
// SpeakerLabel is optional but recommended; when non-empty the model
// can attribute lines and detect dialogue, which materially improves
// chapter cuts on multi-speaker material (interviews, panels).
//
// Indices in the returned ChapterCut[] refer back to positions in this
// slice (0-based, inclusive on both ends).
type EpisodeSegment struct {
	StartMs      int64
	EndMs        int64
	Text         string
	SpeakerLabel string
}

// ExtractEpisodeGlossary derives the canonical episode-level term sheet
// + speaker hints + reference card + (OPT-405) semantic chapter plan
// from the FULL ASR transcript. The result is stored on the Episode row
// and consumed by per-chapter translate calls and ep_chapterize.
//
// Behaviour contract:
//   - Returns (zero-value, nil) when segments is empty or every segment
//     has empty text — caller should treat this as "no glossary, no
//     chapters, fall back to legacy translate path AND fall back to the
//     deterministic DP chapter cuts".
//   - Returns (zero-value, error) on LLM/network/parse failure — caller
//     SHOULD log and continue (the pipeline is more important than the
//     glossary). A strict-parse-failed metric is emitted on schema breach.
//   - Uses cfg.GlossaryModel (recommended for OPT-405: kimi-k2.5 — needs
//     long context + strong reasoning to produce good chapter cuts) so
//     the high-volume translate calls keep using their own retranslation
//     model. When GlossaryModel is empty, falls back to c.model.
//   - chapterizeEnabled controls whether the system prompt instructs the
//     model to also emit chapters[]. When false the model is told to
//     leave chapters=[]; the caller (stage_chapterize.go) then falls
//     back to the deterministic DP algorithm. This lets operators turn
//     OPT-405 off via a feature flag without losing the OPT-402 glossary
//     work.
func (c *Client) ExtractEpisodeGlossary(
	ctx context.Context,
	segments []EpisodeSegment,
	srcLang, tgtLang string,
	chapterizeEnabled bool,
) (GlossaryResult, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return GlossaryResult{}, errors.New("glossary requires OPENAI_BASE_URL and OPENAI_API_KEY")
	}
	if hasNoSpeech(segments) {
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

	userMsg := buildIndexedTranscript(segments, srcLang, chapterizeEnabled)

	// DashScope thinking-mode models (kimi-k2-thinking, qwen3-*-thinking)
	// reject the strict object form of tool_choice with
	// "invalid_parameter_error: tool_choice does not support being set to
	// required or object in thinking mode". Fall back to "auto" for those
	// — the system + user prompts are explicit enough that thinking
	// models still call the tool. Non-thinking models keep the strict
	// force so non-tool-call responses become a parse error rather than
	// a silent prose drift.
	var toolChoice any = forceToolChoice("emit_episode_glossary")
	if isThinkingModelName(model) {
		toolChoice = "auto"
	}

	payload := chatCompletionRequest{
		Model:       model,
		Temperature: 0.1, // canonical glossary should be near-deterministic
		Messages: []chatMessage{
			{Role: "system", Content: glossarySystemPrompt(srcLang, tgtLang, chapterizeEnabled)},
			{Role: "user", Content: userMsg},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_episode_glossary",
				Description: "Submit the canonical glossary, speaker hints, reference card and (OPT-405) semantic chapter plan for one episode.",
				Parameters:  glossaryToolSchema,
			},
		}},
		ToolChoice: toolChoice,
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

// hasNoSpeech reports whether the segment slice carries zero usable text
// (empty slice OR every segment has only whitespace). Used to short-
// circuit the LLM call so the API key isn't burnt on a pure-silence
// episode (which can happen on a music-only track or a misrouted file).
func hasNoSpeech(segments []EpisodeSegment) bool {
	for _, s := range segments {
		if strings.TrimSpace(s.Text) != "" {
			return false
		}
	}
	return true
}

// buildIndexedTranscript renders segments into the user-message format
// the model receives. Each line is:
//
//	[idx] mm:ss-mm:ss SPK_xx: text
//
// Index is 0-based and matches the model's chapter-cut indices on the
// way back. Timestamps are wall-clock from episode start, formatted as
// mm:ss for readability — the LLM doesn't need millisecond precision and
// this keeps the prompt tighter (an hour-long episode adds ~1KB).
//
// SpeakerLabel is omitted entirely when blank so single-speaker material
// stays cache-stable across episodes — important for the qwen / kimi
// providers that do prompt-prefix caching.
//
// chapterizeEnabled flips the trailing instruction so the same payload
// works in both modes (DP-fallback / OPT-405).
func buildIndexedTranscript(segments []EpisodeSegment, srcLang string, chapterizeEnabled bool) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "[Episode ASR transcript — source language: %s — total segments: %d]\n", srcLang, len(segments))
	for i, s := range segments {
		text := strings.TrimSpace(s.Text)
		if text == "" {
			// Skip blanks but DO NOT renumber — the model's [idx] tags
			// must align with the caller's slice index so downstream
			// chapter cuts map back unambiguously.
			continue
		}
		spk := ""
		if s.SpeakerLabel != "" {
			spk = s.SpeakerLabel + ": "
		}
		fmt.Fprintf(&sb, "[%d] %s-%s %s%s\n",
			i, formatMMSS(s.StartMs), formatMMSS(s.EndMs), spk, text)
	}
	sb.WriteString("[End of transcript]")
	if chapterizeEnabled {
		lastIdx := fmt.Sprint(len(segments) - 1)
		sb.WriteString("\n\nReturn the glossary, speakers, reference_card_md AND chapters[] in a single tool call.\n" +
			"\n" +
			"CHAPTERS PARTITION RULES (re-stating because previous models have gotten this wrong):\n" +
			"- chapters[0].start_segment_idx MUST be 0.\n" +
			"- chapters[last].end_segment_idx MUST be " + lastIdx + ".\n" +
			"- For every i > 0: chapters[i].start_segment_idx MUST equal chapters[i-1].end_segment_idx + 1.\n" +
			"- chapters cover [0, " + lastIdx + "] EXACTLY ONCE — no overlap, no gap, no two chapters sharing the same window.\n" +
			"- If you find yourself writing two chapters with identical [start, end] ranges, STOP — that is a tagging mistake; pick distinct contiguous slices instead.")
	} else {
		sb.WriteString("\n\nReturn the glossary, speakers and reference_card_md in a single tool call. " +
			"Leave chapters=[] (chapterization is disabled).")
	}
	return sb.String()
}

// formatMMSS turns a wall-clock millisecond offset into an "mm:ss" tag.
// Hours roll over (so 1h05m becomes "65:00") because the LLM only needs
// monotonic ordering for chapter reasoning, not human-readable hh:mm:ss
// — and keeping it 5 chars wide saves ~10KB on a 4-hour episode prompt.
func formatMMSS(ms int64) string {
	if ms < 0 {
		ms = 0
	}
	totalSec := ms / 1000
	mm := totalSec / 60
	ss := totalSec % 60
	return fmt.Sprintf("%02d:%02d", mm, ss)
}

// isThinkingModelName returns true for DashScope thinking-mode models
// that reject the strict tool_choice form. Used by ExtractEpisodeGlossary
// to fall back to tool_choice="auto" for these models, since they error
// out with "tool_choice does not support being set to required or
// object in thinking mode" otherwise. Substring match on "thinking"
// covers kimi-k2-thinking, qwen3-235b-a22b-thinking-2507, and any
// future *-thinking variants — the convention is stable on DashScope.
func isThinkingModelName(model string) bool {
	return strings.Contains(strings.ToLower(model), "thinking")
}
