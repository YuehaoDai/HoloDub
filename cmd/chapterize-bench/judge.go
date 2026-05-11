// judge.go — LLM-as-judge for cmd/chapterize-bench.
//
// Given one CANDIDATE chapter plan (the "best" run for a given model)
// and the FULL ASR transcript, we ask a separate JUDGE model to score
// the plan along three axes per chapter, plus per-boundary coherence.
//
// Why three axes:
//
//   - boundary_coherence (per cut, 0..5): does the cut land at a real
//     theme transition? A 5 means the speaker explicitly pivots between
//     the last segment of chapter N and the first of chapter N+1 ("OK,
//     next we'll talk about X"); a 0 means the cut splits one sentence
//     of one ongoing argument.
//
//   - title_quality (per chapter, 0..5): does the bilingual title
//     accurately describe what the chapter actually covers, in ≤80 chars?
//     A 5 is concise + accurate + descriptive; 0 is wrong / generic /
//     literally the first words.
//
//   - topic_completeness (per chapter, 0..5): does the chapter contain
//     ONE coherent theme from start to end (5), or does it spill the
//     conclusion into the next chapter / start mid-topic (0)?
//
// We use ONE judge model for ALL candidates so the rankings are on the
// same yardstick. Temperature is fixed at 0.0 for repeatability. The
// schema is structured so the response is one tool call carrying both
// per-boundary and per-chapter score arrays — a single round-trip.
//
// Thinking models (kimi-k2-thinking, qwen3-*-thinking) only support
// tool_choice=auto; for those we DO NOT force the tool. The judge prompt
// repeats the "you MUST call score_chapter_cuts" instruction inline so
// the model still calls the tool. If a thinking model returns prose
// instead, we record an empty verdict — the operator sees that in the
// report and can fall back to a non-thinking judge.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"holodub/internal/chapterize"
	"holodub/internal/config"
	"holodub/internal/llm"
	"holodub/internal/models"
)

// JudgeResult captures everything the report needs about the judge pass
// for one candidate model. Per-axis means (boundary / title / topic) and
// the composite Total are what the ranking table shows; the per-chapter
// + per-boundary detail arrays preserve the verdict for human review of
// suspicious entries.
type JudgeResult struct {
	CandidateModel string `json:"candidate_model"`
	JudgeModel     string `json:"judge_model"`
	Runs           int    `json:"runs"`
	WallTimeMs     int64  `json:"wall_time_ms"`
	Error          string `json:"error,omitempty"`

	BoundaryScores []BoundaryScore `json:"boundary_scores"`
	ChapterScores  []ChapterScore  `json:"chapter_scores"`

	// Aggregated 0..5 averages. -1 means N/A (no scores collected).
	BoundaryAvg float64 `json:"boundary_avg"`
	TitleAvg    float64 `json:"title_avg"`
	TopicAvg    float64 `json:"topic_avg"`
	Total       float64 `json:"total"` // weighted = (boundary + title + topic) / 3 * (count / count)
}

// BoundaryScore is one verdict on the cut between chapters N and N+1.
// BoundaryIdx is 0-based and refers to the cut AFTER chapter N (so
// boundary 0 sits between the 1st and 2nd chapters).
type BoundaryScore struct {
	BoundaryIdx       int    `json:"boundary_idx"`
	BoundaryCoherence int    `json:"boundary_coherence"`
	Rationale         string `json:"rationale"`
}

// ChapterScore is one verdict on chapter N (1-based ordinal).
type ChapterScore struct {
	ChapterIdx        int    `json:"chapter_idx"` // 0-based chapter index
	TitleQuality      int    `json:"title_quality"`
	TopicCompleteness int    `json:"topic_completeness"`
	Rationale         string `json:"rationale"`
}

// runJudge issues judgeRuns calls to the judge model and averages the
// numerical fields (boundary / title / topic). Per-chapter rationales
// from the FIRST run are kept in the result — averaging strings is
// meaningless, and the first run is enough for spot checks.
func runJudge(
	ctx context.Context,
	cfg config.Config,
	ep *models.Episode,
	segments []llm.EpisodeSegment,
	candidateModel string,
	best ExtractRun,
	judgeModel string,
	judgeRuns int,
) JudgeResult {
	out := JudgeResult{
		CandidateModel: candidateModel,
		JudgeModel:     judgeModel,
		Runs:           judgeRuns,
		BoundaryAvg:    -1,
		TitleAvg:       -1,
		TopicAvg:       -1,
		Total:          -1,
	}

	if len(best.FinalRanges) == 0 {
		out.Error = "candidate produced no final ranges to judge"
		return out
	}
	if judgeRuns < 1 {
		judgeRuns = 1
	}

	candidateCfg := cfg
	candidateCfg.GlossaryModel = judgeModel
	client := llm.New(candidateCfg)

	// The judge transcript reuses the SAME indexing the candidate model
	// saw, so the chapter [start, end] segment indices line up with [N]
	// tags in the user message. This is critical: if we re-renumbered
	// here we'd have to translate every chapter index too, and any
	// off-by-one would silently bias the verdict.
	transcript := buildJudgeUserMsg(segments, best.FinalRanges, best.FinalTitles, ep)

	forceTool := !isThinkingModel(judgeModel)

	startedAll := time.Now()
	var (
		boundarySum   = make(map[int]int)
		boundaryCount = make(map[int]int)
		titleSum      = make(map[int]int)
		titleCount    = make(map[int]int)
		topicSum      = make(map[int]int)
		topicCount    = make(map[int]int)
		firstBoundary []BoundaryScore
		firstChapter  []ChapterScore
	)

	for run := 0; run < judgeRuns; run++ {
		args, err := client.RunBenchToolCall(
			ctx,
			"bench_chapter_judge",
			judgeModel,
			0.0, // judge: deterministic
			judgeSystemPrompt(ep.SourceLanguage, ep.TargetLanguage),
			transcript,
			"score_chapter_cuts",
			"Submit per-boundary coherence + per-chapter title/topic scores for the candidate chapterization.",
			scoreToolSchema,
			forceTool,
		)
		if err != nil {
			out.Error = fmt.Sprintf("judge run %d: %v", run+1, err)
			break
		}
		if args.Args == "" {
			out.Error = fmt.Sprintf("judge run %d: model returned no tool call (often happens with thinking models on tool_choice=auto)", run+1)
			break
		}
		var verdict struct {
			BoundaryScores []BoundaryScore `json:"boundary_scores"`
			ChapterScores  []ChapterScore  `json:"chapter_scores"`
		}
		if err := json.Unmarshal([]byte(args.Args), &verdict); err != nil {
			out.Error = fmt.Sprintf("judge run %d: parse args: %v (raw: %.200s)", run+1, err, args.Args)
			break
		}
		if run == 0 {
			firstBoundary = verdict.BoundaryScores
			firstChapter = verdict.ChapterScores
		}
		for _, b := range verdict.BoundaryScores {
			boundarySum[b.BoundaryIdx] += b.BoundaryCoherence
			boundaryCount[b.BoundaryIdx]++
		}
		for _, ch := range verdict.ChapterScores {
			titleSum[ch.ChapterIdx] += ch.TitleQuality
			titleCount[ch.ChapterIdx]++
			topicSum[ch.ChapterIdx] += ch.TopicCompleteness
			topicCount[ch.ChapterIdx]++
		}
	}
	out.WallTimeMs = time.Since(startedAll).Milliseconds()

	// Build averaged per-boundary + per-chapter score arrays, preferring
	// rationales from the first run (averaging text is nonsensical).
	for _, b := range firstBoundary {
		if boundaryCount[b.BoundaryIdx] == 0 {
			continue
		}
		out.BoundaryScores = append(out.BoundaryScores, BoundaryScore{
			BoundaryIdx:       b.BoundaryIdx,
			BoundaryCoherence: boundarySum[b.BoundaryIdx] / boundaryCount[b.BoundaryIdx],
			Rationale:         b.Rationale,
		})
	}
	for _, ch := range firstChapter {
		if titleCount[ch.ChapterIdx] == 0 {
			continue
		}
		out.ChapterScores = append(out.ChapterScores, ChapterScore{
			ChapterIdx:        ch.ChapterIdx,
			TitleQuality:      titleSum[ch.ChapterIdx] / titleCount[ch.ChapterIdx],
			TopicCompleteness: topicSum[ch.ChapterIdx] / topicCount[ch.ChapterIdx],
			Rationale:         ch.Rationale,
		})
	}

	// Aggregate to scalars used in the leaderboard.
	bSum, bCount := 0.0, 0.0
	for _, b := range out.BoundaryScores {
		bSum += float64(b.BoundaryCoherence)
		bCount++
	}
	tlSum, tlCount := 0.0, 0.0
	tpSum, tpCount := 0.0, 0.0
	for _, ch := range out.ChapterScores {
		tlSum += float64(ch.TitleQuality)
		tlCount++
		tpSum += float64(ch.TopicCompleteness)
		tpCount++
	}
	if bCount > 0 {
		out.BoundaryAvg = bSum / bCount
	}
	if tlCount > 0 {
		out.TitleAvg = tlSum / tlCount
	}
	if tpCount > 0 {
		out.TopicAvg = tpSum / tpCount
	}
	if bCount > 0 || tlCount > 0 || tpCount > 0 {
		// Composite: equal weight to the three axes. Missing axes
		// (e.g. 1-chapter plan has no boundaries) contribute 0 weight,
		// not 0 score, so a 1-chapter plan isn't auto-penalised.
		num, den := 0.0, 0.0
		if bCount > 0 {
			num += out.BoundaryAvg
			den += 1
		}
		if tlCount > 0 {
			num += out.TitleAvg
			den += 1
		}
		if tpCount > 0 {
			num += out.TopicAvg
			den += 1
		}
		if den > 0 {
			out.Total = num / den
		}
	}

	return out
}

// judgeSystemPrompt is the judge's instruction set. Stable across all
// candidates so the verdicts are comparable. We deliberately tell the
// model that "the candidate may have made mistakes" so it doesn't go
// soft on a clearly bad cut — judge LLMs default to charity.
func judgeSystemPrompt(srcLang, tgtLang string) string {
	return fmt.Sprintf(
		"You are an expert localisation editor evaluating an automatic chapterization of a long video transcript. "+
			"The transcript is in %s, the localised titles are in %s. "+
			"You will be given (a) the FULL ASR transcript with [idx] tags and (b) one CANDIDATE chapter plan. "+
			"Score each cut and each chapter on a 0..5 integer scale and submit your verdict via the score_chapter_cuts tool — "+
			"do NOT respond with prose; the tool call is mandatory.\n\n"+
			"Scoring rubric (0=poor, 5=perfect):\n"+
			"\n"+
			"boundary_coherence (per cut between consecutive chapters):\n"+
			"  5: the speaker explicitly pivots between the last segment of chapter N and the first of N+1 "+
			"(e.g. \"OK, that's it for X. Let's now move on to Y\"). The cut clearly separates two themes.\n"+
			"  3: a topic shift is implied but not explicitly telegraphed; the cut is reasonable but a careful editor might place it ±1–2 segments away.\n"+
			"  1: the cut breaks an ongoing argument; the conclusion of one theme spills into the next chapter, or one theme starts mid-sentence in the previous one.\n"+
			"  0: the cut is in the middle of a single sentence / single bullet point.\n"+
			"\n"+
			"title_quality (per chapter):\n"+
			"  5: ≤80 chars in BOTH languages, accurately summarises the chapter's actual content, descriptive (not just literal first words).\n"+
			"  3: roughly accurate but generic, e.g. \"Course Introduction\" when the chapter is specifically about lab grading rules.\n"+
			"  1: title only describes a small fraction of the chapter, OR is over the 80-char cap, OR is a literal transcript of the first sentence.\n"+
			"  0: title is wrong or unrelated to the actual content.\n"+
			"\n"+
			"topic_completeness (per chapter):\n"+
			"  5: the chapter covers ONE coherent theme from start to end; no leakage from / into neighbours.\n"+
			"  3: mostly one theme but contains a brief unrelated aside that a careful editor would split out.\n"+
			"  1: the chapter mixes two themes, OR cuts off the theme partway through (the conclusion is in the next chapter).\n"+
			"  0: the chapter has no coherent theme at all.\n"+
			"\n"+
			"Be HONEST and STRICT — most automatic chapterizers make real mistakes; charity hides those. "+
			"Provide a 1-sentence rationale for each score so the operator can sanity-check your verdicts.",
		srcLang, tgtLang,
	)
}

// buildJudgeUserMsg renders the full transcript + candidate chapter
// plan into the judge's user message. Same [idx] tagging as the
// extract prompt so chapter [start, end] indices map straight into
// segment positions.
func buildJudgeUserMsg(
	segments []llm.EpisodeSegment,
	ranges []chapterize.ChapterRange,
	meta []chapterize.LLMChapterMeta,
	ep *models.Episode,
) string {
	var sb strings.Builder
	fmt.Fprintf(&sb,
		"[Episode: %q | source language: %s | target language: %s | total segments: %d]\n",
		ep.Name, ep.SourceLanguage, ep.TargetLanguage, len(segments),
	)

	sb.WriteString("\n## CANDIDATE CHAPTER PLAN (the thing you are scoring)\n")
	for i, r := range ranges {
		titleSrc := ""
		titleTgt := ""
		summary := ""
		if i < len(meta) {
			titleSrc = meta[i].TitleSource
			titleTgt = meta[i].TitleTranslated
			summary = meta[i].SummaryMD
		}
		fmt.Fprintf(&sb,
			"chapter %d: segments [%d..%d]  (%s — %s, %.1fmin)\n  title_source: %s\n  title_translated: %s\n  summary: %s\n",
			i+1, r.StartSegmentIdx, r.EndSegmentIdx,
			formatMMSS(r.StartMs), formatMMSS(r.EndMs),
			float64(r.EndMs-r.StartMs)/60000.0,
			titleSrc, titleTgt, summary,
		)
	}

	sb.WriteString("\n## FULL ASR TRANSCRIPT (use [idx] tags to verify chapter boundaries)\n")
	for i, s := range segments {
		text := strings.TrimSpace(s.Text)
		if text == "" {
			continue
		}
		spk := ""
		if s.SpeakerLabel != "" {
			spk = s.SpeakerLabel + ": "
		}
		fmt.Fprintf(&sb, "[%d] %s-%s %s%s\n",
			i, formatMMSS(s.StartMs), formatMMSS(s.EndMs), spk, text)
	}
	sb.WriteString("[End of transcript]\n")

	sb.WriteString("\n## TASK\n" +
		"Score this candidate via the score_chapter_cuts tool.\n" +
		"- For each cut between consecutive chapters (chapters 1→2, 2→3, …): emit one boundary_scores entry with boundary_idx = cut index (0-based), boundary_coherence in 0..5, and a 1-sentence rationale.\n" +
		"- For each chapter: emit one chapter_scores entry with chapter_idx = chapter index (0-based), title_quality in 0..5, topic_completeness in 0..5, and a 1-sentence rationale.\n" +
		"- Do not return any prose; respond ONLY via the tool call.")
	return sb.String()
}

// scoreToolSchema is the strict schema for the judge's tool call. The
// per-boundary array is OPTIONAL (a 1-chapter plan has no boundaries),
// the per-chapter array is REQUIRED so we can always score titles +
// topic completeness even on degenerate plans.
var scoreToolSchema = json.RawMessage([]byte(`{
  "type": "object",
  "properties": {
    "boundary_scores": {
      "type": "array",
      "description": "One entry per cut between consecutive chapters. boundary_idx 0 is the cut between chapter 1 and chapter 2.",
      "items": {
        "type": "object",
        "properties": {
          "boundary_idx": {"type": "integer", "minimum": 0},
          "boundary_coherence": {"type": "integer", "minimum": 0, "maximum": 5},
          "rationale": {"type": "string"}
        },
        "required": ["boundary_idx", "boundary_coherence", "rationale"],
        "additionalProperties": false
      }
    },
    "chapter_scores": {
      "type": "array",
      "description": "One entry per chapter. chapter_idx is 0-based.",
      "items": {
        "type": "object",
        "properties": {
          "chapter_idx": {"type": "integer", "minimum": 0},
          "title_quality": {"type": "integer", "minimum": 0, "maximum": 5},
          "topic_completeness": {"type": "integer", "minimum": 0, "maximum": 5},
          "rationale": {"type": "string"}
        },
        "required": ["chapter_idx", "title_quality", "topic_completeness", "rationale"],
        "additionalProperties": false
      }
    }
  },
  "required": ["chapter_scores"],
  "additionalProperties": false
}`))

// isThinkingModel returns true for DashScope thinking-mode models which
// reject the strict tool_choice form. List is conservative — we only
// flag models we've actually seen hit the "tool_choice does not support
// being set to required or object in thinking mode" error. Add more as
// new thinking models ship.
func isThinkingModel(model string) bool {
	m := strings.ToLower(model)
	return strings.Contains(m, "thinking")
}
