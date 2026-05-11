// Package pipeline — OPT-406 episode-level Judge dispatch.
//
// maybeJudgeEpisodeAsync fires off a background episode-level judge call
// AFTER ep_episode_merge has finished concatenating all chapters and
// transitioned the Episode to Completed. It mirrors the contract proven
// in maybeJudgeChapterAsync (OPT-409): detached background context with
// its own deadline, observe-only log+drop on any failure, partial UPDATE
// on the result so the episode state-machine is never clobbered.
//
// Why a separate file instead of stuffing into stage_episode_merge.go:
// keeps the merge handler focused on video assembly + manifest write,
// and matches the chapter-level split between stage_merge.go (assembly)
// and stage_tts.go (chapter judge dispatch). Future OPT-407 wiring will
// also live here.
package pipeline

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"holodub/internal/llm"
	"holodub/internal/models"
)

// maybeJudgeEpisodeAsync dispatches the OPT-406 episode-level judge in a
// background goroutine when EPISODE_JUDGE_MODEL is configured. Returns
// immediately; the goroutine has its own deadline (cfg.EpisodeJudgeTimeoutSec,
// default 90s) and writes results via store.UpdateEpisodeJudgeResult on a
// fresh background context so a worker SIGTERM cancelling the merge ctx
// does NOT silently lose the verdict.
//
// Failure modes (network / parse / provider error / DB write) are logged
// and dropped — episode judging is observe-only in the OPT-406 MVP and
// MUST never fail the episode (the merge has already shipped). The DB
// UPDATE only touches episode_judge_score / episode_judge_meta so it is
// safe against concurrent writes from other paths.
//
// Why we accept the chapter slice by value: ep_episode_merge already
// loaded it and verified Status == Completed; reusing it spares another
// DB round-trip. The judge goroutine treats it as read-only.
func (s *Service) maybeJudgeEpisodeAsync(ep *models.Episode, chapters []models.Job) {
	if s == nil || ep == nil {
		return
	}
	if s.cfg.EpisodeJudgeModel == "" {
		return
	}
	if len(chapters) == 0 {
		return
	}

	// Snapshot the bits we need so callers can mutate the Episode after we
	// return without racing the async judge call.
	epCopy := *ep
	chapterMeta := make([]chapterRowSnapshot, len(chapters))
	chapterIDToOrdinal := make(map[uint]int, len(chapters))
	for i, ch := range chapters {
		chapterMeta[i] = chapterRowSnapshot{
			ID:                ch.ID,
			Ordinal:           ch.ChapterOrdinal,
			Title:             ch.ChapterTitle,
			TitleTranslated:   ch.ChapterTitleTranslated,
			StartMs:           ch.ChapterStartMs,
			EndMs:             ch.ChapterEndMs,
			SummaryMD:         ch.ChapterSummaryMD,
			ChapterJudgeScore: copyFloat64Ptr(ch.ChapterJudgeScore),
		}
		if ch.ChapterOrdinal > 0 {
			chapterIDToOrdinal[ch.ID] = ch.ChapterOrdinal
		}
	}

	timeoutSec := s.cfg.EpisodeJudgeTimeoutSec
	if timeoutSec <= 0 {
		timeoutSec = 90
	}
	go func() {
		// Detached background context: a SIGTERM during the episode judge
		// must not silently swallow the verdict.
		ctx, cancel := context.WithTimeout(context.Background(), time.Duration(timeoutSec)*time.Second)
		defer cancel()

		// Single bulk load of every segment under this episode (joins on
		// jobs.episode_id; ordered by job_id ASC, ordinal ASC). One round-
		// trip vs N — a 10-chapter episode would otherwise issue 10 DB
		// fetches inside the judge goroutine.
		segments, err := s.store.ListSegmentsByEpisode(ctx, epCopy.ID)
		if err != nil {
			slog.Warn("episode judge: load segments failed",
				"episode_id", epCopy.ID, "error", err)
			return
		}

		// Build the LLM-side segment slice with chapter_ordinal + per-
		// segment judge hint (when available). Skip empty pairs so they
		// don't dilute the score with rows that carry no signal.
		epSegs := make([]llm.EpisodeJudgeSegment, 0, len(segments))
		for _, seg := range segments {
			if seg.SourceText == "" || seg.TargetText == "" {
				continue
			}
			chapterOrdinal := chapterIDToOrdinal[seg.JobID]
			if chapterOrdinal == 0 {
				// Defensive: if a segment somehow points at a job no
				// longer in our chapters slice (race with re-fan-out?),
				// drop it rather than emit ordinal=0 which would confuse
				// the LLM's [c0.sN] tagging scheme.
				continue
			}
			var segScore *float64
			if seg.JudgeScore != nil {
				v := *seg.JudgeScore
				segScore = &v
			}
			epSegs = append(epSegs, llm.EpisodeJudgeSegment{
				ChapterOrdinal: chapterOrdinal,
				Ordinal:        seg.Ordinal,
				StartMs:        seg.StartMs,
				EndMs:          seg.EndMs,
				SourceText:     seg.SourceText,
				TargetText:     seg.TargetText,
				SegJudgeScore:  segScore,
			})
		}
		if len(epSegs) == 0 {
			slog.Info("episode judge: no segments with text to score, skipping",
				"episode_id", epCopy.ID)
			return
		}

		// Map chapters → LLM-side rows (Title / score hint).
		epChapters := make([]llm.EpisodeJudgeChapterRow, 0, len(chapterMeta))
		for _, m := range chapterMeta {
			epChapters = append(epChapters, llm.EpisodeJudgeChapterRow{
				Ordinal:           m.Ordinal,
				Title:             m.Title,
				TitleTranslated:   m.TitleTranslated,
				StartMs:           m.StartMs,
				EndMs:             m.EndMs,
				ChapterJudgeScore: m.ChapterJudgeScore,
				SummaryMD:         m.SummaryMD,
			})
		}

		result, err := s.llm.JudgeEpisode(ctx, llm.EpisodeJudgeArgs{
			SourceLang:     epCopy.SourceLanguage,
			TargetLang:     epCopy.TargetLanguage,
			EpisodeID:      epCopy.ID,
			EpisodeName:    epCopy.Name,
			EpisodeSummary: epCopy.ReferenceCard,
			GlossaryHint:   formatEpisodeGlossaryHint(epCopy.Glossary),
			Chapters:       epChapters,
			Segments:       epSegs,
		})
		if err != nil {
			slog.Warn("episode judge call failed",
				"episode_id", epCopy.ID, "error", err)
			return
		}
		if result == nil {
			return // judging disabled or empty inputs — should not reach here
		}

		metaJSON, err := json.Marshal(result)
		if err != nil {
			slog.Warn("episode judge result marshal failed",
				"episode_id", epCopy.ID, "error", err)
			return
		}
		if err := s.store.UpdateEpisodeJudgeResult(ctx, epCopy.ID, result.OverallScore(), metaJSON); err != nil {
			slog.Warn("episode judge result persist failed",
				"episode_id", epCopy.ID, "error", err)
			return
		}
		slog.Info("episode judge result recorded",
			"episode_id", epCopy.ID,
			"verdict", result.Verdict,
			"overall_fidelity", result.OverallFidelity,
			"overall_fluency", result.OverallFluency,
			"narrative_coherence", result.NarrativeCoherence,
			"terminology_consistency", result.TerminologyConsistency,
			"register_consistency", result.RegisterConsistency,
			"character_voice_stability", result.CharacterVoiceStability,
			"cultural_localization", result.CulturalLocalization,
			"weakest_chapters_count", len(result.Top3WeakestChapters),
			"weakest_segments_count", len(result.Top3WeakestSegments),
			"glossary_observed_count", len(result.TerminologyGlossaryObserved),
			"segment_count", len(epSegs),
			"chapter_count", len(epChapters),
		)
	}()
}

// chapterRowSnapshot mirrors only the chapter fields the episode judge
// goroutine reads. Keeping it isolated from models.Job avoids accidentally
// reading Job state that downstream callers may mutate after we return.
type chapterRowSnapshot struct {
	ID                uint
	Ordinal           int
	Title             string
	TitleTranslated   string
	StartMs           int64
	EndMs             int64
	SummaryMD         string
	ChapterJudgeScore *float64
}

func copyFloat64Ptr(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}

// formatEpisodeGlossaryHint turns Episode.Glossary (datatypes.JSON, an
// array of {source_term, target_term, context, confidence} objects produced
// by OPT-402's emit_episode_glossary tool) into a compact text block the
// LLM judge can read directly. Returns "" when the glossary is missing or
// not parseable so JudgeEpisode skips the optional [Episode glossary] block.
//
// Best-effort: any unmarshal error swallows silently — the judge can still
// score (just without the canonical term reference) and the worker should
// not log noise on every episode that ran without OPT-402 glossary.
func formatEpisodeGlossaryHint(glossaryJSON []byte) string {
	if len(glossaryJSON) == 0 {
		return ""
	}
	var entries []struct {
		SourceTerm string `json:"source_term"`
		TargetTerm string `json:"target_term"`
		Context    string `json:"context,omitempty"`
	}
	if err := json.Unmarshal(glossaryJSON, &entries); err != nil {
		return ""
	}
	if len(entries) == 0 {
		return ""
	}
	// Compact one-line-per-term format. Capped at 100 entries to keep the
	// prompt under control on extreme cases — we have not observed an
	// episode glossary larger than ~40 in production.
	const maxEntries = 100
	if len(entries) > maxEntries {
		entries = entries[:maxEntries]
	}
	var b []byte
	for _, e := range entries {
		if e.SourceTerm == "" || e.TargetTerm == "" {
			continue
		}
		b = append(b, e.SourceTerm...)
		b = append(b, "  ->  "...)
		b = append(b, e.TargetTerm...)
		if e.Context != "" {
			b = append(b, "    ("...)
			b = append(b, e.Context...)
			b = append(b, ")"...)
		}
		b = append(b, '\n')
	}
	return string(b)
}
