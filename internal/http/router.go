package http

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	stdhttp "net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"holodub/internal/config"
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

	router.GET("/voice-profiles", server.listVoiceProfiles)
	router.POST("/voice-profiles", server.createVoiceProfile)
	router.GET("/voice-profiles/:id", server.getVoiceProfile)
	router.POST("/voice-profiles/:id/validate", server.validateVoiceProfile)
	router.PUT("/jobs/:id/bindings", server.upsertBindings)

	return router
}

func (s *Server) serveUI(c *gin.Context) {
	path := strings.TrimPrefix(c.Param("filepath"), "/")
	if path == "" {
		path = "index.html"
	}
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

func parseUintParam(c *gin.Context, name string) (uint, bool) {
	raw := c.Param(name)
	value, err := strconv.ParseUint(raw, 10, 64)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_id", "invalid id")
		return 0, false
	}
	return uint(value), true
}
