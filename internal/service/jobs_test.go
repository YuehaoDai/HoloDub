package service

import (
	"context"
	"errors"
	"testing"

	"holodub/internal/models"
)

// fakeJobsAPI is a hand-rolled fake used by HTTP-level handler tests in a
// future PR. It also documents the JobsAPI contract: every call records its
// invocation arguments and lets the test pre-program a return error.
type fakeJobsAPI struct {
	starts                []startCall
	retries               []retryCall
	cancels               []uint
	confirmSegmentations  []confirmCall
	retryASRs             []retryASRCall
	startErr              error
	retryErr              error
	cancelErr             error
	confirmErr            error
	retryASRErr           error
}

type startCall struct {
	JobID       uint
	RequestedBy string
}

type retryCall struct {
	JobID       uint
	Stage       models.JobStage
	SegmentIDs  []uint
	RequestedBy string
}

type confirmCall struct {
	JobID       uint
	RequestedBy string
}

type retryASRCall struct {
	JobID       uint
	RequestedBy string
}

func (f *fakeJobsAPI) Start(_ context.Context, jobID uint, requestedBy string) error {
	f.starts = append(f.starts, startCall{JobID: jobID, RequestedBy: requestedBy})
	return f.startErr
}
func (f *fakeJobsAPI) Retry(_ context.Context, jobID uint, stage models.JobStage, ids []uint, by string) error {
	f.retries = append(f.retries, retryCall{JobID: jobID, Stage: stage, SegmentIDs: ids, RequestedBy: by})
	return f.retryErr
}
func (f *fakeJobsAPI) Cancel(_ context.Context, jobID uint) error {
	f.cancels = append(f.cancels, jobID)
	return f.cancelErr
}
func (f *fakeJobsAPI) ConfirmSegmentation(_ context.Context, jobID uint, by string) error {
	f.confirmSegmentations = append(f.confirmSegmentations, confirmCall{JobID: jobID, RequestedBy: by})
	return f.confirmErr
}
func (f *fakeJobsAPI) RetryASR(_ context.Context, jobID uint, by string) error {
	f.retryASRs = append(f.retryASRs, retryASRCall{JobID: jobID, RequestedBy: by})
	return f.retryASRErr
}

func TestJobsAPI_FakeDoubleAsContract(t *testing.T) {
	// This test verifies that the fake satisfies JobsAPI and that callers can
	// treat Start/Retry/... as recorded invocations.
	var api JobsAPI = &fakeJobsAPI{startErr: errors.New("nope")}

	if err := api.Start(context.Background(), 7, "user"); err == nil {
		t.Fatal("expected fake to surface configured error")
	}
	if err := api.Cancel(context.Background(), 7); err != nil {
		t.Fatalf("expected nil err, got %v", err)
	}
	concrete := api.(*fakeJobsAPI)
	if len(concrete.starts) != 1 || concrete.starts[0].JobID != 7 {
		t.Fatalf("expected one start call for job 7, got %+v", concrete.starts)
	}
	if len(concrete.cancels) != 1 || concrete.cancels[0] != 7 {
		t.Fatalf("expected one cancel for 7, got %+v", concrete.cancels)
	}
}
