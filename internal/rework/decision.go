package rework

import (
	"fmt"
)

// Decide is the OPT-407 pure decision function. It maps a DecideInput
// (level, verdict, score, history, config) to a concrete Action. No I/O,
// no time-of-day dependencies, no logging — exhaustively unit testable.
//
// Decision priority (first match wins):
//
//  1. EnabledLevel does NOT cover Level → ActionNoop("level_disabled")
//  2. AccumulatedCostUSD > ceiling      → ActionHaltCost
//  3. Oscillation detection             → ActionEscalateOscillation
//  4. Verdict-level rules (per Level):
//       Segment:
//         verdict=accept    → ActionNoop("verdict_accept")
//         verdict=retry     → ActionSegmentRetry (capped) → ActionEscalateToThinking → ActionAcceptWithBorrow
//         verdict=split     → ActionSegmentSplit (MVP: marker only)
//       Chapter:
//         verdict=chapter_ready       → ActionNoop("verdict_chapter_ready")
//         verdict=needs_revision      → ActionReviseWeakestSegments (capped) → ActionEscalateChapter
//         verdict=needs_major_rework  → ActionEscalateChapter
//       Episode:
//         verdict=production_ready                      → ActionNoop("verdict_production_ready")
//         verdict=needs_minor_revision + terminology<.85 → ActionBroadcastGlossary (capped) → ActionEscalateHumanReview
//         verdict=needs_minor_revision (other axes)      → ActionNoop("verdict_minor_no_actionable_axis")
//         verdict=needs_major_revision OR narrative<.8   → ActionEscalateHumanReview
//
// Caller responsibilities (Engine, not Decide):
//   - Compute AccumulatedCostUSD from History before calling.
//   - Look up WeakestSegmentIDs from judge result ordinals BEFORE calling.
//   - Persist the returned Action via AppendEpisodeReworkAttempt and
//     dispatch the side effect (RetryJob / EnqueueEpisodeStage / set
//     Episode.ReworkStatus) as appropriate.
func Decide(in DecideInput) Action {
	// 1. Feature flag check — if the caller's level isn't covered by the
	//    operator-configured threshold, short-circuit with no work. We
	//    still want the attempt persisted (caller decides) so observability
	//    captures "we saw the verdict but were told to ignore it".
	if !CompareLevel(in.EnabledLevel, in.Level) {
		return Action{
			Type:       ActionNoop,
			SkipReason: fmt.Sprintf("level_disabled (enabled=%s, requested=%s)", in.EnabledLevel, in.Level),
			SegmentIDs: nil,
			JobID:      in.JobID,
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("verdict=%s score=%.2f — observed only", in.Verdict, in.Score),
		}
	}

	// 2. Cost ceiling — once an episode burns more than the budget, we
	//    halt all further auto-rework regardless of verdict. Manual
	//    intervention required.
	if in.EpisodeReworkCostCeilingUSD > 0 && in.AccumulatedCostUSD > in.EpisodeReworkCostCeilingUSD {
		return Action{
			Type:         ActionHaltCost,
			SkipReason:   fmt.Sprintf("cost_ceiling_exceeded_usd_%.2f_of_%.2f", in.AccumulatedCostUSD, in.EpisodeReworkCostCeilingUSD),
			JobID:        in.JobID,
			EpisodeID:    in.EpisodeID,
			ReworkStatus: "halted_cost",
			Note:         "rework engine refused dispatch because per-episode cost ceiling was exceeded; operator review required",
		}
	}

	// 3. Oscillation detection — same target + same verdict consecutive
	//    in history >= threshold means our previous rework decisions are
	//    not converging. Escalate to a manual review path.
	threshold := in.OscillationThreshold
	if threshold <= 0 {
		threshold = 2 // safety default
	}
	consecutive := CountConsecutiveSame(in.History, in.Level, in.TargetID, in.Verdict)
	if consecutive >= threshold {
		return Action{
			Type:         ActionEscalateOscillation,
			SkipReason:   fmt.Sprintf("oscillation_%d_consecutive_same_verdict", consecutive),
			SegmentIDs:   nil,
			JobID:        in.JobID,
			EpisodeID:    in.EpisodeID,
			ReworkStatus: "escalated_oscillation",
			Note:         fmt.Sprintf("level=%s target=%d verdict=%s oscillated %d times — operator review required", in.Level, in.TargetID, in.Verdict, consecutive),
		}
	}

	// 4. Per-level verdict rules.
	switch in.Level {
	case LevelSegment:
		return decideSegment(in)
	case LevelChapter:
		return decideChapter(in)
	case LevelEpisode:
		return decideEpisode(in)
	default:
		return Action{
			Type:       ActionNoop,
			SkipReason: fmt.Sprintf("unknown_level_%q", in.Level),
			Note:       "rework engine received an unrecognised level; this is a programming error",
		}
	}
}

func decideSegment(in DecideInput) Action {
	switch in.Verdict {
	case "accept":
		return Action{
			Type:       ActionNoop,
			SkipReason: "verdict_accept",
			JobID:      in.JobID,
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("segment %d accepted (score=%.2f)", in.TargetID, in.Score),
		}
	case "split":
		// MVP: mark + log. Real split algorithm is OPT-407-followup-1.
		return Action{
			Type:       ActionSegmentSplit,
			SegmentIDs: []uint{in.TargetID},
			JobID:      in.JobID,
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("segment %d flagged for split; OPT-407 MVP records the marker only", in.TargetID),
		}
	case "retry":
		// Count prior retry attempts on THIS segment from history.
		priorRetries := 0
		priorRetryEscalations := 0
		for _, h := range in.History {
			if h.Level != LevelSegment || h.TargetID != in.TargetID {
				continue
			}
			switch h.ActionType {
			case ActionSegmentRetry:
				priorRetries++
			case ActionEscalateToThinking:
				priorRetryEscalations++
			}
		}
		maxRetries := in.SegmentRetryMaxAttempts
		if maxRetries <= 0 {
			maxRetries = 3
		}
		// Step 1: ordinary retry up to N attempts.
		if priorRetries < maxRetries {
			return Action{
				Type:               ActionSegmentRetry,
				SegmentIDs:         []uint{in.TargetID},
				JobID:              in.JobID,
				EpisodeID:          in.EpisodeID,
				Stage:              "tts_duration",
				DriftThresholdHint: 0.05,
				Note:               fmt.Sprintf("retry %d/%d on segment %d (score=%.2f)", priorRetries+1, maxRetries, in.TargetID, in.Score),
			}
		}
		// Step 2: escalate to thinking model once.
		if priorRetryEscalations == 0 {
			return Action{
				Type:        ActionEscalateToThinking,
				SegmentIDs:  []uint{in.TargetID},
				JobID:       in.JobID,
				EpisodeID:   in.EpisodeID,
				Stage:       "tts_duration",
				UseThinking: true,
				Note:        fmt.Sprintf("retry budget exhausted (%d) on segment %d, escalating to thinking model", maxRetries, in.TargetID),
			}
		}
		// Step 3: accept with borrow (no more LLM calls).
		return Action{
			Type:       ActionAcceptWithBorrow,
			SegmentIDs: []uint{in.TargetID},
			JobID:      in.JobID,
			EpisodeID:  in.EpisodeID,
			SkipReason: "all_retries_and_thinking_exhausted",
			Note:       fmt.Sprintf("segment %d accepted-with-borrow after %d retries + 1 thinking escalation (final score=%.2f)", in.TargetID, maxRetries, in.Score),
		}
	default:
		return Action{
			Type:       ActionNoop,
			SkipReason: fmt.Sprintf("unknown_segment_verdict_%q", in.Verdict),
			JobID:      in.JobID,
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("segment %d: unknown verdict %q, taking no action", in.TargetID, in.Verdict),
		}
	}
}

func decideChapter(in DecideInput) Action {
	switch in.Verdict {
	case "chapter_ready":
		return Action{
			Type:       ActionNoop,
			SkipReason: "verdict_chapter_ready",
			JobID:      in.TargetID, // chapter level: TargetID == JobID
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("chapter job %d accepted (score=%.2f)", in.TargetID, in.Score),
		}
	case "needs_revision":
		// Count prior chapter rework rounds on THIS chapter.
		priorRounds := 0
		for _, h := range in.History {
			if h.Level == LevelChapter && h.TargetID == in.TargetID && h.ActionType == ActionReviseWeakestSegments {
				priorRounds++
			}
		}
		maxRounds := in.ChapterReworkMaxRounds
		if maxRounds <= 0 {
			maxRounds = 1
		}
		if priorRounds >= maxRounds {
			return Action{
				Type:         ActionEscalateChapter,
				JobID:        in.TargetID,
				EpisodeID:    in.EpisodeID,
				ReworkStatus: "escalated_chapter",
				SkipReason:   fmt.Sprintf("max_chapter_rounds_%d_reached", maxRounds),
				Note:         fmt.Sprintf("chapter job %d still needs_revision after %d rounds — operator review required", in.TargetID, priorRounds),
			}
		}
		if len(in.WeakestSegmentIDs) == 0 {
			return Action{
				Type:         ActionEscalateChapter,
				JobID:        in.TargetID,
				EpisodeID:    in.EpisodeID,
				ReworkStatus: "escalated_chapter",
				SkipReason:   "no_weakest_segments_resolved",
				Note:         fmt.Sprintf("chapter judge verdict=needs_revision but no weakest segment IDs were resolvable; escalating chapter %d", in.TargetID),
			}
		}
		return Action{
			Type:               ActionReviseWeakestSegments,
			SegmentIDs:         in.WeakestSegmentIDs,
			JobID:              in.TargetID,
			EpisodeID:          in.EpisodeID,
			Stage:              "translate",
			DriftThresholdHint: 0.05,
			Note:               fmt.Sprintf("revising %d weakest segments on chapter job %d (round %d/%d, score=%.2f)", len(in.WeakestSegmentIDs), in.TargetID, priorRounds+1, maxRounds, in.Score),
		}
	case "needs_major_rework":
		return Action{
			Type:         ActionEscalateChapter,
			JobID:        in.TargetID,
			EpisodeID:    in.EpisodeID,
			ReworkStatus: "escalated_chapter",
			SkipReason:   "verdict_needs_major_rework",
			Note:         fmt.Sprintf("chapter job %d verdict=needs_major_rework score=%.2f — operator review required", in.TargetID, in.Score),
		}
	default:
		return Action{
			Type:       ActionNoop,
			SkipReason: fmt.Sprintf("unknown_chapter_verdict_%q", in.Verdict),
			JobID:      in.TargetID,
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("chapter %d: unknown verdict %q", in.TargetID, in.Verdict),
		}
	}
}

func decideEpisode(in DecideInput) Action {
	switch in.Verdict {
	case "production_ready":
		return Action{
			Type:       ActionNoop,
			SkipReason: "verdict_production_ready",
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("episode %d accepted (score=%.2f)", in.EpisodeID, in.Score),
		}
	case "needs_minor_revision":
		// Sub-rule A: cross-chapter terminology drift → broadcast glossary.
		// Sub-rule B: nothing actionable at the cross-chapter level → noop
		// (operator may still drill into chapters).
		const terminologyDriftThreshold = 0.85
		if in.TerminologyConsistency > 0 && in.TerminologyConsistency < terminologyDriftThreshold {
			// Cap broadcast attempts: once per episode pass.
			priorBroadcasts := 0
			for _, h := range in.History {
				if h.Level == LevelEpisode && h.TargetID == in.EpisodeID && h.ActionType == ActionBroadcastGlossary {
					priorBroadcasts++
				}
			}
			if priorBroadcasts >= 1 {
				return Action{
					Type:         ActionEscalateHumanReview,
					EpisodeID:    in.EpisodeID,
					ReworkStatus: "escalated_human",
					SkipReason:   "broadcast_glossary_already_attempted",
					Note:         fmt.Sprintf("episode %d still has terminology drift (terminology=%.2f) after a glossary broadcast — operator review required", in.EpisodeID, in.TerminologyConsistency),
				}
			}
			return Action{
				Type:         ActionBroadcastGlossary,
				EpisodeID:    in.EpisodeID,
				EpisodeStage: "ep_glossary_broadcast",
				ReworkStatus: "in_progress",
				Note:         fmt.Sprintf("dispatching glossary broadcast on episode %d (terminology=%.2f < %.2f)", in.EpisodeID, in.TerminologyConsistency, terminologyDriftThreshold),
			}
		}
		return Action{
			Type:       ActionNoop,
			SkipReason: "verdict_minor_no_actionable_axis",
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("episode %d minor revision but no axis crossed broadcast threshold (terminology=%.2f narrative=%.2f)", in.EpisodeID, in.TerminologyConsistency, in.NarrativeCoherence),
		}
	case "needs_major_revision":
		return Action{
			Type:         ActionEscalateHumanReview,
			EpisodeID:    in.EpisodeID,
			ReworkStatus: "escalated_human",
			SkipReason:   "verdict_needs_major_revision",
			Note:         fmt.Sprintf("episode %d verdict=needs_major_revision score=%.2f — operator review required", in.EpisodeID, in.Score),
		}
	default:
		// Defensive: any other verdict (including "" if the LLM stripped
		// it out) escalates rather than silently dropping the alert.
		const narrativeFloor = 0.8
		if in.NarrativeCoherence > 0 && in.NarrativeCoherence < narrativeFloor {
			return Action{
				Type:         ActionEscalateHumanReview,
				EpisodeID:    in.EpisodeID,
				ReworkStatus: "escalated_human",
				SkipReason:   fmt.Sprintf("narrative_coherence_below_%.2f", narrativeFloor),
				Note:         fmt.Sprintf("episode %d narrative_coherence=%.2f below floor — operator review required", in.EpisodeID, in.NarrativeCoherence),
			}
		}
		return Action{
			Type:       ActionNoop,
			SkipReason: fmt.Sprintf("unknown_episode_verdict_%q", in.Verdict),
			EpisodeID:  in.EpisodeID,
			Note:       fmt.Sprintf("episode %d: unknown verdict %q, taking no action", in.EpisodeID, in.Verdict),
		}
	}
}
