package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/models"

	"gorm.io/driver/postgres"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

type Store struct {
	db *gorm.DB
}

type BindingInput struct {
	SpeakerID      *uint
	SpeakerLabel   string
	VoiceProfileID uint
}

func New(cfg config.Config) (*Store, error) {
	var dialector gorm.Dialector
	switch strings.ToLower(cfg.DatabaseDriver) {
	case "postgres", "postgresql":
		dialector = postgres.Open(cfg.DatabaseDSN)
	case "sqlite", "sqlite3":
		dialector = sqlite.Open(cfg.DatabaseDSN)
	default:
		return nil, fmt.Errorf("unsupported database driver %q", cfg.DatabaseDriver)
	}

	db, err := gorm.Open(dialector, &gorm.Config{})
	if err != nil {
		return nil, fmt.Errorf("open database: %w", err)
	}

	return &Store{db: db}, nil
}

func (s *Store) DB() *gorm.DB {
	return s.db
}

// NewWithDB wraps an existing *gorm.DB in a *Store.  It is a thin test seam
// so that handler-level tests in sibling packages can drive the store
// against an in-memory sqlite database without going through New() and its
// config/DSN plumbing.  Production code MUST continue to use New().
func NewWithDB(db *gorm.DB) *Store {
	return &Store{db: db}
}

// Ping verifies database connectivity. Used by /readyz so orchestrators can
// distinguish between "the process is alive" (liveness) and "the process can
// actually serve requests" (readiness).
func (s *Store) Ping(ctx context.Context) error {
	sqlDB, err := s.db.DB()
	if err != nil {
		return fmt.Errorf("get sql.DB: %w", err)
	}
	return sqlDB.PingContext(ctx)
}

func (s *Store) AutoMigrate() error {
	return s.db.AutoMigrate(
		&models.Episode{},
		&models.Job{},
		&models.VoiceProfile{},
		&models.Speaker{},
		&models.SpeakerVoiceBinding{},
		&models.Segment{},
		&models.JobStageRun{},
		&models.TenantQuota{},
		&models.SegmentSuggestion{},
	)
}

// CreateJob inserts a Job row, transparently allocating its enclosing
// Episode when the caller did not supply one.
//
// OPT-401 contract:
//   - When job.EpisodeID == 0 we open a transaction, create a 1-chapter
//     pending Episode (carrying the same TenantKey / Name / language pair
//     and SourceVideoRelPath = job.InputRelPath), set job.EpisodeID to the
//     fresh episode, force ChapterOrdinal = 1, then insert the job.
//   - When job.EpisodeID != 0 we validate that the episode exists, belongs
//     to the same tenant, is still active (not in a terminal state), and
//     auto-pick the next ChapterOrdinal when zero. This branch is reserved
//     for OPT-403 fan-out and is fully covered by store_test.go.
//
// In both branches the entire write happens in a single GORM transaction so
// a failure leaves no orphan episodes or jobs behind.
func (s *Store) CreateJob(ctx context.Context, job *models.Job) error {
	if job.Status == "" {
		job.Status = models.JobStatusPending
	}
	if job.CurrentStage == "" {
		job.CurrentStage = models.StageMedia
	}
	if job.TenantKey == "" {
		job.TenantKey = "default"
	}
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if job.EpisodeID == 0 {
			ep := models.Episode{
				TenantKey:          job.TenantKey,
				Name:               job.Name,
				SourceVideoRelPath: job.InputRelPath,
				SourceLanguage:     job.SourceLanguage,
				TargetLanguage:     job.TargetLanguage,
				TotalChapters:      1,
				Status:             models.EpisodeStatusPending,
			}
			if err := tx.Create(&ep).Error; err != nil {
				return fmt.Errorf("auto-create episode: %w", err)
			}
			job.EpisodeID = ep.ID
			if job.ChapterOrdinal == 0 {
				job.ChapterOrdinal = 1
			}
			return tx.Create(job).Error
		}

		var ep models.Episode
		if err := tx.First(&ep, job.EpisodeID).Error; err != nil {
			return fmt.Errorf("episode %d: %w", job.EpisodeID, err)
		}
		if ep.TenantKey != "" && ep.TenantKey != job.TenantKey {
			return fmt.Errorf("episode %d belongs to tenant %q, not %q",
				ep.ID, ep.TenantKey, job.TenantKey)
		}
		if ep.Status.IsTerminal() {
			return fmt.Errorf("episode %d is in terminal status %q; cannot append chapter",
				ep.ID, ep.Status)
		}
		if job.ChapterOrdinal == 0 {
			var maxOrdinal int
			if err := tx.Model(&models.Job{}).
				Where("episode_id = ?", job.EpisodeID).
				Select("COALESCE(MAX(chapter_ordinal), 0)").
				Scan(&maxOrdinal).Error; err != nil {
				return fmt.Errorf("compute next chapter ordinal: %w", err)
			}
			job.ChapterOrdinal = maxOrdinal + 1
		}
		return tx.Create(job).Error
	})
}

func (s *Store) ListJobs(ctx context.Context) ([]models.Job, error) {
	var jobs []models.Job
	err := s.db.WithContext(ctx).
		Order("id desc").
		Find(&jobs).Error
	return jobs, err
}

// ── Episode CRUD (OPT-401) ────────────────────────────────────────────────────

// CreateEpisode inserts an Episode row. Most callers should rely on
// Store.CreateJob's transactional auto-creation path instead — this
// helper is exposed primarily for OPT-403 chapterize and back-fill.
func (s *Store) CreateEpisode(ctx context.Context, ep *models.Episode) error {
	if ep.Status == "" {
		ep.Status = models.EpisodeStatusPending
	}
	if ep.TenantKey == "" {
		ep.TenantKey = "default"
	}
	if ep.TotalChapters == 0 {
		ep.TotalChapters = 1
	}
	return s.db.WithContext(ctx).Create(ep).Error
}

// GetEpisode returns a single Episode with its chapter Jobs preloaded
// (ordered by chapter_ordinal asc). Returns gorm.ErrRecordNotFound when
// the episode does not exist.
func (s *Store) GetEpisode(ctx context.Context, id uint) (*models.Episode, error) {
	var ep models.Episode
	err := s.db.WithContext(ctx).
		Preload("Chapters", func(db *gorm.DB) *gorm.DB {
			return db.Order("chapter_ordinal asc, id asc")
		}).
		First(&ep, id).Error
	if err != nil {
		return nil, err
	}
	return &ep, nil
}

// ListEpisodes returns all Episodes ordered by id desc (newest first).
// Tenant filtering is performed by the HTTP handler — the store layer
// returns everything to keep the same shape as ListJobs.
func (s *Store) ListEpisodes(ctx context.Context) ([]models.Episode, error) {
	var eps []models.Episode
	err := s.db.WithContext(ctx).
		Order("id desc").
		Find(&eps).Error
	return eps, err
}

// GetEpisodeChapters returns the chapter Jobs of one Episode ordered by
// chapter_ordinal asc. Returned slice may be empty (legal for a freshly
// created Episode whose chapters have not been fanned out yet).
func (s *Store) GetEpisodeChapters(ctx context.Context, episodeID uint) ([]models.Job, error) {
	var jobs []models.Job
	err := s.db.WithContext(ctx).
		Where("episode_id = ?", episodeID).
		Order("chapter_ordinal asc, id asc").
		Find(&jobs).Error
	return jobs, err
}

// UpdateEpisodeStatus transitions an Episode from its current status to the
// requested target, validating the move via EpisodeStatus.Transition. The
// optional errMsg is recorded only when transitioning to EpisodeStatusFailed
// and is otherwise ignored.
//
// Returns the validation error (with the current status string) when the
// transition is invalid, so callers can decide whether to log-and-skip or
// hard-fail.
func (s *Store) UpdateEpisodeStatus(ctx context.Context, id uint, to models.EpisodeStatus, errMsg string) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var ep models.Episode
		if err := tx.First(&ep, id).Error; err != nil {
			return err
		}
		if _, err := ep.Status.Transition(to); err != nil {
			return err
		}
		now := time.Now().UTC()
		updates := map[string]any{
			"status":     to,
			"updated_at": now,
		}
		if to == models.EpisodeStatusCompleted {
			updates["completed_at"] = &now
		}
		if to == models.EpisodeStatusFailed && errMsg != "" {
			updates["error_message"] = errMsg
		}
		return tx.Model(&models.Episode{}).Where("id = ?", id).Updates(updates).Error
	})
}

// RunBackfillIfNeeded brings legacy databases (created before OPT-401) up to
// the new Episode/Chapter schema. It is safe to call on every startup:
//
//   - If every existing Job already has a non-zero EpisodeID and the
//     episodes table is in sync, the function returns nil immediately.
//   - Otherwise, for each orphan Job we INSERT an Episode whose id equals
//     the Job id (preserving 1:1 backwards compatibility with historical
//     URLs and external references), then UPDATE the Job to point at it.
//   - On Postgres we additionally SETVAL the episodes_id_seq so the next
//     newly created Episode receives a fresh id. Sqlite manages
//     auto-increment per AUTOINCREMENT semantics so no extra step is
//     needed there.
//
// All work happens in a single transaction; partial failure leaves the
// database untouched.
func (s *Store) RunBackfillIfNeeded(ctx context.Context) error {
	var orphanCount int64
	if err := s.db.WithContext(ctx).
		Model(&models.Job{}).
		Where("episode_id IS NULL OR episode_id = 0").
		Count(&orphanCount).Error; err != nil {
		return fmt.Errorf("count orphan jobs: %w", err)
	}
	if orphanCount == 0 {
		return nil
	}

	type orphanJob struct {
		ID                 uint
		TenantKey          string
		Name               string
		InputRelPath       string
		OutputRelPath      string
		SourceLanguage     string
		TargetLanguage     string
		Status             models.JobStatus
		CreatedAt          time.Time
		UpdatedAt          time.Time
		CompletedAt        *time.Time
	}

	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var orphans []orphanJob
		if err := tx.Table("jobs").
			Select("id, tenant_key, name, input_rel_path, output_rel_path, source_language, target_language, status, created_at, updated_at, completed_at").
			Where("episode_id IS NULL OR episode_id = 0").
			Order("id asc").
			Scan(&orphans).Error; err != nil {
			return fmt.Errorf("scan orphan jobs: %w", err)
		}

		for _, oj := range orphans {
			tenant := oj.TenantKey
			if tenant == "" {
				tenant = "default"
			}
			status := mapLegacyJobStatusToEpisode(oj.Status)
			ep := models.Episode{
				ID:                 oj.ID,
				TenantKey:          tenant,
				Name:               oj.Name,
				SourceVideoRelPath: oj.InputRelPath,
				SourceLanguage:     oj.SourceLanguage,
				TargetLanguage:     oj.TargetLanguage,
				TotalChapters:      1,
				Status:             status,
				OutputRelPath:      oj.OutputRelPath,
				CreatedAt:          oj.CreatedAt,
				UpdatedAt:          oj.UpdatedAt,
				CompletedAt:        oj.CompletedAt,
			}
			if err := tx.Create(&ep).Error; err != nil {
				return fmt.Errorf("backfill: insert episode for job %d: %w", oj.ID, err)
			}
			if err := tx.Model(&models.Job{}).
				Where("id = ?", oj.ID).
				Updates(map[string]any{
					"episode_id":      oj.ID,
					"chapter_ordinal": 1,
				}).Error; err != nil {
				return fmt.Errorf("backfill: link job %d to episode: %w", oj.ID, err)
			}
		}

		// Postgres: advance the episodes id sequence past the highest
		// back-filled id so subsequent CreateEpisode calls keep monotonic.
		if tx.Dialector.Name() == "postgres" {
			if err := tx.Exec(
				"SELECT setval('episodes_id_seq', GREATEST((SELECT COALESCE(MAX(id),0) FROM episodes), 1))",
			).Error; err != nil {
				return fmt.Errorf("backfill: setval episodes_id_seq: %w", err)
			}
		}
		return nil
	})
}

// mapLegacyJobStatusToEpisode collapses the eight historical Job statuses
// into the four Episode statuses that OPT-401 understands today (the other
// five are reserved for OPT-402..407). Anything that is not clearly
// terminal becomes "running" so judges/dashboards see something sensible
// for in-flight jobs at the moment of back-fill.
func mapLegacyJobStatusToEpisode(s models.JobStatus) models.EpisodeStatus {
	switch s {
	case models.JobStatusCompleted, models.JobStatusCancelled:
		return models.EpisodeStatusCompleted
	case models.JobStatusFailed, models.JobStatusTimedOut:
		return models.EpisodeStatusFailed
	default:
		return models.EpisodeStatusRunning
	}
}

func (s *Store) GetJob(ctx context.Context, id uint) (*models.Job, error) {
	var job models.Job
	err := s.db.WithContext(ctx).
		Preload("Speakers").
		Preload("StageRuns", func(db *gorm.DB) *gorm.DB {
			return db.Order("started_at desc")
		}).
		Preload("Segments", func(db *gorm.DB) *gorm.DB {
			return db.Order("ordinal asc")
		}).
		First(&job, id).Error
	if err != nil {
		return nil, err
	}
	return &job, nil
}

func (s *Store) SaveJob(ctx context.Context, job *models.Job) error {
	return s.db.WithContext(ctx).Save(job).Error
}

func (s *Store) UpdateJobTranslationSummary(ctx context.Context, jobID uint, summary string) error {
	return s.db.WithContext(ctx).Model(&models.Job{}).
		Where("id = ?", jobID).
		Updates(map[string]any{
			"translation_summary": summary,
			"updated_at":          time.Now().UTC(),
		}).Error
}

func (s *Store) TouchJobHeartbeat(ctx context.Context, jobID uint) error {
	now := time.Now().UTC()
	return s.db.WithContext(ctx).Model(&models.Job{}).
		Where("id = ?", jobID).
		Updates(map[string]any{"heartbeat_at": &now, "updated_at": &now}).Error
}

func (s *Store) UpdateJobState(ctx context.Context, jobID uint, status models.JobStatus, stage models.JobStage, errMsg string, incrementRetry bool) error {
	updates := map[string]any{
		"status":        status,
		"current_stage": stage,
		"error_message": errMsg,
		"updated_at":    time.Now().UTC(),
	}
	if incrementRetry {
		updates["retry_count"] = gorm.Expr("retry_count + 1")
	}
	if status == models.JobStatusRunning {
		now := time.Now().UTC()
		updates["heartbeat_at"] = &now
		updates["started_at"] = gorm.Expr("COALESCE(started_at, ?)", now)
	}
	if status == models.JobStatusCompleted {
		now := time.Now().UTC()
		updates["completed_at"] = &now
	}
	if status == models.JobStatusCancelled {
		now := time.Now().UTC()
		updates["cancelled_at"] = &now
	}
	return s.db.WithContext(ctx).Model(&models.Job{}).Where("id = ?", jobID).Updates(updates).Error
}

func (s *Store) CreateVoiceProfile(ctx context.Context, profile *models.VoiceProfile) error {
	return s.db.WithContext(ctx).Create(profile).Error
}

func (s *Store) ListVoiceProfiles(ctx context.Context) ([]models.VoiceProfile, error) {
	var profiles []models.VoiceProfile
	err := s.db.WithContext(ctx).Order("id desc").Find(&profiles).Error
	return profiles, err
}

func (s *Store) GetVoiceProfile(ctx context.Context, id uint) (*models.VoiceProfile, error) {
	var profile models.VoiceProfile
	if err := s.db.WithContext(ctx).First(&profile, id).Error; err != nil {
		return nil, err
	}
	return &profile, nil
}

func (s *Store) UpdateVoiceProfile(ctx context.Context, profile *models.VoiceProfile) error {
	return s.db.WithContext(ctx).Save(profile).Error
}

func (s *Store) DeleteVoiceProfile(ctx context.Context, id uint) error {
	return s.db.WithContext(ctx).Delete(&models.VoiceProfile{}, id).Error
}

func (s *Store) UpdateVoiceProfileValidation(ctx context.Context, profileID uint, status, errMsg string) error {
	updates := map[string]any{
		"validation_status": status,
		"validation_error":  errMsg,
		"updated_at":        time.Now().UTC(),
	}
	if status == "valid" {
		now := time.Now().UTC()
		updates["validated_at"] = &now
	}
	return s.db.WithContext(ctx).Model(&models.VoiceProfile{}).Where("id = ?", profileID).Updates(updates).Error
}

// UpdateVoiceProfileSpeakingRate updates the empirical speaking-rate estimate for a
// voice profile using an exponential moving average:
//
//	if EstCharsPerSec is nil (first calibration): new = observedRate
//	otherwise: new = alpha * observedRate + (1 - alpha) * old
//
// alpha should be in (0, 1]; 0.3 is a reasonable default (30% new data).
func (s *Store) UpdateVoiceProfileSpeakingRate(ctx context.Context, vpID uint, observedRate float64, alpha float64) error {
	if observedRate <= 0 {
		return nil
	}
	if alpha <= 0 || alpha > 1 {
		alpha = 0.3
	}
	var vp models.VoiceProfile
	if err := s.db.WithContext(ctx).First(&vp, vpID).Error; err != nil {
		return err
	}
	var newRate float64
	if vp.EstCharsPerSec == nil {
		newRate = observedRate
	} else {
		newRate = alpha*observedRate + (1-alpha)*(*vp.EstCharsPerSec)
	}
	return s.db.WithContext(ctx).Model(&models.VoiceProfile{}).
		Where("id = ?", vpID).
		Update("est_chars_per_sec", newRate).Error
}

func (s *Store) ReplaceSegments(ctx context.Context, jobID uint, drafts []models.SegmentDraft) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var existingSpeakers []models.Speaker
		if err := tx.Where("job_id = ?", jobID).Find(&existingSpeakers).Error; err != nil {
			return err
		}

		speakersByLabel := map[string]models.Speaker{}
		for _, speaker := range existingSpeakers {
			speakersByLabel[speaker.Label] = speaker
		}

		for _, draft := range drafts {
			label := draft.SpeakerLabel
			if label == "" {
				label = "SPK_01"
			}
			if _, exists := speakersByLabel[label]; exists {
				continue
			}
			speaker := models.Speaker{
				JobID: jobID,
				Label: label,
				Name:  label,
			}
			if err := tx.Create(&speaker).Error; err != nil {
				return err
			}
			speakersByLabel[label] = speaker
		}

		if err := tx.Where("job_id = ?", jobID).Delete(&models.Segment{}).Error; err != nil {
			return err
		}

		segments := make([]models.Segment, 0, len(drafts))
		for idx, draft := range drafts {
			label := draft.SpeakerLabel
			if label == "" {
				label = "SPK_01"
			}
			speaker := speakersByLabel[label]
			speakerID := speaker.ID
		segment := models.Segment{
			JobID:              jobID,
			SpeakerID:          &speakerID,
			SpeakerLabel:       label,
			Ordinal:            idx,
			StartMs:            draft.StartMs,
			EndMs:              draft.EndMs,
			OriginalDurationMs: draft.EndMs - draft.StartMs,
			SourceText:         draft.Text,
			SplitReason:        draft.SplitReason,
			Status:             models.SegmentStatusPending,
		}
			segments = append(segments, segment)
		}

		if len(segments) == 0 {
			return nil
		}
		return tx.Create(&segments).Error
	})
}

func (s *Store) ListSegments(ctx context.Context, jobID uint, segmentIDs []uint) ([]models.Segment, error) {
	var segments []models.Segment
	query := s.db.WithContext(ctx).Where("job_id = ?", jobID).Order("ordinal asc")
	if len(segmentIDs) > 0 {
		query = query.Where("id IN ?", segmentIDs)
	}
	err := query.Find(&segments).Error
	return segments, err
}

func (s *Store) ListStageRuns(ctx context.Context, jobID uint) ([]models.JobStageRun, error) {
	var runs []models.JobStageRun
	err := s.db.WithContext(ctx).
		Where("job_id = ?", jobID).
		Order("started_at desc").
		Find(&runs).Error
	return runs, err
}

func (s *Store) CreateStageRun(ctx context.Context, run *models.JobStageRun) error {
	return s.db.WithContext(ctx).Create(run).Error
}

func (s *Store) FinishStageRun(ctx context.Context, runID uint, status, errMsg string, durationMs int64, meta map[string]any) error {
	updates := map[string]any{
		"status":        status,
		"error_message": errMsg,
		"duration_ms":   durationMs,
		"updated_at":    time.Now().UTC(),
	}
	now := time.Now().UTC()
	updates["finished_at"] = &now
	if meta != nil {
		updates["meta"] = meta
	}
	return s.db.WithContext(ctx).Model(&models.JobStageRun{}).Where("id = ?", runID).Updates(updates).Error
}

func (s *Store) GetSegment(ctx context.Context, id uint) (*models.Segment, error) {
	var seg models.Segment
	err := s.db.WithContext(ctx).Where("id = ?", id).First(&seg).Error
	if err != nil {
		return nil, err
	}
	return &seg, nil
}

func (s *Store) UpdateSegmentMeta(ctx context.Context, segmentID uint, metaUpdates map[string]any) error {
	seg, err := s.GetSegment(ctx, segmentID)
	if err != nil {
		return err
	}
	merged := make(map[string]any)
	if seg.Meta != nil {
		for k, v := range seg.Meta {
			merged[k] = v
		}
	}
	for k, v := range metaUpdates {
		if v == nil {
			delete(merged, k)
		} else {
			merged[k] = v
		}
	}
	return s.db.WithContext(ctx).Model(&models.Segment{}).Where("id = ?", segmentID).
		Updates(map[string]any{"meta": merged, "updated_at": time.Now().UTC()}).Error
}

func (s *Store) UpdateSegmentTranslations(ctx context.Context, segments []models.Segment) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, segment := range segments {
			updates := map[string]any{
				"target_text": segment.TargetText,
				"status":      segment.Status,
				"updated_at":  time.Now().UTC(),
			}
			if err := tx.Model(&models.Segment{}).Where("id = ?", segment.ID).Updates(updates).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

func (s *Store) UpdateSegmentSynthResults(ctx context.Context, segments []models.Segment) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, segment := range segments {
			updates := map[string]any{
				"tts_audio_rel_path": segment.TTSAudioRelPath,
				"tts_duration_ms":  segment.TTSDurationMs,
				"status":           segment.Status,
				"updated_at":       time.Now().UTC(),
			}
			if err := tx.Model(&models.Segment{}).Where("id = ?", segment.ID).Updates(updates).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

// UpdateSegmentJudgeResult writes the OPT-002 judge verdict for one segment.
// score is the scalar overall score (0..1, currently == JudgeResult.Fidelity);
// metaJSON is the full structured verdict serialised to JSON (issues, sub-scores).
//
// This MUST NOT be called inside the main pipeline transaction — judging is
// async and observe-only, so its failures are non-fatal and should not roll
// back synthesis writes.
func (s *Store) UpdateSegmentJudgeResult(ctx context.Context, segmentID uint, score float64, metaJSON []byte) error {
	updates := map[string]any{
		"judge_score": score,
		"judge_meta":  metaJSON,
		"updated_at":  time.Now().UTC(),
	}
	return s.db.WithContext(ctx).Model(&models.Segment{}).Where("id = ?", segmentID).Updates(updates).Error
}

func (s *Store) UpsertBindings(ctx context.Context, jobID uint, inputs []BindingInput) ([]uint, error) {
	if len(inputs) == 0 {
		return nil, nil
	}

	affectedSpeakerIDs := make([]uint, 0, len(inputs))
	affectedLabels := make([]string, 0, len(inputs))

	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		for _, input := range inputs {
			var speaker models.Speaker
			switch {
			case input.SpeakerID != nil:
				if err := tx.Where("id = ? AND job_id = ?", *input.SpeakerID, jobID).First(&speaker).Error; err != nil {
					return err
				}
			case input.SpeakerLabel != "":
				err := tx.Where("job_id = ? AND label = ?", jobID, input.SpeakerLabel).First(&speaker).Error
				if errors.Is(err, gorm.ErrRecordNotFound) {
					speaker = models.Speaker{
						JobID: jobID,
						Label: input.SpeakerLabel,
						Name:  input.SpeakerLabel,
					}
					if err := tx.Create(&speaker).Error; err != nil {
						return err
					}
				} else if err != nil {
					return err
				}
			default:
				return fmt.Errorf("binding requires speaker_id or speaker_label")
			}

			affectedSpeakerIDs = append(affectedSpeakerIDs, speaker.ID)
			affectedLabels = append(affectedLabels, speaker.Label)

			var binding models.SpeakerVoiceBinding
			err := tx.Where("job_id = ? AND speaker_id = ?", jobID, speaker.ID).First(&binding).Error
			if errors.Is(err, gorm.ErrRecordNotFound) {
				binding = models.SpeakerVoiceBinding{
					JobID:          jobID,
					SpeakerID:      speaker.ID,
					VoiceProfileID: input.VoiceProfileID,
				}
				if err := tx.Create(&binding).Error; err != nil {
					return err
				}
				continue
			}
			if err != nil {
				return err
			}
			binding.VoiceProfileID = input.VoiceProfileID
			if err := tx.Save(&binding).Error; err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}

	var segments []models.Segment
	query := s.db.WithContext(ctx).Model(&models.Segment{}).Where("job_id = ?", jobID)
	if len(affectedSpeakerIDs) > 0 {
		query = query.Where("speaker_id IN ? OR speaker_label IN ?", affectedSpeakerIDs, affectedLabels)
	}
	if err := query.Find(&segments).Error; err != nil {
		return nil, err
	}

	segmentIDs := make([]uint, 0, len(segments))
	for _, segment := range segments {
		segmentIDs = append(segmentIDs, segment.ID)
	}
	return segmentIDs, nil
}

func (s *Store) ListBindings(ctx context.Context, jobID uint) ([]models.SpeakerVoiceBinding, error) {
	var bindings []models.SpeakerVoiceBinding
	err := s.db.WithContext(ctx).
		Preload("Speaker").
		Preload("VoiceProfile").
		Where("job_id = ?", jobID).
		Order("speaker_id asc").
		Find(&bindings).Error
	return bindings, err
}

func (s *Store) ResolveVoiceProfileForSegment(ctx context.Context, jobID uint, segment models.Segment) (*models.VoiceProfile, error) {
	// Priority 1: per-segment voice override
	if segment.VoiceProfileID != nil {
		var profile models.VoiceProfile
		if err := s.db.WithContext(ctx).First(&profile, *segment.VoiceProfileID).Error; err == nil {
			return &profile, nil
		}
		// If the overridden profile was deleted, fall through to speaker binding
	}
	// Priority 2: speaker-level binding
	if segment.SpeakerID == nil {
		// No speaker assigned — use default TTS voice (no profile).
		return nil, nil
	}
	var binding models.SpeakerVoiceBinding
	err := s.db.WithContext(ctx).
		Preload("VoiceProfile").
		Where("job_id = ?", jobID).
		Where("speaker_id = ?", *segment.SpeakerID).
		First(&binding).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		// Speaker exists but has no voice binding — use default TTS voice.
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &binding.VoiceProfile, nil
}

// UpdateSegmentVoice sets or clears the per-segment voice profile override.
// Pass voiceProfileID=0 to clear the override (revert to speaker binding).
func (s *Store) UpdateSegmentVoice(ctx context.Context, segmentID uint, voiceProfileID uint) error {
	var val interface{}
	if voiceProfileID == 0 {
		val = nil
	} else {
		val = voiceProfileID
	}
	return s.db.WithContext(ctx).Model(&models.Segment{}).
		Where("id = ?", segmentID).
		Updates(map[string]any{
			"voice_profile_id": val,
			"updated_at":       time.Now().UTC(),
		}).Error
}

// BulkSetSegmentVoice sets or clears the per-segment voice override for all
// segments of a job in one query. Pass voiceProfileID=0 to clear overrides.
func (s *Store) BulkSetSegmentVoice(ctx context.Context, jobID uint, voiceProfileID uint) error {
	var val interface{}
	if voiceProfileID == 0 {
		val = nil
	} else {
		val = voiceProfileID
	}
	return s.db.WithContext(ctx).Model(&models.Segment{}).
		Where("job_id = ?", jobID).
		Updates(map[string]any{
			"voice_profile_id": val,
			"updated_at":       time.Now().UTC(),
		}).Error
}

// ResetAllSegmentTTS clears TTS results for all segments of a job so the
// tts_duration stage will re-synthesize them from scratch.
func (s *Store) ResetAllSegmentTTS(ctx context.Context, jobID uint) error {
	return s.db.WithContext(ctx).Model(&models.Segment{}).
		Where("job_id = ?", jobID).
		Updates(map[string]any{
			"tts_audio_rel_path": "",
			"tts_duration_ms":    0,
			"status":             "pending",
			"updated_at":         time.Now().UTC(),
		}).Error
}

func (s *Store) ListSegmentsForMerge(ctx context.Context, jobID uint) ([]models.Segment, error) {
	var segments []models.Segment
	err := s.db.WithContext(ctx).
		Where("job_id = ?", jobID).
		Where("tts_audio_rel_path <> ''").
		Order("ordinal asc").
		Find(&segments).Error
	return segments, err
}

func (s *Store) GetSegmentIDsForSpeakers(ctx context.Context, jobID uint, speakerLabels []string) ([]uint, error) {
	var segments []models.Segment
	if err := s.db.WithContext(ctx).
		Select("id").
		Where("job_id = ?", jobID).
		Where("speaker_label IN ?", speakerLabels).
		Find(&segments).Error; err != nil {
		return nil, err
	}

	ids := make([]uint, 0, len(segments))
	for _, segment := range segments {
		ids = append(ids, segment.ID)
	}
	return ids, nil
}

func (s *Store) ResetSegmentsForRerun(ctx context.Context, segmentIDs []uint) error {
	if len(segmentIDs) == 0 {
		return nil
	}
	return s.db.WithContext(ctx).Model(&models.Segment{}).
		Where("id IN ?", segmentIDs).
		Updates(map[string]any{
			"tts_audio_rel_path": "",
			"tts_duration_ms":    0,
			"status":             "pending",
			"updated_at":         time.Now().UTC(),
		}).Error
}

// UpdateSegmentTranslationAndReset atomically updates a segment's target text
// and resets its TTS fields to pending within a single transaction.
func (s *Store) UpdateSegmentTranslationAndReset(ctx context.Context, segmentID uint, targetText string) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		now := time.Now().UTC()
		if err := tx.Model(&models.Segment{}).Where("id = ?", segmentID).
			Updates(map[string]any{
				"target_text": targetText,
				"status":      string(models.SegmentStatusTranslated),
				"updated_at":  now,
			}).Error; err != nil {
			return err
		}
		return tx.Model(&models.Segment{}).Where("id = ?", segmentID).
			Updates(map[string]any{
				"tts_audio_rel_path": "",
				"tts_duration_ms":    0,
				"status":             "pending",
				"updated_at":         now,
			}).Error
	})
}

func (s *Store) RequestJobCancel(ctx context.Context, jobID uint) error {
	now := time.Now().UTC()
	return s.db.WithContext(ctx).Model(&models.Job{}).
		Where("id = ?", jobID).
		Updates(map[string]any{
			"status":               models.JobStatusCancelRequested,
			"cancel_requested_at":  &now,
			"updated_at":           now,
		}).Error
}

func (s *Store) IsCancelRequested(ctx context.Context, jobID uint) (bool, error) {
	var job models.Job
	if err := s.db.WithContext(ctx).Select("status").First(&job, jobID).Error; err != nil {
		return false, err
	}
	return job.Status == models.JobStatusCancelRequested, nil
}

func (s *Store) MarshalJSONMap(input map[string]any) ([]byte, error) {
	if input == nil {
		return []byte("{}"), nil
	}
	return json.Marshal(input)
}

func UniqueUint(values []uint) []uint {
	if len(values) == 0 {
		return nil
	}
	cloned := slices.Clone(values)
	slices.Sort(cloned)
	return slices.Compact(cloned)
}

// ── Segment suggestions (segment_review stage) ────────────────────────────────

// CreateSuggestions bulk-inserts LLM-generated review suggestions for a job.
// Any existing suggestions for the job are deleted first so that re-running
// segment_review is always idempotent.
func (s *Store) CreateSuggestions(ctx context.Context, jobID uint, suggestions []models.SegmentSuggestion) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("job_id = ?", jobID).Delete(&models.SegmentSuggestion{}).Error; err != nil {
			return err
		}
		if len(suggestions) == 0 {
			return nil
		}
		for i := range suggestions {
			suggestions[i].JobID = jobID
			suggestions[i].Status = "pending"
		}
		return tx.Create(&suggestions).Error
	})
}

// ListSuggestions returns all suggestions for a job ordered by ordinal.
func (s *Store) ListSuggestions(ctx context.Context, jobID uint) ([]models.SegmentSuggestion, error) {
	var items []models.SegmentSuggestion
	err := s.db.WithContext(ctx).
		Where("job_id = ?", jobID).
		Order("ordinal asc").
		Find(&items).Error
	return items, err
}

// UpdateSuggestionStatus sets the status of a single suggestion.
// A suggestion that is already accepted or rejected is not modified.
func (s *Store) UpdateSuggestionStatus(ctx context.Context, suggestionID uint, status string) error {
	return s.db.WithContext(ctx).
		Model(&models.SegmentSuggestion{}).
		Where("id = ? AND status = 'pending'", suggestionID).
		Updates(map[string]any{
			"status":     status,
			"updated_at": time.Now().UTC(),
		}).Error
}

// DeleteSuggestionsForJob removes all suggestions for a job.
func (s *Store) DeleteSuggestionsForJob(ctx context.Context, jobID uint) error {
	return s.db.WithContext(ctx).Where("job_id = ?", jobID).Delete(&models.SegmentSuggestion{}).Error
}

// GetSuggestion fetches a single suggestion by id.
func (s *Store) GetSuggestion(ctx context.Context, id uint) (*models.SegmentSuggestion, error) {
	var s2 models.SegmentSuggestion
	if err := s.db.WithContext(ctx).First(&s2, id).Error; err != nil {
		return nil, err
	}
	return &s2, nil
}

// ── Segment structural edits ──────────────────────────────────────────────────

// MergeSegments merges a set of consecutive segments (identified by IDs) into
// a single segment.  The merged segment uses the earliest start_ms and latest
// end_ms, concatenates the source texts with a space, and inherits the
// speaker / voice of the first segment.  All TTS and translation fields are
// cleared so the merged segment starts fresh.
//
// Constraints enforced:
//   - All segment IDs must belong to the specified job.
//   - The segments must be consecutive by ordinal (no gaps allowed).
//   - At least 2 IDs must be provided.
func (s *Store) MergeSegments(ctx context.Context, jobID uint, segmentIDs []uint) error {
	if len(segmentIDs) < 2 {
		return fmt.Errorf("merge requires at least 2 segment IDs")
	}
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var segs []models.Segment
		if err := tx.Where("job_id = ? AND id IN ?", jobID, segmentIDs).
			Order("ordinal asc").
			Find(&segs).Error; err != nil {
			return err
		}
		if len(segs) != len(segmentIDs) {
			return fmt.Errorf("some segment IDs not found in job %d", jobID)
		}
		// Verify consecutive ordinals
		for i := 1; i < len(segs); i++ {
			if segs[i].Ordinal != segs[i-1].Ordinal+1 {
				return fmt.Errorf("segments are not consecutive (ordinal gap between %d and %d)", segs[i-1].Ordinal, segs[i].Ordinal)
			}
		}

		first := segs[0]
		last := segs[len(segs)-1]

		// Concatenate source texts
		texts := make([]string, len(segs))
		for i, seg := range segs {
			texts[i] = seg.SourceText
		}
		mergedText := strings.Join(texts, " ")

		// Update the first segment with merged data
		now := time.Now().UTC()
		if err := tx.Model(&models.Segment{}).Where("id = ?", first.ID).
			Updates(map[string]any{
				"end_ms":              last.EndMs,
				"original_duration_ms": last.EndMs - first.StartMs,
				"source_text":         mergedText,
				"target_text":         "",
				"tts_audio_rel_path":  "",
				"tts_duration_ms":     0,
				"status":              "pending",
				"split_reason":        "merged",
				"updated_at":          now,
			}).Error; err != nil {
			return err
		}

		// Delete the rest
		deleteIDs := make([]uint, 0, len(segs)-1)
		for _, seg := range segs[1:] {
			deleteIDs = append(deleteIDs, seg.ID)
		}
		if err := tx.Where("id IN ?", deleteIDs).Delete(&models.Segment{}).Error; err != nil {
			return err
		}

		// Renumber all ordinals for the job
		return renumberSegmentOrdinals(tx, jobID)
	})
}

// SplitSegment splits one segment into two at a given character index within
// its source text.  The time boundary is estimated by character proportion.
// Both resulting segments have their TTS and translation fields cleared.
func (s *Store) SplitSegment(ctx context.Context, jobID uint, segmentID uint, splitCharIndex int) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var seg models.Segment
		if err := tx.Where("id = ? AND job_id = ?", segmentID, jobID).First(&seg).Error; err != nil {
			return err
		}

		runes := []rune(seg.SourceText)
		total := len(runes)
		if splitCharIndex <= 0 || splitCharIndex >= total {
			return fmt.Errorf("split_char_index %d out of range [1, %d)", splitCharIndex, total)
		}

		textA := strings.TrimSpace(string(runes[:splitCharIndex]))
		textB := strings.TrimSpace(string(runes[splitCharIndex:]))

		// Proportional time split
		ratio := float64(splitCharIndex) / float64(total)
		splitMs := seg.StartMs + int64(ratio*float64(seg.EndMs-seg.StartMs))

		now := time.Now().UTC()

		// Update first half in-place
		if err := tx.Model(&models.Segment{}).Where("id = ?", seg.ID).
			Updates(map[string]any{
				"end_ms":              splitMs,
				"original_duration_ms": splitMs - seg.StartMs,
				"source_text":         textA,
				"target_text":         "",
				"tts_audio_rel_path":  "",
				"tts_duration_ms":     0,
				"status":              "pending",
				"split_reason":        "manual_split",
				"updated_at":          now,
			}).Error; err != nil {
			return err
		}

		// Insert second half after the first — temporarily use a large ordinal, renumber at end
		secondSpeakerID := seg.SpeakerID
		second := models.Segment{
			JobID:              jobID,
			SpeakerID:          secondSpeakerID,
			SpeakerLabel:       seg.SpeakerLabel,
			VoiceProfileID:     seg.VoiceProfileID,
			Ordinal:            seg.Ordinal + 1,
			StartMs:            splitMs,
			EndMs:              seg.EndMs,
			OriginalDurationMs: seg.EndMs - splitMs,
			SourceText:         textB,
			SplitReason:        "manual_split",
			Status:             models.SegmentStatusPending,
		}

		// Make room: shift ordinals of segments after the split point by 1
		if err := tx.Model(&models.Segment{}).
			Where("job_id = ? AND ordinal > ?", jobID, seg.Ordinal).
			Update("ordinal", gorm.Expr("ordinal + 1")).Error; err != nil {
			return err
		}

		if err := tx.Create(&second).Error; err != nil {
			return err
		}

		return renumberSegmentOrdinals(tx, jobID)
	})
}

// UpdateSegmentSourceText replaces the ASR transcript (source_text) of a
// single segment without touching its timing, status or any downstream
// fields (translation / TTS / meta).  This is the shared write path used by
// both the manual "edit ASR transcript" UI control and the per-segment ASR
// re-transcription endpoint during the awaiting_review stage.
//
// Caller must already have validated that:
//   - the parent job is in JobStatusAwaitingReview
//   - the new text is non-empty after trimming
//   - the new text fits the configured length budget
//
// jobID is supplied as a defence-in-depth check so that a leaked segment ID
// from a different job cannot be mutated through this code path.  When the
// (jobID, segmentID) tuple does not match any row, gorm.ErrRecordNotFound is
// returned so handlers can map it to a 404.
func (s *Store) UpdateSegmentSourceText(ctx context.Context, jobID uint, segmentID uint, sourceText string) error {
	result := s.db.WithContext(ctx).Model(&models.Segment{}).
		Where("id = ? AND job_id = ?", segmentID, jobID).
		Updates(map[string]any{
			"source_text": sourceText,
			"updated_at":  time.Now().UTC(),
		})
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected == 0 {
		return gorm.ErrRecordNotFound
	}
	return nil
}

// UpdateSegmentTimes adjusts the start/end timestamps of a single segment.
// original_duration_ms is recalculated from the new range.
// Any cached TTS audio is cleared so the segment is re-synthesised after translation.
// Ordinals are renumbered in case the adjusted start_ms changes the segment order.
func (s *Store) UpdateSegmentTimes(ctx context.Context, segmentID uint, startMs, endMs int64) error {
	if endMs <= startMs {
		return fmt.Errorf("end_ms (%d) must be greater than start_ms (%d)", endMs, startMs)
	}
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var seg models.Segment
		if err := tx.Where("id = ?", segmentID).First(&seg).Error; err != nil {
			return err
		}
		if err := tx.Model(&models.Segment{}).
			Where("id = ?", segmentID).
			Updates(map[string]any{
				"start_ms":             startMs,
				"end_ms":               endMs,
				"original_duration_ms": endMs - startMs,
				"tts_audio_rel_path":   "",
				"tts_duration_ms":      0,
				"status":               "pending",
				"updated_at":           time.Now().UTC(),
			}).Error; err != nil {
			return err
		}
		// Renumber ordinals in case start_ms adjustment changed the segment order.
		return renumberSegmentOrdinals(tx, seg.JobID)
	})
}

// renumberSegmentOrdinals reassigns ordinal values 0,1,2,… to all segments of
// a job sorted by (start_ms, id), ensuring a clean gap-free sequence after any
// structural edits.
func renumberSegmentOrdinals(tx *gorm.DB, jobID uint) error {
	var segs []models.Segment
	if err := tx.Where("job_id = ?", jobID).Order("start_ms asc, id asc").Find(&segs).Error; err != nil {
		return err
	}
	now := time.Now().UTC()
	for i, seg := range segs {
		if seg.Ordinal == i {
			continue
		}
		if err := tx.Model(&models.Segment{}).Where("id = ?", seg.ID).
			Updates(map[string]any{"ordinal": i, "updated_at": now}).Error; err != nil {
			return err
		}
	}
	return nil
}
