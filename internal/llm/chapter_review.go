// Package llm — OPT-403 chapter cut review (Pass 3).
//
// ReviewChapterCuts is the optional LLM pass that runs AFTER the deterministic
// internal/chapterize.DPOptimalCuts decides where chapter boundaries fall.
// It does two things:
//
//  1. Verifies each cut: confirms keep / suggests a ±1 silence-gap shift when
//     the cut splits a coherent paragraph (e.g. ASR put a break in the middle
//     of a question + answer). It does NOT fabricate new cuts — only nudges
//     among the candidate positions the algorithm already produced.
//
//  2. Mints a bilingual chapter title (source language + target language) and
//     a one-sentence summary for each chapter, anchored on the first ~5
//     segments and informed by the episode-level reference card from OPT-402.
//
// Why a separate file: the LLM pass is the only piece of OPT-403 that hits
// the network and depends on prompt engineering; isolating it keeps the
// chapterize package zero-dep + offline-testable while the failure-tolerance
// (LLM fail → fall back to "Chapter N" titles + DP cuts as-is) is implemented
// once here.
package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"holodub/internal/observability"
)

// ChapterCutInput describes one DP-chosen chapter that the LLM must label.
// SegmentSamples are short excerpts (first ≤5 segments by default) used as
// the anchor — the full chapter ASR text would blow the context window for
// a 30-min chapter, and DP cuts are already correctness-checked offline.
type ChapterCutInput struct {
	Ordinal           int      `json:"ordinal"`             // 1-based; what the user sees
	StartMs           int64    `json:"start_ms"`            // chapter start in episode wall-clock
	EndMs             int64    `json:"end_ms"`              // chapter end (exclusive)
	StartSegmentIdx   int      `json:"start_segment_idx"`   // first ASR segment in this chapter
	EndSegmentIdx     int      `json:"end_segment_idx"`     // last ASR segment in this chapter
	SilenceLeftMs     int64    `json:"silence_left_ms"`     // silence gap to previous chapter
	SilenceRightMs    int64    `json:"silence_right_ms"`    // silence gap to next chapter
	OpeningSegments   []string `json:"opening_segments"`    // ≤5 ASR snippets at chapter start
	ClosingSegments   []string `json:"closing_segments"`    // ≤3 ASR snippets at chapter end
}

// ChapterReviewVerdict is one LLM response for one chapter. Action is one of
// "keep" / "shift_left" / "shift_right" — keep means the boundary stays put,
// shift_left/right means the LEFT edge of THIS chapter (i.e. the cut between
// chapter-1 and this one) should be nudged to the previous/next candidate.
// The pipeline applies shifts in left-to-right order so a chain of shifts
// can compose without conflicting.
type ChapterReviewVerdict struct {
	Ordinal          int    `json:"ordinal"`
	Action           string `json:"action"`         // keep / shift_left / shift_right
	TitleSource      string `json:"title_source"`   // bilingual: source-language title
	TitleTranslated  string `json:"title_translated"` // bilingual: target-language title
	SummaryMD        string `json:"summary_md"`     // 1–2 sentence summary in target language
	Rationale        string `json:"rationale,omitempty"` // optional one-liner for shifts
}

// ChapterReviewResult is the strict tool-call output. Verdicts are 1:1 with
// the input chapters (ordered by ordinal). EpisodeTitle is an optional
// LLM-suggested overall title which the UI may use as a fallback when the
// uploader did not provide a Job.Name.
type ChapterReviewResult struct {
	Verdicts     []ChapterReviewVerdict `json:"verdicts"`
	EpisodeTitle string                 `json:"episode_title,omitempty"`
}

// chapterReviewToolSchema mirrors the glossary / judge / segment-review
// schemas — strict, additionalProperties:false, marshalled at init() so a
// typo crashes the binary at startup instead of failing at first invocation.
var chapterReviewToolSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"verdicts": map[string]any{
			"type":        "array",
			"description": "One verdict per chapter, in input order. Length MUST equal the number of input chapters.",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"ordinal": map[string]any{"type": "integer", "minimum": 1},
					"action": map[string]any{
						"type":        "string",
						"enum":        []string{"keep", "shift_left", "shift_right"},
						"description": "keep = boundary is fine; shift_left/right = nudge THIS chapter's LEFT edge to the previous/next candidate (composes left-to-right).",
					},
					"title_source": map[string]any{
						"type":        "string",
						"description": "Concise chapter title in the SOURCE language (≤30 chars).",
					},
					"title_translated": map[string]any{
						"type":        "string",
						"description": "Concise chapter title in the TARGET language (≤30 chars).",
					},
					"summary_md": map[string]any{
						"type":        "string",
						"description": "1–2 sentence summary in the TARGET language. Plain markdown OK.",
					},
					"rationale": map[string]any{"type": "string"},
				},
				"required":             []string{"ordinal", "action", "title_source", "title_translated", "summary_md"},
				"additionalProperties": false,
			},
		},
		"episode_title": map[string]any{
			"type":        "string",
			"description": "OPTIONAL overall episode title in the TARGET language (≤40 chars). Empty string is acceptable when a confident title cannot be derived from the chapter snippets.",
		},
	},
	"required":             []string{"verdicts"},
	"additionalProperties": false,
})

func chapterReviewSystemPrompt(srcLang, tgtLang string) string {
	return fmt.Sprintf(
		"You are a senior video producer reviewing the chapter break decisions for a long-form %s video that will be dubbed into %s. "+
			"A deterministic algorithm has already proposed the chapter cuts and silences; your job is to (1) confirm or nudge each cut "+
			"and (2) write a bilingual title + 1–2 sentence summary for every chapter. Call the emit_chapter_review function — do NOT respond with prose.\n\n"+
			"[Cut review]\n"+
			"- Default action is 'keep'. Only suggest 'shift_left' or 'shift_right' when the proposed cut clearly splits a coherent paragraph "+
			"(question + its answer; setup + punchline; topic + its example).\n"+
			"- Shifts move the LEFT edge of THIS chapter to the previous/next silence candidate; the algorithm composes them in order.\n"+
			"- NEVER suggest a shift for chapter 1 (its left edge is the episode start).\n\n"+
			"[Titles]\n"+
			"- Source title in %s (≤30 chars), target title in %s (≤30 chars).\n"+
			"- Avoid generic placeholders like 'Introduction' / 'Part 1' — anchor on the actual topic.\n"+
			"- If the episode reference card is provided, reuse named entities verbatim for cross-chapter consistency.\n\n"+
			"[Summary]\n"+
			"- 1–2 sentences in %s describing what THIS chapter covers. Plain markdown OK (bold, lists are NOT necessary).",
		srcLang, tgtLang, srcLang, tgtLang, tgtLang,
	)
}

// ReviewChapterCuts asks an LLM to (a) verify the DP-chosen chapter cuts and
// optionally nudge them by ±1 silence-gap and (b) mint a bilingual title +
// short summary for each chapter.
//
// referenceCardMD is optional (use Episode.ReferenceCard from OPT-402); when
// non-empty it is injected verbatim so cross-chapter named-entity / register
// consistency carries over from the episode-level glossary work.
//
// Behaviour contract — mirrors ExtractEpisodeGlossary:
//   - Returns (zero-value, nil) when chapters is empty or chapter-review LLM
//     is disabled by the caller (caller checks ChapterReviewLLMEnabled before
//     calling) — both are normal "use defaults" paths.
//   - Returns (zero-value, error) on LLM/network/parse failure — caller
//     SHOULD log and fall back to DP cuts as-is + "Chapter N" titles. The
//     pipeline NEVER fails because the LLM nudge failed.
//   - Uses cfg.ChapterReviewModel (recommended: qwen-turbo). Falls back to
//     c.model when ChapterReviewModel is empty.
func (c *Client) ReviewChapterCuts(
	ctx context.Context,
	chapters []ChapterCutInput,
	referenceCardMD string,
	srcLang, tgtLang string,
) (ChapterReviewResult, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return ChapterReviewResult{}, errors.New("chapter review requires OPENAI_BASE_URL and OPENAI_API_KEY")
	}
	if len(chapters) == 0 {
		return ChapterReviewResult{}, nil
	}

	model := c.chapterReviewModel
	if model == "" {
		model = c.model
	}
	if model == "" {
		return ChapterReviewResult{}, errors.New("chapter review requires CHAPTER_REVIEW_MODEL or OPENAI_MODEL")
	}

	var userMsg strings.Builder
	if strings.TrimSpace(referenceCardMD) != "" {
		userMsg.WriteString("[Episode reference card — reuse named entities verbatim]\n")
		userMsg.WriteString(referenceCardMD)
		userMsg.WriteString("\n[End reference card]\n\n")
	}
	userMsg.WriteString(fmt.Sprintf("[Proposed chapters: %d total. Confirm or nudge each, then title+summary each.]\n", len(chapters)))
	for _, ch := range chapters {
		userMsg.WriteString(fmt.Sprintf(
			"\n--- chapter %d  [%.1fs → %.1fs, duration %.1fs]  silence-left=%dms silence-right=%dms ---\n",
			ch.Ordinal,
			float64(ch.StartMs)/1000,
			float64(ch.EndMs)/1000,
			float64(ch.EndMs-ch.StartMs)/1000,
			ch.SilenceLeftMs,
			ch.SilenceRightMs,
		))
		if len(ch.OpeningSegments) > 0 {
			userMsg.WriteString("Opening segments:\n")
			for i, s := range ch.OpeningSegments {
				userMsg.WriteString(fmt.Sprintf("  %d. %s\n", i+1, strings.TrimSpace(s)))
			}
		}
		if len(ch.ClosingSegments) > 0 {
			userMsg.WriteString("Closing segments:\n")
			for i, s := range ch.ClosingSegments {
				userMsg.WriteString(fmt.Sprintf("  %d. %s\n", i+1, strings.TrimSpace(s)))
			}
		}
	}

	payload := chatCompletionRequest{
		Model:       model,
		Temperature: 0.2, // titles benefit from a touch of variety, but reviews must stay near-deterministic
		Messages: []chatMessage{
			{Role: "system", Content: chapterReviewSystemPrompt(srcLang, tgtLang)},
			{Role: "user", Content: userMsg.String()},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_chapter_review",
				Description: "Submit the per-chapter review verdict + bilingual title + summary for the proposed cuts.",
				Parameters:  chapterReviewToolSchema,
			},
		}},
		ToolChoice: forceToolChoice("emit_chapter_review"),
	}

	rawArgs, err := c.doChatTool(ctx, OpChapterReview, payload, "emit_chapter_review")
	if err != nil {
		return ChapterReviewResult{}, fmt.Errorf("chapter review tool call: %w", err)
	}
	if rawArgs == "" {
		observability.IncLLMStrictParseFailed(OpChapterReview)
		return ChapterReviewResult{}, errors.New("chapter review: no tool call in response")
	}

	var result ChapterReviewResult
	if err := json.Unmarshal([]byte(rawArgs), &result); err != nil {
		observability.IncLLMStrictParseFailed(OpChapterReview)
		return ChapterReviewResult{}, fmt.Errorf("chapter review: parse tool args: %w (raw: %.200s)", err, rawArgs)
	}

	// Defensive validation: verdicts MUST be 1:1 with input chapters and
	// in the same ordinal sequence. The LLM occasionally drops the last
	// verdict on long inputs; we treat that as a parse-level failure so
	// the caller falls back to defaults rather than risk mismatched titles.
	if len(result.Verdicts) != len(chapters) {
		observability.IncLLMStrictParseFailed(OpChapterReview)
		return ChapterReviewResult{}, fmt.Errorf(
			"chapter review: expected %d verdicts, got %d", len(chapters), len(result.Verdicts))
	}
	for i, v := range result.Verdicts {
		if v.Ordinal != chapters[i].Ordinal {
			observability.IncLLMStrictParseFailed(OpChapterReview)
			return ChapterReviewResult{}, fmt.Errorf(
				"chapter review: verdict[%d].ordinal=%d does not match chapter[%d].ordinal=%d",
				i, v.Ordinal, i, chapters[i].Ordinal)
		}
		switch v.Action {
		case "keep", "shift_left", "shift_right":
		default:
			observability.IncLLMStrictParseFailed(OpChapterReview)
			return ChapterReviewResult{}, fmt.Errorf(
				"chapter review: verdict[%d].action=%q is not in {keep,shift_left,shift_right}",
				i, v.Action)
		}
	}
	return result, nil
}
