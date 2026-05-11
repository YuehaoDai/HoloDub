package store

import (
	"context"
	"errors"
	"strings"
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

// TestListSegmentsAwaitingJudge_FiltersAndOrdersCorrectly verifies the
// OPT-002-followup-2 backfill source query: returns synthesised segments
// that have not been judged yet, ordered most-recent-first, capped at the
// caller's limit, and skipping rows with empty source/target text or with
// an existing judge_score (regardless of value, including zero).
func TestListSegmentsAwaitingJudge_FiltersAndOrdersCorrectly(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	job := &models.Job{
		Name:           "judge-backfill-fixture",
		Status:         models.JobStatusRunning,
		CurrentStage:   models.StageTTSDuration,
		SourceLanguage: "ja",
		TargetLanguage: "zh",
	}
	if err := st.CreateJob(ctx, job); err != nil {
		t.Fatalf("create job: %v", err)
	}

	scoreZero := 0.0
	scorePositive := 0.7
	now := time.Now().UTC()
	rows := []models.Segment{
		{ // EXPECTED: synthesised, no judge yet, both texts present
			JobID:      job.ID,
			Ordinal:    1,
			StartMs:    0,
			EndMs:      4000,
			SourceText: "1番目",
			TargetText: "第一段",
			Status:     models.SegmentStatusSynthesized,
			CreatedAt:  now.Add(-3 * time.Minute),
			UpdatedAt:  now.Add(-3 * time.Minute),
		},
		{ // EXPECTED: synthesised, no judge, more recent — should come first
			JobID:      job.ID,
			Ordinal:    2,
			StartMs:    4000,
			EndMs:      8000,
			SourceText: "2番目",
			TargetText: "第二段",
			Status:     models.SegmentStatusSynthesized,
			CreatedAt:  now.Add(-1 * time.Minute),
			UpdatedAt:  now.Add(-1 * time.Minute),
		},
		{ // FILTERED: not yet synthesised
			JobID:      job.ID,
			Ordinal:    3,
			StartMs:    8000,
			EndMs:      12000,
			SourceText: "3番目",
			TargetText: "第三段",
			Status:     models.SegmentStatusTranslated,
		},
		{ // FILTERED: synthesised but already judged (even score=0 counts as judged)
			JobID:      job.ID,
			Ordinal:    4,
			StartMs:    12000,
			EndMs:      16000,
			SourceText: "4番目",
			TargetText: "第四段",
			Status:     models.SegmentStatusSynthesized,
			JudgeScore: &scoreZero,
		},
		{ // FILTERED: synthesised, judged with high score
			JobID:      job.ID,
			Ordinal:    5,
			StartMs:    16000,
			EndMs:      20000,
			SourceText: "5番目",
			TargetText: "第五段",
			Status:     models.SegmentStatusSynthesized,
			JudgeScore: &scorePositive,
		},
		{ // FILTERED: empty source text (cannot be judged anyway)
			JobID:      job.ID,
			Ordinal:    6,
			StartMs:    20000,
			EndMs:      24000,
			SourceText: "",
			TargetText: "第六段",
			Status:     models.SegmentStatusSynthesized,
		},
		{ // FILTERED: empty target text (synthesis produced silence?)
			JobID:      job.ID,
			Ordinal:    7,
			StartMs:    24000,
			EndMs:      28000,
			SourceText: "7番目",
			TargetText: "",
			Status:     models.SegmentStatusSynthesized,
		},
	}
	if err := st.db.Create(&rows).Error; err != nil {
		t.Fatalf("seed segments: %v", err)
	}

	// Limit larger than match count: should return both expected rows,
	// most-recent-first by id (ordinal 2 before ordinal 1 because we
	// inserted them later).
	got, err := st.ListSegmentsAwaitingJudge(ctx, 100)
	if err != nil {
		t.Fatalf("ListSegmentsAwaitingJudge: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("expected 2 rows, got %d: %+v", len(got), got)
	}
	if got[0].Ordinal != 2 || got[1].Ordinal != 1 {
		t.Fatalf("ordering wrong (want most-recent first by id): got ordinals %d,%d",
			got[0].Ordinal, got[1].Ordinal)
	}

	// Limit smaller than match count: only the most recent.
	got1, err := st.ListSegmentsAwaitingJudge(ctx, 1)
	if err != nil {
		t.Fatalf("ListSegmentsAwaitingJudge limit=1: %v", err)
	}
	if len(got1) != 1 {
		t.Fatalf("expected 1 row when limit=1, got %d", len(got1))
	}
	if got1[0].Ordinal != 2 {
		t.Fatalf("limit=1 must pick most recent (ordinal 2), got %d", got1[0].Ordinal)
	}

	// Limit zero / negative → no work, no error.
	if got0, err := st.ListSegmentsAwaitingJudge(ctx, 0); err != nil || got0 != nil {
		t.Fatalf("limit=0 must short-circuit (got %d rows, err=%v)", len(got0), err)
	}
	if gotNeg, err := st.ListSegmentsAwaitingJudge(ctx, -5); err != nil || gotNeg != nil {
		t.Fatalf("limit<0 must short-circuit (got %d rows, err=%v)", len(gotNeg), err)
	}
}

// TestUpdateEpisodeJudgeResult_PartialUpdateOnly verifies the OPT-406
// partial UPDATE only touches episode_judge_score / episode_judge_meta /
// updated_at — episode state-machine columns (Status, OutputRelPath,
// ChaptersManifestRelPath, OutputLayoutVersion, ReferenceCard, Name)
// MUST NOT be clobbered, because the judge dispatch runs asynchronously
// AFTER ep_episode_merge has already written them and an unrelated
// UpdateEpisodeStatus / UpdateEpisodeOutput may race with us.
func TestUpdateEpisodeJudgeResult_PartialUpdateOnly(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	ep := &models.Episode{
		TenantKey:               "default",
		Name:                    "ep-judge-target",
		SourceLanguage:          "en",
		TargetLanguage:          "zh",
		TotalChapters:           2,
		Status:                  models.EpisodeStatusCompleted,
		OutputRelPath:           "episodes/9001/output/vp0/final.mp4",
		ChaptersManifestRelPath: "episodes/9001/chapters.json",
		OutputLayoutVersion:     2,
		ReferenceCard:           "EXISTING_REFERENCE_CARD",
		DurationMs:              123456,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	// Sanity: nothing in judge columns yet.
	before, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode: %v", err)
	}
	if before.EpisodeJudgeScore != nil {
		t.Fatalf("expected EpisodeJudgeScore nil before update, got %v", *before.EpisodeJudgeScore)
	}

	metaJSON := []byte(`{"verdict":"production_ready","overall_fidelity":0.91}`)
	if err := st.UpdateEpisodeJudgeResult(ctx, ep.ID, 0.91, metaJSON); err != nil {
		t.Fatalf("UpdateEpisodeJudgeResult: %v", err)
	}

	after, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("re-get episode: %v", err)
	}
	if after.EpisodeJudgeScore == nil || *after.EpisodeJudgeScore != 0.91 {
		t.Fatalf("EpisodeJudgeScore want 0.91, got %v", after.EpisodeJudgeScore)
	}
	if len(after.EpisodeJudgeMeta) == 0 {
		t.Fatalf("EpisodeJudgeMeta should be populated, got empty")
	}
	if !strings.Contains(string(after.EpisodeJudgeMeta), "production_ready") {
		t.Fatalf("EpisodeJudgeMeta should contain verdict text, got %q", string(after.EpisodeJudgeMeta))
	}

	// Critical assertion: state-machine and OPT-403/404 columns untouched.
	if after.Status != before.Status {
		t.Fatalf("Status was clobbered: %q → %q", before.Status, after.Status)
	}
	if after.OutputRelPath != before.OutputRelPath {
		t.Fatalf("OutputRelPath was clobbered: %q → %q", before.OutputRelPath, after.OutputRelPath)
	}
	if after.ChaptersManifestRelPath != before.ChaptersManifestRelPath {
		t.Fatalf("ChaptersManifestRelPath was clobbered: %q → %q",
			before.ChaptersManifestRelPath, after.ChaptersManifestRelPath)
	}
	if after.OutputLayoutVersion != before.OutputLayoutVersion {
		t.Fatalf("OutputLayoutVersion was clobbered: %d → %d",
			before.OutputLayoutVersion, after.OutputLayoutVersion)
	}
	if after.ReferenceCard != before.ReferenceCard {
		t.Fatalf("ReferenceCard was clobbered: %q → %q", before.ReferenceCard, after.ReferenceCard)
	}
	if after.Name != before.Name {
		t.Fatalf("Name was clobbered: %q → %q", before.Name, after.Name)
	}
	if after.DurationMs != before.DurationMs {
		t.Fatalf("DurationMs was clobbered: %d → %d", before.DurationMs, after.DurationMs)
	}
}

// TestAppendEpisodeReworkAttempt_AppendsAndAccumulates verifies the OPT-407
// store hook does the four things the engine relies on:
//  1. The first call seeds the JSONB array (was nil).
//  2. The second call APPENDS rather than overwrites.
//  3. accumulated_cost_usd is the running sum across all calls.
//  4. Other episode columns (Status, OutputRelPath, EpisodeJudgeScore)
//     remain untouched — same partial-update guarantee as
//     UpdateEpisodeJudgeResult, since rework hooks fire async and may race
//     with the episode state machine.
func TestAppendEpisodeReworkAttempt_AppendsAndAccumulates(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	prevScore := 0.84
	ep := &models.Episode{
		TenantKey:               "default",
		Name:                    "ep-rework-target",
		SourceLanguage:          "ja",
		TargetLanguage:          "zh",
		TotalChapters:           1,
		Status:                  models.EpisodeStatusCompleted,
		OutputRelPath:           "episodes/9100/output/vp0/final.mp4",
		ChaptersManifestRelPath: "episodes/9100/chapters.json",
		OutputLayoutVersion:     2,
		ReferenceCard:           "EXISTING_REFERENCE_CARD",
		DurationMs:              123456,
		EpisodeJudgeScore:       &prevScore,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	// First attempt — seeds the array, sets cost from nil → 0.0125.
	first := map[string]any{
		"level":      "segment",
		"verdict":    "retry",
		"action":     "segment_retry",
		"target_id":  uint(7),
		"dispatched": true,
	}
	if err := st.AppendEpisodeReworkAttempt(ctx, ep.ID, first, 0.0125); err != nil {
		t.Fatalf("first append: %v", err)
	}

	mid, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode after first append: %v", err)
	}
	if len(mid.ReworkAttempts) == 0 {
		t.Fatalf("ReworkAttempts should be populated after first append")
	}
	if !strings.Contains(string(mid.ReworkAttempts), "segment_retry") {
		t.Fatalf("ReworkAttempts should contain first attempt action, got %q",
			string(mid.ReworkAttempts))
	}
	if mid.AccumulatedCostUSD == nil || *mid.AccumulatedCostUSD != 0.0125 {
		t.Fatalf("AccumulatedCostUSD want 0.0125, got %v", mid.AccumulatedCostUSD)
	}

	// Second attempt — appends to the array, cost should accumulate.
	second := map[string]any{
		"level":      "chapter",
		"verdict":    "needs_revision",
		"action":     "revise_weakest_segments",
		"target_id":  uint(42),
		"dispatched": true,
	}
	if err := st.AppendEpisodeReworkAttempt(ctx, ep.ID, second, 0.0500); err != nil {
		t.Fatalf("second append: %v", err)
	}

	after, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode after second append: %v", err)
	}
	// Both action strings should now be in the JSONB array.
	if !strings.Contains(string(after.ReworkAttempts), "segment_retry") {
		t.Fatalf("ReworkAttempts should still contain first action, got %q",
			string(after.ReworkAttempts))
	}
	if !strings.Contains(string(after.ReworkAttempts), "revise_weakest_segments") {
		t.Fatalf("ReworkAttempts should contain second action, got %q",
			string(after.ReworkAttempts))
	}
	if after.AccumulatedCostUSD == nil {
		t.Fatalf("AccumulatedCostUSD nil after second append")
	}
	wantCost := 0.0125 + 0.0500
	if *after.AccumulatedCostUSD < wantCost-1e-9 || *after.AccumulatedCostUSD > wantCost+1e-9 {
		t.Fatalf("AccumulatedCostUSD want %v, got %v", wantCost, *after.AccumulatedCostUSD)
	}

	// Critical: state-machine columns and the prior episode-judge data
	// MUST NOT be clobbered, because rework dispatch runs async after
	// ep_episode_merge has set them.
	if after.Status != ep.Status {
		t.Fatalf("Status clobbered: %q → %q", ep.Status, after.Status)
	}
	if after.OutputRelPath != ep.OutputRelPath {
		t.Fatalf("OutputRelPath clobbered: %q → %q", ep.OutputRelPath, after.OutputRelPath)
	}
	if after.ReferenceCard != ep.ReferenceCard {
		t.Fatalf("ReferenceCard clobbered: %q → %q", ep.ReferenceCard, after.ReferenceCard)
	}
	if after.EpisodeJudgeScore == nil || *after.EpisodeJudgeScore != prevScore {
		t.Fatalf("EpisodeJudgeScore clobbered: %v → %v", &prevScore, after.EpisodeJudgeScore)
	}
}

// TestAppendEpisodeReworkAttempt_ZeroCostStillPersists verifies the engine
// can record an "observe-only" / non-dispatched attempt by passing
// costDelta=0 and still get the JSONB row appended. Without this path,
// observe-only mode (REWORK_ENGINE_LEVEL=none with engine still wired up)
// would silently lose its audit trail.
func TestAppendEpisodeReworkAttempt_ZeroCostStillPersists(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	ep := &models.Episode{
		TenantKey:      "default",
		Name:           "ep-observe-only",
		SourceLanguage: "ja",
		TargetLanguage: "zh",
		TotalChapters:  1,
		Status:         models.EpisodeStatusCompleted,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	attempt := map[string]any{
		"level":      "segment",
		"action":     "noop",
		"dispatched": false,
		"skip":       "level_disabled",
	}
	if err := st.AppendEpisodeReworkAttempt(ctx, ep.ID, attempt, 0); err != nil {
		t.Fatalf("zero-cost append: %v", err)
	}
	got, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode: %v", err)
	}
	if len(got.ReworkAttempts) == 0 {
		t.Fatalf("ReworkAttempts should be populated even when costDelta=0")
	}
	if got.AccumulatedCostUSD == nil || *got.AccumulatedCostUSD != 0 {
		t.Fatalf("AccumulatedCostUSD want 0, got %v", got.AccumulatedCostUSD)
	}
}

// TestAppendEpisodeReworkAttempt_RejectsZeroID is a defensive guard: the
// engine must never call this with a synthetic / unset episode ID.
func TestAppendEpisodeReworkAttempt_RejectsZeroID(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	err := st.AppendEpisodeReworkAttempt(ctx, 0, map[string]any{"x": 1}, 0.1)
	if err == nil {
		t.Fatalf("expected error for episode_id=0, got nil")
	}
	if !strings.Contains(err.Error(), "episode id is zero") {
		t.Fatalf("error should mention zero id, got %v", err)
	}
}

// TestSetEpisodeReworkStatus_PartialUpdateOnly verifies the OPT-407
// escalation hook (a) writes the requested status and (b) does NOT
// clobber unrelated columns. Same async-safety guarantee as
// AppendEpisodeReworkAttempt — escalation can happen long after
// ep_episode_merge has populated OutputRelPath et al.
func TestSetEpisodeReworkStatus_PartialUpdateOnly(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	prevScore := 0.62
	ep := &models.Episode{
		TenantKey:               "default",
		Name:                    "ep-escalate",
		SourceLanguage:          "ja",
		TargetLanguage:          "zh",
		TotalChapters:           1,
		Status:                  models.EpisodeStatusCompleted,
		OutputRelPath:           "episodes/9200/output/vp0/final.mp4",
		ChaptersManifestRelPath: "episodes/9200/chapters.json",
		OutputLayoutVersion:     2,
		ReferenceCard:           "REF",
		DurationMs:              98765,
		EpisodeJudgeScore:       &prevScore,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	if err := st.SetEpisodeReworkStatus(ctx, ep.ID, "escalated_human"); err != nil {
		t.Fatalf("SetEpisodeReworkStatus: %v", err)
	}
	got, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode: %v", err)
	}
	if got.ReworkStatus != "escalated_human" {
		t.Fatalf("ReworkStatus want %q, got %q", "escalated_human", got.ReworkStatus)
	}
	if got.Status != ep.Status {
		t.Fatalf("Status clobbered: %q → %q", ep.Status, got.Status)
	}
	if got.OutputRelPath != ep.OutputRelPath {
		t.Fatalf("OutputRelPath clobbered: %q → %q", ep.OutputRelPath, got.OutputRelPath)
	}
	if got.EpisodeJudgeScore == nil || *got.EpisodeJudgeScore != prevScore {
		t.Fatalf("EpisodeJudgeScore clobbered: %v → %v", prevScore, got.EpisodeJudgeScore)
	}

	// Operator clear path: empty string MUST clear the flag without raising.
	if err := st.SetEpisodeReworkStatus(ctx, ep.ID, ""); err != nil {
		t.Fatalf("clear escalation: %v", err)
	}
	cleared, err := st.GetEpisode(ctx, ep.ID)
	if err != nil {
		t.Fatalf("get episode after clear: %v", err)
	}
	if cleared.ReworkStatus != "" {
		t.Fatalf("ReworkStatus want empty after clear, got %q", cleared.ReworkStatus)
	}
}

// TestSetEpisodeReworkStatus_RejectsZeroID — same defensive guard as
// AppendEpisodeReworkAttempt.
func TestSetEpisodeReworkStatus_RejectsZeroID(t *testing.T) {
	st := newTestStore(t)
	ctx := context.Background()

	err := st.SetEpisodeReworkStatus(ctx, 0, "halted_cost")
	if err == nil {
		t.Fatalf("expected error for episode_id=0, got nil")
	}
	if !strings.Contains(err.Error(), "episode id is zero") {
		t.Fatalf("error should mention zero id, got %v", err)
	}
}
