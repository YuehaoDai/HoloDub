package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/models"
	"holodub/internal/queue"
	"holodub/internal/store"

	"gorm.io/datatypes"
	"gorm.io/gorm"
)

type chapterClone struct {
	SourceJobID uint `json:"source_job_id"`
	ShadowJobID uint `json:"shadow_job_id"`
	Ordinal     int  `json:"ordinal"`
	Segments    int  `json:"segments"`
}

type cloneReport struct {
	SourceEpisodeID uint           `json:"source_episode_id"`
	ShadowEpisodeID uint           `json:"shadow_episode_id"`
	ShadowName      string         `json:"shadow_name"`
	Enqueued        bool           `json:"enqueued"`
	Chapters        []chapterClone `json:"chapters"`
}

func main() {
	sourceEpisodeID := flag.Uint("source-episode-id", 0, "completed episode to clone")
	nameSuffix := flag.String("name-suffix", "", "suffix appended to the cloned episode name")
	enqueue := flag.Bool("enqueue", true, "enqueue every cloned chapter at tts_duration")
	output := flag.String("output", "", "optional path for the clone report JSON")
	flag.Parse()

	if *sourceEpisodeID == 0 {
		log.Fatal("--source-episode-id is required")
	}
	if strings.TrimSpace(*nameSuffix) == "" {
		*nameSuffix = "lora-shadow-" + time.Now().UTC().Format("20060102-150405")
	}

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("load config: %v", err)
	}
	st, err := store.New(cfg)
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	q := queue.New(cfg)

	ctx := context.Background()
	report, err := cloneEpisode(ctx, st.DB(), q, *sourceEpisodeID, *nameSuffix, *enqueue)
	if err != nil {
		log.Fatalf("clone episode: %v", err)
	}
	raw, _ := json.MarshalIndent(report, "", "  ")
	if strings.TrimSpace(*output) != "" {
		if err := os.MkdirAll(filepath.Dir(*output), 0755); err != nil {
			log.Fatalf("create output directory: %v", err)
		}
		if err := os.WriteFile(*output, append(raw, '\n'), 0644); err != nil {
			log.Fatalf("write output report: %v", err)
		}
	}
	fmt.Println(string(raw))
}

func cloneEpisode(
	ctx context.Context,
	db *gorm.DB,
	q *queue.Queue,
	sourceEpisodeID uint,
	nameSuffix string,
	enqueue bool,
) (cloneReport, error) {
	var report cloneReport
	err := db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var source models.Episode
		if err := tx.Preload("Chapters", func(db *gorm.DB) *gorm.DB {
			return db.Order("chapter_ordinal asc, id asc")
		}).First(&source, sourceEpisodeID).Error; err != nil {
			return fmt.Errorf("load source episode %d: %w", sourceEpisodeID, err)
		}
		if len(source.Chapters) == 0 {
			return fmt.Errorf("source episode %d has no chapters", sourceEpisodeID)
		}

		durationMs := source.DurationMs
		if durationMs <= 0 {
			durationMs = maxChapterEndMs(source.Chapters)
		}
		shadow := models.Episode{
			TenantKey:               source.TenantKey,
			Name:                    fmt.Sprintf("%s %s", source.Name, nameSuffix),
			SourceVideoRelPath:      source.SourceVideoRelPath,
			SourceLanguage:          source.SourceLanguage,
			TargetLanguage:          source.TargetLanguage,
			DurationMs:              durationMs,
			TotalChapters:           source.TotalChapters,
			VocalsRelPath:           source.VocalsRelPath,
			BgmRelPath:              source.BgmRelPath,
			ASRDoneAt:               source.ASRDoneAt,
			GlossaryDoneAt:          source.GlossaryDoneAt,
			Glossary:                copyJSON(source.Glossary),
			ReferenceCard:           source.ReferenceCard,
			Status:                  models.EpisodeStatusRunning,
			OutputLayoutVersion:     2,
			ChaptersManifestRelPath: "",
			LLMChapters:             copyJSON(source.LLMChapters),
		}
		if shadow.TotalChapters == 0 {
			shadow.TotalChapters = len(source.Chapters)
		}
		if err := tx.Create(&shadow).Error; err != nil {
			return fmt.Errorf("create shadow episode: %w", err)
		}
		report = cloneReport{
			SourceEpisodeID: source.ID,
			ShadowEpisodeID: shadow.ID,
			ShadowName:      shadow.Name,
			Enqueued:        enqueue,
		}

		for _, chapter := range source.Chapters {
			cloned, err := cloneChapter(ctx, tx, shadow.ID, chapter)
			if err != nil {
				return err
			}
			report.Chapters = append(report.Chapters, cloned)
		}
		return nil
	})
	if err != nil {
		return cloneReport{}, err
	}
	if enqueue {
		for _, chapter := range report.Chapters {
			task := models.TaskPayload{
				JobID:       chapter.ShadowJobID,
				Stage:       models.StageTTSDuration,
				Attempt:     0,
				RequestedBy: "lora_shadow_clone",
				Reason:      "lora_shadow_episode_tts",
			}
			if err := q.Enqueue(ctx, task); err != nil {
				return report, fmt.Errorf("enqueue chapter job %d: %w", chapter.ShadowJobID, err)
			}
		}
	}
	return report, nil
}

func maxChapterEndMs(chapters []models.Job) int64 {
	var out int64
	for _, chapter := range chapters {
		if chapter.ChapterEndMs > out {
			out = chapter.ChapterEndMs
		}
	}
	return out
}

func cloneChapter(ctx context.Context, tx *gorm.DB, shadowEpisodeID uint, source models.Job) (chapterClone, error) {
	shadow := models.Job{
		TenantKey:              source.TenantKey,
		Name:                   source.Name,
		Status:                 models.JobStatusQueued,
		CurrentStage:           models.StageTTSDuration,
		SourceLanguage:         source.SourceLanguage,
		TargetLanguage:         source.TargetLanguage,
		InputRelPath:           source.InputRelPath,
		VocalsRelPath:          source.VocalsRelPath,
		BgmRelPath:             source.BgmRelPath,
		Config:                 copyJSONMap(source.Config),
		TranslationSummary:     source.TranslationSummary,
		EpisodeID:              shadowEpisodeID,
		ChapterOrdinal:         source.ChapterOrdinal,
		ChapterStartMs:         source.ChapterStartMs,
		ChapterEndMs:           source.ChapterEndMs,
		ChapterTitle:           source.ChapterTitle,
		ChapterTitleTranslated: source.ChapterTitleTranslated,
		ChapterSummaryMD:       source.ChapterSummaryMD,
		MaxRetries:             source.MaxRetries,
	}
	if shadow.ChapterOrdinal == 0 {
		shadow.ChapterOrdinal = 1
	}
	if err := tx.Create(&shadow).Error; err != nil {
		return chapterClone{}, fmt.Errorf("create shadow chapter for source job %d: %w", source.ID, err)
	}

	speakerIDMap, err := cloneSpeakers(ctx, tx, source.ID, shadow.ID)
	if err != nil {
		return chapterClone{}, err
	}
	if err := cloneBindings(ctx, tx, source.ID, shadow.ID, speakerIDMap); err != nil {
		return chapterClone{}, err
	}
	segmentCount, err := cloneSegments(ctx, tx, source.ID, shadow.ID, speakerIDMap)
	if err != nil {
		return chapterClone{}, err
	}
	return chapterClone{
		SourceJobID: source.ID,
		ShadowJobID: shadow.ID,
		Ordinal:     shadow.ChapterOrdinal,
		Segments:    segmentCount,
	}, nil
}

func cloneSpeakers(ctx context.Context, tx *gorm.DB, sourceJobID uint, shadowJobID uint) (map[uint]uint, error) {
	var speakers []models.Speaker
	if err := tx.WithContext(ctx).Where("job_id = ?", sourceJobID).Order("id asc").Find(&speakers).Error; err != nil {
		return nil, fmt.Errorf("list speakers for job %d: %w", sourceJobID, err)
	}
	out := make(map[uint]uint, len(speakers))
	for _, source := range speakers {
		shadow := models.Speaker{
			JobID: shadowJobID,
			Label: source.Label,
			Name:  source.Name,
			Meta:  copyJSONMap(source.Meta),
		}
		if err := tx.Create(&shadow).Error; err != nil {
			return nil, fmt.Errorf("create speaker %q for job %d: %w", source.Label, shadowJobID, err)
		}
		out[source.ID] = shadow.ID
	}
	return out, nil
}

func cloneBindings(ctx context.Context, tx *gorm.DB, sourceJobID uint, shadowJobID uint, speakerIDMap map[uint]uint) error {
	var bindings []models.SpeakerVoiceBinding
	if err := tx.WithContext(ctx).Where("job_id = ?", sourceJobID).Order("id asc").Find(&bindings).Error; err != nil {
		return fmt.Errorf("list bindings for job %d: %w", sourceJobID, err)
	}
	for _, source := range bindings {
		shadowSpeakerID, ok := speakerIDMap[source.SpeakerID]
		if !ok {
			continue
		}
		shadow := models.SpeakerVoiceBinding{
			JobID:          shadowJobID,
			SpeakerID:      shadowSpeakerID,
			VoiceProfileID: source.VoiceProfileID,
		}
		if err := tx.Create(&shadow).Error; err != nil {
			return fmt.Errorf("create binding for job %d speaker %d: %w", shadowJobID, shadowSpeakerID, err)
		}
	}
	return nil
}

func cloneSegments(ctx context.Context, tx *gorm.DB, sourceJobID uint, shadowJobID uint, speakerIDMap map[uint]uint) (int, error) {
	var segments []models.Segment
	if err := tx.WithContext(ctx).Where("job_id = ?", sourceJobID).Order("ordinal asc").Find(&segments).Error; err != nil {
		return 0, fmt.Errorf("list segments for job %d: %w", sourceJobID, err)
	}
	for _, source := range segments {
		var speakerID *uint
		if source.SpeakerID != nil {
			if mapped, ok := speakerIDMap[*source.SpeakerID]; ok {
				speakerID = &mapped
			}
		}
		status := models.SegmentStatusTranslated
		if source.Status == models.SegmentStatusSkipped {
			status = models.SegmentStatusSkipped
		}
		shadow := models.Segment{
			JobID:              shadowJobID,
			SpeakerID:          speakerID,
			SpeakerLabel:       source.SpeakerLabel,
			VoiceProfileID:     source.VoiceProfileID,
			Ordinal:            source.Ordinal,
			StartMs:            source.StartMs,
			EndMs:              source.EndMs,
			OriginalDurationMs: source.OriginalDurationMs,
			SourceText:         source.SourceText,
			TargetText:         source.TargetText,
			SplitReason:        source.SplitReason,
			Status:             status,
			Meta:               shadowSegmentMeta(source.Meta, sourceJobID, source.ID),
		}
		if err := tx.Create(&shadow).Error; err != nil {
			return 0, fmt.Errorf("create segment ordinal %d for job %d: %w", source.Ordinal, shadowJobID, err)
		}
	}
	return len(segments), nil
}

func shadowSegmentMeta(meta datatypes.JSONMap, sourceJobID uint, sourceSegmentID uint) datatypes.JSONMap {
	out := cleanSegmentMeta(meta)
	if out == nil {
		out = datatypes.JSONMap{}
	}
	out["shadow_source_job_id"] = sourceJobID
	out["shadow_source_segment_id"] = sourceSegmentID
	return out
}

func cleanSegmentMeta(meta datatypes.JSONMap) datatypes.JSONMap {
	out := copyJSONMap(meta)
	for _, key := range []string{
		"tts_lora_candidate",
		"tts_lora_candidate_runtime",
		"tts_spoken_text",
		"tts_text_rewrites",
		"tts_text_normalized",
		"voice_config_source",
		"voice_config_sample_relpaths",
	} {
		delete(out, key)
	}
	return out
}

func copyJSONMap(in datatypes.JSONMap) datatypes.JSONMap {
	if in == nil {
		return nil
	}
	out := make(datatypes.JSONMap, len(in))
	for key, value := range in {
		out[key] = value
	}
	return out
}

func copyJSON(in datatypes.JSON) datatypes.JSON {
	if len(in) == 0 {
		return nil
	}
	out := make(datatypes.JSON, len(in))
	copy(out, in)
	return out
}
