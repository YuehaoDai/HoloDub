// Package pipeline — OPT-402 ep_glossary_extract stage handler.
//
// This stage runs ONCE per episode (not per chapter) immediately after
// the chapter pipeline finishes asr_smart. It assembles the FULL ASR
// text for the episode and asks an LLM to derive the canonical
// terminology + speaker hints + reference card. The result is persisted
// on the parent Episode and consumed by stage_tts.go's
// buildEpisodeAwareSummary so every subsequent retranslate sees
// consistent terminology.
//
// Failure mode contract (re-stated from the LLM layer):
//   - Glossary extraction is STRICTLY non-blocking. Any error logs a
//     warning and the stage returns nil, leaving Episode.Glossary empty.
//     The downstream translate path falls back to the legacy summary
//     (== current production behaviour), so a transient LLM outage
//     never fails an episode.
package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"

	"holodub/internal/models"
)

// runEpisodeGlossaryExtract executes the OPT-402 ep_glossary_extract
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
	// 1-chapter case: just one chapter.
	chapters, err := s.store.GetEpisodeChapters(ctx, ep.ID)
	if err != nil || len(chapters) == 0 {
		slog.Warn("ep_glossary_extract: no chapters found, skipping",
			"episode_id", ep.ID, "error", err)
		return nil
	}

	var fullText strings.Builder
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
			if seg.SpeakerLabel != "" {
				fmt.Fprintf(&fullText, "[%s] ", seg.SpeakerLabel)
			}
			fullText.WriteString(strings.TrimSpace(seg.SourceText))
			fullText.WriteString("\n")
		}
	}

	transcript := strings.TrimSpace(fullText.String())
	if transcript == "" {
		slog.Warn("ep_glossary_extract: empty transcript, skipping",
			"episode_id", ep.ID)
		return nil
	}

	slog.Info("ep_glossary_extract starting",
		"episode_id", ep.ID,
		"transcript_chars", len(transcript),
		"chapter_count", len(chapters),
	)

	result, err := s.llm.ExtractEpisodeGlossary(ctx,
		transcript, ep.SourceLanguage, ep.TargetLanguage)
	if err != nil {
		// Non-fatal: log and return nil so the stage doesn't trip the
		// worker retry path. The translate stage will fall back to the
		// legacy no-glossary summary.
		slog.Warn("ep_glossary_extract: LLM call failed, leaving glossary empty",
			"episode_id", ep.ID, "error", err)
		return nil
	}

	glossaryJSON, err := json.Marshal(result.Glossary)
	if err != nil {
		slog.Warn("ep_glossary_extract: marshal glossary failed, skipping persist",
			"episode_id", ep.ID, "error", err)
		return nil
	}

	if err := s.store.UpdateEpisodeGlossary(ctx, ep.ID, glossaryJSON, result.ReferenceCard); err != nil {
		// Persist failure IS surfaced — caller (HandleTask) will retry.
		// A repeatedly failing persist is a real DB problem worth bouncing.
		return fmt.Errorf("ep_glossary_extract: persist glossary: %w", err)
	}

	slog.Info("ep_glossary_extract completed",
		"episode_id", ep.ID,
		"glossary_entries", len(result.Glossary),
		"speakers", len(result.Speakers),
		"reference_card_chars", len(result.ReferenceCard),
	)

	// OPT-403 chain: enqueue ep_chapterize so it can decide short-circuit vs
	// fan-out using the freshly-written reference card. The handler is a no-
	// op when ChapterizeEnabled=false or when the episode is short enough to
	// fit one chapter; the cost of always enqueueing is one queue
	// round-trip + one DB read, which is negligible compared to the LLM
	// glossary work we just finished.
	if s.cfg.ChapterizeEnabled {
		if err := s.EnqueueEpisodeStage(ctx,
			ep.ID,
			models.EpisodeStageChapterize,
			"pipeline",
			"glossary_extract_completed",
		); err != nil {
			slog.Warn("opt-403 enqueue ep_chapterize failed; chapter 1 will stay parked if it was awaiting_chapterize",
				"episode_id", ep.ID, "error", err)
		}
	}
	return nil
}
