package store

import (
	"context"
	"errors"
	"testing"

	"holodub/internal/models"

	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

// newTestStore spins up an in-memory sqlite DB with the full HoloDub schema
// migrated.  Tests can mutate it without affecting any shared state.
func newTestStore(t *testing.T) *Store {
	t.Helper()
	db, err := gorm.Open(sqlite.Open("file::memory:?cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open in-memory sqlite: %v", err)
	}
	st := &Store{db: db}
	if err := st.AutoMigrate(); err != nil {
		t.Fatalf("auto-migrate: %v", err)
	}
	return st
}

// seedAwaitingReviewSegment inserts a job with a single pending segment so
// tests can exercise UpdateSegmentSourceText against a realistic shape.
func seedAwaitingReviewSegment(t *testing.T, st *Store) (uint, uint) {
	t.Helper()
	ctx := context.Background()
	job := &models.Job{
		Name:           "test",
		Status:         models.JobStatusAwaitingReview,
		CurrentStage:   models.StageSegmentReview,
		SourceLanguage: "ja",
		TargetLanguage: "zh",
	}
	if err := st.CreateJob(ctx, job); err != nil {
		t.Fatalf("create job: %v", err)
	}
	if err := st.ReplaceSegments(ctx, job.ID, []models.SegmentDraft{{
		StartMs:      1000,
		EndMs:        3500,
		Text:         "原始 ASR 文本",
		SpeakerLabel: "SPK_01",
		SplitReason:  "rule",
	}}); err != nil {
		t.Fatalf("replace segments: %v", err)
	}
	segs, err := st.ListSegments(ctx, job.ID, nil)
	if err != nil || len(segs) != 1 {
		t.Fatalf("list segments: err=%v len=%d", err, len(segs))
	}
	return job.ID, segs[0].ID
}

func TestUpdateSegmentSourceText_OverwritesOnlySourceText(t *testing.T) {
	st := newTestStore(t)
	jobID, segID := seedAwaitingReviewSegment(t, st)
	ctx := context.Background()

	before, err := st.GetSegment(ctx, segID)
	if err != nil {
		t.Fatalf("get segment: %v", err)
	}

	if err := st.UpdateSegmentSourceText(ctx, jobID, segID, "用户修正后的文本"); err != nil {
		t.Fatalf("update source text: %v", err)
	}

	after, err := st.GetSegment(ctx, segID)
	if err != nil {
		t.Fatalf("get segment after: %v", err)
	}
	if after.SourceText != "用户修正后的文本" {
		t.Fatalf("source_text not updated: got %q", after.SourceText)
	}
	if after.StartMs != before.StartMs || after.EndMs != before.EndMs {
		t.Fatalf("timing changed unexpectedly: before=%d/%d after=%d/%d",
			before.StartMs, before.EndMs, after.StartMs, after.EndMs)
	}
	if after.OriginalDurationMs != before.OriginalDurationMs {
		t.Fatalf("original_duration_ms changed unexpectedly: before=%d after=%d",
			before.OriginalDurationMs, after.OriginalDurationMs)
	}
	if after.Status != before.Status {
		t.Fatalf("status changed unexpectedly: before=%q after=%q", before.Status, after.Status)
	}
	if after.TargetText != before.TargetText {
		t.Fatalf("target_text changed unexpectedly: before=%q after=%q", before.TargetText, after.TargetText)
	}
	if after.TTSAudioRelPath != before.TTSAudioRelPath {
		t.Fatalf("tts_audio_rel_path changed unexpectedly")
	}
	if !after.UpdatedAt.After(before.UpdatedAt) && !after.UpdatedAt.Equal(before.UpdatedAt) {
		t.Fatalf("updated_at not advanced")
	}
}

func TestUpdateSegmentSourceText_WrongJobReturnsNotFound(t *testing.T) {
	st := newTestStore(t)
	jobID, segID := seedAwaitingReviewSegment(t, st)
	ctx := context.Background()

	otherJobID := jobID + 999
	err := st.UpdateSegmentSourceText(ctx, otherJobID, segID, "不能跨 job 写入")
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		t.Fatalf("expected ErrRecordNotFound, got %v", err)
	}

	seg, err := st.GetSegment(ctx, segID)
	if err != nil {
		t.Fatalf("get segment: %v", err)
	}
	if seg.SourceText != "原始 ASR 文本" {
		t.Fatalf("source_text leaked across jobs: %q", seg.SourceText)
	}
}

func TestUpdateSegmentSourceText_MissingSegmentReturnsNotFound(t *testing.T) {
	st := newTestStore(t)
	jobID, _ := seedAwaitingReviewSegment(t, st)
	ctx := context.Background()

	err := st.UpdateSegmentSourceText(ctx, jobID, 999999, "文本")
	if !errors.Is(err, gorm.ErrRecordNotFound) {
		t.Fatalf("expected ErrRecordNotFound, got %v", err)
	}
}
