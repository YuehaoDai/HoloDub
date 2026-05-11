// Package pipeline — OPT-407 ep_glossary_broadcast stage handler.
//
// This stage is the OPT-407 episode-level rework action. It is triggered ON
// DEMAND by the rework engine when an episode-level judge returns
// needs_minor_revision with terminology_consistency below the threshold —
// the engine enqueues an EpisodeStageGlossaryBroadcast TaskPayload via
// EnqueueEpisodeStage, and this handler picks it up.
//
// What it does, in order:
//   1. Re-runs the OPT-402 glossary extractor on the SAME ASR transcript
//      (no source-text mutation; the ASR transcript is stable post-asr_smart).
//   2. Diffs the new glossary against the persisted Episode.Glossary by
//      `source` term — finds all source terms whose target translation
//      changed, plus any source terms newly added.
//   3. Searches every chapter's segments for SourceText that mentions one
//      of those changed source terms (substring match — the source term IS
//      the original-language phrase the translator picked up on first pass).
//   4. Truncates the affected segment list at MAX_GLOSSARY_BROADCAST_SEGMENTS
//      (default 20) per chapter to keep blast radius bounded; if a single
//      chapter exceeds that many hits, we record the excess in the log so
//      operators can tune the threshold.
//   5. For each (chapter, affected segment IDs) pair, calls
//      ResetSegmentsForRerun + RetryJob(stage=translate) so the translator
//      re-runs on those segments with the NEW glossary loaded as context.
//   6. Persists the new glossary onto Episode (after step 5 enqueues are
//      done — if we wrote first and crashed before re-queueing, the new
//      glossary would silently apply to NO segments).
//
// Failure-mode contract (mirrors stage_glossary_extract.go):
//   - LLM extraction failure → log warning, return nil, do nothing. The
//     Episode keeps its existing glossary; the rework engine's next pass
//     can try again.
//   - Marshal failure → ditto.
//   - Per-chapter ResetSegmentsForRerun OR RetryJob failure → log + skip
//     that chapter, continue with the next one. Best-effort.
//   - Final glossary persist failure → log + return the error so the worker
//     can retry the broadcast (idempotent, since the diff is recomputed
//     from scratch each pass).
//
// Cost: one LLM glossary call (covered by EPISODE_REWORK_COST_CEILING_USD)
// plus N translate stage invocations across affected chapters.
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

// maxGlossaryBroadcastSegmentsPerChapter caps the number of segments any
// single chapter can re-translate during one broadcast pass. Plan §9
// risk-mitigation: a runaway broadcast (glossary changed a generic term
// like "the") would otherwise fan out across the whole episode. Hardcoded
// for the OPT-407 MVP — promote to env if operators ever need to tune it.
const maxGlossaryBroadcastSegmentsPerChapter = 20

// runEpisodeGlossaryBroadcast executes the OPT-407 ep_glossary_broadcast
// episode-stage. The TaskPayload carries EpisodeID; JobID is unused.
func (s *Service) runEpisodeGlossaryBroadcast(ctx context.Context, task models.TaskPayload) error {
	if task.EpisodeID == 0 {
		return fmt.Errorf("ep_glossary_broadcast: empty EpisodeID")
	}
	if !s.cfg.GlossaryEnabled {
		// Defensive: if an operator turns OPT-402 off but the rework
		// engine still dispatches, refuse rather than crash.
		slog.Info("ep_glossary_broadcast: glossary disabled by config, skipping",
			"episode_id", task.EpisodeID)
		return nil
	}

	ep, err := s.store.GetEpisode(ctx, task.EpisodeID)
	if err != nil || ep == nil {
		return fmt.Errorf("ep_glossary_broadcast: load episode %d: %w", task.EpisodeID, err)
	}

	chapters, err := s.store.GetEpisodeChapters(ctx, ep.ID)
	if err != nil || len(chapters) == 0 {
		slog.Warn("ep_glossary_broadcast: no chapters found, skipping",
			"episode_id", ep.ID, "error", err)
		return nil
	}

	// Step 1: re-extract glossary from the canonical transcript.
	llmSegments := make([]llm.EpisodeSegment, 0, 256)
	for _, chapter := range chapters {
		segs, segErr := s.store.ListSegments(ctx, chapter.ID, nil)
		if segErr != nil {
			slog.Warn("ep_glossary_broadcast: list segments failed for chapter; continuing with what we have",
				"episode_id", ep.ID, "chapter_id", chapter.ID, "error", segErr)
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
		slog.Warn("ep_glossary_broadcast: empty transcript, skipping",
			"episode_id", ep.ID)
		return nil
	}

	slog.Info("ep_glossary_broadcast starting",
		"episode_id", ep.ID,
		"segment_count", len(llmSegments),
		"chapter_count", len(chapters),
	)

	// chapterizeEnabled=false: rework only mutates the glossary; we do
	// NOT want to re-derive chapter boundaries on a re-run because the
	// downstream chapter pipeline is already past chapterize.
	result, err := s.llm.ExtractEpisodeGlossary(ctx,
		llmSegments, ep.SourceLanguage, ep.TargetLanguage, false)
	if err != nil {
		slog.Warn("ep_glossary_broadcast: LLM extraction failed, leaving glossary unchanged",
			"episode_id", ep.ID, "error", err)
		return nil
	}

	// Step 2: diff old vs new glossary by source term.
	oldGlossary, err := decodePersistedGlossary(ep.Glossary)
	if err != nil {
		slog.Warn("ep_glossary_broadcast: cannot decode existing glossary; treating as empty",
			"episode_id", ep.ID, "error", err)
		oldGlossary = nil
	}
	changedTerms := diffGlossaryTerms(oldGlossary, result.Glossary)
	if len(changedTerms) == 0 {
		slog.Info("ep_glossary_broadcast: no glossary terms changed, no segments to re-translate",
			"episode_id", ep.ID,
			"old_size", len(oldGlossary),
			"new_size", len(result.Glossary),
		)
		// Still persist the (possibly identical) new glossary so the
		// updated_at stamp reflects the rework attempt.
		return s.persistBroadcastGlossary(ctx, ep.ID, result)
	}

	slog.Info("ep_glossary_broadcast: glossary terms changed",
		"episode_id", ep.ID,
		"changed_term_count", len(changedTerms),
		"old_size", len(oldGlossary),
		"new_size", len(result.Glossary),
	)

	// Step 3 + 4 + 5: per-chapter find affected segments, cap, re-translate.
	totalRequeued := 0
	totalTruncated := 0
	for _, chapter := range chapters {
		segs, segErr := s.store.ListSegments(ctx, chapter.ID, nil)
		if segErr != nil {
			slog.Warn("ep_glossary_broadcast: list segments for re-translate failed; skipping chapter",
				"episode_id", ep.ID, "chapter_id", chapter.ID, "error", segErr)
			continue
		}
		affected := segmentsMatchingTerms(segs, changedTerms)
		if len(affected) == 0 {
			continue
		}
		truncated := 0
		if len(affected) > maxGlossaryBroadcastSegmentsPerChapter {
			truncated = len(affected) - maxGlossaryBroadcastSegmentsPerChapter
			affected = affected[:maxGlossaryBroadcastSegmentsPerChapter]
		}
		totalTruncated += truncated

		if resetErr := s.store.ResetSegmentsForRerun(ctx, affected); resetErr != nil {
			slog.Warn("ep_glossary_broadcast: ResetSegmentsForRerun failed; skipping chapter",
				"episode_id", ep.ID, "chapter_id", chapter.ID, "error", resetErr)
			continue
		}
		if retryErr := s.RetryJob(ctx, chapter.ID, models.StageTranslate, affected, "rework_engine_glossary_broadcast"); retryErr != nil {
			slog.Warn("ep_glossary_broadcast: RetryJob failed; segments stay reset (next worker pass will pick them up)",
				"episode_id", ep.ID, "chapter_id", chapter.ID, "error", retryErr)
			continue
		}
		totalRequeued += len(affected)
		slog.Info("ep_glossary_broadcast: chapter re-translate enqueued",
			"episode_id", ep.ID,
			"chapter_id", chapter.ID,
			"chapter_ordinal", chapter.ChapterOrdinal,
			"affected_segments", len(affected),
			"truncated", truncated,
		)
	}

	// Step 6: persist the new glossary AFTER the re-translate enqueues so
	// a mid-flight crash leaves the OLD glossary in place + segments
	// awaiting translate; the next broadcast pass recomputes from scratch.
	if persistErr := s.persistBroadcastGlossary(ctx, ep.ID, result); persistErr != nil {
		return fmt.Errorf("ep_glossary_broadcast: persist new glossary: %w", persistErr)
	}

	slog.Info("ep_glossary_broadcast completed",
		"episode_id", ep.ID,
		"changed_term_count", len(changedTerms),
		"requeued_segments", totalRequeued,
		"truncated_segments", totalTruncated,
		"new_glossary_size", len(result.Glossary),
	)
	return nil
}

// persistBroadcastGlossary writes the new glossary back onto the Episode.
// LLM chapters are intentionally NOT touched — broadcast preserves the
// existing chapter boundaries (chapterizeEnabled=false above means
// result.Chapters is always empty, but this is doubly defensive).
func (s *Service) persistBroadcastGlossary(ctx context.Context, episodeID uint, result llm.GlossaryResult) error {
	glossaryJSON, err := json.Marshal(result.Glossary)
	if err != nil {
		return fmt.Errorf("marshal new glossary: %w", err)
	}
	// nil llmChaptersJSON → UpdateEpisodeGlossary leaves the column alone.
	return s.store.UpdateEpisodeGlossary(ctx, episodeID, glossaryJSON, result.ReferenceCard, nil)
}

// decodePersistedGlossary parses Episode.Glossary (datatypes.JSON bytes)
// into a slice of GlossaryEntry. Returns (nil, nil) when the column is
// empty or NULL; returns the error only if the bytes are non-empty and
// fail to parse.
func decodePersistedGlossary(raw []byte) ([]llm.GlossaryEntry, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	var entries []llm.GlossaryEntry
	if err := json.Unmarshal(raw, &entries); err != nil {
		return nil, fmt.Errorf("decode persisted glossary: %w", err)
	}
	return entries, nil
}

// diffGlossaryTerms returns the source-language strings whose target
// translation either changed or was newly added between old and new. The
// source term is what we substring-match against Segment.SourceText, so
// renaming the target only (without changing the source) IS a meaningful
// change worth re-translating.
func diffGlossaryTerms(oldEntries, newEntries []llm.GlossaryEntry) []string {
	oldByKey := make(map[string]string, len(oldEntries))
	for _, e := range oldEntries {
		key := strings.TrimSpace(e.Source)
		if key == "" {
			continue
		}
		oldByKey[key] = strings.TrimSpace(e.Target)
	}
	changed := make([]string, 0, len(newEntries))
	seen := make(map[string]struct{}, len(newEntries))
	for _, e := range newEntries {
		src := strings.TrimSpace(e.Source)
		if src == "" {
			continue
		}
		if _, dup := seen[src]; dup {
			continue
		}
		seen[src] = struct{}{}
		newTarget := strings.TrimSpace(e.Target)
		oldTarget, existed := oldByKey[src]
		if !existed || oldTarget != newTarget {
			changed = append(changed, src)
		}
	}
	return changed
}

// segmentsMatchingTerms returns the IDs of segments whose SourceText
// contains ANY of the given source terms (case-insensitive). Iterates the
// segments slice once, applies a tiny per-segment substring scan; for the
// MVP scale (≤ 1000 segments × ≤ 50 changed terms) this is well under the
// LLM-call cost of the surrounding broadcast.
func segmentsMatchingTerms(segs []models.Segment, terms []string) []uint {
	if len(terms) == 0 {
		return nil
	}
	loweredTerms := make([]string, 0, len(terms))
	for _, t := range terms {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		loweredTerms = append(loweredTerms, strings.ToLower(t))
	}
	if len(loweredTerms) == 0 {
		return nil
	}
	out := make([]uint, 0, len(segs))
	for _, seg := range segs {
		hay := strings.ToLower(seg.SourceText)
		if hay == "" {
			continue
		}
		for _, term := range loweredTerms {
			if strings.Contains(hay, term) {
				out = append(out, seg.ID)
				break
			}
		}
	}
	return out
}
