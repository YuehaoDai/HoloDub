// Package rework — OPT-407 Closed-loop rework engine.
//
// This package converts the three layers of judge verdicts (segment OPT-002,
// chapter OPT-409, episode OPT-406) into concrete rework Actions. The
// decision logic is a pure function with no I/O so it can be unit tested
// exhaustively against every row of the OPT-407 decision table; the
// surrounding Engine wraps it with persistence + dispatch (see engine.go).
//
// MVP scope (OPT-407 first ship):
//   - Segment-level: convert verdict=retry into a RetryJob(stage=tts_duration)
//     reusing the existing pipeline retry path. verdict=split is recorded
//     as an attempt + Segment.NeedsSplit flag but the actual split algorithm
//     is deferred to OPT-407-followup-1.
//   - Chapter-level: verdict=needs_revision dispatches the top-3 weakest
//     segments through RetryJob(stage=translate) so they re-translate AND
//     re-synthesise. verdict=needs_major_rework escalates (no auto-rework).
//   - Episode-level: verdict=needs_minor_revision + cross-chapter terminology
//     drift triggers a broadcast_glossary update (new EpisodeStage).
//     verdict=needs_major_revision escalates to human review.
//
// Hard guards on every level (NEVER bypassed):
//   - Per-level retry cap
//   - Per-episode accumulated cost ceiling
//   - Oscillation detection (same target, same verdict, N consecutive)
//   - REWORK_ENGINE_LEVEL feature flag (default "none" — observe-only)
//
// Anything that would loop forever is forced into ActionEscalateOscillation
// or ActionHaltCost, which write the rework_status column on the Episode
// for operator review and stop dispatching new work.
package rework

import "time"

// Level identifies which judge layer fired (and which rule subset to apply).
type Level string

const (
	LevelNone    Level = "none"
	LevelSegment Level = "segment"
	LevelChapter Level = "chapter"
	LevelEpisode Level = "episode"
)

// CompareLevel returns whether `enabled` covers (>=) the requested `requested`
// level. Order: none < segment < chapter < episode. The engine config field
// REWORK_ENGINE_LEVEL is a single threshold so users can flip "all rework
// up to chapter level" without juggling multiple flags. An "episode" setting
// implicitly covers segment and chapter levels too.
func CompareLevel(enabled, requested Level) bool {
	rank := map[Level]int{
		LevelNone:    0,
		LevelSegment: 1,
		LevelChapter: 2,
		LevelEpisode: 3,
	}
	return rank[enabled] >= rank[requested] && rank[requested] > 0
}

// ParseLevel turns the raw env value (REWORK_ENGINE_LEVEL=...) into a Level
// constant. Unknown / empty values collapse to LevelNone so a typo never
// silently enables rework.
func ParseLevel(s string) Level {
	switch s {
	case string(LevelSegment):
		return LevelSegment
	case string(LevelChapter):
		return LevelChapter
	case string(LevelEpisode):
		return LevelEpisode
	default:
		return LevelNone
	}
}

// ActionType is the discriminator for Action. Keep this list small and
// stable — operators read it from rework_attempts.action_type and any new
// value should ship with a CHANGELOG entry + roadmap follow-up.
type ActionType string

const (
	// ActionNoop = no rework dispatched. SkipReason carries the textual
	// reason ("level_disabled", "verdict_accept", "max_attempts_reached", ...).
	// Engine still appends the attempt so observability stays complete.
	ActionNoop ActionType = "noop"

	// ActionSegmentRetry = re-run TTS for one specific segment with adaptive
	// drift threshold. Reuses (*Service).RetryJob(stage=tts_duration).
	ActionSegmentRetry ActionType = "segment_retry"

	// ActionSegmentSplit = mark Segment.NeedsSplit + log. The actual
	// algorithm to slice the segment at a silence midpoint is OPT-407-
	// followup-1 (kept out of MVP to bound scope); the marker is
	// surfaced on the segment row so the UI can render a "split needed"
	// badge today.
	ActionSegmentSplit ActionType = "segment_split"

	// ActionEscalateToThinking = re-run TTS for one segment with the
	// thinking model forced for retranslation. Used after the regular
	// retry budget is exhausted on a stubborn segment. Implementation
	// detail: payload includes UseThinking=true; the existing TTS retry
	// path already honours this hint via cfg.RetranslationThinkingModel.
	ActionEscalateToThinking ActionType = "escalate_to_thinking"

	// ActionAcceptWithBorrow = give up retrying. Mark the segment as
	// "accepted with borrow" so downstream merge can clip overflow into
	// the trailing gap (the existing borrow path in stage_merge.go) and
	// no further LLM cost is incurred.
	ActionAcceptWithBorrow ActionType = "accept_with_borrow"

	// ActionReviseWeakestSegments = chapter-level rework. Dispatches
	// RetryJob(stage=translate, segmentIDs=top_3_weakest) so the listed
	// segments re-translate AND auto-advance to tts_duration. The chapter
	// merge re-runs once they finish.
	ActionReviseWeakestSegments ActionType = "revise_weakest_segments"

	// ActionEscalateChapter = chapter verdict was needs_major_rework. Set
	// rework_status on the Episode + log. Operator can manually trigger
	// further work; we never auto-rework an entire chapter (would be a
	// cost grenade).
	ActionEscalateChapter ActionType = "escalate_chapter"

	// ActionBroadcastGlossary = episode-level glossary refresh. Enqueues
	// the new EpisodeStage `ep_glossary_broadcast` which re-extracts the
	// glossary, diffs old vs new, and dispatches RetryJob(stage=translate)
	// for affected segments under a per-episode cap.
	ActionBroadcastGlossary ActionType = "broadcast_glossary"

	// ActionEscalateHumanReview = episode verdict was needs_major_revision
	// OR narrative_coherence < threshold. Always manual; we set
	// rework_status="escalated_human" + log so the EpisodeDetail UI can
	// surface the alert.
	ActionEscalateHumanReview ActionType = "escalate_human_review"

	// ActionEscalateOscillation = same target hit the same verdict N
	// consecutive times. Stops dispatching at this level for this target
	// (rework_status="escalated_oscillation" if episode-level).
	ActionEscalateOscillation ActionType = "escalate_oscillation"

	// ActionHaltCost = accumulated_cost_usd > ceiling. Halts ALL rework
	// for this episode (rework_status="halted_cost"); resumed only after
	// operator clears the flag.
	ActionHaltCost ActionType = "halt_cost"
)

// Action is the structured rework decision. Engine.Execute switches on Type
// and consumes the matching payload fields; unused fields stay zero.
//
// Why one big struct instead of a sealed interface tree: every action shares
// the same dispatch pattern (look up an ID, call RetryJob OR set a flag OR
// enqueue an episode stage), so a tagged union keeps the code branchy but
// flat. A type-per-action tree would add interface plumbing for marginal
// type-safety win.
type Action struct {
	Type ActionType `json:"type"`

	// SkipReason is populated when Type == ActionNoop or any of the halt /
	// escalate variants — explains in operator-readable language why no
	// real dispatch happened (e.g. "level_disabled", "max_attempts_3_reached",
	// "cost_ceiling_exceeded_usd_2.10_of_2.00").
	SkipReason string `json:"skip_reason,omitempty"`

	// SegmentIDs is the target segment(s) for ActionSegmentRetry,
	// ActionSegmentSplit, ActionReviseWeakestSegments. Always non-empty
	// for those action types.
	SegmentIDs []uint `json:"segment_ids,omitempty"`

	// JobID is the chapter Job containing the segments (chapter-level
	// dispatch needs it to scope RetryJob).
	JobID uint `json:"job_id,omitempty"`

	// EpisodeID is the parent episode (episode-level dispatch + history /
	// cost / oscillation always reference it).
	EpisodeID uint `json:"episode_id,omitempty"`

	// Stage is the JobStage to retry (tts_duration for segment-level,
	// translate for chapter-level revise). Empty for non-retry actions.
	Stage string `json:"stage,omitempty"`

	// EpisodeStage is the episode-level stage to enqueue
	// (ep_glossary_broadcast for ActionBroadcastGlossary). Empty for
	// non-broadcast actions.
	EpisodeStage string `json:"episode_stage,omitempty"`

	// UseThinking flips the retranslation path to the thinking model for
	// ActionEscalateToThinking. Plumbed via TaskPayload reason (the
	// existing TTS retry loop reads useThinking based on stuck-threshold;
	// OPT-407 uses Reason="rework_escalate_thinking" to bias that decision).
	UseThinking bool `json:"use_thinking,omitempty"`

	// DriftThresholdHint is the desired drift threshold for the retry
	// (e.g. 0.05 for tighter sync after verdict=retry). Currently
	// informational — the production retry path uses
	// cfg.RetranslationDriftThreshold; OPT-201 SegmentAgent will plumb
	// this through.
	DriftThresholdHint float64 `json:"drift_threshold_hint,omitempty"`

	// ReworkStatus is the column value to set on Episode.ReworkStatus when
	// this action implies a state change ("escalated_human" / "halted_cost"
	// / "escalated_oscillation" / "in_progress" / ""). Empty = no change.
	ReworkStatus string `json:"rework_status,omitempty"`

	// Note is a human-readable one-line rationale persisted onto the
	// rework_attempts entry for operator readability ("verdict=retry,
	// attempt 2/3, segment 12345").
	Note string `json:"note,omitempty"`
}

// IsNoop reports whether this action does no real dispatch. Useful for
// callers that want to skip persistence entirely (we still persist for
// observability, but Engine.Execute can short-circuit external side
// effects).
func (a Action) IsNoop() bool {
	switch a.Type {
	case ActionNoop, ActionEscalateChapter, ActionEscalateHumanReview,
		ActionEscalateOscillation, ActionHaltCost, ActionAcceptWithBorrow,
		ActionSegmentSplit:
		return true
	}
	return false
}

// ReworkAttempt is one historical entry persisted on episodes.rework_attempts.
//
// The engine appends one ReworkAttempt per call (whether or not it dispatched
// real work) so observability is complete: an operator inspecting the
// rework_attempts column sees BOTH "we evaluated and skipped because level
// was disabled" and "we dispatched a retry that subsequently improved /
// failed". This avoids the silent-decision class of bug.
type ReworkAttempt struct {
	Level        Level      `json:"level"`
	TargetID     uint       `json:"target_id"`         // segment_id / job_id / episode_id depending on Level
	JobID        uint       `json:"job_id,omitempty"`  // populated when target is segment / chapter
	Verdict      string     `json:"verdict"`           // input verdict that drove the decision
	BeforeScore  float64    `json:"before_score"`      // judge score that triggered this attempt
	ActionType   ActionType `json:"action_type"`
	Dispatched   bool       `json:"dispatched"`        // true when Engine actually called RetryJob / EnqueueEpisodeStage
	SkipReason   string     `json:"skip_reason,omitempty"`
	Stage        string     `json:"stage,omitempty"`
	SegmentIDs   []uint     `json:"segment_ids,omitempty"`
	CostUSDDelta float64    `json:"cost_usd_delta"`    // cost we estimate the dispatched work will incur (used for cost ceiling tracking)
	Note         string     `json:"note,omitempty"`
	Timestamp    time.Time  `json:"ts"`
}

// DecideInput is the snapshot Decide() consumes. Pure values — no
// context, no DB, no queue — so the function is exhaustively unit-testable.
//
// All level-specific fields are tagged so the rules in decide.go can read
// "what verdict came in", "what was the score", "what auxiliary axes did
// we observe" without needing the full judge result types from package llm
// (avoiding a circular import: rework would otherwise need internal/llm,
// which depends on internal/observability, which is already a dependency).
type DecideInput struct {
	Level        Level  // which judge layer fired
	EnabledLevel Level  // current REWORK_ENGINE_LEVEL config
	Verdict      string // raw verdict string from the judge
	TargetID     uint   // segment_id / job_id / episode_id
	JobID        uint   // parent chapter Job (for segment + chapter levels); 0 for episode level
	EpisodeID    uint   // for cost / oscillation tracking & history scoping; required
	Score        float64 // judge OverallScore() for the target

	// Auxiliary axes used by some decision rules. Zero means "not provided"
	// (caller didn't populate, defaults will not trip the rule).
	TerminologyConsistency float64 // episode judge axis (cross-chapter glossary drift)
	NarrativeCoherence     float64 // episode judge axis (cross-chapter discourse flow)

	// WeakestSegmentIDs is the chapter-judge / episode-judge top-N weakest
	// segments, looked up from ordinals to real segment IDs by the caller.
	// Limited to <= 3 in the MVP (matches each judge schema's maxItems=3).
	WeakestSegmentIDs []uint

	// History is the existing episodes.rework_attempts entries. Decide
	// inspects this for oscillation detection and per-level cap
	// enforcement; it never mutates.
	History []ReworkAttempt

	// AccumulatedCostUSD is the running total of cost incurred by past
	// rework on this episode (sum of CostUSDDelta in History, plus any
	// initial pipeline cost the caller may want to include). Used only
	// for the cost ceiling check.
	AccumulatedCostUSD float64

	// Configuration values, plumbed from internal/config so the pure
	// function never reads env vars directly.
	SegmentRetryMaxAttempts     int
	ChapterReworkMaxRounds      int
	EpisodeReworkCostCeilingUSD float64
	OscillationThreshold        int
}

// CountConsecutiveSame returns how many entries at the END of `history`
// share the same (Level, TargetID, Verdict) as `cur`. Used for oscillation
// detection: if the count is >= threshold after appending the current
// attempt, we escalate. Exported because Engine also needs to reason about
// it for logging.
func CountConsecutiveSame(history []ReworkAttempt, level Level, targetID uint, verdict string) int {
	count := 0
	for i := len(history) - 1; i >= 0; i-- {
		h := history[i]
		if h.Level == level && h.TargetID == targetID && h.Verdict == verdict {
			count++
			continue
		}
		break
	}
	return count
}
