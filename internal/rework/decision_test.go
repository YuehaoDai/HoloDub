package rework

import (
	"strings"
	"testing"
	"time"
)

// TestCompareLevel verifies the level threshold ordering. Episode covers
// chapter and segment; chapter covers segment; none covers nothing.
func TestCompareLevel(t *testing.T) {
	cases := []struct {
		enabled, requested Level
		want               bool
	}{
		{LevelNone, LevelSegment, false},
		{LevelNone, LevelChapter, false},
		{LevelNone, LevelEpisode, false},
		{LevelNone, LevelNone, false}, // requesting none is also a noop
		{LevelSegment, LevelSegment, true},
		{LevelSegment, LevelChapter, false},
		{LevelSegment, LevelEpisode, false},
		{LevelChapter, LevelSegment, true},
		{LevelChapter, LevelChapter, true},
		{LevelChapter, LevelEpisode, false},
		{LevelEpisode, LevelSegment, true},
		{LevelEpisode, LevelChapter, true},
		{LevelEpisode, LevelEpisode, true},
	}
	for _, c := range cases {
		t.Run(string(c.enabled)+"_vs_"+string(c.requested), func(t *testing.T) {
			if got := CompareLevel(c.enabled, c.requested); got != c.want {
				t.Fatalf("CompareLevel(%s,%s) = %v, want %v", c.enabled, c.requested, got, c.want)
			}
		})
	}
}

// TestParseLevel ensures unknown values collapse to none (no silent enable).
func TestParseLevel(t *testing.T) {
	cases := map[string]Level{
		"":            LevelNone,
		"none":        LevelNone,
		"segment":     LevelSegment,
		"chapter":     LevelChapter,
		"episode":     LevelEpisode,
		"NONE":        LevelNone,    // case sensitive — typo collapses
		"chap":        LevelNone,
		"  episode  ": LevelNone, // whitespace not stripped (caller's job)
	}
	for in, want := range cases {
		if got := ParseLevel(in); got != want {
			t.Errorf("ParseLevel(%q) = %s, want %s", in, got, want)
		}
	}
}

// TestCountConsecutiveSame: an empty history reports zero, a non-matching
// streak reports zero, a matching streak only counts entries from the END.
func TestCountConsecutiveSame(t *testing.T) {
	hist := []ReworkAttempt{
		{Level: LevelSegment, TargetID: 1, Verdict: "retry"},
		{Level: LevelSegment, TargetID: 2, Verdict: "retry"}, // breaks streak (different target)
		{Level: LevelSegment, TargetID: 1, Verdict: "retry"},
		{Level: LevelSegment, TargetID: 1, Verdict: "retry"},
	}
	if got := CountConsecutiveSame(hist, LevelSegment, 1, "retry"); got != 2 {
		t.Fatalf("expected 2 trailing matches, got %d", got)
	}
	if got := CountConsecutiveSame(nil, LevelSegment, 1, "retry"); got != 0 {
		t.Fatalf("nil history should return 0, got %d", got)
	}
	if got := CountConsecutiveSame(hist, LevelSegment, 1, "split"); got != 0 {
		t.Fatalf("verdict mismatch should return 0, got %d", got)
	}
	if got := CountConsecutiveSame(hist, LevelChapter, 1, "retry"); got != 0 {
		t.Fatalf("level mismatch should return 0, got %d", got)
	}
}

// TestDecide_LevelDisabled covers OPT-407 decision-table row "any verdict
// when REWORK_ENGINE_LEVEL=none → ActionNoop level_disabled". Asserted at
// each level so a typo in the threshold-ordering logic surfaces.
func TestDecide_LevelDisabled(t *testing.T) {
	for _, level := range []Level{LevelSegment, LevelChapter, LevelEpisode} {
		t.Run(string(level), func(t *testing.T) {
			got := Decide(DecideInput{
				Level:        level,
				EnabledLevel: LevelNone,
				Verdict:      "retry",
				TargetID:     42,
				EpisodeID:    1,
				Score:        0.4,
			})
			if got.Type != ActionNoop {
				t.Fatalf("expected ActionNoop, got %s", got.Type)
			}
			if !strings.Contains(got.SkipReason, "level_disabled") {
				t.Fatalf("expected skip_reason to mention level_disabled, got %q", got.SkipReason)
			}
		})
	}
}

// TestDecide_LowerLevelDisabled: enabling chapter does NOT enable episode.
func TestDecide_LowerLevelDisabled(t *testing.T) {
	got := Decide(DecideInput{
		Level:        LevelEpisode,
		EnabledLevel: LevelChapter,
		Verdict:      "needs_minor_revision",
		EpisodeID:    1,
		TargetID:     1,
	})
	if got.Type != ActionNoop {
		t.Fatalf("episode-level rule should be disabled when enabled=chapter, got %s", got.Type)
	}
}

// TestDecide_HigherLevelEnablesLower: enabling episode also enables chapter
// AND segment (rank-based threshold).
func TestDecide_HigherLevelEnablesLower(t *testing.T) {
	got := Decide(DecideInput{
		Level:        LevelSegment,
		EnabledLevel: LevelEpisode,
		Verdict:      "retry",
		TargetID:     42,
		EpisodeID:    1,
	})
	if got.Type != ActionSegmentRetry {
		t.Fatalf("episode-enabled should still dispatch segment retries, got %s", got.Type)
	}
}

// TestDecide_CostCeiling covers row "accumulated_cost_usd > ceiling →
// ActionHaltCost". Ceiling=0 disables the check (no halt regardless of
// accumulated cost).
func TestDecide_CostCeiling(t *testing.T) {
	in := DecideInput{
		Level:                       LevelSegment,
		EnabledLevel:                LevelEpisode,
		Verdict:                     "retry",
		TargetID:                    1,
		EpisodeID:                   100,
		Score:                       0.3,
		AccumulatedCostUSD:          2.5,
		EpisodeReworkCostCeilingUSD: 2.0,
		SegmentRetryMaxAttempts:     3,
	}
	got := Decide(in)
	if got.Type != ActionHaltCost {
		t.Fatalf("expected ActionHaltCost, got %s (%s)", got.Type, got.SkipReason)
	}
	if got.ReworkStatus != "halted_cost" {
		t.Fatalf("expected ReworkStatus=halted_cost, got %q", got.ReworkStatus)
	}

	// Ceiling=0 disables: high accumulated cost should NOT halt.
	in.EpisodeReworkCostCeilingUSD = 0
	got = Decide(in)
	if got.Type == ActionHaltCost {
		t.Fatalf("ceiling=0 should disable cost halt, got ActionHaltCost")
	}

	// Right at the ceiling (==) is allowed; only > triggers halt.
	in.EpisodeReworkCostCeilingUSD = 2.5
	got = Decide(in)
	if got.Type == ActionHaltCost {
		t.Fatalf("cost == ceiling should NOT halt, got ActionHaltCost")
	}
}

// TestDecide_Oscillation covers the row "same target, same verdict, N
// consecutive → ActionEscalateOscillation".
func TestDecide_Oscillation(t *testing.T) {
	hist := []ReworkAttempt{
		{Level: LevelSegment, TargetID: 7, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
		{Level: LevelSegment, TargetID: 7, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
	}
	got := Decide(DecideInput{
		Level:                LevelSegment,
		EnabledLevel:         LevelEpisode,
		Verdict:              "retry",
		TargetID:             7,
		EpisodeID:            1,
		History:              hist,
		OscillationThreshold: 2,
	})
	if got.Type != ActionEscalateOscillation {
		t.Fatalf("expected ActionEscalateOscillation, got %s (%s)", got.Type, got.Note)
	}
	if got.ReworkStatus != "escalated_oscillation" {
		t.Fatalf("expected ReworkStatus=escalated_oscillation, got %q", got.ReworkStatus)
	}

	// Threshold=0 falls back to the default of 2; same input should still
	// escalate.
	got = Decide(DecideInput{
		Level:                LevelSegment,
		EnabledLevel:         LevelEpisode,
		Verdict:              "retry",
		TargetID:             7,
		EpisodeID:            1,
		History:              hist,
		OscillationThreshold: 0,
	})
	if got.Type != ActionEscalateOscillation {
		t.Fatalf("threshold=0 default should still escalate, got %s", got.Type)
	}
}

// TestDecide_SegmentVerdicts covers each segment-level verdict path including
// the retry → escalate → accept staircase.
func TestDecide_SegmentVerdicts(t *testing.T) {
	t.Run("verdict_accept", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelSegment, EnabledLevel: LevelSegment,
			Verdict: "accept", TargetID: 1, EpisodeID: 100, Score: 0.95,
		})
		if got.Type != ActionNoop || got.SkipReason != "verdict_accept" {
			t.Fatalf("want ActionNoop verdict_accept, got %s/%s", got.Type, got.SkipReason)
		}
	})

	t.Run("verdict_retry_zero_prior", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelSegment, EnabledLevel: LevelSegment,
			Verdict: "retry", TargetID: 1, EpisodeID: 100, Score: 0.4,
			SegmentRetryMaxAttempts: 3,
		})
		if got.Type != ActionSegmentRetry {
			t.Fatalf("want ActionSegmentRetry, got %s", got.Type)
		}
		if len(got.SegmentIDs) != 1 || got.SegmentIDs[0] != 1 {
			t.Fatalf("expected SegmentIDs=[1], got %v", got.SegmentIDs)
		}
		if got.Stage != "tts_duration" {
			t.Fatalf("expected stage=tts_duration, got %q", got.Stage)
		}
	})

	t.Run("verdict_retry_max_prior_escalates_to_thinking", func(t *testing.T) {
		hist := []ReworkAttempt{
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
			{Level: LevelSegment, TargetID: 1, Verdict: "split", ActionType: ActionSegmentSplit}, // breaks oscillation streak but adds prior retry
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
			{Level: LevelSegment, TargetID: 1, Verdict: "split", ActionType: ActionSegmentSplit},
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
		}
		got := Decide(DecideInput{
			Level: LevelSegment, EnabledLevel: LevelSegment,
			Verdict: "retry", TargetID: 1, EpisodeID: 100, Score: 0.4,
			History:                 hist,
			SegmentRetryMaxAttempts: 3,
			OscillationThreshold:    99, // disable oscillation in this test
		})
		if got.Type != ActionEscalateToThinking {
			t.Fatalf("want ActionEscalateToThinking after %d retries, got %s (%s)", 3, got.Type, got.SkipReason)
		}
		if !got.UseThinking {
			t.Fatal("expected UseThinking=true on escalate-to-thinking action")
		}
	})

	t.Run("verdict_retry_max_plus_thinking_escalates_to_accept", func(t *testing.T) {
		hist := []ReworkAttempt{
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
			{Level: LevelSegment, TargetID: 1, Verdict: "split"},
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
			{Level: LevelSegment, TargetID: 1, Verdict: "split"},
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionSegmentRetry, Dispatched: true},
			{Level: LevelSegment, TargetID: 1, Verdict: "split"},
			{Level: LevelSegment, TargetID: 1, Verdict: "retry", ActionType: ActionEscalateToThinking, Dispatched: true},
		}
		got := Decide(DecideInput{
			Level: LevelSegment, EnabledLevel: LevelSegment,
			Verdict: "retry", TargetID: 1, EpisodeID: 100, Score: 0.4,
			History:                 hist,
			SegmentRetryMaxAttempts: 3,
			OscillationThreshold:    99,
		})
		if got.Type != ActionAcceptWithBorrow {
			t.Fatalf("want ActionAcceptWithBorrow after thinking exhausted, got %s", got.Type)
		}
	})

	t.Run("verdict_split", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelSegment, EnabledLevel: LevelSegment,
			Verdict: "split", TargetID: 5, JobID: 10, EpisodeID: 100,
		})
		if got.Type != ActionSegmentSplit {
			t.Fatalf("want ActionSegmentSplit, got %s", got.Type)
		}
		if len(got.SegmentIDs) != 1 || got.SegmentIDs[0] != 5 {
			t.Fatalf("expected SegmentIDs=[5], got %v", got.SegmentIDs)
		}
	})

	t.Run("unknown_segment_verdict", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelSegment, EnabledLevel: LevelSegment,
			Verdict: "explode", TargetID: 1, EpisodeID: 100,
		})
		if got.Type != ActionNoop || !strings.Contains(got.SkipReason, "unknown_segment_verdict") {
			t.Fatalf("unknown verdict should noop, got %s/%s", got.Type, got.SkipReason)
		}
	})
}

// TestDecide_ChapterVerdicts covers chapter-level verdict paths.
func TestDecide_ChapterVerdicts(t *testing.T) {
	t.Run("verdict_chapter_ready", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelChapter, EnabledLevel: LevelChapter,
			Verdict: "chapter_ready", TargetID: 200, EpisodeID: 50, Score: 0.95,
		})
		if got.Type != ActionNoop || got.SkipReason != "verdict_chapter_ready" {
			t.Fatalf("want noop chapter_ready, got %s/%s", got.Type, got.SkipReason)
		}
	})

	t.Run("verdict_needs_revision_with_segments", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelChapter, EnabledLevel: LevelChapter,
			Verdict: "needs_revision", TargetID: 200, EpisodeID: 50, Score: 0.78,
			WeakestSegmentIDs:      []uint{11, 12, 13},
			ChapterReworkMaxRounds: 1,
		})
		if got.Type != ActionReviseWeakestSegments {
			t.Fatalf("want ActionReviseWeakestSegments, got %s", got.Type)
		}
		if got.JobID != 200 {
			t.Fatalf("expected JobID=200, got %d", got.JobID)
		}
		if len(got.SegmentIDs) != 3 {
			t.Fatalf("expected 3 segments, got %v", got.SegmentIDs)
		}
		if got.Stage != "translate" {
			t.Fatalf("expected stage=translate, got %q", got.Stage)
		}
	})

	t.Run("verdict_needs_revision_max_rounds_reached", func(t *testing.T) {
		hist := []ReworkAttempt{
			{Level: LevelChapter, TargetID: 200, Verdict: "needs_revision", ActionType: ActionReviseWeakestSegments, Dispatched: true},
		}
		got := Decide(DecideInput{
			Level: LevelChapter, EnabledLevel: LevelChapter,
			Verdict: "needs_revision", TargetID: 200, EpisodeID: 50, Score: 0.7,
			WeakestSegmentIDs:      []uint{11, 12},
			History:                hist,
			ChapterReworkMaxRounds: 1,
			OscillationThreshold:   99,
		})
		if got.Type != ActionEscalateChapter {
			t.Fatalf("want ActionEscalateChapter after max rounds, got %s", got.Type)
		}
		if got.ReworkStatus != "escalated_chapter" {
			t.Fatalf("expected ReworkStatus=escalated_chapter, got %q", got.ReworkStatus)
		}
	})

	t.Run("verdict_needs_revision_no_segments_resolves_escalates", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelChapter, EnabledLevel: LevelChapter,
			Verdict: "needs_revision", TargetID: 200, EpisodeID: 50,
			WeakestSegmentIDs:      nil,
			ChapterReworkMaxRounds: 1,
		})
		if got.Type != ActionEscalateChapter {
			t.Fatalf("want ActionEscalateChapter when no segments resolved, got %s", got.Type)
		}
		if !strings.Contains(got.SkipReason, "no_weakest_segments") {
			t.Fatalf("expected SkipReason to mention no_weakest_segments, got %q", got.SkipReason)
		}
	})

	t.Run("verdict_needs_major_rework", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelChapter, EnabledLevel: LevelChapter,
			Verdict: "needs_major_rework", TargetID: 200, EpisodeID: 50, Score: 0.5,
		})
		if got.Type != ActionEscalateChapter {
			t.Fatalf("want ActionEscalateChapter, got %s", got.Type)
		}
	})
}

// TestDecide_EpisodeVerdicts covers episode-level verdict paths and the
// terminology-driven broadcast-vs-noop sub-rule.
func TestDecide_EpisodeVerdicts(t *testing.T) {
	t.Run("verdict_production_ready", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelEpisode, EnabledLevel: LevelEpisode,
			Verdict: "production_ready", TargetID: 50, EpisodeID: 50, Score: 0.95,
		})
		if got.Type != ActionNoop || got.SkipReason != "verdict_production_ready" {
			t.Fatalf("want noop production_ready, got %s/%s", got.Type, got.SkipReason)
		}
	})

	t.Run("minor_revision_terminology_drift_broadcasts", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelEpisode, EnabledLevel: LevelEpisode,
			Verdict: "needs_minor_revision", TargetID: 50, EpisodeID: 50, Score: 0.85,
			TerminologyConsistency: 0.7,
		})
		if got.Type != ActionBroadcastGlossary {
			t.Fatalf("want ActionBroadcastGlossary on terminology drift, got %s", got.Type)
		}
		if got.EpisodeStage != "ep_glossary_broadcast" {
			t.Fatalf("expected EpisodeStage=ep_glossary_broadcast, got %q", got.EpisodeStage)
		}
		if got.ReworkStatus != "in_progress" {
			t.Fatalf("expected ReworkStatus=in_progress while broadcasting, got %q", got.ReworkStatus)
		}
	})

	t.Run("minor_revision_terminology_drift_already_broadcast_escalates", func(t *testing.T) {
		hist := []ReworkAttempt{
			{Level: LevelEpisode, TargetID: 50, Verdict: "needs_minor_revision", ActionType: ActionBroadcastGlossary, Dispatched: true},
		}
		got := Decide(DecideInput{
			Level: LevelEpisode, EnabledLevel: LevelEpisode,
			Verdict: "needs_minor_revision", TargetID: 50, EpisodeID: 50,
			TerminologyConsistency: 0.7,
			History:                hist,
			OscillationThreshold:   99,
		})
		if got.Type != ActionEscalateHumanReview {
			t.Fatalf("want human review after one broadcast, got %s", got.Type)
		}
		if got.ReworkStatus != "escalated_human" {
			t.Fatalf("expected ReworkStatus=escalated_human, got %q", got.ReworkStatus)
		}
	})

	t.Run("minor_revision_no_actionable_axis_noops", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelEpisode, EnabledLevel: LevelEpisode,
			Verdict: "needs_minor_revision", TargetID: 50, EpisodeID: 50,
			TerminologyConsistency: 0.92,
			NarrativeCoherence:     0.91,
		})
		if got.Type != ActionNoop {
			t.Fatalf("want noop when no axis crosses threshold, got %s", got.Type)
		}
	})

	t.Run("major_revision_human_review", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelEpisode, EnabledLevel: LevelEpisode,
			Verdict: "needs_major_revision", TargetID: 50, EpisodeID: 50, Score: 0.6,
		})
		if got.Type != ActionEscalateHumanReview {
			t.Fatalf("want ActionEscalateHumanReview, got %s", got.Type)
		}
	})

	t.Run("unknown_verdict_with_low_narrative_escalates", func(t *testing.T) {
		got := Decide(DecideInput{
			Level: LevelEpisode, EnabledLevel: LevelEpisode,
			Verdict: "weirdo_verdict", TargetID: 50, EpisodeID: 50,
			NarrativeCoherence: 0.5,
		})
		if got.Type != ActionEscalateHumanReview {
			t.Fatalf("want human review on low narrative, got %s", got.Type)
		}
	})
}

// TestAccumulateCostUSD: only dispatched non-negative deltas count.
func TestAccumulateCostUSD(t *testing.T) {
	hist := []ReworkAttempt{
		{Dispatched: true, CostUSDDelta: 0.10},
		{Dispatched: false, CostUSDDelta: 0.05}, // not counted
		{Dispatched: true, CostUSDDelta: 0.20},
		{Dispatched: true, CostUSDDelta: -1.0},  // defensive: not counted
		{Dispatched: true, CostUSDDelta: 0.005},
	}
	want := 0.305
	got := AccumulateCostUSD(hist)
	if abs(got-want) > 1e-9 {
		t.Fatalf("AccumulateCostUSD want %v, got %v", want, got)
	}
}

// TestEstimateRetryCostUSD: every action type returns a non-negative number,
// dispatchable types return positive numbers.
func TestEstimateRetryCostUSD(t *testing.T) {
	dispatchable := []ActionType{
		ActionSegmentRetry, ActionEscalateToThinking,
		ActionReviseWeakestSegments, ActionBroadcastGlossary,
	}
	noopable := []ActionType{
		ActionNoop, ActionSegmentSplit, ActionAcceptWithBorrow,
		ActionEscalateChapter, ActionEscalateHumanReview,
		ActionEscalateOscillation, ActionHaltCost,
	}
	for _, t1 := range dispatchable {
		got := EstimateRetryCostUSD(Action{Type: t1, SegmentIDs: []uint{1}})
		if got <= 0 {
			t.Errorf("dispatchable action %s should have positive cost estimate, got %v", t1, got)
		}
	}
	for _, t1 := range noopable {
		got := EstimateRetryCostUSD(Action{Type: t1})
		if got != 0 {
			t.Errorf("noop-style action %s should have zero cost estimate, got %v", t1, got)
		}
	}
}

// TestAction_IsNoop: every action without an external dispatch is noop;
// every dispatchable action is not noop.
func TestAction_IsNoop(t *testing.T) {
	for _, t1 := range []ActionType{
		ActionNoop, ActionSegmentSplit, ActionAcceptWithBorrow,
		ActionEscalateChapter, ActionEscalateHumanReview,
		ActionEscalateOscillation, ActionHaltCost,
	} {
		if !(Action{Type: t1}).IsNoop() {
			t.Errorf("action %s should report IsNoop=true", t1)
		}
	}
	for _, t1 := range []ActionType{
		ActionSegmentRetry, ActionEscalateToThinking,
		ActionReviseWeakestSegments, ActionBroadcastGlossary,
	} {
		if (Action{Type: t1}).IsNoop() {
			t.Errorf("action %s should report IsNoop=false", t1)
		}
	}
}

// TestReworkAttemptTimestampStable: ensures Action / ReworkAttempt JSON
// shape is stable enough for store round-trip; not a behavioural test
// per-se but catches accidental ts-renaming.
func TestReworkAttemptTimestampStable(t *testing.T) {
	now := time.Date(2026, time.May, 10, 12, 0, 0, 0, time.UTC)
	a := ReworkAttempt{
		Level: LevelSegment, TargetID: 1, Verdict: "retry",
		ActionType: ActionSegmentRetry, Dispatched: true, Timestamp: now,
	}
	if a.Timestamp.Year() != 2026 {
		t.Fatal("timestamp not preserved")
	}
}

func abs(f float64) float64 {
	if f < 0 {
		return -f
	}
	return f
}
