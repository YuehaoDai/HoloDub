package rework

import (
	"context"
	"testing"

	"holodub/internal/models"
)

// fakeRetryAPI is a recording implementation of RetryJobAPI used by the
// engine dispatch test. It does not depend on the queue / store / ml
// stack — every call is captured in an internal slice so the test can
// assert exactly what the engine dispatched.
type fakeRetryAPI struct {
	retryCalls   []retryCall
	episodeCalls []episodeCall
	dispatchCalls []dispatchCall
}

type retryCall struct {
	jobID       uint
	stage       models.JobStage
	segmentIDs  []uint
	requestedBy string
}
type episodeCall struct {
	episodeID   uint
	stage       models.EpisodeStage
	requestedBy string
	reason      string
}
type dispatchCall struct {
	jobID       uint
	stage       models.JobStage
	segmentIDs  []uint
	requestedBy string
	hint        *models.ReworkHint
}

func (f *fakeRetryAPI) RetryJob(_ context.Context, jobID uint, stage models.JobStage, segIDs []uint, by string) error {
	f.retryCalls = append(f.retryCalls, retryCall{jobID, stage, append([]uint(nil), segIDs...), by})
	return nil
}
func (f *fakeRetryAPI) EnqueueEpisodeStage(_ context.Context, epID uint, stage models.EpisodeStage, by, reason string) error {
	f.episodeCalls = append(f.episodeCalls, episodeCall{epID, stage, by, reason})
	return nil
}
func (f *fakeRetryAPI) DispatchSegmentRework(_ context.Context, jobID uint, stage models.JobStage, segIDs []uint, by string, hint *models.ReworkHint) error {
	f.dispatchCalls = append(f.dispatchCalls, dispatchCall{jobID, stage, append([]uint(nil), segIDs...), by, hint})
	return nil
}

// TestEngine_ExecuteSegmentRetry_UsesDispatchWithHint verifies that an
// ActionSegmentRetry is routed through DispatchSegmentRework with a
// populated ReworkHint instead of the legacy RetryJob path. This is
// the OPT-407-followup-2 / OPT-201 PR-8 invariant.
func TestEngine_ExecuteSegmentRetry_UsesDispatchWithHint(t *testing.T) {
	api := &fakeRetryAPI{}
	eng := &Engine{api: api}

	action := Action{
		Type:               ActionSegmentRetry,
		JobID:              7,
		SegmentIDs:         []uint{42},
		Stage:              "tts_duration",
		DriftThresholdHint: 0.05,
		SkipReason:         "drift_override (orig_verdict=accept ...)",
	}
	if err := eng.execute(context.Background(), action); err != nil {
		t.Fatalf("execute err: %v", err)
	}
	if len(api.dispatchCalls) != 1 {
		t.Fatalf("want 1 dispatch call, got %d", len(api.dispatchCalls))
	}
	if len(api.retryCalls) != 0 {
		t.Errorf("legacy RetryJob should NOT be called for ActionSegmentRetry under PR-8, got %d calls", len(api.retryCalls))
	}
	d := api.dispatchCalls[0]
	if d.jobID != 7 || len(d.segmentIDs) != 1 || d.segmentIDs[0] != 42 {
		t.Errorf("dispatch routing wrong: %+v", d)
	}
	if d.hint == nil {
		t.Fatalf("dispatch hint must be non-nil for ActionSegmentRetry")
	}
	if d.hint.PrevVerdict != "retry" {
		t.Errorf("hint.PrevVerdict: want 'retry', got %q", d.hint.PrevVerdict)
	}
	if d.hint.DriftThresholdHint != 0.05 {
		t.Errorf("hint.DriftThresholdHint: want 0.05, got %f", d.hint.DriftThresholdHint)
	}
}

func TestEngine_ExecuteEscalateToThinking_HintCarriesVerdict(t *testing.T) {
	api := &fakeRetryAPI{}
	eng := &Engine{api: api}
	if err := eng.execute(context.Background(), Action{
		Type:       ActionEscalateToThinking,
		JobID:      1,
		SegmentIDs: []uint{1},
		Stage:      "tts_duration",
	}); err != nil {
		t.Fatalf("execute err: %v", err)
	}
	if len(api.dispatchCalls) != 1 {
		t.Fatalf("want 1 dispatch call, got %d", len(api.dispatchCalls))
	}
	hint := api.dispatchCalls[0].hint
	if hint == nil || hint.PrevVerdict != "retry_thinking" {
		t.Errorf("EscalateToThinking should set PrevVerdict='retry_thinking', got %+v", hint)
	}
}
