// Package llm — OPT-409 chapter-level Judge.
//
// JudgeChapter asks an LLM to score one entire chapter (= one Job under a
// multi-chapter Episode, see OPT-401) on cross-segment dimensions that
// segment-level OPT-002 judge cannot see and episode-level OPT-406 judge
// would see too late:
//
//   - narrative_coherence_within_chapter
//   - speaker_voice_stability_within_chapter
//   - terminology_consistency_within_chapter
//   - register_consistency_within_chapter
//   - overall_fidelity_chapter / overall_fluency_chapter
//
// Plus a top-3 weakest segment list (with recommended fix) so OPT-407
// closed-loop rework knows which segments to re-translate.
//
// The MVP runs in observe-only mode (CHAPTER_JUDGE_OBSERVE_ONLY=true): the
// score is persisted on jobs.chapter_judge_score / chapter_judge_meta but
// does NOT influence episode_merge or any other decision. Decision wiring
// is deferred to OPT-407 (closed-loop rework engine).
//
// Why a separate file: chapter judge has a stable contract independent of
// the segment-level judge AND independent of the chapter-review schema
// (OPT-403). Keeping it isolated makes it trivial for OPT-407 to build a
// rework decision table on top.
package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"holodub/internal/observability"
)

// ChapterJudgeSegment is one (src, tgt) pair fed into JudgeChapter.
// Ordinal is 1-indexed within the chapter (matches Segment.Ordinal in DB).
// SegJudgeScore is the optional segment-level OPT-002 score so the chapter
// judge can correlate cross-segment quality with single-segment signals
// when populated; nil when segment-level judging was disabled / pending.
type ChapterJudgeSegment struct {
	Ordinal       int
	StartMs       int64
	EndMs         int64
	SourceText    string
	TargetText    string
	SegJudgeScore *float64
}

// ChapterJudgeArgs is the input to one chapter judge call.
//
// EpisodeSummary is typically Job.TranslationSummary, providing register +
// glossary + named-entity context the judge needs for terminology / voice
// scoring without reading the entire episode again.
//
// GlossaryHint is the optional formatted episode glossary (OPT-402's
// canonical term table) — when provided the judge is asked to call out
// any segment that diverges from the established translation.
type ChapterJudgeArgs struct {
	SourceLang     string
	TargetLang     string
	ChapterOrdinal int
	ChapterTitle   string
	EpisodeSummary string
	GlossaryHint   string
	Segments       []ChapterJudgeSegment
}

// ChapterJudgeWeakSegment is one entry in the top-3-weakest list.
type ChapterJudgeWeakSegment struct {
	Ordinal         int    `json:"ordinal"`
	Issue           string `json:"issue"`
	RecommendedFix  string `json:"recommended_fix"`
}

// ChapterJudgeResult is the structured verdict returned by the chapter judge LLM.
//
// All six axis scores are 0..1 scalars (1 = best). Verdict is one of
// "chapter_ready" | "needs_revision" | "needs_major_rework"; observe-only
// mode logs but does not act on it (OPT-407 will).
type ChapterJudgeResult struct {
	NarrativeCoherenceWithinChapter      float64                   `json:"narrative_coherence_within_chapter"`
	SpeakerVoiceStabilityWithinChapter   float64                   `json:"speaker_voice_stability_within_chapter"`
	TerminologyConsistencyWithinChapter  float64                   `json:"terminology_consistency_within_chapter"`
	RegisterConsistencyWithinChapter     float64                   `json:"register_consistency_within_chapter"`
	OverallFidelityChapter               float64                   `json:"overall_fidelity_chapter"`
	OverallFluencyChapter                float64                   `json:"overall_fluency_chapter"`
	Top3WeakestSegments                  []ChapterJudgeWeakSegment `json:"top_3_weakest_segments,omitempty"`
	Verdict                              string                    `json:"verdict"`
	OneParagraphSummary                  string                    `json:"one_paragraph_summary,omitempty"`
}

// OverallScore returns a single scalar 0..1 used as Job.ChapterJudgeScore.
//
// Currently equal to OverallFidelityChapter (the single most decision-
// relevant axis when OPT-407 wires up rework). Kept as a method so future
// changes (eg. weighted average) only touch one place.
func (r ChapterJudgeResult) OverallScore() float64 {
	if r.OverallFidelityChapter > 0 {
		return r.OverallFidelityChapter
	}
	// Fallback: average of any available axis (defends against a provider
	// that only populates a subset).
	var sum float64
	var n int
	for _, v := range []float64{
		r.NarrativeCoherenceWithinChapter,
		r.SpeakerVoiceStabilityWithinChapter,
		r.TerminologyConsistencyWithinChapter,
		r.RegisterConsistencyWithinChapter,
		r.OverallFluencyChapter,
	} {
		if v > 0 {
			sum += v
			n++
		}
	}
	if n == 0 {
		return 0
	}
	return sum / float64(n)
}

// chapterJudgeToolSchema is the strict JSON Schema for emit_chapter_judge_verdict.
// Marshalled once at init() so a typo crashes immediately, not on first request.
var chapterJudgeToolSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"narrative_coherence_within_chapter": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Within this chapter only: do consecutive segments flow logically? Detect dangling discourse markers (segments starting with 'But'/'So'/'And' with no antecedent), missing connectives, contradictions. 1.0 = continuous discourse, 0.0 = disjoint.",
		},
		"speaker_voice_stability_within_chapter": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Within this chapter only: does each speaker keep one consistent voice? Multi-speaker chapters: each speaker stable individually. Single-speaker: one voice throughout. 1.0 = perfectly stable, 0.0 = sounds like the speaker swapped mid-chapter.",
		},
		"terminology_consistency_within_chapter": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Within this chapter only: are recurring proper nouns / technical terms translated consistently? List any divergent term in top_3_weakest_segments.",
		},
		"register_consistency_within_chapter": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Within this chapter only: does the speaker's tone (academic / casual / formal) stay stable? Flag jarring shifts.",
		},
		"overall_fidelity_chapter": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Aggregate semantic preservation across this chapter (NOT just each segment in isolation). 1.0 = no meaning omitted/added/distorted, 0.0 = systematically wrong meaning.",
		},
		"overall_fluency_chapter": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "How natural would this chapter sound spoken aloud end-to-end by a native target-language speaker? 1.0 = native quality, 0.0 = unintelligible / robotic.",
		},
		"top_3_weakest_segments": map[string]any{
			"type": "array",
			"description": "Up to 3 weakest segments in THIS chapter, with concrete issue + recommended fix. Empty when verdict='chapter_ready'.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"ordinal":         map[string]any{"type": "integer", "description": "1-indexed segment position within the chapter."},
					"issue":           map[string]any{"type": "string"},
					"recommended_fix": map[string]any{"type": "string"},
				},
				"required":             []string{"ordinal", "issue", "recommended_fix"},
				"additionalProperties": false,
			},
			"maxItems": 3,
		},
		"verdict": map[string]any{
			"type": "string",
			"enum": []string{"chapter_ready", "needs_revision", "needs_major_rework"},
			"description": "chapter_ready = all axes >= 0.85; needs_revision = any axis in [0.7, 0.85) OR <=2 weak segments; needs_major_rework = any axis < 0.7 OR >2 weak segments.",
		},
		"one_paragraph_summary": map[string]any{
			"type": "string",
			"description": "2-3 sentences in the target language summarising overall chapter quality and the most important fix. Optional; populate when verdict != 'chapter_ready'.",
		},
	},
	"required": []string{
		"narrative_coherence_within_chapter",
		"speaker_voice_stability_within_chapter",
		"terminology_consistency_within_chapter",
		"register_consistency_within_chapter",
		"overall_fidelity_chapter",
		"overall_fluency_chapter",
		"verdict",
	},
	"additionalProperties": false,
})

func chapterJudgeSystemPrompt(srcLang, tgtLang string) string {
	return fmt.Sprintf(
		"You are a senior dubbing localization director performing a chapter-level QA review for one chapter of a longer episode.\n\n"+
			"Source language: %s. Target language: %s.\n\n"+
			"You will receive every segment's source text + dubbed translation in order, plus optional per-segment fidelity score (0..1) and an optional episode-level reference card. "+
			"Score this CHAPTER along six axes plus a verdict, by calling the emit_chapter_judge_verdict function.\n\n"+
			"[Scoring discipline]\n"+
			"- All scores are scalars in [0, 1] (1.0 = best, 0.0 = unusable).\n"+
			"- Be strict but fair: an over-cautious verdict wastes rework budget; an over-permissive verdict ships errors.\n"+
			"- Focus on CROSS-SEGMENT properties — single-segment fidelity / fluency are already covered by the segment-level judge.\n"+
			"- Use the optional seg_judge=X.XX hints to spot internal inconsistency (a chapter with one bad segment is fine; a chapter with five 0.5 segments is not).\n\n"+
			"[Verdict mapping (apply strictly)]\n"+
			"- chapter_ready       = every axis >= 0.85 AND no segment listed in top_3_weakest_segments\n"+
			"- needs_revision      = any axis in [0.7, 0.85) OR 1-2 weak segments listed\n"+
			"- needs_major_rework  = any axis < 0.7 OR 3 weak segments listed\n\n"+
			"[top_3_weakest_segments policy]\n"+
			"- List at most 3 segments. If verdict=chapter_ready leave the array empty.\n"+
			"- For each weak segment: ordinal (the 1-indexed position WITHIN this chapter, matching the [seg{N}] tag), issue (one short concrete sentence), recommended_fix (one short actionable suggestion).\n",
		srcLang, tgtLang,
	)
}

func buildChapterJudgeUserMsg(args ChapterJudgeArgs) string {
	var b strings.Builder
	if title := strings.TrimSpace(args.ChapterTitle); title != "" {
		fmt.Fprintf(&b, "[Chapter %d title] %s\n\n", args.ChapterOrdinal, title)
	} else if args.ChapterOrdinal > 0 {
		fmt.Fprintf(&b, "[Chapter %d]\n\n", args.ChapterOrdinal)
	}
	if summary := strings.TrimSpace(args.EpisodeSummary); summary != "" {
		b.WriteString("[Episode reference card — terminology / register / register guide]\n")
		b.WriteString(summary)
		b.WriteString("\n[End of reference card]\n\n")
	}
	if hint := strings.TrimSpace(args.GlossaryHint); hint != "" {
		b.WriteString("[Episode glossary — canonical term translations]\n")
		b.WriteString(hint)
		b.WriteString("\n[End of glossary]\n\n")
	}
	b.WriteString("[All segments in this chapter, in order]\n")
	for _, seg := range args.Segments {
		durSec := float64(seg.EndMs-seg.StartMs) / 1000.0
		fmt.Fprintf(&b, "[seg%d] dur=%.1fs", seg.Ordinal, durSec)
		if seg.SegJudgeScore != nil {
			fmt.Fprintf(&b, " seg_judge=%.2f", *seg.SegJudgeScore)
		}
		fmt.Fprintf(&b, "\n  %s: %s\n  %s: %s\n",
			args.SourceLang, seg.SourceText, args.TargetLang, seg.TargetText)
	}
	b.WriteString("\nNow call emit_chapter_judge_verdict with your scores.")
	return b.String()
}

// JudgeChapter scores one whole chapter. Returns nil, nil when chapter judge
// model is not configured (judging disabled) so callers can skip silently.
// Never panics; observe-only callers should treat any non-nil error as a
// best-effort log+drop — a judge failure must NOT cause the chapter or
// episode to fail.
func (c *Client) JudgeChapter(ctx context.Context, args ChapterJudgeArgs) (*ChapterJudgeResult, error) {
	if c.chapterJudgeModel == "" {
		return nil, nil
	}
	if c.baseURL == "" || c.apiKey == "" {
		return nil, errors.New("chapter judge requires OPENAI_BASE_URL and OPENAI_API_KEY")
	}
	if len(args.Segments) == 0 {
		// Empty chapter — nothing to score; not an error.
		return nil, nil
	}

	// DashScope thinking-mode models (kimi-k2-thinking, qwen3-*-thinking)
	// reject the strict object form of tool_choice with
	// "invalid_parameter_error: tool_choice does not support being set to
	// required or object in thinking mode". Fall back to "auto" — the
	// system + user prompts are explicit enough that thinking models still
	// call the tool. Non-thinking models keep the strict force so non-tool
	// responses become a parse error rather than silent prose drift.
	// Same pattern used by OPT-405 glossary.go.
	var toolChoice any = forceToolChoice("emit_chapter_judge_verdict")
	if isThinkingModelName(c.chapterJudgeModel) {
		toolChoice = "auto"
	}

	payload := chatCompletionRequest{
		Model:       c.chapterJudgeModel,
		Temperature: 0.1, // judges should be near-deterministic
		Messages: []chatMessage{
			{Role: "system", Content: chapterJudgeSystemPrompt(args.SourceLang, args.TargetLang)},
			{Role: "user", Content: buildChapterJudgeUserMsg(args)},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_chapter_judge_verdict",
				Description: "Submit the structured chapter-level verdict for one chapter of a multi-chapter episode.",
				Parameters:  chapterJudgeToolSchema,
			},
		}},
		ToolChoice: toolChoice,
	}

	rawArgs, err := c.doChatTool(ctx, OpChapterJudge, payload, "emit_chapter_judge_verdict")
	if err != nil {
		return nil, fmt.Errorf("chapter judge tool call: %w", err)
	}
	if rawArgs == "" {
		// Provider returned content instead of tool — count as parse failure.
		// We do NOT silently fall back to content parsing here: judge results
		// must be schema-validated.
		observability.IncLLMStrictParseFailed(OpChapterJudge)
		return nil, errors.New("chapter judge: no tool call in response")
	}

	var result ChapterJudgeResult
	if err := json.Unmarshal([]byte(rawArgs), &result); err != nil {
		observability.IncLLMStrictParseFailed(OpChapterJudge)
		return nil, fmt.Errorf("chapter judge: parse tool args: %w (raw: %.200s)", err, rawArgs)
	}
	if result.Verdict == "" {
		// Schema requires verdict; if it slipped through (weak provider
		// schema enforcement), default to "needs_revision" so the chapter
		// is re-examined by an operator rather than silently accepted.
		result.Verdict = "needs_revision"
	}
	return &result, nil
}
