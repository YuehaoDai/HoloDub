package rework

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/models"
	"holodub/internal/observability"
	"holodub/internal/store"

	"gorm.io/gorm"
)

// RetryJobAPI is the narrow interface the rework Engine needs from the
// pipeline service. Defined in this package (not in `pipeline`) so the
// Engine depends only on `store` + `models` + an interface, breaking the
// would-be circular import (pipeline → rework → pipeline).
//
// *pipeline.Service satisfies this interface today via its existing
// (*Service).RetryJob and (*Service).EnqueueEpisodeStage methods.
type RetryJobAPI interface {
	RetryJob(ctx context.Context, jobID uint, stage models.JobStage, segmentIDs []uint, requestedBy string) error
	EnqueueEpisodeStage(ctx context.Context, episodeID uint, stage models.EpisodeStage, requestedBy, reason string) error
}

// Engine wires Decide() with the persistence + dispatch side effects.
//
// Each MaybeRework* method:
//  1. loads Episode + extracts history / accumulated cost,
//  2. resolves any judge-result ordinals into real DB segment IDs,
//  3. calls Decide with a populated DecideInput,
//  4. records the resulting Action onto episodes.rework_attempts,
//  5. dispatches the action's external side effect (RetryJob / enqueue
//     EpisodeStage / set Episode.ReworkStatus).
//
// All errors are logged and swallowed — rework engine failures MUST NOT
// fail the surrounding judge goroutine, which already runs in a detached
// background context. The caller (stage_tts.go / stage_episode_judge.go)
// invokes us as fire-and-forget; we are the last hop.
type Engine struct {
	cfg     config.Config
	store   *store.Store
	api     RetryJobAPI
	clock   func() time.Time // injectable for tests
}

// NewEngine constructs the OPT-407 Engine. cfg.ReworkEngineLevel governs
// whether decisions actually dispatch (LevelNone = persist attempt, skip
// dispatch); the engine is otherwise live as soon as cfg is read.
func NewEngine(cfg config.Config, st *store.Store, api RetryJobAPI) *Engine {
	return &Engine{
		cfg:   cfg,
		store: st,
		api:   api,
		clock: time.Now,
	}
}

// MaybeReworkSegment is the OPT-407 hook called from maybeJudgeSegmentAsync
// AFTER UpdateSegmentJudgeResult succeeds. Inputs come straight from the
// segment-level llm.JudgeResult (verdict + OverallScore) so the engine
// does not need to import package llm.
//
// driftSec (OPT-407-followup-6) is the segment's "actual TTS audio length
// minus target slot length" in seconds (signed). Positive = audio longer
// than target. Pass 0 when drift is unknown — the drift guard then
// silently disables itself for this segment.
//
// Always safe to call — gates on cfg.ReworkEngineLevel internally and on
// EpisodeID == 0 (orphan segments are rare but should not crash us).
func (e *Engine) MaybeReworkSegment(
	ctx context.Context,
	jobID uint,
	episodeID uint,
	segmentID uint,
	verdict string,
	score float64,
	driftSec float64,
) {
	if e == nil || e.store == nil {
		return
	}
	if episodeID == 0 || segmentID == 0 {
		return
	}
	in := DecideInput{
		Level:                  LevelSegment,
		EnabledLevel:           ParseLevel(e.cfg.ReworkEngineLevel),
		Verdict:                verdict,
		TargetID:               segmentID,
		JobID:                  jobID,
		EpisodeID:              episodeID,
		Score:                  score,
		DriftSec:               driftSec,
		DriftHardLimitOverSec:  e.cfg.SegmentDriftHardLimitOverSec,
		DriftHardLimitUnderSec: e.cfg.SegmentDriftHardLimitUnderSec,
	}
	e.runDecisionLoop(ctx, in)
}

// MaybeReworkChapter is the OPT-407 hook called from maybeJudgeChapterAsync
// AFTER UpdateChapterJudgeResult succeeds. Inputs are sourced from the
// chapter-level llm.ChapterJudgeResult.
//
// weakestOrdinals MUST be the 1-indexed segment ordinals WITHIN the
// chapter (matching ChapterJudgeWeakSegment.Ordinal). The engine resolves
// those into real DB segment IDs via ListSegments before calling Decide.
// Pass nil / empty when verdict has no actionable rework target.
func (e *Engine) MaybeReworkChapter(
	ctx context.Context,
	jobID uint,
	episodeID uint,
	verdict string,
	score float64,
	weakestOrdinals []int,
) {
	if e == nil || e.store == nil {
		return
	}
	if episodeID == 0 || jobID == 0 {
		return
	}
	weakestIDs := e.resolveOrdinalsToIDs(ctx, jobID, weakestOrdinals)
	in := DecideInput{
		Level:             LevelChapter,
		EnabledLevel:      ParseLevel(e.cfg.ReworkEngineLevel),
		Verdict:           verdict,
		TargetID:          jobID, // chapter level: TargetID == JobID
		JobID:             jobID,
		EpisodeID:         episodeID,
		Score:             score,
		WeakestSegmentIDs: weakestIDs,
	}
	e.runDecisionLoop(ctx, in)
}

// MaybeReworkEpisode is the OPT-407 hook called from maybeJudgeEpisodeAsync
// AFTER UpdateEpisodeJudgeResult succeeds. Inputs are sourced from the
// episode-level llm.EpisodeJudgeResult.
//
// terminologyConsistency / narrativeCoherence are the corresponding axis
// scores (used by the rule for broadcast vs human-review escalation).
func (e *Engine) MaybeReworkEpisode(
	ctx context.Context,
	episodeID uint,
	verdict string,
	score float64,
	terminologyConsistency float64,
	narrativeCoherence float64,
) {
	if e == nil || e.store == nil {
		return
	}
	if episodeID == 0 {
		return
	}
	in := DecideInput{
		Level:                  LevelEpisode,
		EnabledLevel:           ParseLevel(e.cfg.ReworkEngineLevel),
		Verdict:                verdict,
		TargetID:               episodeID,
		EpisodeID:              episodeID,
		Score:                  score,
		TerminologyConsistency: terminologyConsistency,
		NarrativeCoherence:     narrativeCoherence,
	}
	e.runDecisionLoop(ctx, in)
}

// runDecisionLoop is the shared post-load → Decide → persist → dispatch
// path used by all three Maybe* entry points. It loads the Episode,
// fills in the cost / history / config fields, calls Decide, then
// records + dispatches.
func (e *Engine) runDecisionLoop(ctx context.Context, in DecideInput) {
	ep, err := e.store.GetEpisode(ctx, in.EpisodeID)
	if err != nil {
		if !errors.Is(err, gorm.ErrRecordNotFound) {
			slog.Warn("rework engine: load episode failed; skipping",
				"episode_id", in.EpisodeID, "level", in.Level, "error", err)
		}
		return
	}
	// Halt all rework once the episode has been escalated / halted; the
	// operator must clear rework_status before we resume. This is the safety
	// equivalent of "burn everything down" — ensures a stuck episode never
	// keeps generating dispatch attempts on every subsequent judge run.
	if isHaltedStatus(ep.ReworkStatus) {
		slog.Info("rework engine: episode is halted, skipping",
			"episode_id", in.EpisodeID,
			"rework_status", ep.ReworkStatus,
			"level", in.Level,
			"verdict", in.Verdict,
		)
		return
	}

	in.History = decodeAttempts(ep.ReworkAttempts)
	in.AccumulatedCostUSD = AccumulateCostUSD(in.History)
	if ep.AccumulatedCostUSD != nil && *ep.AccumulatedCostUSD > in.AccumulatedCostUSD {
		// Trust the persisted total when it's higher (the column may
		// include LLM cost OUTSIDE rework attempts that we still want
		// counted — currently only rework attempts contribute, but keep
		// this defensive for OPT-407-followup-3 multi-source ledger).
		in.AccumulatedCostUSD = *ep.AccumulatedCostUSD
	}

	in.SegmentRetryMaxAttempts = e.cfg.SegmentRetryMaxAttempts
	in.ChapterReworkMaxRounds = e.cfg.ChapterReworkMaxRounds
	in.EpisodeReworkCostCeilingUSD = e.cfg.EpisodeReworkCostCeilingUSD
	in.OscillationThreshold = e.cfg.ReworkOscillationThreshold
	// OPT-407-followup-6: drift guard config is plumbed here too so
	// callers that don't go through MaybeReworkSegment (chapter / episode
	// paths) still receive the threshold values. They never set DriftSec
	// so the guard is naturally inert at chapter / episode level.
	in.DriftHardLimitOverSec = e.cfg.SegmentDriftHardLimitOverSec
	in.DriftHardLimitUnderSec = e.cfg.SegmentDriftHardLimitUnderSec

	action := Decide(in)

	dispatched := false
	if !action.IsNoop() && CompareLevel(in.EnabledLevel, in.Level) {
		if err := e.execute(ctx, action); err != nil {
			slog.Warn("rework engine: dispatch failed; recording attempt as not-dispatched",
				"episode_id", in.EpisodeID,
				"level", in.Level,
				"action", action.Type,
				"error", err,
			)
			// Record as not-dispatched (skip_reason carries the failure)
			// so the next pass sees the gap and can retry.
			action.SkipReason = "dispatch_failed: " + err.Error()
		} else {
			dispatched = true
		}
	}

	costDelta := 0.0
	if dispatched {
		costDelta = EstimateRetryCostUSD(action)
	}

	attempt := ReworkAttempt{
		Level:        in.Level,
		TargetID:     in.TargetID,
		JobID:        in.JobID,
		Verdict:      in.Verdict,
		BeforeScore:  in.Score,
		ActionType:   action.Type,
		Dispatched:   dispatched,
		SkipReason:   action.SkipReason,
		Stage:        action.Stage,
		SegmentIDs:   action.SegmentIDs,
		CostUSDDelta: costDelta,
		Note:         action.Note,
		Timestamp:    e.clock().UTC(),
	}

	if appErr := e.store.AppendEpisodeReworkAttempt(ctx, in.EpisodeID, attempt, costDelta); appErr != nil {
		slog.Warn("rework engine: append rework attempt failed",
			"episode_id", in.EpisodeID, "level", in.Level, "error", appErr)
	}

	// Episode-level status mutations (escalated_human / halted_cost / etc.)
	// are best-effort: a status mismatch never blocks dispatch.
	if action.ReworkStatus != "" {
		if statusErr := e.store.SetEpisodeReworkStatus(ctx, in.EpisodeID, action.ReworkStatus); statusErr != nil {
			slog.Warn("rework engine: set rework_status failed",
				"episode_id", in.EpisodeID, "status", action.ReworkStatus, "error", statusErr)
		}
	}

	observability.IncReworkAction(string(in.Level), string(action.Type), boolToStr(dispatched))

	slog.Info("rework engine: decision recorded",
		"episode_id", in.EpisodeID,
		"level", in.Level,
		"target_id", in.TargetID,
		"verdict", in.Verdict,
		"score", in.Score,
		"action", action.Type,
		"dispatched", dispatched,
		"skip_reason", action.SkipReason,
		"cost_delta_usd", costDelta,
		"acc_cost_usd", in.AccumulatedCostUSD,
		"note", action.Note,
	)
}

// execute dispatches the side effect of an Action through the RetryJobAPI
// or directly via the store. ActionNoop and the various escalate variants
// have no external side effect beyond the Episode.ReworkStatus update
// handled in runDecisionLoop.
func (e *Engine) execute(ctx context.Context, action Action) error {
	if e.api == nil {
		return errors.New("rework engine: RetryJobAPI not configured")
	}
	switch action.Type {
	case ActionSegmentRetry, ActionEscalateToThinking:
		// Single-segment retry on tts_duration.
		// RetryJob already calls ResetSegmentsForRerun for stage=tts_duration.
		return e.api.RetryJob(ctx, action.JobID,
			models.JobStage(action.Stage), action.SegmentIDs, "rework_engine")

	case ActionReviseWeakestSegments:
		// Chapter-level: re-translate the listed segments. RetryJob does
		// NOT auto-reset for stage=translate, so we explicitly reset
		// before re-queueing — otherwise runTTSDuration would skip the
		// segments because their status is still "synthesized".
		if err := e.store.ResetSegmentsForRerun(ctx, action.SegmentIDs); err != nil {
			return err
		}
		return e.api.RetryJob(ctx, action.JobID,
			models.JobStage(action.Stage), action.SegmentIDs, "rework_engine_chapter")

	case ActionBroadcastGlossary:
		return e.api.EnqueueEpisodeStage(ctx, action.EpisodeID,
			models.EpisodeStage(action.EpisodeStage),
			"rework_engine", "rework_episode_glossary_drift")

	default:
		// Non-dispatching actions (Noop / Escalate*/AcceptWithBorrow / Halt /
		// Split) fall through with no error — runDecisionLoop will still
		// persist the attempt + status.
		return nil
	}
}

// resolveOrdinalsToIDs converts chapter-internal segment ordinals (the
// 1-indexed values that appear in ChapterJudgeWeakSegment.Ordinal) into
// real DB segment IDs by listing the chapter's segments and matching on
// Segment.Ordinal. Returns an empty slice if the chapter has no segments
// or the lookup fails.
func (e *Engine) resolveOrdinalsToIDs(ctx context.Context, jobID uint, ordinals []int) []uint {
	if len(ordinals) == 0 {
		return nil
	}
	segs, err := e.store.ListSegments(ctx, jobID, nil)
	if err != nil || len(segs) == 0 {
		return nil
	}
	want := make(map[int]struct{}, len(ordinals))
	for _, o := range ordinals {
		if o > 0 {
			want[o] = struct{}{}
		}
	}
	out := make([]uint, 0, len(want))
	for _, seg := range segs {
		if _, ok := want[seg.Ordinal]; ok {
			out = append(out, seg.ID)
		}
	}
	return out
}

func decodeAttempts(jsonBytes []byte) []ReworkAttempt {
	if len(jsonBytes) == 0 {
		return nil
	}
	var out []ReworkAttempt
	if err := json.Unmarshal(jsonBytes, &out); err != nil {
		// Corrupt history is logged-and-ignored: better to start fresh
		// than crash the rework hook.
		slog.Warn("rework engine: rework_attempts decode failed; treating as empty",
			"error", err)
		return nil
	}
	return out
}

func boolToStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

// IsHaltedReworkStatus reports whether an Episode's rework_status indicates
// that no further auto-rework should be attempted. The engine refuses to
// dispatch when this is true; an operator must clear the column manually.
//
// Exported so non-engine callers (judge_backfill, future operator endpoints)
// can apply the same gate without re-implementing the status set.
func IsHaltedReworkStatus(s string) bool {
	switch strings.TrimSpace(s) {
	case "halted_cost", "escalated_human", "escalated_oscillation",
		"escalated_chapter":
		return true
	}
	return false
}

// isHaltedStatus is the legacy unexported alias kept for the existing
// engine call site. It will be inlined / removed once all internal uses
// migrate to the exported name; keeping it as a shim avoids a noisy diff
// in this followup.
func isHaltedStatus(s string) bool { return IsHaltedReworkStatus(s) }
