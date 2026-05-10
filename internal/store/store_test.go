package store

import (
	"context"
	"errors"
	"testing"
	"time"

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

// ── Episode / Chapter (OPT-401) ──────────────────────────────────────────────

// TestCreateJob_AutoCreatesEpisode covers the backwards-compatible default
// path: callers that do not supply an episode_id get a 1-chapter Episode
// transparently allocated and linked.
func TestCreateJob_AutoCreatesEpisode(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	job := &models.Job{
		Name:           "auto-ep",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		InputRelPath:   "uploads/x.mp4",
	}
	if err := st.CreateJob(ctx, job); err != nil {
		t.Fatalf("create job: %v", err)
	}
	if job.EpisodeID == 0 {
		t.Fatal("EpisodeID should be auto-assigned")
	}
	if job.ChapterOrdinal != 1 {
		t.Fatalf("ChapterOrdinal expected 1 (1-chapter shortcut), got %d", job.ChapterOrdinal)
	}

	ep, err := st.GetEpisode(ctx, job.EpisodeID)
	if err != nil {
		t.Fatalf("get episode: %v", err)
	}
	if ep.TenantKey != "default" {
		t.Fatalf("tenant_key expected 'default', got %q", ep.TenantKey)
	}
	if ep.SourceVideoRelPath != "uploads/x.mp4" {
		t.Fatalf("source_video_rel_path mismatch: %q", ep.SourceVideoRelPath)
	}
	if ep.TotalChapters != 1 {
		t.Fatalf("TotalChapters expected 1, got %d", ep.TotalChapters)
	}
	if ep.Status != models.EpisodeStatusPending {
		t.Fatalf("Status expected pending, got %q", ep.Status)
	}
	if len(ep.Chapters) != 1 || ep.Chapters[0].ID != job.ID {
		t.Fatalf("chapters preload broken: %+v", ep.Chapters)
	}
}

// TestCreateJob_WithExistingEpisodeAssignsNextOrdinal covers the OPT-403
// fan-out path (caller supplies episode_id; we auto-pick the next ordinal).
func TestCreateJob_WithExistingEpisodeAssignsNextOrdinal(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	ep := &models.Episode{
		TenantKey:      "default",
		Name:           "long",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		TotalChapters:  3,
		Status:         models.EpisodeStatusPending,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	for i := 0; i < 3; i++ {
		job := &models.Job{
			Name:           "chapter",
			SourceLanguage: "ja",
			TargetLanguage: "zh",
			InputRelPath:   "uploads/long.mp4",
			EpisodeID:      ep.ID,
		}
		if err := st.CreateJob(ctx, job); err != nil {
			t.Fatalf("create chapter job %d: %v", i, err)
		}
		if job.ChapterOrdinal != i+1 {
			t.Fatalf("expected ChapterOrdinal=%d, got %d", i+1, job.ChapterOrdinal)
		}
	}

	chapters, err := st.GetEpisodeChapters(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get chapters: %v", err)
	}
	if len(chapters) != 3 {
		t.Fatalf("expected 3 chapters, got %d", len(chapters))
	}
	for i, ch := range chapters {
		if ch.ChapterOrdinal != i+1 {
			t.Fatalf("chapter[%d] ordinal = %d, expected %d", i, ch.ChapterOrdinal, i+1)
		}
	}
}

// TestCreateJob_WithMissingEpisodeFails ensures we do not silently swallow
// a stale or fabricated episode_id.
func TestCreateJob_WithMissingEpisodeFails(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	job := &models.Job{
		Name:           "bad",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		EpisodeID:      99999,
	}
	if err := st.CreateJob(ctx, job); err == nil {
		t.Fatalf("expected error for missing episode")
	}
}

// TestCreateJob_RejectsTerminalEpisode prevents racing a terminal episode
// against a late-arriving fan-out request.
func TestCreateJob_RejectsTerminalEpisode(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	ep := &models.Episode{
		TenantKey:      "default",
		Name:           "done",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		Status:         models.EpisodeStatusCompleted,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	job := &models.Job{
		Name:           "late",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		EpisodeID:      ep.ID,
	}
	if err := st.CreateJob(ctx, job); err == nil {
		t.Fatalf("expected error appending chapter to terminal episode")
	}
}

// TestUpdateEpisodeStatus exercises both legal and illegal transitions
// against the actual store wiring (DB row + state-machine validation).
func TestUpdateEpisodeStatus(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	ep := &models.Episode{
		TenantKey:      "default",
		Name:           "ep",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		Status:         models.EpisodeStatusPending,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	if err := st.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusRunning, ""); err != nil {
		t.Fatalf("pending -> running: %v", err)
	}
	if err := st.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusCompleted, ""); err != nil {
		t.Fatalf("running -> completed (1-chapter shortcut): %v", err)
	}
	got, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode: %v", err)
	}
	if got.Status != models.EpisodeStatusCompleted {
		t.Fatalf("status not persisted: %q", got.Status)
	}
	if got.CompletedAt == nil {
		t.Fatal("CompletedAt should be set when transitioning to completed")
	}

	if err := st.UpdateEpisodeStatus(ctx, ep.ID, models.EpisodeStatusRunning, ""); err == nil {
		t.Fatal("expected error transitioning out of terminal completed")
	}
}

// TestRunBackfillIfNeeded validates the legacy-schema rescue path: orphan
// jobs (episode_id IS NULL / 0) get 1:1 Episodes whose ids equal the job
// ids, and re-running the back-fill is a no-op.
func TestRunBackfillIfNeeded(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	// Insert two orphan jobs by bypassing CreateJob (simulates pre-OPT-401
	// rows surviving an in-place schema upgrade).
	now := time.Now().UTC()
	completedAt := now.Add(-time.Hour)
	orphans := []models.Job{
		{
			Name:           "legacy-completed",
			TenantKey:      "default",
			Status:         models.JobStatusCompleted,
			CurrentStage:   models.StageMerge,
			SourceLanguage: "ja",
			TargetLanguage: "zh",
			InputRelPath:   "uploads/legacy1.mp4",
			OutputRelPath:  "jobs/1/final.mp4",
			CreatedAt:      now.Add(-2 * time.Hour),
			UpdatedAt:      now.Add(-time.Hour),
			CompletedAt:    &completedAt,
		},
		{
			Name:           "legacy-running",
			TenantKey:      "tenantA",
			Status:         models.JobStatusRunning,
			CurrentStage:   models.StageTTSDuration,
			SourceLanguage: "en",
			TargetLanguage: "zh",
			InputRelPath:   "uploads/legacy2.mp4",
			CreatedAt:      now.Add(-30 * time.Minute),
			UpdatedAt:      now.Add(-5 * time.Minute),
		},
	}
	if err := st.db.Create(&orphans).Error; err != nil {
		t.Fatalf("seed orphan jobs: %v", err)
	}
	// Force episode_id back to NULL — GORM AutoMigrate's default '1' would
	// otherwise hide the test pre-condition. (sqlite stores NULL via Update.)
	if err := st.db.Model(&models.Job{}).
		Where("id IN ?", []uint{orphans[0].ID, orphans[1].ID}).
		Update("episode_id", nil).Error; err != nil {
		t.Fatalf("null-out episode_id: %v", err)
	}

	if err := st.RunBackfillIfNeeded(ctx); err != nil {
		t.Fatalf("run backfill: %v", err)
	}

	for _, oj := range orphans {
		ep, err := st.GetEpisode(ctx, oj.ID)
		if err != nil {
			t.Fatalf("get back-filled episode for job %d: %v", oj.ID, err)
		}
		if ep.ID != oj.ID {
			t.Fatalf("episode id mismatch: expected %d, got %d", oj.ID, ep.ID)
		}
		if ep.TenantKey != oj.TenantKey {
			t.Fatalf("tenant_key mismatch: %q vs %q", ep.TenantKey, oj.TenantKey)
		}
		if ep.SourceVideoRelPath != oj.InputRelPath {
			t.Fatalf("source_video_rel_path mismatch: %q vs %q", ep.SourceVideoRelPath, oj.InputRelPath)
		}
		var refreshed models.Job
		if err := st.db.First(&refreshed, oj.ID).Error; err != nil {
			t.Fatalf("refresh job %d: %v", oj.ID, err)
		}
		if refreshed.EpisodeID != oj.ID {
			t.Fatalf("job.episode_id not back-filled: got %d, expected %d", refreshed.EpisodeID, oj.ID)
		}
		if refreshed.ChapterOrdinal != 1 {
			t.Fatalf("job.chapter_ordinal not back-filled: got %d", refreshed.ChapterOrdinal)
		}
	}

	// Status mapping spot-checks.
	ep1, _ := st.GetEpisode(ctx, orphans[0].ID)
	if ep1.Status != models.EpisodeStatusCompleted {
		t.Fatalf("legacy completed -> %q (want completed)", ep1.Status)
	}
	ep2, _ := st.GetEpisode(ctx, orphans[1].ID)
	if ep2.Status != models.EpisodeStatusRunning {
		t.Fatalf("legacy running -> %q (want running)", ep2.Status)
	}

	// Re-running back-fill must be a no-op.
	before, _ := st.ListEpisodes(ctx)
	if err := st.RunBackfillIfNeeded(ctx); err != nil {
		t.Fatalf("idempotent run: %v", err)
	}
	after, _ := st.ListEpisodes(ctx)
	if len(after) != len(before) {
		t.Fatalf("re-run created episodes: before=%d after=%d", len(before), len(after))
	}
}
