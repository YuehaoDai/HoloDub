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

func (s *Store) AutoMigrate() error {
	return s.db.AutoMigrate(
		&models.Job{},
		&models.VoiceProfile{},
		&models.Speaker{},
		&models.SpeakerVoiceBinding{},
		&models.Segment{},
		&models.JobStageRun{},
		&models.TenantQuota{},
	)
}

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
	return s.db.WithContext(ctx).Create(job).Error
}

func (s *Store) ListJobs(ctx context.Context) ([]models.Job, error) {
	var jobs []models.Job
	err := s.db.WithContext(ctx).
		Order("id desc").
		Find(&jobs).Error
	return jobs, err
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
			Status:             "pending",
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
		var profile models.VoiceProfile
		if err := s.db.WithContext(ctx).Order("id asc").First(&profile).Error; err != nil {
			return nil, err
		}
		return &profile, nil
	}
	var binding models.SpeakerVoiceBinding
	err := s.db.WithContext(ctx).
		Preload("VoiceProfile").
		Where("job_id = ?", jobID).
		Where("speaker_id = ?", *segment.SpeakerID).
		First(&binding).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		var profile models.VoiceProfile
		if err := s.db.WithContext(ctx).Order("id asc").First(&profile).Error; err != nil {
			return nil, err
		}
		return &profile, nil
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
				"status":      "translated",
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
