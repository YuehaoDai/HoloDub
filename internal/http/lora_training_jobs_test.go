package http

import (
	"encoding/json"
	stdhttp "net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holodub/internal/ml"
	"holodub/internal/models"

	"github.com/gin-gonic/gin"
	"gorm.io/datatypes"
)

func newLoraTrainingJobRejectTestRouter() *gin.Engine {
	gin.SetMode(gin.TestMode)
	server := &Server{}
	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(tenantContextKey, "default")
		c.Set(requestIDContextKey, "test-req")
		c.Next()
	})
	r.POST("/lora/training-jobs", server.createLoraTrainingJob)
	return r
}

func TestNewLoraTrainingJobPlanBuildsCommandOnlyPlan(t *testing.T) {
	dropout := 0.05
	lr := 0.0002
	alpha := 16.0
	rank := 4
	maxSteps := 120
	saveEvery := 20
	evalInterval := 10
	textLoss := 0.2
	melLoss := 0.8
	durationControl := true
	durationDropout := 0.1
	stopLoss := 2.0
	tailLoss := 1.5
	tailWindow := 64
	durationStopBCE := 0.0
	durationPositionLoss := 0.2
	durationPositionWindow := 32
	gradientCheckpointing := false

	job, err := newLoraTrainingJobPlan("default", loraTrainingJobRequest{
		Name:                       "pilot step 120",
		RunID:                      "pilot-episode146-code1500-r4-step120",
		SourceTier:                 "silver",
		TrainFeatures:              "data/training/lora_features/pilot-episode146-code1500/train.features.jsonl",
		ValFeatures:                "data/training/lora_features/pilot-episode146-code1500/val.features.jsonl",
		CanaryFeatures:             "data/training/lora_runs/pilot-episode146-code1500-r4-step80/canary84.features.jsonl",
		OutputRoot:                 "data/training/lora_runs",
		VoicePromptRelPath:         "voices/test_ref.wav",
		Rank:                       &rank,
		Alpha:                      &alpha,
		Dropout:                    &dropout,
		LR:                         &lr,
		MaxSteps:                   &maxSteps,
		SaveEvery:                  &saveEvery,
		EvalInterval:               &evalInterval,
		TextLossWeight:             &textLoss,
		MelLossWeight:              &melLoss,
		DurationControl:            &durationControl,
		DurationSource:             "target",
		DurationDropout:            &durationDropout,
		MelStopLossWeight:          &stopLoss,
		MelEndLossWeight:           &tailLoss,
		MelEndWindow:               &tailWindow,
		DurationStopBCEWeight:      &durationStopBCE,
		DurationPositionLossWeight: &durationPositionLoss,
		DurationPositionLossWindow: &durationPositionWindow,
		GradientCheckpointing:      &gradientCheckpointing,
		Precision:                  "fp16",
	})
	if err != nil {
		t.Fatalf("new plan: %v", err)
	}
	if job.Status != models.LoraTrainingJobStatusPlanned {
		t.Fatalf("status = %q", job.Status)
	}
	if job.ExecutionMode != "command_only" {
		t.Fatalf("execution mode = %q", job.ExecutionMode)
	}
	if job.OutputDirRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step120" {
		t.Fatalf("output dir = %q", job.OutputDirRelPath)
	}
	if job.AdapterRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step120/adapter_latest.pt" {
		t.Fatalf("adapter relpath = %q", job.AdapterRelPath)
	}

	trainCommand, _ := job.Commands["train_adapter"].(string)
	canaryCommand, _ := job.Commands["canary_loop"].(string)
	regressionCommand, _ := job.Commands["regression_check"].(string)
	if !strings.Contains(trainCommand, "tools/lora_train_indextts2_smoke.py") {
		t.Fatalf("training command missing trainer: %s", trainCommand)
	}
	if !strings.Contains(trainCommand, "--duration-control") {
		t.Fatalf("training command must keep duration control: %s", trainCommand)
	}
	if !strings.Contains(trainCommand, "--duration-source target") ||
		!strings.Contains(trainCommand, "--duration-dropout 0.1") ||
		!strings.Contains(trainCommand, "--duration-position-loss-weight 0.2") ||
		!strings.Contains(trainCommand, "--duration-position-loss-window 32") ||
		!strings.Contains(trainCommand, "--mel-stop-loss-weight 2") ||
		!strings.Contains(trainCommand, "--mel-end-loss-weight 1.5") ||
		!strings.Contains(trainCommand, "--mel-end-window 64") {
		t.Fatalf("training command must preserve v8 duration-position recipe: %s", trainCommand)
	}
	if !strings.Contains(canaryCommand, "--max-fit-delta 0.05") {
		t.Fatalf("canary command must keep hard fit delta: %s", canaryCommand)
	}
	if !strings.Contains(canaryCommand, "--candidate-families flat,split_light_vector,split_mix") {
		t.Fatalf("canary command must use production v1 families: %s", canaryCommand)
	}
	if !strings.Contains(regressionCommand, "--max-max-drift-ms 300") {
		t.Fatalf("regression command must keep max drift gate: %s", regressionCommand)
	}
	if job.GateConfig["high_fit_delta_is_hard_failure"] != true {
		t.Fatalf("fit delta hard gate missing: %+v", job.GateConfig)
	}
}

func TestLoraTrainingSmokeRequestUsesIsolatedOneStepOutput(t *testing.T) {
	maxSteps := 120
	job, err := newLoraTrainingJobPlan("default", loraTrainingJobRequest{
		RunID:          "pilot-episode146-code1500-r4-step120",
		TrainFeatures:  "data/training/lora_features/pilot/train.features.jsonl",
		ValFeatures:    "data/training/lora_features/pilot/val.features.jsonl",
		CanaryFeatures: "data/training/lora_features/pilot/canary.features.jsonl",
		OutputRoot:     "data/training/lora_runs",
		MaxSteps:       &maxSteps,
	})
	if err != nil {
		t.Fatalf("new plan: %v", err)
	}

	request := loraTrainingSmokeRequest(job, "training/lora_runs/pilot-episode146-code1500-r4-step120/_smoke_step1")

	if request.MaxSteps != 1 || request.SaveEvery != 1 || request.EvalInterval != 1 {
		t.Fatalf("smoke must force one-step cadence, got max=%d save=%d eval=%d",
			request.MaxSteps, request.SaveEvery, request.EvalInterval)
	}
	if request.OutputDirRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step120/_smoke_step1" {
		t.Fatalf("smoke output dir = %q", request.OutputDirRelPath)
	}
	if request.OutputDirRelPath == job.OutputDirRelPath {
		t.Fatalf("smoke output must not overwrite full training output dir")
	}
	if request.DurationSource != "target" ||
		request.DurationDropout != 0.1 ||
		request.DurationPositionLossWeight != 0.2 ||
		request.DurationPositionLossWindow != 32 ||
		request.MelStopLossWeight != 2.0 ||
		request.MelEndLossWeight != 1.5 ||
		request.MelEndWindow != 64 {
		t.Fatalf("smoke request must preserve duration-position recipe, got %+v", request)
	}
}

func TestLoraTrainingFullRequestUsesOfficialOutputAndCadence(t *testing.T) {
	maxSteps := 120
	saveEvery := 20
	evalInterval := 10
	job, err := newLoraTrainingJobPlan("default", loraTrainingJobRequest{
		RunID:          "pilot-episode146-code1500-r4-step120",
		TrainFeatures:  "data/training/lora_features/pilot/train.features.jsonl",
		ValFeatures:    "data/training/lora_features/pilot/val.features.jsonl",
		CanaryFeatures: "data/training/lora_features/pilot/canary.features.jsonl",
		OutputRoot:     "data/training/lora_runs",
		MaxSteps:       &maxSteps,
		SaveEvery:      &saveEvery,
		EvalInterval:   &evalInterval,
	})
	if err != nil {
		t.Fatalf("new plan: %v", err)
	}

	request := loraTrainingFullRequest(job)

	if request.MaxSteps != maxSteps || request.SaveEvery != saveEvery || request.EvalInterval != evalInterval {
		t.Fatalf("full request must keep plan cadence, got max=%d save=%d eval=%d",
			request.MaxSteps, request.SaveEvery, request.EvalInterval)
	}
	if request.OutputDirRelPath != job.OutputDirRelPath {
		t.Fatalf("full output dir = %q, want %q", request.OutputDirRelPath, job.OutputDirRelPath)
	}
	if strings.Contains(request.OutputDirRelPath, "_smoke") {
		t.Fatalf("full output must not use smoke directory: %q", request.OutputDirRelPath)
	}
	if request.DurationSource != "target" ||
		request.DurationDropout != 0.1 ||
		request.DurationPositionLossWeight != 0.2 ||
		request.DurationPositionLossWindow != 32 ||
		request.MelStopLossWeight != 2.0 ||
		request.MelEndLossWeight != 1.5 ||
		request.MelEndWindow != 64 {
		t.Fatalf("full request must preserve duration-position recipe, got %+v", request)
	}
}

func TestLoraTrainingCanaryRequestUsesFullAdapterAndFixedPolicy(t *testing.T) {
	job, err := newLoraTrainingJobPlan("default", loraTrainingJobRequest{
		RunID:          "pilot-episode146-code1500-r4-step120",
		TrainFeatures:  "data/training/lora_features/pilot/train.features.jsonl",
		ValFeatures:    "data/training/lora_features/pilot/val.features.jsonl",
		CanaryFeatures: "data/training/lora_features/pilot/canary.features.jsonl",
		OutputRoot:     "data/training/lora_runs",
	})
	if err != nil {
		t.Fatalf("new plan: %v", err)
	}
	job.Artifacts["full_adapter"] = "training/lora_runs/pilot-episode146-code1500-r4-step120/adapter_latest.pt"

	request := loraTrainingCanaryRequest(job, loraTrainingCanaryRunOptions{
		SkipInference: true,
		Resume:        true,
		SampleIDs:     "seg10569,seg10686",
	})

	if request.AdapterRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step120/adapter_latest.pt" {
		t.Fatalf("adapter relpath = %q", request.AdapterRelPath)
	}
	if request.ExistingRunRootRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step120/canary84_full84_regen_v1" {
		t.Fatalf("existing run root = %q", request.ExistingRunRootRelPath)
	}
	if request.CandidateFamilies != "flat,split_light_vector,split_mix" {
		t.Fatalf("candidate families = %q", request.CandidateFamilies)
	}
	if request.SpokenTextPolicy != "technical-v3" {
		t.Fatalf("spoken text policy = %q", request.SpokenTextPolicy)
	}
	if request.MaxFitDelta != 0.05 || request.MaxDriftMs != 500 {
		t.Fatalf("gate drift/fit mismatch: %+v", request)
	}
	if !request.SkipInference || !request.Resume || request.SampleIDs != "seg10569,seg10686" {
		t.Fatalf("canary reuse options not forwarded: %+v", request)
	}
}

func TestLoraTrainingRegressionRequestPinsGoldenGatesAndAnchor(t *testing.T) {
	job, err := newLoraTrainingJobPlan("default", loraTrainingJobRequest{
		RunID:          "pilot-episode146-code1500-r4-step120",
		TrainFeatures:  "data/training/lora_features/pilot/train.features.jsonl",
		ValFeatures:    "data/training/lora_features/pilot/val.features.jsonl",
		CanaryFeatures: "data/training/lora_features/pilot/canary.features.jsonl",
		OutputRoot:     "data/training/lora_runs",
	})
	if err != nil {
		t.Fatalf("new plan: %v", err)
	}

	request := loraTrainingRegressionRequest(job)

	if request.MinCount != 84 {
		t.Fatalf("min count = %d", request.MinCount)
	}
	if request.MaxMeanDriftMs != 50 || request.MaxMaxDriftMs != 300 {
		t.Fatalf("drift gates changed: %+v", request)
	}
	if !containsString(request.ExpectChain, "seg10569=shard01_selected") {
		t.Fatalf("seg10569 selected anchor missing: %+v", request.ExpectChain)
	}
	if !containsString(request.ForbidChain, "seg10569=split_light_vector_10") {
		t.Fatalf("seg10569 tempo rescue forbid anchor missing: %+v", request.ForbidChain)
	}
}

func TestLoraTrainingResultMetricsArePersistedAsArtifacts(t *testing.T) {
	artifacts := datatypes.JSONMap{}

	mergeLoraTrainingCanaryResultArtifacts(artifacts, &ml.LoraCanaryRunResponse{
		Status:        "completed",
		RunDirRelPath: "training/lora_runs/run-a/canary84_full84_regen_v1",
		Metrics: map[string]any{
			"count":                   float64(84),
			"mean_abs_drift_ms":       39.16,
			"join_quality_fail_count": float64(0),
		},
	})

	if artifacts["canary_metric_count"] != float64(84) {
		t.Fatalf("canary_metric_count = %#v", artifacts["canary_metric_count"])
	}
	if artifacts["canary_metric_mean_abs_drift_ms"] != 39.16 {
		t.Fatalf("canary_metric_mean_abs_drift_ms = %#v", artifacts["canary_metric_mean_abs_drift_ms"])
	}
	if artifacts["canary_metric_join_quality_fail_count"] != float64(0) {
		t.Fatalf("canary_metric_join_quality_fail_count = %#v", artifacts["canary_metric_join_quality_fail_count"])
	}
}

func TestCreateLoraTrainingJobRejectsUnsafeRunIDAndInvalidGates(t *testing.T) {
	cases := []struct {
		name string
		body map[string]any
		want string
	}{
		{
			name: "unsafe run id",
			body: map[string]any{
				"run_id":          "../bad",
				"train_features":  "training/features/train.jsonl",
				"val_features":    "training/features/val.jsonl",
				"canary_features": "training/features/canary.jsonl",
			},
			want: "run_id must be a single directory name",
		},
		{
			name: "bad duration source",
			body: map[string]any{
				"run_id":           "safe-run",
				"train_features":   "training/features/train.jsonl",
				"val_features":     "training/features/val.jsonl",
				"canary_features":  "training/features/canary.jsonl",
				"duration_source":  "stretch",
				"max_steps":        10,
				"rank":             4,
				"alpha":            16,
				"learning_rate":    0.0002,
				"save_every":       5,
				"eval_interval":    5,
				"mel_loss_weight":  0.8,
				"text_loss_weight": 0.2,
			},
			want: "duration_source must be audio or target",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := newLoraTrainingJobRejectTestRouter()
			payload, _ := json.Marshal(tc.body)
			req := httptest.NewRequest("POST", "/lora/training-jobs", strings.NewReader(string(payload)))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()
			r.ServeHTTP(rec, req)
			if rec.Code != stdhttp.StatusBadRequest {
				t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), tc.want) {
				t.Fatalf("response %q does not contain %q", rec.Body.String(), tc.want)
			}
		})
	}
}
