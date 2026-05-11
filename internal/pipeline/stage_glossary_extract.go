// Package pipeline — OPT-402 + OPT-405 ep_glossary_extract stage handler.
//
// This stage runs ONCE per episode (not per chapter) immediately after
// the chapter pipeline finishes asr_smart. It assembles the FULL ASR
// transcript as an INDEXED segment list and asks an LLM (recommended:
// kimi-k2.5) to derive — in ONE tool call — the canonical terminology,
// speaker hints, reference card, AND (OPT-405) a SEMANTIC chapter plan.
//
// The result is persisted on the parent Episode and consumed by:
//   - stage_tts.go::buildEpisodeAwareSummary (glossary + reference_card)
//   - stage_chapterize.go (llm_chapters; falls back to DP on miss)
//
// Failure mode contract (re-stated from the LLM layer):
//   - Glossary extraction is STRICTLY non-blocking. Any error logs a
//     warning and the stage returns nil, leaving Episode.Glossary AND
//     Episode.LLMChapters empty. The downstream translate path falls
//     back to the legacy summary AND the chapterize stage falls back to
//     the deterministic DP algorithm — both are current production
//     behaviour, so a transient LLM outage never fails an episode.
package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"holodub/internal/llm"
	"holodub/internal/models"
)

// runEpisodeGlossaryExtract executes the OPT-402+OPT-405 ep_glossary_extract
// episode-stage. The TaskPayload carries EpisodeID (not JobID — episode
// stages operate on the parent Episode, not a single chapter).
func (s *Service) runEpisodeGlossaryExtract(ctx context.Context, task models.TaskPayload) error {
	if !s.cfg.GlossaryEnabled {
		slog.Info("ep_glossary_extract disabled by config, skipping",
			"episode_id", task.EpisodeID)
		return nil
	}
	if task.EpisodeID == 0 {
		// 1-chapter shortcut: caller may pass JobID via the same payload.
		// The shortcut wrapper (see pipeline.go::HandleTask dispatch) is
		// responsible for routing through job → episode resolution before
		// arriving here, so an empty EpisodeID here is a bug.
		return fmt.Errorf("ep_glossary_extract: empty EpisodeID")
	}

	ep, err := s.store.GetEpisode(ctx, task.EpisodeID)
	if err != nil || ep == nil {
		return fmt.Errorf("ep_glossary_extract: load episode %d: %w", task.EpisodeID, err)
	}

	// Collect ASR segments from every chapter in chronological order.
	// 1-chapter case: just one chapter; on the long-video path the worker
	// reaches us before chapter fan-out so all segments still live under
	// chapter 1, which keeps the indexing identical regardless.
	chapters, err := s.store.GetEpisodeChapters(ctx, ep.ID)
	if err != nil || len(chapters) == 0 {
		slog.Warn("ep_glossary_extract: no chapters found, skipping",
			"episode_id", ep.ID, "error", err)
		return nil
	}

	llmSegments := make([]llm.EpisodeSegment, 0, 256)
	for _, chapter := range chapters {
		segs, err := s.store.ListSegments(ctx, chapter.ID, nil)
		if err != nil {
			slog.Warn("ep_glossary_extract: list segments failed for chapter; skipping",
				"episode_id", ep.ID, "chapter_id", chapter.ID, "error", err)
			continue
		}
		for _, seg := range segs {
			if strings.TrimSpace(seg.SourceText) == "" {
				continue
			}
			llmSegments = append(llmSegments, llm.EpisodeSegment{
				StartMs:      chapter.ChapterStartMs + seg.StartMs,
				EndMs:        chapter.ChapterStartMs + seg.EndMs,
				Text:         strings.TrimSpace(seg.SourceText),
				SpeakerLabel: seg.SpeakerLabel,
			})
		}
	}
	if len(llmSegments) == 0 {
		slog.Warn("ep_glossary_extract: empty transcript, skipping",
			"episode_id", ep.ID)
		return nil
	}

	// OPT-405 gate: ask the LLM for chapters[] only when both the global
	// chapterize switch AND the LLM-driven sub-switch are on. Otherwise
	// the model is told to leave chapters=[] and the downstream
	// ep_chapterize stage falls back to the DP algorithm (legacy OPT-403
	// behaviour, preserved as the safety net).
	chapterizeEnabled := s.cfg.ChapterizeEnabled && s.cfg.ChapterizeLLMDriven
	totalDurationMs := int64(0)
	if last := llmSegments[len(llmSegments)-1]; last.EndMs > totalDurationMs {
		totalDurationMs = last.EndMs
	}
	slog.Info("ep_glossary_extract starting",
		"episode_id", ep.ID,
		"segment_count", len(llmSegments),
		"chapter_count", len(chapters),
		"total_duration_ms", totalDurationMs,
		"llm_chapterize_enabled", chapterizeEnabled,
	)

	result, err := s.llm.ExtractEpisodeGlossary(ctx,
		llmSegments, ep.SourceLanguage, ep.TargetLanguage, chapterizeEnabled)
	if err != nil {
		// Non-fatal: log and return nil so the stage doesn't trip the
		// worker retry path. The translate stage will fall back to the
		// legacy no-glossary summary AND chapterize will fall back to DP.
		slog.Warn("ep_glossary_extract: LLM call failed, leaving glossary empty",
			"episode_id", ep.ID, "error", err)
		// Continue to the chapterize enqueue below so DP can still run.
		return s.enqueueChapterize(ctx, ep.ID, "glossary_llm_failed")
	}

	glossaryJSON, err := json.Marshal(result.Glossary)
	if err != nil {
		slog.Warn("ep_glossary_extract: marshal glossary failed, skipping persist",
			"episode_id", ep.ID, "error", err)
		return s.enqueueChapterize(ctx, ep.ID, "glossary_marshal_failed")
	}

	// Marshal LLM chapters only when the model actually produced them.
	// Empty / nil result.Chapters means "model declined" → store nothing
	// → ep_chapterize falls back to DP. This is intentional and important:
	// it lets the operator turn OPT-405 off (CHAPTERIZE_LLM_DRIVEN=false)
	// for one episode by re-running glossary, without needing a manual
	// DB cleanup.
	var llmChaptersJSON []byte
	if chapterizeEnabled && len(result.Chapters) > 0 {
		llmChaptersJSON, err = json.Marshal(result.Chapters)
		if err != nil {
			slog.Warn("ep_glossary_extract: marshal llm_chapters failed; falling back to DP",
				"episode_id", ep.ID, "error", err)
			llmChaptersJSON = nil
		}
	}

	if err := s.store.UpdateEpisodeGlossary(ctx, ep.ID, glossaryJSON, result.ReferenceCard, llmChaptersJSON); err != nil {
		// Persist failure IS surfaced — caller (HandleTask) will retry.
		// A repeatedly failing persist is a real DB problem worth bouncing.
		return fmt.Errorf("ep_glossary_extract: persist glossary: %w", err)
	}

	slog.Info("ep_glossary_extract completed",
		"episode_id", ep.ID,
		"glossary_entries", len(result.Glossary),
		"speakers", len(result.Speakers),
		"reference_card_chars", len(result.ReferenceCard),
		"llm_chapter_count", len(result.Chapters),
	)

	return s.enqueueChapterize(ctx, ep.ID, "glossary_extract_completed")
}

// enqueueChapterize is the OPT-403 chain step: enqueue ep_chapterize so
// it can decide short-circuit vs fan-out, with or without an LLM-suggested
// plan to consult. Pulled out into a helper so all return paths from
// runEpisodeGlossaryExtract (happy path, LLM failure, marshal failure)
// reach the chapterize stage — chapter 1 might be parked at
// JobStatusAwaitingChapterize and would otherwise stall forever.
func (s *Service) enqueueChapterize(ctx context.Context, episodeID uint, reason string) error {
	if !s.cfg.ChapterizeEnabled {
		return nil
	}
	if err := s.EnqueueEpisodeStage(ctx,
		episodeID,
		models.EpisodeStageChapterize,
		"pipeline",
		reason,
	); err != nil {
		slog.Warn("opt-403 enqueue ep_chapterize failed; chapter 1 will stay parked if it was awaiting_chapterize",
			"episode_id", episodeID, "reason", reason, "error", err)
	}
	return nil
}
