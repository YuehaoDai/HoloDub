// Package llm — OPT-406 episode-level Judge (productized).
//
// JudgeEpisode asks an LLM to score one entire Episode (= the concatenation
// of every chapter Job under it, see OPT-401 / 403 / 404) on cross-chapter
// dimensions that segment-level OPT-002 and chapter-level OPT-409 judges
// cannot see:
//
//   - terminology_consistency           (cross-chapter glossary drift)
//   - register_consistency              (academic / casual / formal stays stable)
//   - narrative_coherence               (full-episode discourse flow)
//   - character_voice_stability         (one speaker keeps one voice across chapters)
//   - cultural_localization             (idioms / units / examples adapted)
//   - overall_fidelity                  (aggregate semantic preservation)
//   - overall_fluency                   (end-to-end native-speaker test)
//
// Plus two top-3 weakest lists — top_3_weakest_chapters (which whole
// chapters need rework) AND top_3_weakest_segments (chapter_ordinal +
// ordinal pinpointing) — so OPT-407 closed-loop rework can dispatch
// chapter-level OR segment-level retranslation precisely.
//
// The MVP runs in observe-only mode (EPISODE_JUDGE_OBSERVE_ONLY=true): the
// score is persisted on episodes.episode_judge_score / episode_judge_meta
// but does NOT influence anything else. Decision wiring lives in OPT-407.
//
// Why a separate file: episode judge has a stable contract independent of
// the chapter judge AND independent of the segment judge. Keeping it
// isolated makes it trivial for OPT-407 to build a multi-level rework
// decision table on top of the three judge layers.
//
// Mirrors the pattern proven in chapter_judge.go: strict tool schema,
// thinking-model tool_choice fallback via isThinkingModelName, default-
// verdict on missing-verdict slip-through. The only structural deltas vs
// chapter judge are:
//   - 7 axes vs 6 (adds character_voice_stability + cultural_localization)
//   - 2 weakest arrays vs 1 (chapters + segments vs segments only)
//   - top-level glossary observation array (cross-chapter terminology drift)
//   - different verdict vocabulary (production_ready vs chapter_ready) so
//     the OPT-407 decision table can fan out into different actions.
package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"holodub/internal/observability"
)

// EpisodeJudgeChapterRow is one chapter overview entry fed into JudgeEpisode.
// Ordinal is 1-indexed across the episode. ChapterJudgeScore (when populated)
// gives the LLM a per-chapter signal so it can correlate cross-chapter drift
// with internal chapter quality.
type EpisodeJudgeChapterRow struct {
	Ordinal           int
	Title             string
	TitleTranslated   string
	StartMs           int64
	EndMs             int64
	ChapterJudgeScore *float64 // optional OPT-409 chapter-level score
	SummaryMD         string   // optional one-line synopsis (e.g. ChapterSummaryMD)
}

// EpisodeJudgeSegment is one (src, tgt) pair fed into JudgeEpisode.
//
// ChapterOrdinal is the 1-indexed parent chapter position; the LLM uses it
// in top_3_weakest_segments so OPT-407 can locate the segment without an
// ambiguous global ordinal lookup.
//
// SegJudgeScore is the optional OPT-002 segment score; populated when
// segment-level judging was enabled and ran successfully on this segment.
type EpisodeJudgeSegment struct {
	ChapterOrdinal int
	Ordinal        int
	StartMs        int64
	EndMs          int64
	SourceText     string
	TargetText     string
	SegJudgeScore  *float64
}

// EpisodeJudgeArgs is the input to one episode judge call.
//
// EpisodeSummary is typically Episode.ReferenceCard, providing register +
// glossary + named-entity context. GlossaryHint is the optional formatted
// canonical term table from Episode.Glossary (OPT-402). Both let the judge
// score terminology / register without re-deriving them from the raw text.
type EpisodeJudgeArgs struct {
	SourceLang     string
	TargetLang     string
	EpisodeID      uint
	EpisodeName    string
	EpisodeSummary string
	GlossaryHint   string
	Chapters       []EpisodeJudgeChapterRow
	Segments       []EpisodeJudgeSegment
}

// EpisodeJudgeWeakChapter is one entry in the top-3-weakest-chapters list
// — pinpoints a whole chapter that needs rework with a concrete fix hint.
type EpisodeJudgeWeakChapter struct {
	Ordinal        int    `json:"ordinal"`
	Issue          string `json:"issue"`
	RecommendedFix string `json:"recommended_fix"`
}

// EpisodeJudgeWeakSegment is one entry in the top-3-weakest-segments list.
// ChapterOrdinal + Ordinal jointly identify the segment so OPT-407 can
// dispatch a precise segment-level retranslate.
type EpisodeJudgeWeakSegment struct {
	ChapterOrdinal int    `json:"chapter_ordinal"`
	Ordinal        int    `json:"ordinal"`
	Issue          string `json:"issue"`
	RecommendedFix string `json:"recommended_fix"`
}

// EpisodeJudgeGlossaryEntry is one observed cross-chapter glossary item:
// what source term the judge saw and what target term won out across the
// episode. The note column flags inconsistent / divergent translations
// detected within the episode.
type EpisodeJudgeGlossaryEntry struct {
	SourceTerm string `json:"source_term"`
	TargetTerm string `json:"target_term"`
	Note       string `json:"note,omitempty"`
}

// EpisodeJudgeResult is the structured verdict returned by the episode
// judge LLM. All seven axis scores are 0..1 scalars (1 = best). Verdict is
// one of "production_ready" | "needs_minor_revision" | "needs_major_revision";
// observe-only mode logs but does not act on it (OPT-407 will).
type EpisodeJudgeResult struct {
	TerminologyConsistency     float64 `json:"terminology_consistency"`
	RegisterConsistency        float64 `json:"register_consistency"`
	NarrativeCoherence         float64 `json:"narrative_coherence"`
	CharacterVoiceStability    float64 `json:"character_voice_stability"`
	CulturalLocalization       float64 `json:"cultural_localization"`
	OverallFidelity            float64 `json:"overall_fidelity"`
	OverallFluency             float64 `json:"overall_fluency"`
	Top3WeakestChapters        []EpisodeJudgeWeakChapter   `json:"top_3_weakest_chapters,omitempty"`
	Top3WeakestSegments        []EpisodeJudgeWeakSegment   `json:"top_3_weakest_segments,omitempty"`
	TerminologyGlossaryObserved []EpisodeJudgeGlossaryEntry `json:"terminology_glossary_observed,omitempty"`
	Verdict                    string `json:"verdict"`
	OneParagraphSummary        string `json:"one_paragraph_summary,omitempty"`
}

// OverallScore returns a single scalar 0..1 used as Episode.EpisodeJudgeScore.
//
// Currently equal to OverallFidelity (the single most decision-relevant
// axis when OPT-407 wires up rework). Kept as a method so future changes
// (e.g. weighted average across the 7 axes) only touch one place.
func (r EpisodeJudgeResult) OverallScore() float64 {
	if r.OverallFidelity > 0 {
		return r.OverallFidelity
	}
	// Fallback: average of any available axis (defends against a provider
	// that only populates a subset).
	var sum float64
	var n int
	for _, v := range []float64{
		r.TerminologyConsistency,
		r.RegisterConsistency,
		r.NarrativeCoherence,
		r.CharacterVoiceStability,
		r.CulturalLocalization,
		r.OverallFluency,
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

// episodeJudgeToolSchema is the strict JSON Schema for emit_episode_judge_verdict.
// Marshalled once at init() so a typo crashes immediately, not on first request.
var episodeJudgeToolSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"terminology_consistency": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Across the WHOLE episode (every chapter): are recurring proper nouns / technical terms translated consistently? Detect a term translated one way in chapter 2 and a different way in chapter 5. List each divergent term in terminology_glossary_observed with a note.",
		},
		"register_consistency": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Across the WHOLE episode: does the speaker's tone (academic / casual / formal) stay stable, or does it jump (e.g. lecture mode in chapter 1 → chatty in chapter 3)?",
		},
		"narrative_coherence": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "End-to-end discourse flow: do chapters connect logically? Detect dangling discourse markers across chapter boundaries (chapter 4 starts with 'But' with no antecedent in chapter 3), missing connectives, contradictions.",
		},
		"character_voice_stability": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Multi-speaker episodes: does each speaker keep one consistent voice across chapters? Single-speaker: does the lecturer / narrator sound like the SAME person from start to end?",
		},
		"cultural_localization": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Are idioms, examples, units, cultural references adapted appropriately for the target-language audience without losing source meaning?",
		},
		"overall_fidelity": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "Aggregate semantic preservation across the WHOLE episode (NOT just each segment in isolation). 1.0 = no meaning omitted/added/distorted, 0.0 = systematically wrong meaning.",
		},
		"overall_fluency": map[string]any{
			"type": "number", "minimum": 0, "maximum": 1,
			"description": "How natural would this episode sound spoken aloud end-to-end by a native target-language speaker? 1.0 = native quality, 0.0 = unintelligible / robotic.",
		},
		"top_3_weakest_chapters": map[string]any{
			"type": "array",
			"description": "Up to 3 weakest chapters in this episode (whole-chapter rework candidates). Empty when verdict='production_ready'.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"ordinal":         map[string]any{"type": "integer", "description": "1-indexed chapter ordinal."},
					"issue":           map[string]any{"type": "string"},
					"recommended_fix": map[string]any{"type": "string"},
				},
				"required":             []string{"ordinal", "issue", "recommended_fix"},
				"additionalProperties": false,
			},
			"maxItems": 3,
		},
		"top_3_weakest_segments": map[string]any{
			"type": "array",
			"description": "Up to 3 weakest INDIVIDUAL segments across the entire episode (segment-level rework candidates). Each entry MUST include chapter_ordinal + ordinal so the segment can be located unambiguously. Empty when verdict='production_ready'.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"chapter_ordinal": map[string]any{"type": "integer", "description": "1-indexed parent chapter ordinal."},
					"ordinal":         map[string]any{"type": "integer", "description": "1-indexed segment position WITHIN that chapter, matching the [c{C}.s{N}] tag."},
					"issue":           map[string]any{"type": "string"},
					"recommended_fix": map[string]any{"type": "string"},
				},
				"required":             []string{"chapter_ordinal", "ordinal", "issue", "recommended_fix"},
				"additionalProperties": false,
			},
			"maxItems": 3,
		},
		"terminology_glossary_observed": map[string]any{
			"type": "array",
			"description": "Cross-chapter terminology actually observed. For each recurring term: source_term, the target_term that won out across the episode, and an optional note flagging inconsistent or contested translations.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"source_term": map[string]any{"type": "string"},
					"target_term": map[string]any{"type": "string"},
					"note":        map[string]any{"type": "string"},
				},
				"required":             []string{"source_term", "target_term"},
				"additionalProperties": false,
			},
		},
		"verdict": map[string]any{
			"type": "string",
			"enum": []string{"production_ready", "needs_minor_revision", "needs_major_revision"},
			"description": "production_ready = every axis >= 0.9 AND no weakest entries; needs_minor_revision = any axis in [0.8, 0.9) OR 1-2 weak entries (chapters or segments combined); needs_major_revision = any axis < 0.8 OR 3 weak entries.",
		},
		"one_paragraph_summary": map[string]any{
			"type": "string",
			"description": "3-4 sentences in the target language summarising overall quality and the most important fix. Optional; populate when verdict != 'production_ready'.",
		},
	},
	"required": []string{
		"terminology_consistency",
		"register_consistency",
		"narrative_coherence",
		"character_voice_stability",
		"cultural_localization",
		"overall_fidelity",
		"overall_fluency",
		"verdict",
	},
	"additionalProperties": false,
})

func episodeJudgeSystemPrompt(srcLang, tgtLang string) string {
	return fmt.Sprintf(
		"You are a senior dubbing localization director performing a final episode-level QA review of a multi-chapter dubbed video.\n\n"+
			"Source language: %s. Target language: %s.\n\n"+
			"You will receive: (a) an episode reference card (register / glossary / characters), (b) the canonical episode glossary if available, (c) a chapter overview with each chapter's title and OPT-409 chapter-judge score when populated, (d) every segment in order with its parent chapter ordinal, source text + dubbed translation, and optional segment-level fidelity score. "+
			"Score this EPISODE on seven axes plus a verdict by calling the emit_episode_judge_verdict function.\n\n"+
			"[Scoring discipline]\n"+
			"- All scores are scalars in [0, 1] (1.0 = best, 0.0 = unusable).\n"+
			"- Be strict but fair: an over-cautious verdict wastes rework budget; an over-permissive verdict ships errors.\n"+
			"- Focus on CROSS-CHAPTER properties — single-segment fidelity / fluency are already covered by the segment-level judge, and within-chapter coherence is already covered by the chapter-level judge. Your job is what BOTH of them miss.\n"+
			"- Use the per-segment seg_judge=X.XX hints AND the per-chapter chapter_judge=X.XX hints to spot internal inconsistency: an episode whose chapters all score 0.95 individually but where terminology drifts between them is NOT production_ready.\n\n"+
			"[Verdict mapping (apply strictly)]\n"+
			"- production_ready       = every axis >= 0.9 AND zero weak chapters AND zero weak segments\n"+
			"- needs_minor_revision   = any axis in [0.8, 0.9) OR 1-2 weak entries (chapters + segments combined)\n"+
			"- needs_major_revision   = any axis < 0.8 OR 3+ weak entries\n\n"+
			"[Top-3 weakest policy]\n"+
			"- top_3_weakest_chapters: up to 3 entries, list a WHOLE CHAPTER when the rework target is the chapter as a unit (terminology drift, character voice broken, etc.).\n"+
			"- top_3_weakest_segments: up to 3 entries, list INDIVIDUAL SEGMENTS when the rework target is one specific line (each must include chapter_ordinal + ordinal pinpointing).\n"+
			"- It is fine for one episode to have entries in both lists; together they should not exceed ~5 actionable items so OPT-407 closed-loop rework stays tractable.\n"+
			"- If verdict=production_ready leave both arrays empty.\n\n"+
			"[Glossary observation]\n"+
			"- Populate terminology_glossary_observed with the recurring terms you saw across multiple chapters.\n"+
			"- For terms whose translation drifted between chapters, set note to a one-sentence flag (e.g. \"chapter 2 used X, chapter 5 used Y\"). Drifted terms are the primary OPT-407 broadcast_glossary_update trigger.\n",
		srcLang, tgtLang,
	)
}

func buildEpisodeJudgeUserMsg(args EpisodeJudgeArgs) string {
	var b strings.Builder
	if name := strings.TrimSpace(args.EpisodeName); name != "" {
		fmt.Fprintf(&b, "[Episode] %s (id=%d, %d chapters)\n\n", name, args.EpisodeID, len(args.Chapters))
	} else if args.EpisodeID > 0 {
		fmt.Fprintf(&b, "[Episode id=%d, %d chapters]\n\n", args.EpisodeID, len(args.Chapters))
	}
	if summary := strings.TrimSpace(args.EpisodeSummary); summary != "" {
		b.WriteString("[Episode reference card — register / characters / topic]\n")
		b.WriteString(summary)
		b.WriteString("\n[End of reference card]\n\n")
	}
	if hint := strings.TrimSpace(args.GlossaryHint); hint != "" {
		b.WriteString("[Episode glossary — canonical term translations]\n")
		b.WriteString(hint)
		b.WriteString("\n[End of glossary]\n\n")
	}
	if len(args.Chapters) > 0 {
		b.WriteString("[Chapter overview]\n")
		for _, ch := range args.Chapters {
			fmt.Fprintf(&b, "  c%d", ch.Ordinal)
			if title := strings.TrimSpace(ch.Title); title != "" {
				fmt.Fprintf(&b, " · %s", title)
			}
			if t := strings.TrimSpace(ch.TitleTranslated); t != "" {
				fmt.Fprintf(&b, " (%s)", t)
			}
			if dur := float64(ch.EndMs-ch.StartMs) / 1000.0; dur > 0 {
				fmt.Fprintf(&b, " · %.0fs", dur)
			}
			if ch.ChapterJudgeScore != nil {
				fmt.Fprintf(&b, " · chapter_judge=%.2f", *ch.ChapterJudgeScore)
			}
			if synopsis := strings.TrimSpace(ch.SummaryMD); synopsis != "" {
				fmt.Fprintf(&b, " · %s", synopsis)
			}
			b.WriteString("\n")
		}
		b.WriteString("\n")
	}
	b.WriteString("[All segments across all chapters, in order]\n")
	for _, seg := range args.Segments {
		durSec := float64(seg.EndMs-seg.StartMs) / 1000.0
		fmt.Fprintf(&b, "[c%d.s%d] dur=%.1fs", seg.ChapterOrdinal, seg.Ordinal, durSec)
		if seg.SegJudgeScore != nil {
			fmt.Fprintf(&b, " seg_judge=%.2f", *seg.SegJudgeScore)
		}
		fmt.Fprintf(&b, "\n  %s: %s\n  %s: %s\n",
			args.SourceLang, seg.SourceText, args.TargetLang, seg.TargetText)
	}
	b.WriteString("\nNow call emit_episode_judge_verdict with your scores.")
	return b.String()
}

// JudgeEpisode scores one whole episode. Returns nil, nil when episode
// judge model is not configured (judging disabled) so callers can skip
// silently. Never panics; observe-only callers should treat any non-nil
// error as a best-effort log+drop — a judge failure must NOT cause the
// episode merge to fail.
func (c *Client) JudgeEpisode(ctx context.Context, args EpisodeJudgeArgs) (*EpisodeJudgeResult, error) {
	if c.episodeJudgeModel == "" {
		return nil, nil
	}
	if c.baseURL == "" || c.apiKey == "" {
		return nil, errors.New("episode judge requires OPENAI_BASE_URL and OPENAI_API_KEY")
	}
	if len(args.Segments) == 0 {
		// Empty episode — nothing to score; not an error.
		return nil, nil
	}

	// DashScope thinking-mode models reject the strict object form of
	// tool_choice; fall back to "auto". Same pattern as chapter_judge.go
	// and OPT-405 glossary.go.
	var toolChoice any = forceToolChoice("emit_episode_judge_verdict")
	if isThinkingModelName(c.episodeJudgeModel) {
		toolChoice = "auto"
	}

	payload := chatCompletionRequest{
		Model:       c.episodeJudgeModel,
		Temperature: 0.1, // judges should be near-deterministic
		Messages: []chatMessage{
			{Role: "system", Content: episodeJudgeSystemPrompt(args.SourceLang, args.TargetLang)},
			{Role: "user", Content: buildEpisodeJudgeUserMsg(args)},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_episode_judge_verdict",
				Description: "Submit the structured episode-level verdict for one multi-chapter dubbed episode.",
				Parameters:  episodeJudgeToolSchema,
			},
		}},
		ToolChoice: toolChoice,
	}

	rawArgs, err := c.doChatTool(ctx, OpEpisodeJudge, payload, "emit_episode_judge_verdict")
	if err != nil {
		return nil, fmt.Errorf("episode judge tool call: %w", err)
	}
	if rawArgs == "" {
		// Provider returned content instead of tool — count as parse failure.
		// We do NOT silently fall back to content parsing here: judge results
		// must be schema-validated. (response_format=json_object fallback
		// is OPT-406-followup-3 if we ever see this in production.)
		observability.IncLLMStrictParseFailed(OpEpisodeJudge)
		return nil, errors.New("episode judge: no tool call in response")
	}

	var result EpisodeJudgeResult
	if err := json.Unmarshal([]byte(rawArgs), &result); err != nil {
		observability.IncLLMStrictParseFailed(OpEpisodeJudge)
		return nil, fmt.Errorf("episode judge: parse tool args: %w (raw: %.200s)", err, rawArgs)
	}
	if result.Verdict == "" {
		// Schema requires verdict; if it slipped through (weak provider
		// schema enforcement), default to "needs_minor_revision" so an
		// operator re-examines the episode rather than silently treating
		// it as production_ready.
		result.Verdict = "needs_minor_revision"
	}
	return &result, nil
}
