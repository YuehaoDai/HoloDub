package http

import (
	"context"
	"errors"
	"fmt"
	stdhttp "net/http"
	"path"
	"path/filepath"
	"strings"
	"time"

	"holodub/internal/ml"
	"holodub/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/datatypes"
	"gorm.io/gorm"
)

type loraTrainingJobRequest struct {
	Name                       string   `json:"name"`
	RunID                      string   `json:"run_id"`
	SourceTier                 string   `json:"source_tier"`
	TrainFeaturesRelPath       string   `json:"train_features_relpath"`
	TrainFeatures              string   `json:"train_features"`
	ValFeaturesRelPath         string   `json:"val_features_relpath"`
	ValFeatures                string   `json:"val_features"`
	CanaryFeaturesRelPath      string   `json:"canary_features_relpath"`
	CanaryFeatures             string   `json:"canary_features"`
	OutputRootRelPath          string   `json:"output_root_relpath"`
	OutputRoot                 string   `json:"output_root"`
	VoicePromptRelPath         string   `json:"voice_prompt_relpath"`
	Rank                       *int     `json:"rank"`
	Alpha                      *float64 `json:"alpha"`
	Dropout                    *float64 `json:"dropout"`
	LearningRate               *float64 `json:"learning_rate"`
	LR                         *float64 `json:"lr"`
	MaxSteps                   *int     `json:"max_steps"`
	SaveEvery                  *int     `json:"save_every"`
	EvalInterval               *int     `json:"eval_interval"`
	TextLossWeight             *float64 `json:"text_loss_weight"`
	MelLossWeight              *float64 `json:"mel_loss_weight"`
	DurationControl            *bool    `json:"duration_control"`
	DurationSource             string   `json:"duration_source"`
	DurationDropout            *float64 `json:"duration_dropout"`
	MelStopLossWeight          *float64 `json:"mel_stop_loss_weight"`
	MelEndLossWeight           *float64 `json:"mel_end_loss_weight"`
	MelEndWindow               *int     `json:"mel_end_window"`
	DurationStopBCEWeight      *float64 `json:"duration_stop_bce_weight"`
	DurationPositionLossWeight *float64 `json:"duration_position_loss_weight"`
	DurationPositionLossWindow *int     `json:"duration_position_loss_window"`
	GradientCheckpointing      *bool    `json:"gradient_checkpointing"`
	Precision                  string   `json:"precision"`
}

type loraTrainingCanaryStartRequest struct {
	SkipInference bool   `json:"skip_inference"`
	Resume        bool   `json:"resume"`
	SampleIDs     string `json:"sample_ids"`
}

type loraTrainingCanaryRunOptions struct {
	SkipInference bool
	Resume        bool
	SampleIDs     string
}

func (s *Server) listLoraTrainingJobs(c *gin.Context) {
	jobs, err := s.store.ListLoraTrainingJobs(c.Request.Context())
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_lora_training_jobs_failed", err.Error())
		return
	}
	tenantKey := tenantKeyFromContext(c)
	filtered := make([]models.LoraTrainingJob, 0, len(jobs))
	for _, job := range jobs {
		if job.TenantKey == "" || job.TenantKey == tenantKey {
			filtered = append(filtered, job)
		}
	}
	c.JSON(stdhttp.StatusOK, gin.H{"training_jobs": filtered})
}

func (s *Server) createLoraTrainingJob(c *gin.Context) {
	var request loraTrainingJobRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_lora_training_job_request", err.Error())
		return
	}
	job, err := newLoraTrainingJobPlan(tenantKeyFromContext(c), request)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_lora_training_job_request", err.Error())
		return
	}
	if err := s.store.CreateLoraTrainingJob(c.Request.Context(), &job); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "create_lora_training_job_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusCreated, job)
}

func (s *Server) getLoraTrainingJob(c *gin.Context) {
	jobID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	job, err := s.store.GetLoraTrainingJob(c.Request.Context(), jobID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		respondError(c, stdhttp.StatusNotFound, "lora_training_job_not_found", "LoRA training job not found")
		return
	}
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_lora_training_job_failed", err.Error())
		return
	}
	tenantKey := tenantKeyFromContext(c)
	if job.TenantKey != "" && job.TenantKey != tenantKey {
		respondError(c, stdhttp.StatusNotFound, "lora_training_job_not_found", "LoRA training job not found")
		return
	}
	c.JSON(stdhttp.StatusOK, job)
}

func (s *Server) runLoraTrainingJobSmoke(c *gin.Context) {
	jobID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	job, err := s.authorizedLoraTrainingJob(c, jobID)
	if err != nil {
		return
	}

	smokeOutputDir := path.Join(job.OutputDirRelPath, "_smoke_step1")
	artifacts := copyJSONMap(job.Artifacts)
	artifacts["smoke_status"] = "running"
	artifacts["smoke_output_dir"] = smokeOutputDir
	artifacts["smoke_started_at"] = time.Now().UTC().Format(time.RFC3339)
	delete(artifacts, "smoke_error")
	_ = s.store.UpdateLoraTrainingJobRuntime(
		c.Request.Context(),
		job.ID,
		"command_only+ml_smoke",
		artifacts,
		"",
	)

	client := ml.New(s.cfg.MLServiceURL)
	result, err := client.RunLoraTrainingSmoke(c.Request.Context(), loraTrainingSmokeRequest(*job, smokeOutputDir))
	artifacts = copyJSONMap(artifacts)
	artifacts["smoke_finished_at"] = time.Now().UTC().Format(time.RFC3339)
	if err != nil {
		artifacts["smoke_status"] = "failed"
		artifacts["smoke_error"] = err.Error()
		_ = s.store.UpdateLoraTrainingJobRuntime(
			c.Request.Context(),
			job.ID,
			"command_only+ml_smoke",
			artifacts,
			err.Error(),
		)
		respondError(c, stdhttp.StatusBadGateway, "lora_training_smoke_failed", err.Error())
		return
	}
	mergeLoraTrainingSmokeResultArtifacts(artifacts, result)
	if result.Status != "completed" {
		_ = s.store.UpdateLoraTrainingJobRuntime(
			c.Request.Context(),
			job.ID,
			"command_only+ml_smoke",
			artifacts,
			fmt.Sprintf("smoke status: %s", result.Status),
		)
	} else {
		_ = s.store.UpdateLoraTrainingJobRuntime(
			c.Request.Context(),
			job.ID,
			"command_only+ml_smoke_verified",
			artifacts,
			"",
		)
	}
	updated, getErr := s.store.GetLoraTrainingJob(c.Request.Context(), job.ID)
	if getErr != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_lora_training_job_failed", getErr.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"training_job": updated, "smoke_result": result})
}

func (s *Server) startLoraTrainingJobFull(c *gin.Context) {
	if !s.cfg.LoraTrainingFullRunnerEnabled {
		respondError(
			c,
			stdhttp.StatusForbidden,
			"lora_training_full_runner_disabled",
			"Full LoRA training runner is disabled. Set LORA_TRAINING_FULL_RUNNER_ENABLED=true to enable it.",
		)
		return
	}
	jobID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	job, err := s.authorizedLoraTrainingJob(c, jobID)
	if err != nil {
		return
	}
	if job.Status == models.LoraTrainingJobStatusQueued || job.Status == models.LoraTrainingJobStatusRunning {
		respondError(c, stdhttp.StatusConflict, "lora_training_job_already_running", "LoRA training job is already queued or running")
		return
	}

	now := time.Now().UTC().Format(time.RFC3339)
	artifacts := copyJSONMap(job.Artifacts)
	artifacts["full_status"] = "queued"
	artifacts["full_output_dir"] = job.OutputDirRelPath
	artifacts["full_requested_at"] = now
	artifacts["full_max_steps"] = job.MaxSteps
	delete(artifacts, "full_error")
	if err := s.store.UpdateLoraTrainingJobState(
		c.Request.Context(),
		job.ID,
		models.LoraTrainingJobStatusQueued,
		"ml_full_queued",
		artifacts,
		"",
	); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "queue_lora_training_failed", err.Error())
		return
	}

	queued, getErr := s.store.GetLoraTrainingJob(c.Request.Context(), job.ID)
	if getErr != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_lora_training_job_failed", getErr.Error())
		return
	}
	go s.runLoraTrainingJobFullAsync(*queued)
	c.JSON(stdhttp.StatusAccepted, gin.H{"training_job": queued, "runner_status": "queued"})
}

func (s *Server) startLoraTrainingJobCanary(c *gin.Context) {
	if !s.cfg.LoraTrainingValidationRunnerEnabled {
		respondError(
			c,
			stdhttp.StatusForbidden,
			"lora_training_validation_runner_disabled",
			"LoRA validation runner is disabled. Set LORA_TRAINING_VALIDATION_RUNNER_ENABLED=true to enable it.",
		)
		return
	}
	jobID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	job, err := s.authorizedLoraTrainingJob(c, jobID)
	if err != nil {
		return
	}
	if job.Status == models.LoraTrainingJobStatusQueued || job.Status == models.LoraTrainingJobStatusRunning {
		respondError(c, stdhttp.StatusConflict, "lora_training_job_already_running", "LoRA training job is already queued or running")
		return
	}

	var request loraTrainingCanaryStartRequest
	if c.Request.ContentLength > 0 {
		if err := c.ShouldBindJSON(&request); err != nil {
			respondError(c, stdhttp.StatusBadRequest, "invalid_lora_canary_start_request", err.Error())
			return
		}
	}
	options := loraTrainingCanaryRunOptions{
		SkipInference: request.SkipInference,
		Resume:        request.Resume,
		SampleIDs:     strings.TrimSpace(request.SampleIDs),
	}

	artifacts := copyJSONMap(job.Artifacts)
	canaryRunDir := stringArtifact(artifacts, "canary_run_dir", path.Join(job.OutputDirRelPath, "canary84_full84_regen_v1"))
	artifacts["canary_status"] = "queued"
	artifacts["canary_run_dir"] = canaryRunDir
	artifacts["canary_requested_at"] = time.Now().UTC().Format(time.RFC3339)
	artifacts["canary_skip_inference"] = options.SkipInference
	artifacts["canary_resume"] = options.Resume
	if options.SampleIDs != "" {
		artifacts["canary_sample_ids"] = options.SampleIDs
	} else {
		delete(artifacts, "canary_sample_ids")
	}
	delete(artifacts, "canary_error")
	if err := s.store.UpdateLoraTrainingJobState(
		c.Request.Context(),
		job.ID,
		models.LoraTrainingJobStatusQueued,
		"ml_canary_queued",
		artifacts,
		"",
	); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "queue_lora_canary_failed", err.Error())
		return
	}
	queued, getErr := s.store.GetLoraTrainingJob(c.Request.Context(), job.ID)
	if getErr != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_lora_training_job_failed", getErr.Error())
		return
	}
	go s.runLoraTrainingJobCanaryAsync(*queued, options)
	c.JSON(stdhttp.StatusAccepted, gin.H{"training_job": queued, "runner_status": "queued"})
}

func (s *Server) startLoraTrainingJobRegression(c *gin.Context) {
	if !s.cfg.LoraTrainingValidationRunnerEnabled {
		respondError(
			c,
			stdhttp.StatusForbidden,
			"lora_training_validation_runner_disabled",
			"LoRA validation runner is disabled. Set LORA_TRAINING_VALIDATION_RUNNER_ENABLED=true to enable it.",
		)
		return
	}
	jobID, ok := parseUintParam(c, "id")
	if !ok {
		return
	}
	job, err := s.authorizedLoraTrainingJob(c, jobID)
	if err != nil {
		return
	}
	if job.Status == models.LoraTrainingJobStatusQueued || job.Status == models.LoraTrainingJobStatusRunning {
		respondError(c, stdhttp.StatusConflict, "lora_training_job_already_running", "LoRA training job is already queued or running")
		return
	}

	artifacts := copyJSONMap(job.Artifacts)
	reportRelPath := path.Join(stringArtifact(artifacts, "canary_run_dir", path.Join(job.OutputDirRelPath, "canary84_full84_regen_v1")), "regression_report.json")
	artifacts["regression_status"] = "queued"
	artifacts["regression_report"] = reportRelPath
	artifacts["regression_requested_at"] = time.Now().UTC().Format(time.RFC3339)
	delete(artifacts, "regression_error")
	if err := s.store.UpdateLoraTrainingJobState(
		c.Request.Context(),
		job.ID,
		models.LoraTrainingJobStatusQueued,
		"ml_regression_queued",
		artifacts,
		"",
	); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "queue_lora_regression_failed", err.Error())
		return
	}
	queued, getErr := s.store.GetLoraTrainingJob(c.Request.Context(), job.ID)
	if getErr != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_lora_training_job_failed", getErr.Error())
		return
	}
	go s.runLoraTrainingJobRegressionAsync(*queued)
	c.JSON(stdhttp.StatusAccepted, gin.H{"training_job": queued, "runner_status": "queued"})
}

func (s *Server) runLoraTrainingJobFullAsync(job models.LoraTrainingJob) {
	ctx, cancel := context.WithTimeout(context.Background(), loraTrainingFullTimeout(job))
	defer cancel()

	artifacts := copyJSONMap(job.Artifacts)
	artifacts["full_status"] = "running"
	artifacts["full_started_at"] = time.Now().UTC().Format(time.RFC3339)
	delete(artifacts, "full_error")
	_ = s.store.UpdateLoraTrainingJobState(
		ctx,
		job.ID,
		models.LoraTrainingJobStatusRunning,
		"ml_full_running",
		artifacts,
		"",
	)

	client := ml.NewWithTimeout(s.cfg.MLServiceURL, loraTrainingFullTimeout(job))
	result, err := client.RunLoraTrainingFull(ctx, loraTrainingFullRequest(job))
	artifacts = copyJSONMap(artifacts)
	artifacts["full_finished_at"] = time.Now().UTC().Format(time.RFC3339)
	if err != nil {
		artifacts["full_status"] = "failed"
		artifacts["full_error"] = err.Error()
		_ = s.store.UpdateLoraTrainingJobState(
			context.Background(),
			job.ID,
			models.LoraTrainingJobStatusFailed,
			"ml_full_failed",
			artifacts,
			err.Error(),
		)
		return
	}

	mergeLoraTrainingFullResultArtifacts(artifacts, result)
	if result.Status != "completed" {
		message := fmt.Sprintf("full training status: %s", result.Status)
		_ = s.store.UpdateLoraTrainingJobState(
			context.Background(),
			job.ID,
			models.LoraTrainingJobStatusFailed,
			"ml_full_failed",
			artifacts,
			message,
		)
		return
	}
	_ = s.store.UpdateLoraTrainingJobState(
		context.Background(),
		job.ID,
		models.LoraTrainingJobStatusCompleted,
		"ml_full_completed",
		artifacts,
		"",
	)
}

func (s *Server) runLoraTrainingJobCanaryAsync(job models.LoraTrainingJob, options loraTrainingCanaryRunOptions) {
	ctx, cancel := context.WithTimeout(context.Background(), loraTrainingCanaryTimeout(job))
	defer cancel()

	artifacts := copyJSONMap(job.Artifacts)
	artifacts["canary_status"] = "running"
	artifacts["canary_started_at"] = time.Now().UTC().Format(time.RFC3339)
	artifacts["canary_skip_inference"] = options.SkipInference
	artifacts["canary_resume"] = options.Resume
	if options.SampleIDs != "" {
		artifacts["canary_sample_ids"] = options.SampleIDs
	}
	delete(artifacts, "canary_error")
	_ = s.store.UpdateLoraTrainingJobState(
		ctx,
		job.ID,
		models.LoraTrainingJobStatusRunning,
		"ml_canary_running",
		artifacts,
		"",
	)

	client := ml.NewWithTimeout(s.cfg.MLServiceURL, loraTrainingCanaryTimeout(job))
	result, err := client.RunLoraTrainingCanary(ctx, loraTrainingCanaryRequest(job, options))
	artifacts = copyJSONMap(artifacts)
	artifacts["canary_finished_at"] = time.Now().UTC().Format(time.RFC3339)
	if err != nil {
		artifacts["canary_status"] = "failed"
		artifacts["canary_error"] = err.Error()
		_ = s.store.UpdateLoraTrainingJobState(
			context.Background(),
			job.ID,
			models.LoraTrainingJobStatusFailed,
			"ml_canary_failed",
			artifacts,
			err.Error(),
		)
		return
	}
	mergeLoraTrainingCanaryResultArtifacts(artifacts, result)
	if result.Status != "completed" {
		message := fmt.Sprintf("canary status: %s", result.Status)
		_ = s.store.UpdateLoraTrainingJobState(
			context.Background(),
			job.ID,
			models.LoraTrainingJobStatusFailed,
			"ml_canary_failed",
			artifacts,
			message,
		)
		return
	}
	_ = s.store.UpdateLoraTrainingJobState(
		context.Background(),
		job.ID,
		models.LoraTrainingJobStatusCompleted,
		"ml_canary_completed",
		artifacts,
		"",
	)
}

func (s *Server) runLoraTrainingJobRegressionAsync(job models.LoraTrainingJob) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
	defer cancel()

	artifacts := copyJSONMap(job.Artifacts)
	artifacts["regression_status"] = "running"
	artifacts["regression_started_at"] = time.Now().UTC().Format(time.RFC3339)
	delete(artifacts, "regression_error")
	_ = s.store.UpdateLoraTrainingJobState(
		ctx,
		job.ID,
		models.LoraTrainingJobStatusRunning,
		"ml_regression_running",
		artifacts,
		"",
	)

	client := ml.NewWithTimeout(s.cfg.MLServiceURL, 30*time.Minute)
	result, err := client.RunLoraTrainingRegression(ctx, loraTrainingRegressionRequest(job))
	artifacts = copyJSONMap(artifacts)
	artifacts["regression_finished_at"] = time.Now().UTC().Format(time.RFC3339)
	if err != nil {
		artifacts["regression_status"] = "failed"
		artifacts["regression_error"] = err.Error()
		_ = s.store.UpdateLoraTrainingJobState(
			context.Background(),
			job.ID,
			models.LoraTrainingJobStatusFailed,
			"ml_regression_failed",
			artifacts,
			err.Error(),
		)
		return
	}
	mergeLoraTrainingRegressionResultArtifacts(artifacts, result)
	if result.Status != "completed" {
		message := fmt.Sprintf("regression status: %s", result.Status)
		_ = s.store.UpdateLoraTrainingJobState(
			context.Background(),
			job.ID,
			models.LoraTrainingJobStatusFailed,
			"ml_regression_failed",
			artifacts,
			message,
		)
		return
	}
	_ = s.store.UpdateLoraTrainingJobState(
		context.Background(),
		job.ID,
		models.LoraTrainingJobStatusCompleted,
		"ml_regression_completed",
		artifacts,
		"",
	)
}

func (s *Server) authorizedLoraTrainingJob(c *gin.Context, jobID uint) (*models.LoraTrainingJob, error) {
	job, err := s.store.GetLoraTrainingJob(c.Request.Context(), jobID)
	if errors.Is(err, gorm.ErrRecordNotFound) {
		respondError(c, stdhttp.StatusNotFound, "lora_training_job_not_found", "LoRA training job not found")
		return nil, err
	}
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "get_lora_training_job_failed", err.Error())
		return nil, err
	}
	tenantKey := tenantKeyFromContext(c)
	if job.TenantKey != "" && job.TenantKey != tenantKey {
		respondError(c, stdhttp.StatusNotFound, "lora_training_job_not_found", "LoRA training job not found")
		return nil, gorm.ErrRecordNotFound
	}
	return job, nil
}

func newLoraTrainingJobPlan(tenantKey string, request loraTrainingJobRequest) (models.LoraTrainingJob, error) {
	runID, err := cleanLoraRunID(request.RunID)
	if err != nil {
		return models.LoraTrainingJob{}, err
	}
	trainFeatures, err := cleanDataRelPath(firstNonEmptyString(request.TrainFeaturesRelPath, request.TrainFeatures), "train_features")
	if err != nil {
		return models.LoraTrainingJob{}, err
	}
	valFeatures, err := cleanDataRelPath(firstNonEmptyString(request.ValFeaturesRelPath, request.ValFeatures), "val_features")
	if err != nil {
		return models.LoraTrainingJob{}, err
	}
	canaryFeatures, err := cleanDataRelPath(firstNonEmptyString(request.CanaryFeaturesRelPath, request.CanaryFeatures), "canary_features")
	if err != nil {
		return models.LoraTrainingJob{}, err
	}
	outputRoot, err := cleanDataRelPath(firstNonEmptyString(request.OutputRootRelPath, request.OutputRoot, "training/lora_runs"), "output_root")
	if err != nil {
		return models.LoraTrainingJob{}, err
	}
	voicePrompt, err := cleanDataRelPath(firstNonEmptyString(request.VoicePromptRelPath, "voices/test_ref.wav"), "voice_prompt")
	if err != nil {
		return models.LoraTrainingJob{}, err
	}

	outputDir := path.Join(outputRoot, runID)
	adapterRelPath := path.Join(outputDir, "adapter_latest.pt")
	canaryRunDir := path.Join(outputDir, "canary84_full84_regen_v1")
	gates := loraTrainingGateConfig()

	job := models.LoraTrainingJob{
		TenantKey:                  firstNonEmptyString(tenantKey, "default"),
		Name:                       firstNonEmptyString(strings.TrimSpace(request.Name), runID),
		Status:                     models.LoraTrainingJobStatusPlanned,
		RunID:                      runID,
		SourceTier:                 firstNonEmptyString(strings.TrimSpace(request.SourceTier), "silver"),
		TrainFeaturesRelPath:       trainFeatures,
		ValFeaturesRelPath:         valFeatures,
		CanaryFeaturesRelPath:      canaryFeatures,
		OutputRootRelPath:          outputRoot,
		OutputDirRelPath:           outputDir,
		AdapterRelPath:             adapterRelPath,
		VoicePromptRelPath:         voicePrompt,
		Rank:                       intDefault(request.Rank, 4),
		Alpha:                      floatDefault(request.Alpha, 16),
		Dropout:                    floatDefault(request.Dropout, 0.05),
		LearningRate:               floatDefault(firstFloat(request.LearningRate, request.LR), 0.0002),
		MaxSteps:                   intDefault(request.MaxSteps, 120),
		SaveEvery:                  intDefault(request.SaveEvery, 20),
		EvalInterval:               intDefault(request.EvalInterval, 10),
		TextLossWeight:             floatDefault(request.TextLossWeight, 0.2),
		MelLossWeight:              floatDefault(request.MelLossWeight, 0.8),
		DurationControl:            boolDefault(request.DurationControl, true),
		DurationSource:             firstNonEmptyString(strings.TrimSpace(request.DurationSource), "target"),
		DurationDropout:            floatDefault(request.DurationDropout, 0.1),
		MelStopLossWeight:          floatDefault(request.MelStopLossWeight, 2.0),
		MelEndLossWeight:           floatDefault(request.MelEndLossWeight, 1.5),
		MelEndWindow:               intDefault(request.MelEndWindow, 64),
		DurationStopBCEWeight:      floatDefault(request.DurationStopBCEWeight, 0.0),
		DurationPositionLossWeight: floatDefault(request.DurationPositionLossWeight, 0.2),
		DurationPositionLossWindow: intDefault(request.DurationPositionLossWindow, 32),
		GradientCheckpointing:      boolDefault(request.GradientCheckpointing, false),
		Precision:                  firstNonEmptyString(strings.TrimSpace(request.Precision), "fp16"),
		ExecutionMode:              "command_only",
		GateConfig:                 datatypes.JSONMap(gates),
		Artifacts: datatypes.JSONMap{
			"output_dir":             outputDir,
			"adapter":                adapterRelPath,
			"canary_run_dir":         canaryRunDir,
			"training_summary":       path.Join(outputDir, "summary.json"),
			"canary_loop_summary":    path.Join(canaryRunDir, "loop_summary.json"),
			"candidate_gate_report":  path.Join(canaryRunDir, "candidate_gate_report.json"),
			"listening_pack":         path.Join(canaryRunDir, "listening_pack", "index.html"),
			"quality_baseline":       path.Join(canaryRunDir, "quality_baseline.json"),
			"human_review_report":    path.Join(canaryRunDir, "human_review_report.json"),
			"risk_queue":             path.Join(canaryRunDir, "risk_queue.json"),
			"adapter_compare_report": path.Join(outputDir, "adapter_compare_report.json"),
		},
	}
	if err := validateLoraTrainingJobPlan(job); err != nil {
		return models.LoraTrainingJob{}, err
	}
	job.Commands = datatypes.JSONMap(loraTrainingCommands(job, canaryRunDir))
	return job, nil
}

func loraTrainingSmokeRequest(job models.LoraTrainingJob, smokeOutputDir string) ml.LoraTrainingRunRequest {
	return ml.LoraTrainingRunRequest{
		TrainFeaturesRelPath:       job.TrainFeaturesRelPath,
		ValFeaturesRelPath:         job.ValFeaturesRelPath,
		OutputDirRelPath:           smokeOutputDir,
		Rank:                       job.Rank,
		Alpha:                      job.Alpha,
		Dropout:                    job.Dropout,
		LearningRate:               job.LearningRate,
		MaxSteps:                   1,
		SaveEvery:                  1,
		EvalInterval:               1,
		TextLossWeight:             job.TextLossWeight,
		MelLossWeight:              job.MelLossWeight,
		DurationControl:            job.DurationControl,
		DurationSource:             job.DurationSource,
		DurationDropout:            job.DurationDropout,
		MelStopLossWeight:          job.MelStopLossWeight,
		MelEndLossWeight:           job.MelEndLossWeight,
		MelEndWindow:               job.MelEndWindow,
		DurationStopBCEWeight:      job.DurationStopBCEWeight,
		DurationPositionLossWeight: job.DurationPositionLossWeight,
		DurationPositionLossWindow: job.DurationPositionLossWindow,
		GradientCheckpointing:      job.GradientCheckpointing,
		Precision:                  job.Precision,
	}
}

func loraTrainingFullRequest(job models.LoraTrainingJob) ml.LoraTrainingRunRequest {
	return ml.LoraTrainingRunRequest{
		TrainFeaturesRelPath:       job.TrainFeaturesRelPath,
		ValFeaturesRelPath:         job.ValFeaturesRelPath,
		OutputDirRelPath:           job.OutputDirRelPath,
		Rank:                       job.Rank,
		Alpha:                      job.Alpha,
		Dropout:                    job.Dropout,
		LearningRate:               job.LearningRate,
		MaxSteps:                   job.MaxSteps,
		SaveEvery:                  job.SaveEvery,
		EvalInterval:               job.EvalInterval,
		TextLossWeight:             job.TextLossWeight,
		MelLossWeight:              job.MelLossWeight,
		DurationControl:            job.DurationControl,
		DurationSource:             job.DurationSource,
		DurationDropout:            job.DurationDropout,
		MelStopLossWeight:          job.MelStopLossWeight,
		MelEndLossWeight:           job.MelEndLossWeight,
		MelEndWindow:               job.MelEndWindow,
		DurationStopBCEWeight:      job.DurationStopBCEWeight,
		DurationPositionLossWeight: job.DurationPositionLossWeight,
		DurationPositionLossWindow: job.DurationPositionLossWindow,
		GradientCheckpointing:      job.GradientCheckpointing,
		Precision:                  job.Precision,
	}
}

func loraTrainingCanaryRequest(job models.LoraTrainingJob, options loraTrainingCanaryRunOptions) ml.LoraCanaryRunRequest {
	canaryRunDir := stringArtifact(job.Artifacts, "canary_run_dir", path.Join(job.OutputDirRelPath, "canary84_full84_regen_v1"))
	adapterRelPath := stringArtifact(job.Artifacts, "full_adapter", job.AdapterRelPath)
	existingRunRoot := ""
	if options.SkipInference {
		existingRunRoot = canaryRunDir
	}
	return ml.LoraCanaryRunRequest{
		FeaturesRelPath:         job.CanaryFeaturesRelPath,
		RunDirRelPath:           canaryRunDir,
		AdapterRelPath:          adapterRelPath,
		ExistingRunRootRelPath:  existingRunRoot,
		VoicePromptRelPath:      job.VoicePromptRelPath,
		CandidateFamilies:       "flat,split_light_vector,split_mix",
		SpokenTextPolicy:        "technical-v3",
		DurationTokenRatePolicy: "piecewise",
		MaxFitDelta:             0.05,
		MaxDriftMs:              500,
		MinSimilarity:           0.97,
		TargetSimilarity:        0.985,
		MinF0Range:              4.5,
		TargetF0Range:           5.4,
		SkipInference:           options.SkipInference,
		Resume:                  options.Resume,
		SampleIDs:               strings.TrimSpace(options.SampleIDs),
	}
}

func loraTrainingRegressionRequest(job models.LoraTrainingJob) ml.LoraRegressionRunRequest {
	canaryRunDir := stringArtifact(job.Artifacts, "canary_run_dir", path.Join(job.OutputDirRelPath, "canary84_full84_regen_v1"))
	return ml.LoraRegressionRunRequest{
		SummaryRelPath:         stringArtifact(job.Artifacts, "canary_loop_summary", path.Join(canaryRunDir, "loop_summary.json")),
		GateReportRelPath:      stringArtifact(job.Artifacts, "candidate_gate_report", path.Join(canaryRunDir, "candidate_gate_report.json")),
		RiskQueueRelPath:       stringArtifact(job.Artifacts, "risk_queue", path.Join(canaryRunDir, "risk_queue.json")),
		ReportRelPath:          path.Join(canaryRunDir, "regression_report.json"),
		MinCount:               84,
		MaxMeanDriftMs:         50,
		MaxMaxDriftMs:          300,
		MinMeanSimilarity:      0.99,
		MinMinSimilarity:       0.96,
		MaxJoinQualityFailures: 0,
		ExpectChain:            []string{"seg10569=shard01_selected"},
		ForbidChain:            []string{"seg10569=split_light_vector_10"},
	}
}

func mergeLoraTrainingSmokeResultArtifacts(artifacts datatypes.JSONMap, result *ml.LoraTrainingRunResponse) {
	artifacts["smoke_status"] = result.Status
	if result.ExitCode != nil {
		artifacts["smoke_exit_code"] = *result.ExitCode
	}
	artifacts["smoke_output_dir"] = result.OutputDirRelPath
	artifacts["smoke_stdout_log"] = result.StdoutLogRelPath
	artifacts["smoke_stderr_log"] = result.StderrLogRelPath
	artifacts["smoke_diagnostics"] = result.Diagnostics
	artifacts["smoke_command"] = result.Command
	if result.AdapterRelPath != "" {
		artifacts["smoke_adapter"] = result.AdapterRelPath
	}
	if result.SummaryRelPath != "" {
		artifacts["smoke_summary"] = result.SummaryRelPath
	}
	if result.MetricsRelPath != "" {
		artifacts["smoke_metrics"] = result.MetricsRelPath
	}
}

func mergeLoraTrainingCanaryResultArtifacts(artifacts datatypes.JSONMap, result *ml.LoraCanaryRunResponse) {
	artifacts["canary_status"] = result.Status
	if result.ExitCode != nil {
		artifacts["canary_exit_code"] = *result.ExitCode
	}
	artifacts["canary_run_dir"] = result.RunDirRelPath
	artifacts["canary_stdout_log"] = result.StdoutLogRelPath
	artifacts["canary_stderr_log"] = result.StderrLogRelPath
	artifacts["canary_command"] = result.Command
	artifacts["canary_diagnostics"] = result.Diagnostics
	if result.LoopSummaryRelPath != "" {
		artifacts["canary_loop_summary"] = result.LoopSummaryRelPath
	}
	if result.CandidateGateReportRelPath != "" {
		artifacts["candidate_gate_report"] = result.CandidateGateReportRelPath
	}
	if result.ListeningPackRelPath != "" {
		artifacts["listening_pack"] = result.ListeningPackRelPath
	}
	if result.QualityBaselineRelPath != "" {
		artifacts["quality_baseline"] = result.QualityBaselineRelPath
	}
	if result.HumanReviewReportRelPath != "" {
		artifacts["human_review_report"] = result.HumanReviewReportRelPath
	}
	if result.RiskQueueRelPath != "" {
		artifacts["risk_queue"] = result.RiskQueueRelPath
	}
	mergeLoraMetricArtifacts("canary", artifacts, result.Metrics)
}

func mergeLoraTrainingFullResultArtifacts(artifacts datatypes.JSONMap, result *ml.LoraTrainingRunResponse) {
	artifacts["full_status"] = result.Status
	if result.ExitCode != nil {
		artifacts["full_exit_code"] = *result.ExitCode
	}
	artifacts["full_output_dir"] = result.OutputDirRelPath
	artifacts["full_stdout_log"] = result.StdoutLogRelPath
	artifacts["full_stderr_log"] = result.StderrLogRelPath
	artifacts["full_diagnostics"] = result.Diagnostics
	artifacts["full_command"] = result.Command
	if result.AdapterRelPath != "" {
		artifacts["full_adapter"] = result.AdapterRelPath
	}
	if result.SummaryRelPath != "" {
		artifacts["full_summary"] = result.SummaryRelPath
	}
	if result.MetricsRelPath != "" {
		artifacts["full_metrics"] = result.MetricsRelPath
	}
}

func mergeLoraTrainingRegressionResultArtifacts(artifacts datatypes.JSONMap, result *ml.LoraRegressionRunResponse) {
	artifacts["regression_status"] = result.Status
	if result.ExitCode != nil {
		artifacts["regression_exit_code"] = *result.ExitCode
	}
	artifacts["regression_report"] = result.ReportRelPath
	artifacts["regression_stdout_log"] = result.StdoutLogRelPath
	artifacts["regression_stderr_log"] = result.StderrLogRelPath
	artifacts["regression_command"] = result.Command
	artifacts["regression_diagnostics"] = result.Diagnostics
	mergeLoraMetricArtifacts("regression", artifacts, result.Metrics)
}

func mergeLoraMetricArtifacts(prefix string, artifacts datatypes.JSONMap, metrics map[string]any) {
	if len(metrics) == 0 {
		return
	}
	artifacts[prefix+"_metrics"] = metrics
	for _, key := range []string{
		"count",
		"mean_abs_drift_ms",
		"max_abs_drift_ms",
		"mean_similarity",
		"min_similarity",
		"join_quality_fail_count",
		"listening_override_count",
		"fallback_samples_count",
		"low_asr_samples_count",
		"low_critical_term_samples_count",
		"max_fit_tempo_delta",
	} {
		if value, ok := metrics[key]; ok {
			artifacts[prefix+"_metric_"+key] = value
		}
	}
}

func loraTrainingFullTimeout(job models.LoraTrainingJob) time.Duration {
	hours := 6
	if job.MaxSteps > 1000 {
		hours = 12
	}
	return time.Duration(hours) * time.Hour
}

func loraTrainingCanaryTimeout(job models.LoraTrainingJob) time.Duration {
	if job.MaxSteps > 1000 {
		return 18 * time.Hour
	}
	return 12 * time.Hour
}

func stringArtifact(artifacts datatypes.JSONMap, key, fallback string) string {
	if value, ok := artifacts[key].(string); ok && strings.TrimSpace(value) != "" {
		return value
	}
	return fallback
}

func copyJSONMap(in datatypes.JSONMap) datatypes.JSONMap {
	out := datatypes.JSONMap{}
	for key, value := range in {
		out[key] = value
	}
	return out
}

func validateLoraTrainingJobPlan(job models.LoraTrainingJob) error {
	if job.TrainFeaturesRelPath == "" {
		return errors.New("train_features is required")
	}
	if job.ValFeaturesRelPath == "" {
		return errors.New("val_features is required")
	}
	if job.CanaryFeaturesRelPath == "" {
		return errors.New("canary_features is required")
	}
	if job.OutputDirRelPath == "" || job.AdapterRelPath == "" {
		return errors.New("output run directory is required")
	}
	if job.Rank <= 0 {
		return errors.New("rank must be positive")
	}
	if job.Alpha <= 0 {
		return errors.New("alpha must be positive")
	}
	if job.MaxSteps <= 0 {
		return errors.New("max_steps must be positive")
	}
	if job.SaveEvery <= 0 || job.EvalInterval <= 0 {
		return errors.New("save_every and eval_interval must be positive")
	}
	if job.LearningRate <= 0 {
		return errors.New("learning_rate must be positive")
	}
	if job.DurationSource != "audio" && job.DurationSource != "target" {
		return errors.New("duration_source must be audio or target")
	}
	if job.Precision != "fp16" && job.Precision != "fp32" {
		return errors.New("precision must be fp16 or fp32")
	}
	return nil
}

func loraTrainingCommands(job models.LoraTrainingJob, canaryRunDir string) map[string]any {
	trainArgs := []any{
		"python",
		"tools/lora_train_indextts2_smoke.py",
		"--train-features", dataCommandPath(job.TrainFeaturesRelPath),
		"--val-features", dataCommandPath(job.ValFeaturesRelPath),
		"--output-dir", dataCommandPath(job.OutputDirRelPath),
		"--rank", job.Rank,
		"--alpha", job.Alpha,
		"--dropout", job.Dropout,
		"--lr", job.LearningRate,
		"--max-steps", job.MaxSteps,
		"--save-every", job.SaveEvery,
		"--eval-interval", job.EvalInterval,
		"--text-loss-weight", job.TextLossWeight,
		"--mel-loss-weight", job.MelLossWeight,
		"--duration-source", job.DurationSource,
		"--duration-dropout", job.DurationDropout,
		"--mel-stop-loss-weight", job.MelStopLossWeight,
		"--mel-end-loss-weight", job.MelEndLossWeight,
		"--mel-end-window", job.MelEndWindow,
		"--duration-stop-bce-weight", job.DurationStopBCEWeight,
		"--duration-position-loss-weight", job.DurationPositionLossWeight,
		"--duration-position-loss-window", job.DurationPositionLossWindow,
	}
	if job.DurationControl {
		trainArgs = append(trainArgs, "--duration-control")
	}
	if job.GradientCheckpointing {
		trainArgs = append(trainArgs, "--gradient-checkpointing")
	}
	if job.Precision == "fp32" {
		trainArgs = append(trainArgs, "--no-fp16")
	}

	canaryArgs := []any{
		"python",
		"tools/lora_canary_candidate_loop.py",
		"--features", dataCommandPath(job.CanaryFeaturesRelPath),
		"--run-dir", dataCommandPath(canaryRunDir),
		"--adapter", dataCommandPath(job.AdapterRelPath),
		"--data-root", "data",
		"--voice-prompt-relpath", job.VoicePromptRelPath,
		"--candidate-families", "flat,split_light_vector,split_mix",
		"--spoken-text-policy", "technical-v3",
		"--duration-token-rate-policy", "piecewise",
		"--max-fit-delta", "0.05",
		"--max-drift-ms", "500",
		"--min-similarity", "0.97",
		"--target-similarity", "0.985",
		"--min-f0-range", "4.5",
		"--target-f0-range", "5.4",
	}

	regressionArgs := []any{
		"python",
		"tools/lora_canary_regression_check.py",
		"--summary", dataCommandPath(path.Join(canaryRunDir, "loop_summary.json")),
		"--gate-report", dataCommandPath(path.Join(canaryRunDir, "candidate_gate_report.json")),
		"--min-count", "84",
		"--max-mean-drift-ms", "50",
		"--max-max-drift-ms", "300",
		"--min-mean-similarity", "0.99",
		"--min-min-similarity", "0.96",
		"--max-join-quality-failures", "0",
	}

	return map[string]any{
		"train_adapter":    shellCommand(trainArgs),
		"canary_loop":      shellCommand(canaryArgs),
		"regression_check": shellCommand(regressionArgs),
	}
}

func loraTrainingGateConfig() map[string]any {
	return map[string]any{
		"canary_min_count":               84,
		"canary_max_mean_abs_drift_ms":   50,
		"canary_max_max_abs_drift_ms":    300,
		"canary_min_mean_asr":            0.99,
		"canary_min_min_asr":             0.96,
		"join_quality_failures":          0,
		"final_mix_asr_gate_required":    true,
		"high_fit_delta_is_hard_failure": true,
		"max_fit_delta":                  0.05,
		"max_drift_ms":                   500,
		"min_similarity":                 0.97,
		"target_similarity":              0.985,
		"min_f0_range":                   4.5,
		"target_f0_range":                5.4,
	}
}

func cleanLoraRunID(raw string) (string, error) {
	runID := strings.TrimSpace(raw)
	if runID == "" {
		return "", errors.New("run_id is required")
	}
	runID = strings.ReplaceAll(runID, "\\", "/")
	if strings.Contains(runID, "/") || strings.Contains(runID, "..") {
		return "", errors.New("run_id must be a single directory name")
	}
	return runID, nil
}

func cleanDataRelPath(raw, field string) (string, error) {
	value := strings.TrimSpace(filepath.ToSlash(raw))
	value = strings.TrimPrefix(value, "/data/")
	value = strings.TrimPrefix(value, "data/")
	value = strings.Trim(value, "/")
	if value == "" {
		return "", nil
	}
	if filepath.IsAbs(filepath.FromSlash(value)) || looksLikeWindowsAbs(value) {
		return "", fmt.Errorf("%s must be relative to DATA_ROOT", field)
	}
	value = path.Clean(value)
	if value == "." {
		return "", nil
	}
	if strings.HasPrefix(value, "../") || value == ".." || strings.Contains(value, "/../") {
		return "", fmt.Errorf("%s cannot contain ..", field)
	}
	return value, nil
}

func looksLikeWindowsAbs(value string) bool {
	return len(value) >= 3 && value[1] == ':' && (value[2] == '/' || value[2] == '\\')
}

func dataCommandPath(rel string) string {
	if rel == "" {
		return ""
	}
	if strings.HasPrefix(rel, "data/") {
		return rel
	}
	return "data/" + rel
}

func shellCommand(args []any) string {
	parts := make([]string, 0, len(args))
	for _, arg := range args {
		parts = append(parts, quoteShellArg(fmt.Sprint(arg)))
	}
	return strings.Join(parts, " ")
}

func quoteShellArg(value string) string {
	if value == "" {
		return `""`
	}
	for _, r := range value {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			continue
		}
		switch r {
		case '_', '.', '/', ':', '=', ',', '+', '-':
			continue
		default:
			return `"` + strings.ReplaceAll(value, `"`, `\"`) + `"`
		}
	}
	return value
}

func intDefault(value *int, fallback int) int {
	if value == nil {
		return fallback
	}
	return *value
}

func floatDefault(value *float64, fallback float64) float64 {
	if value == nil {
		return fallback
	}
	return *value
}

func boolDefault(value *bool, fallback bool) bool {
	if value == nil {
		return fallback
	}
	return *value
}

func firstFloat(values ...*float64) *float64 {
	for _, value := range values {
		if value != nil {
			return value
		}
	}
	return nil
}
