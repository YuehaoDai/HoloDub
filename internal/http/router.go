package http

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	stdhttp "net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/media"
	"holodub/internal/models"
	"holodub/internal/observability"
	"holodub/internal/pipeline"
	"holodub/internal/storage"
	"holodub/internal/store"
	"holodub/internal/ui"

	"github.com/gin-gonic/gin"
	"gorm.io/datatypes"
	"gorm.io/gorm"
)

type Server struct {
	cfg      config.Config
	store    *store.Store
	pipeline *pipeline.Service
}

type createJobRequest struct {
	Name           string         `json:"name"`
	ExternalID     string         `json:"external_id"`
	TenantKey      string         `json:"tenant_key"`
	InputRelPath   string         `json:"input_relpath" binding:"required"`
	SourceLanguage string         `json:"source_language"`
	TargetLanguage string         `json:"target_language" binding:"required"`
	WebhookURL     string         `json:"webhook_url"`
	WebhookSecret  string         `json:"webhook_secret"`
	MaxRetries     int            `json:"max_retries"`
	DeadlineAt     *time.Time     `json:"deadline_at"`
	Config         map[string]any `json:"config"`
	AutoStart      bool           `json:"auto_start"`
}

type retryJobRequest struct {
	Stage      string `json:"stage"`
	SegmentIDs []uint `json:"segment_ids"`
}

type voiceProfileRequest struct {
	TenantKey         string         `json:"tenant_key"`
	Name              string         `json:"name" binding:"required"`
	Mode              string         `json:"mode"`
	Provider          string         `json:"provider"`
	Language          string         `json:"language"`
	SampleRelPaths    []string       `json:"sample_relpaths"`
	CheckpointRelPath string         `json:"checkpoint_relpath"`
	IndexRelPath      string         `json:"index_relpath"`
	ConfigRelPath     string         `json:"config_relpath"`
	InternalSpeakerID string         `json:"internal_speaker_id"`
	Meta              map[string]any `json:"meta"`
}

type bindingItem struct {
	SpeakerID      *uint  `json:"speaker_id"`
	SpeakerLabel   string `json:"speaker_label"`
	VoiceProfileID uint   `json:"voice_profile_id" binding:"required"`
}

type upsertBindingsRequest struct {
	Bindings      []bindingItem `json:"bindings" binding:"required"`
	RerunAffected bool          `json:"rerun_affected"`
}

type artifactInfo struct {
	RelPath      string    `json:"relpath"`
	SizeBytes    int64     `json:"size_bytes"`
	ModifiedAt   time.Time `json:"modified_at"`
}

func NewRouter(cfg config.Config, st *store.Store, pipelineSvc *pipeline.Service) *gin.Engine {
	server := &Server{cfg: cfg, store: st, pipeline: pipelineSvc}
	router := gin.New()

	_ = router.SetTrustedProxies(cfg.TrustedProxies)
	router.Use(
		requestIDMiddleware(),
		tenantMiddleware(cfg.DefaultTenantKey),
		metricsMiddleware(),
		loggingMiddleware(),
		gin.Recovery(),
		apiKeyAuthMiddleware(cfg.APIAuthToken),
		rateLimitMiddleware(cfg.RequestRateLimitRPS, cfg.RequestRateLimitBurst),
	)

	router.GET("/", func(c *gin.Context) { c.Redirect(stdhttp.StatusTemporaryRedirect, "/ui/") })
	router.GET("/ui", func(c *gin.Context) { c.Redirect(stdhttp.StatusTemporaryRedirect, "/ui/") })
	router.GET("/ui/*filepath", server.serveUI)

	router.GET("/healthz", server.handleHealth)
	router.GET("/ml-health", server.handleMLHealth)
	if cfg.EnableMetrics {
		router.GET("/metrics", gin.WrapH(observability.MetricsHandler()))
	}

	router.GET("/jobs", server.listJobs)
	router.POST("/jobs", server.createJob)
	router.GET("/jobs/:id", server.getJob)
	router.POST("/jobs/:id/start", server.startJob)
	router.POST("/jobs/:id/retry", server.retryJob)
	router.POST("/jobs/:id/cancel", server.cancelJob)
	router.GET("/jobs/:id/segments", server.listSegments)
	router.GET("/jobs/:id/stage-runs", server.listStageRuns)
	router.GET("/jobs/:id/bindings", server.listBindings)
	router.GET("/jobs/:id/artifacts", server.listArtifacts)
	router.POST("/jobs/:id/segments/:segmentId/rerun", server.rerunSegment)
	router.GET("/jobs/:id/tts/:ordinal", server.serveSegmentAudio)
	router.GET("/jobs/:id/audio/:ordinal", server.serveOriginalAudio)
	router.PATCH("/jobs/:id/segments/:segmentId", server.patchSegment)
	router.PATCH("/jobs/:id/segments/:segmentId/quality", server.patchSegmentQuality)

	router.GET("/voice-profiles", server.listVoiceProfiles)
	router.POST("/voice-profiles", server.createVoiceProfile)
	router.GET("/voice-profiles/:id", server.getVoiceProfile)
	router.POST("/voice-profiles/:id/validate", server.validateVoiceProfile)
	router.PUT("/jobs/:id/bindings", server.upsertBindings)
	router.POST("/jobs/:id/segments/:segmentId/preview-voice", server.previewSegmentVoice)
	router.GET("/jobs/:id/preview-voice/:segmentId", server.servePreviewAudio)

	return router
}

func (s *Server) serveUI(c *gin.Context) {
	path := strings.TrimPrefix(c.Param("filepath"), "/")
	if path == "" {
		// Serve index.html directly to avoid redirect loop caused by
		// http.FileServer redirecting /index.html → / → /ui/ → loop.
		// Never cache index.html so browsers always fetch the latest entry point.
		data, err := fs.ReadFile(ui.FS(), "index.html")
		if err != nil {
			c.Status(stdhttp.StatusNotFound)
			return
		}
		c.Header("Cache-Control", "no-store, no-cache, must-revalidate")
		c.Data(stdhttp.StatusOK, "text/html; charset=utf-8", data)
		return
	}

	// Check whether the requested path exists as a real asset (JS/CSS/etc.).
	// If not, serve index.html so Vue Router can handle client-side navigation
	// (e.g. direct access to /ui/jobs/123 after a browser refresh).
	if _, err := fs.Stat(ui.FS(), path); err != nil {
		data, readErr := fs.ReadFile(ui.FS(), "index.html")
		if readErr != nil {
			c.Status(stdhttp.StatusNotFound)
			return
		}
		c.Header("Cache-Control", "no-store, no-cache, must-revalidate")
		c.Data(stdhttp.StatusOK, "text/html; charset=utf-8", data)
		return
	}
	// Hashed assets (JS/CSS) are safe to cache aggressively.
	c.Header("Cache-Control", "public, max-age=31536000, immutable")
	c.FileFromFS(path, stdhttp.FS(ui.FS()))
}

func (s *Server) handleHealth(c *gin.Context) {
	c.JSON(stdhttp.StatusOK, gin.H{
		"status":             "ok",
		"environment":        s.cfg.Environment,
		"metrics_enabled":    s.cfg.EnableMetrics,
		"default_tenant_key": s.cfg.DefaultTenantKey,
		"timestamp":          time.Now().UTC(),
	})
}

func (s *Server) handleMLHealth(c *gin.Context) {
	resp, err := s.pipeline.MLHealth(c.Request.Context())
	if err != nil {
		c.JSON(stdhttp.StatusServiceUnavailable, gin.H{
			"error":   "ml_service_unavailable",
			"message": err.Error(),
		})
		return
	}
	c.JSON(stdhttp.StatusOK, resp)
}

func (s *Server) listJobs(c *gin.Context) {
	jobs, err := s.store.ListJobs(c.Request.Context())
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_jobs_failed", err.Error())
		return
	}
	tenantKey := tenantKeyFromContext(c)
	filtered := make([]models.Job, 0, len(jobs))
	for _, job := range jobs {
		if job.TenantKey == "" || job.TenantKey == tenantKey {
			filtered = append(filtered, job)
		}
	}
	c.JSON(stdhttp.StatusOK, gin.H{"jobs": filtered})
}

func (s *Server) createJob(c *gin.Context) {
	var request createJobRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_job_request", err.Error())
		return
	}

	tenantKey := request.TenantKey
	if tenantKey == "" {
		tenantKey = tenantKeyFromContext(c)
	}

	job := models.Job{
		TenantKey:      tenantKey,
		ExternalID:     request.ExternalID,
		Name:           request.Name,
		Status:         models.JobStatusPending,
		CurrentStage:   models.StageMedia,
		SourceLanguage: request.SourceLanguage,
		TargetLanguage: request.TargetLanguage,
		InputRelPath:   request.InputRelPath,
		Config:         datatypes.JSONMap(request.Config),
		WebhookURL:     request.WebhookURL,
		WebhookSecret:  request.WebhookSecret,
		MaxRetries:     request.MaxRetries,
		DeadlineAt:     request.DeadlineAt,
	}
	if err := s.store.CreateJob(c.Request.Context(), &job); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "create_job_failed", err.Error())
		return
	}

	if request.AutoStart {
		if err := s.pipeline.StartJob(c.Request.Context(), job.ID, "api"); err != nil {
			respondError(c, stdhttp.StatusInternalServerError, "enqueue_job_failed", err.Error())
			return
		}
	}
	c.JSON(stdhttp.StatusCreated, job)
}

func (s *Server) getJob(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	c.JSON(stdhttp.StatusOK, job)
}

func (s *Server) startJob(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	if err := s.pipeline.StartJob(c.Request.Context(), job.ID, "api"); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "start_job_failed", err.Error())
		return
	}
	respondAccepted(c, gin.H{"queued": true, "stage": models.StageMedia})
}

func (s *Server) retryJob(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	var request retryJobRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_retry_request", err.Error())
		return
	}

	var stage models.JobStage
	if request.Stage != "" {
		stage = models.JobStage(request.Stage)
	}
	if err := s.pipeline.RetryJob(c.Request.Context(), job.ID, stage, request.SegmentIDs, "api"); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "retry_job_failed", err.Error())
		return
	}
	respondAccepted(c, gin.H{"queued": true, "stage": stage, "segment_ids": request.SegmentIDs})
}

func (s *Server) cancelJob(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	if err := s.store.RequestJobCancel(c.Request.Context(), job.ID); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "cancel_job_failed", err.Error())
		return
	}
	respondAccepted(c, gin.H{"cancel_requested": true, "job_id": job.ID})
}

func (s *Server) listSegments(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	segments, err := s.store.ListSegments(c.Request.Context(), job.ID, nil)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_segments_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"segments": segments})
}

func (s *Server) listStageRuns(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	runs, err := s.store.ListStageRuns(c.Request.Context(), job.ID)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_stage_runs_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"stage_runs": runs})
}

func (s *Server) listBindings(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	bindings, err := s.store.ListBindings(c.Request.Context(), job.ID)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_bindings_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"bindings": bindings})
}

func (s *Server) listArtifacts(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}

	jobRoot := storage.ResolveDataPath(s.cfg.DataRoot, filepath.ToSlash(filepath.Join("jobs", strconv.Itoa(int(job.ID)))))
	artifacts := []artifactInfo{}
	_ = filepath.WalkDir(jobRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		info, statErr := d.Info()
		if statErr != nil {
			return nil
		}
		relPath, relErr := filepath.Rel(s.cfg.DataRoot, path)
		if relErr != nil {
			return nil
		}
		artifacts = append(artifacts, artifactInfo{
			RelPath:    filepath.ToSlash(relPath),
			SizeBytes:  info.Size(),
			ModifiedAt: info.ModTime(),
		})
		return nil
	})
	c.JSON(stdhttp.StatusOK, gin.H{"artifacts": artifacts})
}

func (s *Server) rerunSegment(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	segmentID, ok := parseUintParam(c, "segmentId")
	if !ok {
		return
	}
	if err := s.pipeline.RetryJob(c.Request.Context(), job.ID, models.StageTTSDuration, []uint{segmentID}, "segment_rerun"); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "rerun_segment_failed", err.Error())
		return
	}
	respondAccepted(c, gin.H{"queued": true, "job_id": job.ID, "segment_id": segmentID})
}

func (s *Server) listVoiceProfiles(c *gin.Context) {
	profiles, err := s.store.ListVoiceProfiles(c.Request.Context())
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_voice_profiles_failed", err.Error())
		return
	}
	tenantKey := tenantKeyFromContext(c)
	filtered := make([]models.VoiceProfile, 0, len(profiles))
	for _, profile := range profiles {
		if profile.TenantKey == "" || profile.TenantKey == tenantKey {
			filtered = append(filtered, profile)
		}
	}
	c.JSON(stdhttp.StatusOK, gin.H{"voice_profiles": filtered})
}

func (s *Server) createVoiceProfile(c *gin.Context) {
	var request voiceProfileRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_voice_profile_request", err.Error())
		return
	}

	samples, err := json.Marshal(request.SampleRelPaths)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_sample_paths", err.Error())
		return
	}

	tenantKey := request.TenantKey
	if tenantKey == "" {
		tenantKey = tenantKeyFromContext(c)
	}

	profile := models.VoiceProfile{
		TenantKey:         tenantKey,
		Name:              request.Name,
		Mode:              request.Mode,
		Provider:          request.Provider,
		Language:          request.Language,
		SampleRelPaths:    datatypes.JSON(samples),
		CheckpointRelPath: request.CheckpointRelPath,
		IndexRelPath:      request.IndexRelPath,
		ConfigRelPath:     request.ConfigRelPath,
		InternalSpeakerID: request.InternalSpeakerID,
		Meta:              datatypes.JSONMap(request.Meta),
	}
	if err := s.store.CreateVoiceProfile(c.Request.Context(), &profile); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "create_voice_profile_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusCreated, profile)
}

func (s *Server) getVoiceProfile(c *gin.Context) {
	profileID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	profile, err := s.store.GetVoiceProfile(c.Request.Context(), profileID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		respondError(c, stdhttp.StatusNotFound, "voice_profile_not_found", "voice profile not found")
		return
	}
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_voice_profile_failed", err.Error())
		return
	}
	if profile.TenantKey != "" && profile.TenantKey != tenantKeyFromContext(c) {
		respondError(c, stdhttp.StatusNotFound, "voice_profile_not_found", "voice profile not found")
		return
	}
	c.JSON(stdhttp.StatusOK, profile)
}

func (s *Server) validateVoiceProfile(c *gin.Context) {
	profileID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	profile, err := s.store.GetVoiceProfile(c.Request.Context(), profileID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		respondError(c, stdhttp.StatusNotFound, "voice_profile_not_found", "voice profile not found")
		return
	}
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_voice_profile_failed", err.Error())
		return
	}

	paths := []string{profile.CheckpointRelPath, profile.IndexRelPath, profile.ConfigRelPath}
	if len(profile.SampleRelPaths) > 0 {
		var samplePaths []string
		if err := json.Unmarshal(profile.SampleRelPaths, &samplePaths); err == nil {
			paths = append(paths, samplePaths...)
		}
	}

	missing := []string{}
	for _, relPath := range paths {
		if strings.TrimSpace(relPath) == "" {
			continue
		}
		if _, err := os.Stat(storage.ResolveDataPath(s.cfg.DataRoot, relPath)); err != nil {
			missing = append(missing, relPath)
		}
	}

	status := "valid"
	errMsg := ""
	if len(missing) > 0 {
		status = "invalid"
		errMsg = "missing paths: " + strings.Join(missing, ", ")
	}
	if err := s.store.UpdateVoiceProfileValidation(c.Request.Context(), profileID, status, errMsg); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "update_voice_profile_validation_failed", err.Error())
		return
	}

	c.JSON(stdhttp.StatusOK, gin.H{
		"voice_profile_id": profileID,
		"status":           status,
		"missing_paths":    missing,
	})
}

func (s *Server) upsertBindings(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	var request upsertBindingsRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_binding_request", err.Error())
		return
	}

	inputs := make([]store.BindingInput, 0, len(request.Bindings))
	for _, binding := range request.Bindings {
		inputs = append(inputs, store.BindingInput{
			SpeakerID:      binding.SpeakerID,
			SpeakerLabel:   binding.SpeakerLabel,
			VoiceProfileID: binding.VoiceProfileID,
		})
	}

	segmentIDs, err := s.store.UpsertBindings(c.Request.Context(), job.ID, inputs)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "upsert_bindings_failed", err.Error())
		return
	}

	if request.RerunAffected && len(segmentIDs) > 0 {
		if err := s.pipeline.RetryJob(context.Background(), job.ID, models.StageTTSDuration, segmentIDs, "binding_update"); err != nil {
			respondError(c, stdhttp.StatusInternalServerError, "rerun_affected_segments_failed", err.Error())
			return
		}
	}

	c.JSON(stdhttp.StatusOK, gin.H{"updated": true, "affected_segment_ids": segmentIDs})
}

func (s *Server) getJobForTenant(c *gin.Context) (*models.Job, bool) {
	jobID, ok := parseUintParam(c, "id")
	if !ok {
		return nil, false
	}
	job, err := s.store.GetJob(c.Request.Context(), jobID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		respondError(c, stdhttp.StatusNotFound, "job_not_found", "job not found")
		return nil, false
	}
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_job_failed", err.Error())
		return nil, false
	}
	tenantKey := tenantKeyFromContext(c)
	if job.TenantKey != "" && job.TenantKey != tenantKey {
		respondError(c, stdhttp.StatusNotFound, "job_not_found", "job not found")
		return nil, false
	}
	return job, true
}

func (s *Server) serveSegmentAudio(c *gin.Context) {
	_, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	ordinal := c.Param("ordinal")
	ordinalInt, err := strconv.Atoi(ordinal)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_ordinal", "invalid ordinal")
		return
	}
	audioPath := filepath.Join(s.cfg.DataRoot, "jobs", c.Param("id"),
		"tts", fmt.Sprintf("segment-%04d.wav", ordinalInt))
	if _, err := os.Stat(audioPath); err != nil {
		respondError(c, stdhttp.StatusNotFound, "audio_not_found", "audio file not found")
		return
	}
	c.Header("Content-Type", "audio/wav")
	c.Header("Cache-Control", "no-store, no-cache, must-revalidate")
	c.File(audioPath)
}

func (s *Server) serveOriginalAudio(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	ordinal := c.Param("ordinal")
	ordinalInt, err := strconv.Atoi(ordinal)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_ordinal", "invalid ordinal")
		return
	}
	segments, err := s.store.ListSegments(c.Request.Context(), job.ID, nil)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_segments_failed", err.Error())
		return
	}
	var seg *models.Segment
	for i := range segments {
		if segments[i].Ordinal == ordinalInt {
			seg = &segments[i]
			break
		}
	}
	if seg == nil {
		respondError(c, stdhttp.StatusNotFound, "segment_not_found", "segment not found")
		return
	}
	vocalsRelPath := job.VocalsRelPath
	if vocalsRelPath == "" {
		vocalsRelPath = filepath.Join("jobs", strconv.Itoa(int(job.ID)), "separate", "vocals.wav")
	}
	vocalsPath := storage.ResolveDataPath(s.cfg.DataRoot, filepath.ToSlash(vocalsRelPath))
	if _, err := os.Stat(vocalsPath); err != nil {
		respondError(c, stdhttp.StatusNotFound, "vocals_not_found", "vocals file not found")
		return
	}
	tmpPath := filepath.Join(os.TempDir(), fmt.Sprintf("holodub-orig-%d-%d.wav", job.ID, ordinalInt))
	defer os.Remove(tmpPath)
	if err := media.TrimAudioSegment(s.cfg.FFmpegBin, vocalsPath, tmpPath, int64(seg.StartMs), int64(seg.EndMs)); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "trim_failed", err.Error())
		return
	}
	c.Header("Content-Type", "audio/wav")
	c.Header("Cache-Control", "no-store, no-cache, must-revalidate")
	c.File(tmpPath)
}

type patchSegmentRequest struct {
	TargetText     string `json:"target_text"`
	Rerun          bool   `json:"rerun"`
	VoiceProfileID *uint  `json:"voice_profile_id"`
}

func (s *Server) patchSegment(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	segmentID, ok := parseUintParam(c, "segmentId")
	if !ok {
		return
	}

	var request patchSegmentRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_patch_request", err.Error())
		return
	}

	// Handle per-segment voice override update
	if request.VoiceProfileID != nil {
		if err := s.store.UpdateSegmentVoice(c.Request.Context(), segmentID, *request.VoiceProfileID); err != nil {
			respondError(c, stdhttp.StatusInternalServerError, "patch_segment_voice_failed", err.Error())
			return
		}
		// If only changing voice (no text change), and rerun is requested, retrigger TTS
		if request.TargetText == "" && request.Rerun {
			if err := s.pipeline.RetryJob(c.Request.Context(), job.ID, models.StageTTSDuration, []uint{segmentID}, "segment_voice_patch"); err != nil {
				respondError(c, stdhttp.StatusInternalServerError, "rerun_segment_failed", err.Error())
				return
			}
			c.JSON(stdhttp.StatusOK, gin.H{"updated": true, "segment_id": segmentID, "rerun": true})
			return
		}
	}

	if request.TargetText == "" {
		c.JSON(stdhttp.StatusOK, gin.H{"updated": true, "segment_id": segmentID, "rerun": false})
		return
	}

	segment := models.Segment{
		ID:         segmentID,
		TargetText: request.TargetText,
		Status:     "translated",
	}

	if request.Rerun {
		if err := s.store.UpdateSegmentTranslationAndReset(c.Request.Context(), segmentID, request.TargetText); err != nil {
			respondError(c, stdhttp.StatusInternalServerError, "patch_segment_failed", err.Error())
			return
		}
		if err := s.pipeline.RetryJob(c.Request.Context(), job.ID, models.StageTTSDuration, []uint{segmentID}, "segment_patch"); err != nil {
			respondError(c, stdhttp.StatusInternalServerError, "rerun_segment_failed", err.Error())
			return
		}
	} else {
		if err := s.store.UpdateSegmentTranslations(c.Request.Context(), []models.Segment{segment}); err != nil {
			respondError(c, stdhttp.StatusInternalServerError, "patch_segment_failed", err.Error())
			return
		}
	}

	c.JSON(stdhttp.StatusOK, gin.H{"updated": true, "segment_id": segmentID, "rerun": request.Rerun})
}

type patchSegmentQualityRequest struct {
	Quality string `json:"quality" binding:"required,oneof=good bad skip"`
}

func (s *Server) patchSegmentQuality(c *gin.Context) {
	_, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	segmentID, ok := parseUintParam(c, "segmentId")
	if !ok {
		return
	}
	var request patchSegmentQualityRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_quality_request", err.Error())
		return
	}
	if err := s.store.UpdateSegmentMeta(c.Request.Context(), segmentID, map[string]any{"quality": request.Quality}); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "patch_quality_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"updated": true, "segment_id": segmentID, "quality": request.Quality})
}

type previewVoiceRequest struct {
	VoiceProfileID uint `json:"voice_profile_id" binding:"required"`
}

func (s *Server) previewSegmentVoice(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	segmentID, ok := parseUintParam(c, "segmentId")
	if !ok {
		return
	}
	var request previewVoiceRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_preview_request", err.Error())
		return
	}
	profile, err := s.store.GetVoiceProfile(c.Request.Context(), request.VoiceProfileID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		respondError(c, stdhttp.StatusNotFound, "voice_profile_not_found", "voice profile not found")
		return
	}
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_voice_profile_failed", err.Error())
		return
	}
	seg, err := s.store.GetSegment(c.Request.Context(), segmentID)
	if err != nil {
		respondError(c, stdhttp.StatusNotFound, "segment_not_found", "segment not found")
		return
	}

	audioRelPath, actualMs, err := s.pipeline.PreviewVoice(c.Request.Context(), job.ID, *seg, *profile)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "preview_voice_failed", err.Error())
		return
	}

	key := ""
	if v, exists := c.Get("api_key"); exists {
		if s, ok := v.(string); ok {
			key = s
		}
	}
	previewURL := fmt.Sprintf("/jobs/%d/preview-voice/%d?vp=%d", job.ID, seg.ID, request.VoiceProfileID)
	if key != "" {
		previewURL += "&api_key=" + key
	}
	c.JSON(stdhttp.StatusOK, gin.H{
		"audio_relpath":      audioRelPath,
		"actual_duration_ms": actualMs,
		"preview_url":        previewURL,
	})
}

func (s *Server) servePreviewAudio(c *gin.Context) {
	_, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	segmentID, ok := parseUintParam(c, "segmentId")
	if !ok {
		return
	}
	vpID := c.Query("vp")
	if vpID == "" {
		respondError(c, stdhttp.StatusBadRequest, "missing_vp", "vp query param required")
		return
	}
	jobID, _ := parseUintParam(c, "id")
	audioPath := filepath.Join(s.cfg.DataRoot, "preview", fmt.Sprintf("job_%d_seg_%d_vp_%s.wav", jobID, segmentID, vpID))
	if _, err := os.Stat(audioPath); err != nil {
		respondError(c, stdhttp.StatusNotFound, "preview_not_found", "preview audio not found; run preview-voice first")
		return
	}
	c.Header("Content-Type", "audio/wav")
	c.Header("Cache-Control", "no-store, no-cache, must-revalidate")
	c.File(audioPath)
}

func parseUintParam(c *gin.Context, name string) (uint, bool) {
	raw := c.Param(name)
	value, err := strconv.ParseUint(raw, 10, 64)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_id", "invalid id")
		return 0, false
	}
	return uint(value), true
}
