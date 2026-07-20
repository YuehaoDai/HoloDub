package http

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"
)

func TestScanLoraAdaptersDiscoversAdapterAndContractMetadata(t *testing.T) {
	dataRoot := t.TempDir()
	runDir := filepath.Join(dataRoot, "training", "lora_runs", "pilot-episode146-code1500-r4-step80")
	featureDir := filepath.Join(dataRoot, "training", "lora_features", "pilot-episode146-code1500")
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(featureDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(runDir, "adapter_step80.pt"), []byte("adapter"), 0o644); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, filepath.Join(runDir, "summary.json"), map[string]any{
		"metadata": map[string]any{
			"rank":             4,
			"alpha":            16,
			"dropout":          0.05,
			"duration_control": true,
			"duration_source":  "audio",
			"trainable_params": 1966080,
			"total_params":     867946719,
			"train_features":   "/data/training/lora_features/pilot-episode146-code1500/train.features.jsonl",
			"val_features":     "/data/training/lora_features/pilot-episode146-code1500/val.features.jsonl",
		},
		"last_train": map[string]any{"step": 80},
		"final_val":  map[string]any{"text_loss": 1.87},
	})
	if err := os.WriteFile(filepath.Join(featureDir, "train.features.jsonl"), []byte("{}\n{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(featureDir, "val.features.jsonl"), []byte("{}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, filepath.Join(featureDir, "features_manifest.json"), map[string]any{
		"splits": map[string]any{
			"test": map[string]any{"accepted": 1},
		},
	})

	adapters, err := scanLoraAdapters(dataRoot)
	if err != nil {
		t.Fatalf("scanLoraAdapters error: %v", err)
	}
	if len(adapters) != 1 {
		t.Fatalf("adapter count = %d, want 1", len(adapters))
	}
	got := adapters[0]
	if got.AdapterRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step80/adapter_step80.pt" {
		t.Fatalf("adapter relpath = %q", got.AdapterRelPath)
	}
	if got.PromotionStatus != "pilot_only" {
		t.Fatalf("promotion status = %q", got.PromotionStatus)
	}
	if got.Training.Rank != float64(4) || got.Training.Steps != float64(80) {
		t.Fatalf("training summary not parsed: %+v", got.Training)
	}
	if got.Dataset.TrainFeatureRows != 2 || got.Dataset.ValFeatureRows != 1 || got.Dataset.TestFeatureRows != 1 {
		t.Fatalf("dataset rows not parsed: %+v", got.Dataset)
	}
	if got.CheckpointStep != 80 {
		t.Fatalf("checkpoint step = %d, want 80", got.CheckpointStep)
	}
	if got.VoiceCompatibility.ContainsVoiceIdentity {
		t.Fatalf("default adapter should not be modeled as a voice profile: %+v", got.VoiceCompatibility)
	}

	contract := loraTrainingContract()
	hard, ok := contract["hard_rejection_reasons"].([]string)
	if !ok {
		t.Fatalf("hard rejection reasons missing: %+v", contract)
	}
	if !containsString(hard, "high_fit_delta") {
		t.Fatalf("high_fit_delta must be hard failure: %+v", hard)
	}
	objective := contract["objective_gates"].(gin.H)
	if objective["max_fit_delta"] != 0.05 || objective["high_fit_delta_is_hard_failure"] != true {
		t.Fatalf("fit gate must stay hard at 5%%: %+v", objective)
	}
	decision := contract["training_decision_policy"].(gin.H)
	if decision["fit_audit_first"] != true || decision["per_lecture_training_default"] != false {
		t.Fatalf("training decision policy drifted: %+v", decision)
	}
	if decision["production_selected_max_fit_delta"] != 0.05 {
		t.Fatalf("production fit threshold drifted: %+v", decision)
	}
	recipe := decision["duration_recipe"].(gin.H)
	if recipe["duration_source"] != "target" ||
		recipe["duration_position_loss_weight"] != 0.2 ||
		recipe["duration_position_loss_window"] != 32 ||
		recipe["feature_cps_feasible_max"] != 5.6 {
		t.Fatalf("duration recipe drifted: %+v", recipe)
	}
	references := contract["reference_baselines"].(gin.H)
	lecture08 := references["lecture08_highfit_repair"].(gin.H)
	if lecture08["id"] != "lecture08_highfit_12of12_tailfade_textrepair_v1" {
		t.Fatalf("lecture 8 reference baseline missing: %+v", references)
	}
	if lecture08["threshold_relaxation_used"] != false || lecture08["max_fit_delta"] != 0.0422 {
		t.Fatalf("lecture 8 reference must prove no threshold relaxation: %+v", lecture08)
	}
	if lecture08["v9_repair_manifest_relpath"] == "" {
		t.Fatalf("lecture 8 v9 repair manifest path missing: %+v", lecture08)
	}
	if lecture08["status"] != "comparison_baseline_superseded_by_audible_episode_drag" {
		t.Fatalf("lecture 8 highfit baseline should no longer be acceptance-pending: %+v", lecture08)
	}
	if lecture08["v9_training_plan_relpath"] == "" || lecture08["v9_training_status"] != "trained_holdout_regressed" {
		t.Fatalf("lecture 8 v9 training plan missing: %+v", lecture08)
	}
	if lecture08["v9_base_features_relpath"] == "" || lecture08["v9_adapter_run_relpath"] == "" {
		t.Fatalf("lecture 8 v9 training paths missing: %+v", lecture08)
	}
	if lecture08["v9_holdout_selected_count"] != 4 || lecture08["v9_holdout_count"] != 12 {
		t.Fatalf("lecture 8 v9 holdout regression evidence missing: %+v", lecture08)
	}
	goldmatch := references["lecture08_goldmatch_reference"].(gin.H)
	if goldmatch["adapter_relpath"] != "training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200/adapter_latest.pt" {
		t.Fatalf("lecture 8 goldmatch reference adapter drifted: %+v", goldmatch)
	}
	if goldmatch["strict_pass_count"] != 10 || goldmatch["selected_above_5pct_fit"] != 0 {
		t.Fatalf("lecture 8 goldmatch gates drifted: %+v", goldmatch)
	}

	lockedRunner := contract["runner_capabilities"].(gin.H)["full_training"]
	if lockedRunner != "locked_until_background_runner" {
		t.Fatalf("full runner should default locked, got %v", lockedRunner)
	}
	unlockedRunner := loraTrainingContract(true)["runner_capabilities"].(gin.H)["full_training"]
	if unlockedRunner != "ml_service_background_runner" {
		t.Fatalf("full runner flag should unlock background runner, got %v", unlockedRunner)
	}
}

func TestScanLoraAdapterRegistryGroupsRunsAndQualityEvidence(t *testing.T) {
	dataRoot := t.TempDir()
	runDir := filepath.Join(dataRoot, "training", "lora_runs", "pilot-episode146-code1500-r4-step80")
	t.Setenv("LORA_CANDIDATE_EXECUTION_ENABLED", "true")
	t.Setenv("ML_LORA_CANDIDATE_EXECUTION_ENABLED", "true")
	t.Setenv("LORA_SPLIT_PLANNING_ENABLED", "true")
	t.Setenv("INDEXTTS2_LORA_ADAPTER_PATH", "training/lora_runs/pilot-episode146-code1500-r4-step80/adapter_step80.pt")
	if err := os.MkdirAll(runDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"adapter_step60.pt", "adapter_step80.pt", "adapter_latest.pt"} {
		if err := os.WriteFile(filepath.Join(runDir, name), []byte(name), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	writeJSON(t, filepath.Join(runDir, "summary.json"), map[string]any{
		"metadata": map[string]any{
			"rank":        4,
			"source_tier": "silver",
		},
		"last_train": map[string]any{"step": 80},
	})

	canaryDir := filepath.Join(runDir, "canary84_full84_regen_v1")
	if err := os.MkdirAll(filepath.Join(canaryDir, "listening_pack"), 0o755); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, filepath.Join(canaryDir, "candidate_gate_report.json"), map[string]any{
		"status":         "pass",
		"selected_count": 84,
	})
	writeJSON(t, filepath.Join(canaryDir, "quality_baseline.json"), map[string]any{
		"status":      "shadow_pass",
		"baseline_id": "canary84_full84_selected_combined_auto_v2_listening_override",
		"drift": map[string]any{
			"mean_abs_drift_ms": 39.16,
			"max_abs_drift_ms":  298.73,
		},
		"join_quality": map[string]any{"failures": 0},
		"final_mix_asr": map[string]any{
			"gate_pass": true,
			"summary": map[string]any{
				"mean_similarity": 0.9931,
				"min_similarity":  0.9625,
			},
		},
	})
	compareDir := filepath.Join(runDir, "adapter_compare_step80_vs_latest_smoke8")
	if err := os.MkdirAll(compareDir, 0o755); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, filepath.Join(compareDir, "adapter_compare_report.json"), map[string]any{
		"status":                   "smoke_complete_needs_full_canary",
		"winner":                   "tie",
		"decision":                 "tie_keep_runtime_until_full_canary",
		"runtime_label":            "step80",
		"canonical_label":          "latest",
		"sample_count":             8,
		"promotion_allowed":        false,
		"selected_audio_identical": true,
		"comparison": map[string]any{
			"selected_audio_identical": true,
			"core_metrics_identical":   true,
			"adapter_files_identical":  false,
		},
	})
	if err := os.WriteFile(filepath.Join(canaryDir, "listening_pack", "index.html"), []byte("<html></html>"), 0o644); err != nil {
		t.Fatal(err)
	}
	smokeDir := filepath.Join(runDir, "_smoke_step1")
	if err := os.MkdirAll(smokeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(smokeDir, "adapter_latest.pt"), []byte("smoke-adapter"), 0o644); err != nil {
		t.Fatal(err)
	}
	tempProbeDir := filepath.Join(dataRoot, "training", "lora_runs", "__route_probe_smoke")
	if err := os.MkdirAll(tempProbeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(tempProbeDir, "adapter_latest.pt"), []byte("probe-adapter"), 0o644); err != nil {
		t.Fatal(err)
	}

	unlinkedDir := filepath.Join(dataRoot, "training", "lora_runs", "main_flow_shadow_episode147_final")
	if err := os.MkdirAll(unlinkedDir, 0o755); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, filepath.Join(unlinkedDir, "quality_baseline.json"), map[string]any{
		"status": "human_accepted",
	})

	registry, err := scanLoraAdapterRegistry(dataRoot)
	if err != nil {
		t.Fatalf("scanLoraAdapterRegistry error: %v", err)
	}
	if len(registry.Runs) != 1 {
		t.Fatalf("run count = %d, want 1", len(registry.Runs))
	}
	run := registry.Runs[0]
	if run.CheckpointCount != 3 {
		t.Fatalf("checkpoint count = %d, want 3", run.CheckpointCount)
	}
	for _, adapter := range registry.Adapters {
		if adapter.RunID == "_smoke_step1" || adapter.RunID == "__route_probe_smoke" {
			t.Fatalf("ephemeral smoke adapter leaked into registry: %+v", adapter)
		}
	}
	wantCanonical := "training/lora_runs/pilot-episode146-code1500-r4-step80/adapter_latest.pt"
	if run.CanonicalAdapterRelPath != wantCanonical {
		t.Fatalf("canonical adapter = %q, want %q", run.CanonicalAdapterRelPath, wantCanonical)
	}
	if !run.Checkpoints[0].IsCanonical || run.Checkpoints[0].CheckpointRole != "latest" {
		t.Fatalf("first checkpoint should be canonical latest: %+v", run.Checkpoints[0])
	}
	if run.PromotionStatus != "pilot_only" || !containsString(run.PromotionReasons, "training_data_not_gold") {
		t.Fatalf("silver run must stay pilot_only: %s %+v", run.PromotionStatus, run.PromotionReasons)
	}
	if len(run.Evidence) != 3 {
		t.Fatalf("linked evidence count = %d, want 3: %+v", len(run.Evidence), run.Evidence)
	}
	compare := firstEvidenceOfType(run.Evidence, "adapter_compare")
	if compare == nil || compare.Status != "smoke_complete_needs_full_canary" {
		t.Fatalf("adapter compare evidence missing: %+v", run.Evidence)
	}
	if compare.Metrics["winner"] != "tie" || compare.Metrics["decision"] != "tie_keep_runtime_until_full_canary" {
		t.Fatalf("adapter compare metrics not parsed: %+v", compare.Metrics)
	}
	hasReviewPack := false
	for _, item := range run.Evidence {
		hasReviewPack = hasReviewPack || item.ReviewPackRelPath != ""
	}
	if !hasReviewPack {
		t.Fatalf("review pack relpath not discovered: %+v", run.Evidence)
	}
	if !hasGate(run.GateStatus, "dataset_tier", "fail") {
		t.Fatalf("dataset gate should fail for silver data: %+v", run.GateStatus)
	}
	if !hasGate(run.GateStatus, "hard_failures", "pass") {
		t.Fatalf("hard failure gate should pass with zero join failures and low drift: %+v", run.GateStatus)
	}
	if len(registry.UnlinkedEvidence) != 1 {
		t.Fatalf("unlinked evidence count = %d, want 1: %+v", len(registry.UnlinkedEvidence), registry.UnlinkedEvidence)
	}

	foundCanonicalFlat := false
	for _, adapter := range registry.Adapters {
		if adapter.AdapterRelPath == wantCanonical && adapter.IsCanonical {
			foundCanonicalFlat = true
		}
	}
	if !foundCanonicalFlat {
		t.Fatalf("flat adapter list did not retain canonical annotation: %+v", registry.Adapters)
	}
	if !registry.RuntimeSelection.CandidateExecutionEnabled || !registry.RuntimeSelection.MLCandidateExecutionEnabled {
		t.Fatalf("runtime candidate flags not parsed: %+v", registry.RuntimeSelection)
	}
	if registry.RuntimeSelection.AdapterRelPath != "training/lora_runs/pilot-episode146-code1500-r4-step80/adapter_step80.pt" {
		t.Fatalf("runtime adapter relpath = %q", registry.RuntimeSelection.AdapterRelPath)
	}
	if !registry.RuntimeSelection.MatchedRegistry {
		t.Fatalf("runtime adapter should match registry: %+v", registry.RuntimeSelection)
	}
	if registry.RuntimeSelection.MatchesRunCanonical {
		t.Fatalf("runtime step80 should be reported as different from adapter_latest canonical: %+v", registry.RuntimeSelection)
	}
	if !containsString(registry.RuntimeSelection.Notes, "runtime_adapter_differs_from_registry_canonical") {
		t.Fatalf("runtime canonical mismatch note missing: %+v", registry.RuntimeSelection)
	}
}

func writeJSON(t *testing.T, path string, value any) {
	t.Helper()
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}

func containsString(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func hasGate(values []loraAdapterGateStatus, key, status string) bool {
	for _, value := range values {
		if value.Key == key && value.Status == status {
			return true
		}
	}
	return false
}
