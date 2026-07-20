package http

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	stdhttp "net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"holodub/internal/storage"

	"github.com/gin-gonic/gin"
)

type loraAdapterInfo struct {
	ID                 string                        `json:"id"`
	Name               string                        `json:"name"`
	RunID              string                        `json:"run_id"`
	RunRelPath         string                        `json:"run_relpath"`
	AdapterRelPath     string                        `json:"adapter_relpath"`
	SummaryRelPath     string                        `json:"summary_relpath,omitempty"`
	QualityRelPath     string                        `json:"quality_relpath,omitempty"`
	SizeBytes          int64                         `json:"size_bytes"`
	ModifiedAt         time.Time                     `json:"modified_at"`
	SHA256             string                        `json:"sha256,omitempty"`
	IsCanonical        bool                          `json:"is_canonical"`
	CheckpointRole     string                        `json:"checkpoint_role,omitempty"`
	CheckpointStep     int                           `json:"checkpoint_step,omitempty"`
	PromotionStatus    string                        `json:"promotion_status"`
	PromotionReasons   []string                      `json:"promotion_reasons"`
	Training           loraAdapterTrainingSummary    `json:"training"`
	Dataset            loraAdapterDatasetSummary     `json:"dataset"`
	Quality            loraAdapterQualitySummary     `json:"quality"`
	VoiceCompatibility loraAdapterVoiceCompatibility `json:"voice_compatibility"`
	Naming             loraAdapterNaming             `json:"naming"`
	Artifacts          map[string]string             `json:"artifacts"`
}

type loraAdapterRegistry struct {
	Adapters         []loraAdapterInfo           `json:"adapters"`
	Runs             []loraAdapterRunInfo        `json:"runs"`
	UnlinkedEvidence []loraAdapterEvidence       `json:"unlinked_evidence"`
	RuntimeSelection loraAdapterRuntimeSelection `json:"runtime_selection"`
}

type loraAdapterRuntimeSelection struct {
	CandidateExecutionEnabled   bool     `json:"candidate_execution_enabled"`
	MLCandidateExecutionEnabled bool     `json:"ml_candidate_execution_enabled"`
	SplitPlanningEnabled        bool     `json:"split_planning_enabled"`
	AdapterRelPath              string   `json:"adapter_relpath,omitempty"`
	AdapterID                   string   `json:"adapter_id,omitempty"`
	RunID                       string   `json:"run_id,omitempty"`
	MatchedRegistry             bool     `json:"matched_registry"`
	MatchesRunCanonical         bool     `json:"matches_run_canonical"`
	Notes                       []string `json:"notes"`
}

type loraAdapterRunInfo struct {
	ID                      string                        `json:"id"`
	Name                    string                        `json:"name"`
	RunRelPath              string                        `json:"run_relpath"`
	CanonicalAdapterID      string                        `json:"canonical_adapter_id,omitempty"`
	CanonicalAdapterRelPath string                        `json:"canonical_adapter_relpath,omitempty"`
	PromotionStatus         string                        `json:"promotion_status"`
	PromotionReasons        []string                      `json:"promotion_reasons"`
	CheckpointCount         int                           `json:"checkpoint_count"`
	Checkpoints             []loraAdapterInfo             `json:"checkpoints"`
	Evidence                []loraAdapterEvidence         `json:"evidence"`
	GateStatus              []loraAdapterGateStatus       `json:"gate_status"`
	Training                loraAdapterTrainingSummary    `json:"training"`
	Dataset                 loraAdapterDatasetSummary     `json:"dataset"`
	Quality                 loraAdapterQualitySummary     `json:"quality"`
	VoiceCompatibility      loraAdapterVoiceCompatibility `json:"voice_compatibility"`
	Naming                  loraAdapterNaming             `json:"naming"`
	Artifacts               map[string]string             `json:"artifacts"`
}

type loraAdapterEvidence struct {
	ID                string         `json:"id"`
	Type              string         `json:"type"`
	Status            string         `json:"status,omitempty"`
	BaselineID        string         `json:"baseline_id,omitempty"`
	RelPath           string         `json:"relpath"`
	Summary           string         `json:"summary,omitempty"`
	ReviewPackRelPath string         `json:"review_pack_relpath,omitempty"`
	ModifiedAt        time.Time      `json:"modified_at"`
	Metrics           map[string]any `json:"metrics,omitempty"`
}

type loraAdapterGateStatus struct {
	Key      string `json:"key"`
	Status   string `json:"status"`
	Reason   string `json:"reason,omitempty"`
	Evidence string `json:"evidence,omitempty"`
}

type loraAdapterTrainingSummary struct {
	Rank            any            `json:"rank,omitempty"`
	Alpha           any            `json:"alpha,omitempty"`
	Dropout         any            `json:"dropout,omitempty"`
	Steps           any            `json:"steps,omitempty"`
	TrainableParams any            `json:"trainable_params,omitempty"`
	TotalParams     any            `json:"total_params,omitempty"`
	DurationControl any            `json:"duration_control,omitempty"`
	DurationSource  any            `json:"duration_source,omitempty"`
	BaselineVal     map[string]any `json:"baseline_val,omitempty"`
	FinalVal        map[string]any `json:"final_val,omitempty"`
}

type loraAdapterDatasetSummary struct {
	SourceTier       string         `json:"source_tier"`
	TrainFeatures    string         `json:"train_features,omitempty"`
	ValFeatures      string         `json:"val_features,omitempty"`
	TrainFeatureRows int            `json:"train_feature_rows"`
	ValFeatureRows   int            `json:"val_feature_rows"`
	TestFeatureRows  int            `json:"test_feature_rows"`
	FeatureSplits    map[string]any `json:"feature_splits,omitempty"`
	Warnings         []string       `json:"warnings"`
}

type loraAdapterQualitySummary struct {
	Status             string         `json:"status"`
	BaselineID         string         `json:"baseline_id,omitempty"`
	MeanASR            any            `json:"mean_asr,omitempty"`
	WeightedASR        any            `json:"weighted_asr,omitempty"`
	MeanAbsDriftMS     any            `json:"mean_abs_drift_ms,omitempty"`
	MaxAbsDriftMS      any            `json:"max_abs_drift_ms,omitempty"`
	JoinQualityFailure any            `json:"join_quality_failures,omitempty"`
	FinalMixASRGate    any            `json:"final_mix_asr_gate,omitempty"`
	Raw                map[string]any `json:"raw,omitempty"`
}

type loraAdapterVoiceCompatibility struct {
	ContainsVoiceIdentity     bool     `json:"contains_voice_identity"`
	VoiceBiasRisk             string   `json:"voice_bias_risk"`
	IntendedScope             string   `json:"intended_scope"`
	CompatibleVoiceProfileIDs []uint   `json:"compatible_voice_profile_ids"`
	Notes                     []string `json:"notes"`
}

type loraAdapterNaming struct {
	RecommendedID     string `json:"recommended_id"`
	RecommendedLabel  string `json:"recommended_label"`
	Convention        string `json:"convention"`
	ExampleProduction string `json:"example_production"`
}

func (s *Server) listLoraAdapters(c *gin.Context) {
	registry, err := scanLoraAdapterRegistry(s.cfg.DataRoot)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_lora_adapters_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{
		"adapters":          registry.Adapters,
		"runs":              registry.Runs,
		"unlinked_evidence": registry.UnlinkedEvidence,
		"runtime_selection": registry.RuntimeSelection,
		"training_contract": loraTrainingContract(
			s.cfg.LoraTrainingFullRunnerEnabled,
			s.cfg.LoraTrainingValidationRunnerEnabled,
		),
	})
}

func (s *Server) getLoraTrainingContract(c *gin.Context) {
	c.JSON(stdhttp.StatusOK, loraTrainingContract(
		s.cfg.LoraTrainingFullRunnerEnabled,
		s.cfg.LoraTrainingValidationRunnerEnabled,
	))
}

func scanLoraAdapterRegistry(dataRoot string) (loraAdapterRegistry, error) {
	adapters, err := scanLoraAdapters(dataRoot)
	if err != nil {
		return loraAdapterRegistry{}, err
	}
	evidence, err := scanLoraEvidence(dataRoot)
	if err != nil {
		return loraAdapterRegistry{}, err
	}
	adapters, runs, unlinked := buildLoraAdapterRuns(adapters, evidence)
	runtime := loraRuntimeSelection(dataRoot, adapters, runs)
	return loraAdapterRegistry{
		Adapters:         adapters,
		Runs:             runs,
		UnlinkedEvidence: unlinked,
		RuntimeSelection: runtime,
	}, nil
}

func scanLoraAdapters(dataRoot string) ([]loraAdapterInfo, error) {
	runsRoot, err := storage.SecureJoinUnderRoot(dataRoot, "training/lora_runs")
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(runsRoot); os.IsNotExist(err) {
		return []loraAdapterInfo{}, nil
	}

	adapters := []loraAdapterInfo{}
	err = filepath.WalkDir(runsRoot, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			if path != runsRoot && (shouldSkipLoraRegistryDir(entry.Name()) || loraRegistryRelDepth(runsRoot, path) > 1) {
				return filepath.SkipDir
			}
			return nil
		}
		name := entry.Name()
		if filepath.Ext(name) != ".pt" || !strings.HasPrefix(name, "adapter") {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		adapter, err := buildLoraAdapterInfo(dataRoot, path, info)
		if err != nil {
			return err
		}
		adapters = append(adapters, adapter)
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(adapters, func(i, j int) bool {
		if adapters[i].ModifiedAt.Equal(adapters[j].ModifiedAt) {
			return adapters[i].AdapterRelPath < adapters[j].AdapterRelPath
		}
		return adapters[i].ModifiedAt.After(adapters[j].ModifiedAt)
	})
	return adapters, nil
}

func buildLoraAdapterRuns(adapters []loraAdapterInfo, evidence []loraAdapterEvidence) ([]loraAdapterInfo, []loraAdapterRunInfo, []loraAdapterEvidence) {
	runIndexes := map[string][]int{}
	for idx := range adapters {
		adapters[idx].CheckpointStep = loraCheckpointStep(adapters[idx].AdapterRelPath)
		adapters[idx].CheckpointRole = "checkpoint"
		runIndexes[adapters[idx].RunRelPath] = append(runIndexes[adapters[idx].RunRelPath], idx)
	}

	runs := make([]loraAdapterRunInfo, 0, len(runIndexes))
	linkedEvidence := map[string]bool{}
	for runRelPath, indexes := range runIndexes {
		if len(indexes) == 0 {
			continue
		}
		canonicalIndex := chooseCanonicalCheckpoint(adapters, indexes)
		adapters[canonicalIndex].IsCanonical = true
		adapters[canonicalIndex].CheckpointRole = canonicalCheckpointRole(adapters[canonicalIndex])
		canonical := adapters[canonicalIndex]

		checkpoints := make([]loraAdapterInfo, 0, len(indexes))
		for _, idx := range indexes {
			checkpoints = append(checkpoints, adapters[idx])
		}
		sort.Slice(checkpoints, func(i, j int) bool {
			if checkpoints[i].IsCanonical != checkpoints[j].IsCanonical {
				return checkpoints[i].IsCanonical
			}
			if checkpoints[i].CheckpointStep != checkpoints[j].CheckpointStep {
				return checkpoints[i].CheckpointStep > checkpoints[j].CheckpointStep
			}
			return checkpoints[i].ModifiedAt.After(checkpoints[j].ModifiedAt)
		})

		runEvidence := []loraAdapterEvidence{}
		for _, item := range evidence {
			if loraEvidenceBelongsToRun(item, runRelPath, canonical.RunID) {
				runEvidence = append(runEvidence, item)
				linkedEvidence[item.ID] = true
			}
		}
		sort.Slice(runEvidence, func(i, j int) bool {
			if runEvidence[i].ModifiedAt.Equal(runEvidence[j].ModifiedAt) {
				return runEvidence[i].RelPath < runEvidence[j].RelPath
			}
			return runEvidence[i].ModifiedAt.After(runEvidence[j].ModifiedAt)
		})

		gates := loraRunGateStatus(canonical, runEvidence)
		promotionStatus, promotionReasons := loraRunPromotionStatus(canonical, gates, runEvidence)
		artifacts := cloneStringMap(canonical.Artifacts)
		artifacts["canonical_adapter"] = canonical.AdapterRelPath
		if len(runEvidence) > 0 {
			artifacts["quality_evidence"] = runEvidence[0].RelPath
		}

		runs = append(runs, loraAdapterRunInfo{
			ID:                      canonical.RunID,
			Name:                    canonical.RunID,
			RunRelPath:              runRelPath,
			CanonicalAdapterID:      canonical.ID,
			CanonicalAdapterRelPath: canonical.AdapterRelPath,
			PromotionStatus:         promotionStatus,
			PromotionReasons:        promotionReasons,
			CheckpointCount:         len(checkpoints),
			Checkpoints:             checkpoints,
			Evidence:                runEvidence,
			GateStatus:              gates,
			Training:                canonical.Training,
			Dataset:                 canonical.Dataset,
			Quality:                 canonical.Quality,
			VoiceCompatibility:      canonical.VoiceCompatibility,
			Naming:                  canonical.Naming,
			Artifacts:               artifacts,
		})
	}

	sort.Slice(runs, func(i, j int) bool {
		if runs[i].PromotionStatus != runs[j].PromotionStatus {
			return promotionStatusRank(runs[i].PromotionStatus) < promotionStatusRank(runs[j].PromotionStatus)
		}
		return runs[i].RunRelPath < runs[j].RunRelPath
	})

	unlinked := []loraAdapterEvidence{}
	for _, item := range evidence {
		if !linkedEvidence[item.ID] {
			unlinked = append(unlinked, item)
		}
	}
	return adapters, runs, unlinked
}

func loraRuntimeSelection(dataRoot string, adapters []loraAdapterInfo, runs []loraAdapterRunInfo) loraAdapterRuntimeSelection {
	selection := loraAdapterRuntimeSelection{
		CandidateExecutionEnabled:   envBool("LORA_CANDIDATE_EXECUTION_ENABLED"),
		MLCandidateExecutionEnabled: envBool("ML_LORA_CANDIDATE_EXECUTION_ENABLED"),
		SplitPlanningEnabled:        envBool("LORA_SPLIT_PLANNING_ENABLED"),
		Notes:                       []string{},
	}
	raw := strings.TrimSpace(os.Getenv("INDEXTTS2_LORA_ADAPTER_PATH"))
	if raw == "" {
		selection.Notes = append(selection.Notes, "runtime_adapter_not_configured")
		return selection
	}
	selection.AdapterRelPath = normalizeLoraAdapterRelPath(dataRoot, raw)
	if selection.AdapterRelPath == "" {
		selection.AdapterRelPath = filepath.ToSlash(raw)
	}
	selection.AdapterID = loraAdapterID(selection.AdapterRelPath)

	for _, adapter := range adapters {
		if adapter.AdapterRelPath != selection.AdapterRelPath {
			continue
		}
		selection.MatchedRegistry = true
		selection.RunID = adapter.RunID
		for _, run := range runs {
			if run.RunRelPath == adapter.RunRelPath {
				selection.MatchesRunCanonical = run.CanonicalAdapterRelPath == adapter.AdapterRelPath
				break
			}
		}
		if !selection.MatchesRunCanonical {
			selection.Notes = append(selection.Notes, "runtime_adapter_differs_from_registry_canonical")
		}
		return selection
	}
	selection.Notes = append(selection.Notes, "runtime_adapter_not_found_in_registry")
	return selection
}

func normalizeLoraAdapterRelPath(dataRoot, raw string) string {
	value := strings.TrimSpace(raw)
	if value == "" {
		return ""
	}
	value = filepath.ToSlash(value)
	value = strings.TrimPrefix(value, "/data/")
	value = strings.TrimPrefix(value, "data/")
	if !filepath.IsAbs(value) {
		return value
	}
	cleanRoot, err := filepath.Abs(filepath.Clean(dataRoot))
	if err != nil {
		return filepath.ToSlash(value)
	}
	rel, err := filepath.Rel(cleanRoot, filepath.Clean(value))
	if err != nil {
		return filepath.ToSlash(value)
	}
	return filepath.ToSlash(rel)
}

func envBool(key string) bool {
	value := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	return value == "1" || value == "true" || value == "yes" || value == "on"
}

func buildLoraAdapterInfo(dataRoot, adapterPath string, info os.FileInfo) (loraAdapterInfo, error) {
	cleanRoot, err := filepath.Abs(filepath.Clean(dataRoot))
	if err != nil {
		return loraAdapterInfo{}, err
	}
	runDir := filepath.Dir(adapterPath)
	relAdapter, err := filepath.Rel(cleanRoot, adapterPath)
	if err != nil {
		return loraAdapterInfo{}, err
	}
	relRun, err := filepath.Rel(cleanRoot, runDir)
	if err != nil {
		return loraAdapterInfo{}, err
	}
	relAdapter = filepath.ToSlash(relAdapter)
	relRun = filepath.ToSlash(relRun)
	runID := filepath.Base(runDir)
	adapterStem := strings.TrimSuffix(filepath.Base(adapterPath), filepath.Ext(adapterPath))

	summaryPath := filepath.Join(runDir, "summary.json")
	summary := readJSONMap(summaryPath)
	qualityPath := filepath.Join(runDir, "quality_baseline.json")
	quality := readJSONMap(qualityPath)

	training := loraTrainingSummaryFromJSON(summary)
	dataset := loraDatasetSummaryFromJSON(dataRoot, summary)
	qualitySummary := loraQualitySummaryFromJSON(quality)
	promotionStatus, promotionReasons := loraPromotionStatus(dataset, qualitySummary)

	adapter := loraAdapterInfo{
		ID:                 loraAdapterID(relAdapter),
		Name:               fmt.Sprintf("%s / %s", runID, adapterStem),
		RunID:              runID,
		RunRelPath:         relRun,
		AdapterRelPath:     relAdapter,
		SizeBytes:          info.Size(),
		ModifiedAt:         info.ModTime(),
		SHA256:             shortFileSHA256(adapterPath),
		CheckpointStep:     loraCheckpointStep(relAdapter),
		CheckpointRole:     "checkpoint",
		PromotionStatus:    promotionStatus,
		PromotionReasons:   promotionReasons,
		Training:           training,
		Dataset:            dataset,
		Quality:            qualitySummary,
		VoiceCompatibility: defaultLoraVoiceCompatibility(),
		Naming:             loraNaming(runID, adapterStem),
		Artifacts:          map[string]string{"adapter": relAdapter},
	}
	if len(summary) > 0 {
		adapter.SummaryRelPath = filepath.ToSlash(filepath.Join(relRun, "summary.json"))
		adapter.Artifacts["summary"] = adapter.SummaryRelPath
	}
	if len(quality) > 0 {
		adapter.QualityRelPath = filepath.ToSlash(filepath.Join(relRun, "quality_baseline.json"))
		adapter.Artifacts["quality_baseline"] = adapter.QualityRelPath
	}
	return adapter, nil
}

func scanLoraEvidence(dataRoot string) ([]loraAdapterEvidence, error) {
	runsRoot, err := storage.SecureJoinUnderRoot(dataRoot, "training/lora_runs")
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(runsRoot); os.IsNotExist(err) {
		return []loraAdapterEvidence{}, nil
	}
	cleanRoot, err := filepath.Abs(filepath.Clean(dataRoot))
	if err != nil {
		return nil, err
	}

	evidence := []loraAdapterEvidence{}
	err = filepath.WalkDir(runsRoot, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			if path != runsRoot && (shouldSkipLoraRegistryDir(entry.Name()) || loraRegistryRelDepth(runsRoot, path) > 2) {
				return filepath.SkipDir
			}
			return nil
		}
		evidenceType := loraEvidenceType(entry.Name())
		if evidenceType == "" {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		item, err := buildLoraEvidence(cleanRoot, path, evidenceType, info)
		if err != nil {
			return err
		}
		evidence = append(evidence, item)
		return nil
	})
	if err != nil {
		return nil, err
	}
	sort.Slice(evidence, func(i, j int) bool {
		if evidence[i].ModifiedAt.Equal(evidence[j].ModifiedAt) {
			return evidence[i].RelPath < evidence[j].RelPath
		}
		return evidence[i].ModifiedAt.After(evidence[j].ModifiedAt)
	})
	return evidence, nil
}

func shouldSkipLoraRegistryDir(name string) bool {
	normalized := strings.ToLower(strings.TrimSpace(name))
	if normalized == "" || strings.HasPrefix(normalized, ".") || strings.HasPrefix(normalized, "__") || strings.HasPrefix(normalized, "_smoke") {
		return true
	}
	if strings.Contains(normalized, "listening_pack") ||
		strings.HasPrefix(normalized, "selected") ||
		strings.HasPrefix(normalized, "repair_candidates") ||
		strings.HasPrefix(normalized, "final_mix") ||
		strings.HasPrefix(normalized, "final_video") ||
		strings.HasPrefix(normalized, "tts_segment_asr") ||
		strings.HasPrefix(normalized, "codec_probe") ||
		strings.HasSuffix(normalized, "_asr") {
		return true
	}
	switch normalized {
	case "raw", "selected", "listening_pack", "asr", "split", "gatefit", "plan", "parts", "audio", "wav", "tmp", "temp", "cache":
		return true
	default:
		return strings.HasPrefix(normalized, "human_review_pack")
	}
}

func loraRegistryRelDepth(root, path string) int {
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == "." {
		return 0
	}
	rel = strings.Trim(filepath.ToSlash(rel), "/")
	if rel == "" {
		return 0
	}
	return len(strings.Split(rel, "/"))
}

func loraEvidenceType(filename string) string {
	switch filename {
	case "quality_baseline.json":
		return "quality_baseline"
	case "candidate_gate_report.json":
		return "candidate_gate"
	case "combined_summary.json":
		return "combined_summary"
	case "loop_summary.json":
		return "loop_summary"
	case "human_review_report.json":
		return "human_review"
	case "human_acceptance_checklist.md":
		return "human_acceptance"
	case "review_manifest.json":
		return "review_manifest"
	case "adapter_compare_report.json":
		return "adapter_compare"
	default:
		return ""
	}
}

func buildLoraEvidence(cleanDataRoot, path, evidenceType string, info os.FileInfo) (loraAdapterEvidence, error) {
	relPath, err := filepath.Rel(cleanDataRoot, path)
	if err != nil {
		return loraAdapterEvidence{}, err
	}
	relPath = filepath.ToSlash(relPath)
	raw := map[string]any(nil)
	if strings.EqualFold(filepath.Ext(path), ".json") {
		raw = readJSONMap(path)
	}
	status := firstNonEmptyString(
		stringFromJSON(raw["status"]),
		stringFromJSON(raw["verdict"]),
		stringFromJSON(raw["gate_status"]),
	)
	baselineID := stringFromJSON(raw["baseline_id"])
	metrics := extractLoraEvidenceMetrics(raw)
	summary := loraEvidenceSummary(evidenceType, status, metrics)
	reviewPackRelPath := siblingReviewPackRelPath(cleanDataRoot, filepath.Dir(path))

	return loraAdapterEvidence{
		ID:                loraAdapterID(relPath),
		Type:              evidenceType,
		Status:            status,
		BaselineID:        baselineID,
		RelPath:           relPath,
		Summary:           summary,
		ReviewPackRelPath: reviewPackRelPath,
		ModifiedAt:        info.ModTime(),
		Metrics:           metrics,
	}, nil
}

func extractLoraEvidenceMetrics(raw map[string]any) map[string]any {
	if len(raw) == 0 {
		return nil
	}
	metrics := map[string]any{}
	if drift, ok := raw["drift"].(map[string]any); ok {
		copyMetric(metrics, "mean_abs_drift_ms", drift["mean_abs_drift_ms"])
		copyMetric(metrics, "max_abs_drift_ms", drift["max_abs_drift_ms"])
	}
	if joins, ok := raw["join_quality"].(map[string]any); ok {
		copyMetric(metrics, "join_quality_failures", joins["failures"])
	}
	copyMetric(metrics, "join_quality_failures", raw["join_quality_failures"])
	if asr, ok := raw["final_mix_asr"].(map[string]any); ok {
		copyMetric(metrics, "final_mix_asr_gate", asr["gate_pass"])
		if summary, ok := asr["summary"].(map[string]any); ok {
			copyMetric(metrics, "mean_asr", summary["mean_similarity"])
			copyMetric(metrics, "weighted_asr", summary["weighted_similarity"])
			copyMetric(metrics, "min_asr", summary["min_similarity"])
		}
	}
	if selected, ok := raw["selected_metrics"].(map[string]any); ok {
		copyMetric(metrics, "selected_mean_abs_drift_ms", selected["mean_abs_drift_ms"])
		copyMetric(metrics, "selected_max_abs_drift_ms", selected["max_abs_drift_ms"])
		copyMetric(metrics, "selected_mean_asr", selected["mean_asr"])
		copyMetric(metrics, "selected_mean_similarity", selected["mean_similarity"])
		copyMetric(metrics, "selected_min_asr", selected["min_asr"])
		copyMetric(metrics, "selected_min_similarity", selected["min_similarity"])
	}
	copyMetric(metrics, "winner", raw["winner"])
	copyMetric(metrics, "decision", raw["decision"])
	copyMetric(metrics, "runtime_label", raw["runtime_label"])
	copyMetric(metrics, "canonical_label", raw["canonical_label"])
	copyMetric(metrics, "promotion_allowed", raw["promotion_allowed"])
	copyMetric(metrics, "sample_count", raw["sample_count"])
	if comparison, ok := raw["comparison"].(map[string]any); ok {
		copyMetric(metrics, "selected_audio_identical", comparison["selected_audio_identical"])
		copyMetric(metrics, "core_metrics_identical", comparison["core_metrics_identical"])
		copyMetric(metrics, "adapter_files_identical", comparison["adapter_files_identical"])
	}
	copyMetric(metrics, "count", raw["count"])
	copyMetric(metrics, "selected_count", raw["selected_count"])
	copyMetric(metrics, "fallback_count", raw["fallback_count"])
	if len(metrics) == 0 {
		return nil
	}
	return metrics
}

func copyMetric(metrics map[string]any, key string, value any) {
	if value != nil {
		metrics[key] = value
	}
}

func loraEvidenceSummary(evidenceType, status string, metrics map[string]any) string {
	parts := []string{evidenceType}
	if status != "" {
		parts = append(parts, status)
	}
	if mean, ok := metricFloat(metrics["mean_abs_drift_ms"]); ok {
		parts = append(parts, fmt.Sprintf("mean drift %.1fms", mean))
	}
	if maxDrift, ok := metricFloat(metrics["max_abs_drift_ms"]); ok {
		parts = append(parts, fmt.Sprintf("max drift %.1fms", maxDrift))
	}
	if meanASR, ok := metricFloat(metrics["mean_asr"]); ok {
		parts = append(parts, fmt.Sprintf("ASR %.4f", meanASR))
	}
	if winner := stringFromJSON(metrics["winner"]); winner != "" {
		parts = append(parts, "winner "+winner)
	}
	if decision := stringFromJSON(metrics["decision"]); decision != "" {
		parts = append(parts, decision)
	}
	return strings.Join(parts, " / ")
}

func siblingReviewPackRelPath(cleanDataRoot, dir string) string {
	candidates := []string{
		filepath.Join(dir, "listening_pack", "index.html"),
		filepath.Join(dir, "human_review_pack", "index.html"),
	}
	matches, _ := filepath.Glob(filepath.Join(dir, "human_review_pack*", "index.html"))
	candidates = append(candidates, matches...)
	for _, candidate := range candidates {
		if _, err := os.Stat(candidate); err == nil {
			rel, err := filepath.Rel(cleanDataRoot, candidate)
			if err == nil {
				return filepath.ToSlash(rel)
			}
		}
	}
	return ""
}

func chooseCanonicalCheckpoint(adapters []loraAdapterInfo, indexes []int) int {
	best := indexes[0]
	for _, idx := range indexes[1:] {
		if canonicalScore(adapters[idx]) > canonicalScore(adapters[best]) {
			best = idx
			continue
		}
		if canonicalScore(adapters[idx]) == canonicalScore(adapters[best]) {
			if adapters[idx].CheckpointStep > adapters[best].CheckpointStep {
				best = idx
				continue
			}
			if adapters[idx].CheckpointStep == adapters[best].CheckpointStep && adapters[idx].ModifiedAt.After(adapters[best].ModifiedAt) {
				best = idx
			}
		}
	}
	return best
}

func canonicalScore(adapter loraAdapterInfo) int {
	name := strings.ToLower(filepath.Base(adapter.AdapterRelPath))
	switch {
	case name == "adapter_latest.pt":
		return 100_000
	case strings.Contains(name, "best"):
		return 90_000 + adapter.CheckpointStep
	case adapter.CheckpointStep > 0:
		return adapter.CheckpointStep
	default:
		return 0
	}
}

func canonicalCheckpointRole(adapter loraAdapterInfo) string {
	name := strings.ToLower(filepath.Base(adapter.AdapterRelPath))
	if name == "adapter_latest.pt" {
		return "latest"
	}
	if strings.Contains(name, "best") {
		return "best"
	}
	if adapter.CheckpointStep > 0 {
		return "step"
	}
	return "checkpoint"
}

func loraCheckpointStep(relPath string) int {
	stem := strings.TrimSuffix(filepath.Base(relPath), filepath.Ext(relPath))
	pos := strings.LastIndex(stem, "step")
	if pos < 0 {
		return 0
	}
	raw := stem[pos+len("step"):]
	raw = strings.TrimLeft(raw, "_-")
	digits := strings.Builder{}
	for _, r := range raw {
		if r < '0' || r > '9' {
			break
		}
		digits.WriteRune(r)
	}
	if digits.Len() == 0 {
		return 0
	}
	value, err := strconv.Atoi(digits.String())
	if err != nil {
		return 0
	}
	return value
}

func loraEvidenceBelongsToRun(evidence loraAdapterEvidence, runRelPath, runID string) bool {
	relPath := filepath.ToSlash(evidence.RelPath)
	runRelPath = strings.TrimSuffix(filepath.ToSlash(runRelPath), "/")
	if relPath == runRelPath || strings.HasPrefix(relPath, runRelPath+"/") {
		return true
	}
	return runID != "" && strings.Contains(relPath, runID)
}

func loraRunGateStatus(adapter loraAdapterInfo, evidence []loraAdapterEvidence) []loraAdapterGateStatus {
	gates := []loraAdapterGateStatus{}
	if adapter.Dataset.SourceTier == "gold" {
		gates = append(gates, loraAdapterGateStatus{Key: "dataset_tier", Status: "pass", Reason: "gold_training_data"})
	} else {
		gates = append(gates, loraAdapterGateStatus{Key: "dataset_tier", Status: "fail", Reason: "training_data_not_gold"})
	}

	if found := firstEvidenceOfType(evidence, "loop_summary", "combined_summary", "candidate_gate"); found != nil {
		gates = append(gates, loraAdapterGateStatus{Key: "canary_regression", Status: "warn", Reason: "canary_evidence_linked_needs_fixed_baseline_check", Evidence: found.RelPath})
	} else {
		gates = append(gates, loraAdapterGateStatus{Key: "canary_regression", Status: "missing", Reason: "missing_canary_regression"})
	}

	if found := firstEvidenceOfType(evidence, "quality_baseline"); found != nil {
		status := "warn"
		reason := "quality_evidence_linked_needs_review"
		if found.Status == "shadow_pass" || found.Status == "human_accepted" {
			status = "pass"
			reason = "episode_shadow_passed"
		}
		gates = append(gates, loraAdapterGateStatus{Key: "episode_shadow", Status: status, Reason: reason, Evidence: found.RelPath})
	} else {
		gates = append(gates, loraAdapterGateStatus{Key: "episode_shadow", Status: "missing", Reason: "missing_episode_shadow"})
	}

	if found := firstEvidenceOfType(evidence, "human_acceptance", "human_review", "review_manifest"); found != nil {
		gates = append(gates, loraAdapterGateStatus{Key: "human_acceptance", Status: "warn", Reason: "human_review_evidence_linked", Evidence: found.RelPath})
	} else {
		gates = append(gates, loraAdapterGateStatus{Key: "human_acceptance", Status: "missing", Reason: "missing_human_acceptance"})
	}

	hardStatus := hardFailureGateStatus(adapter, evidence)
	gates = append(gates, hardStatus)
	return gates
}

func hardFailureGateStatus(adapter loraAdapterInfo, evidence []loraAdapterEvidence) loraAdapterGateStatus {
	for _, item := range evidence {
		if failures, ok := metricFloat(item.Metrics["join_quality_failures"]); ok && failures > 0 {
			return loraAdapterGateStatus{Key: "hard_failures", Status: "fail", Reason: "join_quality_failures_present", Evidence: item.RelPath}
		}
		if drift, ok := metricFloat(item.Metrics["max_abs_drift_ms"]); ok && drift > 500 {
			return loraAdapterGateStatus{Key: "hard_failures", Status: "fail", Reason: "high_drift", Evidence: item.RelPath}
		}
	}
	if failures, ok := metricFloat(adapter.Quality.JoinQualityFailure); ok && failures > 0 {
		return loraAdapterGateStatus{Key: "hard_failures", Status: "fail", Reason: "join_quality_failures_present", Evidence: adapter.QualityRelPath}
	}
	if drift, ok := metricFloat(adapter.Quality.MaxAbsDriftMS); ok && drift > 500 {
		return loraAdapterGateStatus{Key: "hard_failures", Status: "fail", Reason: "high_drift", Evidence: adapter.QualityRelPath}
	}
	if firstEvidenceOfType(evidence, "quality_baseline", "candidate_gate", "loop_summary", "combined_summary") == nil && adapter.Quality.Status == "missing_quality_baseline" {
		return loraAdapterGateStatus{Key: "hard_failures", Status: "missing", Reason: "missing_quality_evidence"}
	}
	return loraAdapterGateStatus{Key: "hard_failures", Status: "pass", Reason: "no_hard_failures_detected"}
}

func loraRunPromotionStatus(adapter loraAdapterInfo, gates []loraAdapterGateStatus, evidence []loraAdapterEvidence) (string, []string) {
	reasons := []string{}
	hasMissing := false
	hasWarn := false
	for _, gate := range gates {
		switch gate.Status {
		case "fail":
			reasons = append(reasons, gate.Reason)
		case "missing":
			hasMissing = true
			reasons = append(reasons, gate.Reason)
		case "warn":
			hasWarn = true
		}
	}
	if len(reasons) > 0 {
		return "pilot_only", uniqueStrings(reasons)
	}
	if adapter.Dataset.SourceTier != "gold" {
		return "pilot_only", []string{"training_data_not_gold"}
	}
	if hasMissing {
		return "pilot_only", []string{"missing_required_gate"}
	}
	if hasWarn || len(evidence) == 0 {
		return "needs_review", []string{"quality_status_requires_review"}
	}
	return "candidate_ready", []string{"objective_gates_passed"}
}

func firstEvidenceOfType(evidence []loraAdapterEvidence, types ...string) *loraAdapterEvidence {
	for idx := range evidence {
		for _, evidenceType := range types {
			if evidence[idx].Type == evidenceType {
				return &evidence[idx]
			}
		}
	}
	return nil
}

func cloneStringMap(input map[string]string) map[string]string {
	out := map[string]string{}
	for key, value := range input {
		out[key] = value
	}
	return out
}

func promotionStatusRank(status string) int {
	switch status {
	case "candidate_ready":
		return 0
	case "needs_review":
		return 1
	case "pilot_only":
		return 2
	default:
		return 3
	}
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func metricFloat(raw any) (float64, bool) {
	switch value := raw.(type) {
	case float64:
		return value, true
	case float32:
		return float64(value), true
	case int:
		return float64(value), true
	case int64:
		return float64(value), true
	case json.Number:
		parsed, err := value.Float64()
		return parsed, err == nil
	default:
		return 0, false
	}
}

func uniqueStrings(values []string) []string {
	seen := map[string]bool{}
	out := []string{}
	for _, value := range values {
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	return out
}

func readJSONMap(path string) map[string]any {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var out map[string]any
	if err := json.Unmarshal(data, &out); err != nil {
		return nil
	}
	return out
}

func loraTrainingSummaryFromJSON(summary map[string]any) loraAdapterTrainingSummary {
	meta, _ := summary["metadata"].(map[string]any)
	lastTrain, _ := summary["last_train"].(map[string]any)
	baselineVal, _ := summary["baseline_val"].(map[string]any)
	finalVal, _ := summary["final_val"].(map[string]any)
	training := loraAdapterTrainingSummary{
		BaselineVal: baselineVal,
		FinalVal:    finalVal,
	}
	if meta != nil {
		training.Rank = meta["rank"]
		training.Alpha = meta["alpha"]
		training.Dropout = meta["dropout"]
		training.TrainableParams = meta["trainable_params"]
		training.TotalParams = meta["total_params"]
		training.DurationControl = meta["duration_control"]
		training.DurationSource = meta["duration_source"]
	}
	if lastTrain != nil {
		training.Steps = lastTrain["step"]
	}
	return training
}

func loraDatasetSummaryFromJSON(dataRoot string, summary map[string]any) loraAdapterDatasetSummary {
	meta, _ := summary["metadata"].(map[string]any)
	dataset := loraAdapterDatasetSummary{
		SourceTier: "silver_or_unknown",
		Warnings: []string{
			"Current pilot data is treated as silver unless a gold manifest is attached.",
			"Silver adapters are allowed for smoke/candidate experiments, not final quality claims.",
		},
	}
	if meta == nil {
		dataset.Warnings = append(dataset.Warnings, "summary.json metadata is missing")
		return dataset
	}
	if tier := strings.TrimSpace(stringFromJSON(meta["source_tier"])); tier != "" {
		dataset.SourceTier = tier
	} else if tier := strings.TrimSpace(stringFromJSON(meta["dataset_tier"])); tier != "" {
		dataset.SourceTier = tier
	}
	dataset.TrainFeatures, _ = meta["train_features"].(string)
	dataset.ValFeatures, _ = meta["val_features"].(string)
	dataset.TrainFeatureRows = countJSONL(resolveDataPathLike(dataRoot, dataset.TrainFeatures))
	dataset.ValFeatureRows = countJSONL(resolveDataPathLike(dataRoot, dataset.ValFeatures))

	featureDir := filepath.Dir(resolveDataPathLike(dataRoot, dataset.TrainFeatures))
	featureManifest := readJSONMap(filepath.Join(featureDir, "features_manifest.json"))
	if tier := strings.TrimSpace(stringFromJSON(featureManifest["source_tier"])); tier != "" {
		dataset.SourceTier = tier
	} else if tier := strings.TrimSpace(stringFromJSON(featureManifest["dataset_tier"])); tier != "" {
		dataset.SourceTier = tier
	}
	if splits, ok := featureManifest["splits"].(map[string]any); ok {
		dataset.FeatureSplits = splits
		if test, ok := splits["test"].(map[string]any); ok {
			dataset.TestFeatureRows = intFromJSONNumber(test["accepted"])
		}
	}
	return dataset
}

func loraQualitySummaryFromJSON(quality map[string]any) loraAdapterQualitySummary {
	if len(quality) == 0 {
		return loraAdapterQualitySummary{Status: "missing_quality_baseline"}
	}
	out := loraAdapterQualitySummary{
		Status:     stringFromJSON(quality["status"]),
		BaselineID: stringFromJSON(quality["baseline_id"]),
		Raw:        quality,
	}
	if drift, ok := quality["drift"].(map[string]any); ok {
		out.MeanAbsDriftMS = drift["mean_abs_drift_ms"]
		out.MaxAbsDriftMS = drift["max_abs_drift_ms"]
	}
	if joins, ok := quality["join_quality"].(map[string]any); ok {
		out.JoinQualityFailure = joins["failures"]
	}
	if out.JoinQualityFailure == nil {
		out.JoinQualityFailure = quality["join_quality_failures"]
	}
	if asr, ok := quality["final_mix_asr"].(map[string]any); ok {
		out.FinalMixASRGate = asr["gate_pass"]
		if summary, ok := asr["summary"].(map[string]any); ok {
			out.MeanASR = summary["mean_similarity"]
			out.WeightedASR = summary["weighted_similarity"]
		}
	}
	if out.Status == "" {
		out.Status = "quality_baseline_present"
	}
	return out
}

func loraPromotionStatus(dataset loraAdapterDatasetSummary, quality loraAdapterQualitySummary) (string, []string) {
	reasons := []string{}
	if dataset.SourceTier != "gold" {
		reasons = append(reasons, "training_data_not_gold")
	}
	if quality.Status == "missing_quality_baseline" {
		reasons = append(reasons, "missing_quality_baseline")
	}
	if len(reasons) > 0 {
		return "pilot_only", reasons
	}
	if quality.Status == "shadow_pass" || quality.Status == "human_accepted" {
		return "candidate_ready", []string{"objective_gates_passed"}
	}
	return "needs_review", []string{"quality_status_requires_review"}
}

func defaultLoraVoiceCompatibility() loraAdapterVoiceCompatibility {
	return loraAdapterVoiceCompatibility{
		ContainsVoiceIdentity:     false,
		VoiceBiasRisk:             "medium_if_trained_from_single_voice",
		IntendedScope:             "model_strategy_adapter",
		CompatibleVoiceProfileIDs: []uint{},
		Notes: []string{
			"Manage LoRA adapters separately from voice profiles.",
			"An adapter can bias speaking style if the training set is dominated by one voice.",
			"Production selection should record both voice_profile_id and lora_adapter_id.",
		},
	}
}

func loraNaming(runID, adapterStem string) loraAdapterNaming {
	recommendedID := strings.ToLower(strings.ReplaceAll(runID+"-"+adapterStem, "_", "-"))
	return loraAdapterNaming{
		RecommendedID:     recommendedID,
		RecommendedLabel:  fmt.Sprintf("%s %s", runID, adapterStem),
		Convention:        "<domain>-<dataset-tier>-<episode-or-corpus>-r<rank>-s<step>-<gate>",
		ExampleProduction: "raft-zh-gold2h-r8-s1200-canary84p-ep-pass",
	}
}

func loraTrainingContract(fullRunnerEnabled ...bool) gin.H {
	fullTraining := "locked_until_background_runner"
	if len(fullRunnerEnabled) > 0 && fullRunnerEnabled[0] {
		fullTraining = "ml_service_background_runner"
	}
	validationRunner := "locked_until_validation_runner"
	if len(fullRunnerEnabled) > 1 && fullRunnerEnabled[1] {
		validationRunner = "ml_service_validation_runner"
	}
	return gin.H{
		"schema_version": "holodub.lora_adapter_training_contract.v1",
		"asset_model": gin.H{
			"adapter_is_voice_profile": false,
			"adapter_role":             "model strategy / duration and domain pacing adapter",
			"voice_role":               "speaker identity via VoiceProfile sample prompts or checkpoints",
			"compatibility_rule":       "Persist voice_profile_id and lora_adapter_id separately; only mark adapters voice-scoped when trained from a controlled single-voice gold set.",
		},
		"data_requirements": gin.H{
			"silver_use":              "smoke and duration calibration only",
			"gold_min_hours":          2,
			"gold_target_hours":       5,
			"required_coverage":       []string{"short", "long", "mixed Latin terms", "numbers", "section references", "natural pauses", "tail completeness"},
			"fixed_holdout_required":  true,
			"human_approval_required": true,
		},
		"objective_gates": gin.H{
			"canary_min_count":               84,
			"canary_max_mean_abs_drift_ms":   50,
			"canary_max_max_abs_drift_ms":    300,
			"canary_min_mean_asr":            0.99,
			"canary_min_min_asr":             0.96,
			"join_quality_failures":          0,
			"episode_over_drift_gt_300":      0,
			"episode_under_drift_gt_700":     0,
			"final_mix_asr_gate_required":    true,
			"high_fit_delta_is_hard_failure": true,
			"max_fit_delta":                  0.05,
		},
		"training_decision_policy": gin.H{
			"fit_audit_first":                   true,
			"training_pack_audit_required":      true,
			"training_pack_audit_tool":          "tools/mit6824_lora_training_pack_audit.py",
			"training_readiness_required":       true,
			"training_readiness_tool":           "tools/mit6824_lora_training_readiness.py",
			"reference_overlap_blocks_training": true,
			"min_new_source_ids_before_retrain": 40,
			"production_selected_max_fit_delta": 0.05,
			"low_final_drift_not_sufficient":    true,
			"per_lecture_training_default":      false,
			"duration_recipe": gin.H{
				"duration_control":              true,
				"duration_source":               "target",
				"duration_dropout":              0.1,
				"mel_stop_loss_weight":          2.0,
				"mel_end_loss_weight":           1.5,
				"mel_end_window":                64,
				"duration_stop_bce_weight":      0.0,
				"duration_position_loss_weight": 0.2,
				"duration_position_loss_window": 32,
				"feature_cps_feasible_max":      5.6,
				"promotion_fit_delta_max":       0.05,
				"checkpoint_selection_rule":     "generated_duration_holdout_not_final_step_loss",
			},
			"repair_order": []string{
				"reject_selected_audio_above_5_percent_fit",
				"check_speech_rate_feasibility",
				"repair_or_compress_target_text_before_tts",
				"regenerate_split_candidates_with_5_percent_fit_gate",
				"repair_loud_split_boundaries_when_join_diagnostics_match",
				"train_only_after_repeated_fixed_holdout_failures",
			},
		},
		"reference_baselines": gin.H{
			"lecture08_highfit_repair": gin.H{
				"id":                         "lecture08_highfit_12of12_tailfade_textrepair_v1",
				"status":                     "comparison_baseline_superseded_by_audible_episode_drag",
				"selected_count":             12,
				"mean_abs_drift_ms":          66.48,
				"max_abs_drift_ms":           278.41,
				"mean_asr":                   0.9915,
				"min_asr":                    0.9756,
				"join_quality_failures":      0,
				"max_fit_delta":              0.0422,
				"required_next_step":         "repair_fixed_highfit_holdout_before_next_full_episode_shadow",
				"listening_pack_relpath":     "training/lora_runs/mit6824-lec08-v8-feasible-cps56-durationpos-r4-step160/lecture08_highfit_12of12_tailfade_textrepair_v1/listening_pack/index.html",
				"training_decision":          "train_or_repair_when_fixed_holdout_regresses_or_episode_fit_audit_fails",
				"audible_episode_review":     "dragged_speech_blocks_acceptance",
				"v9_training_status":         "trained_holdout_regressed",
				"v9_training_plan_relpath":   "validation/mit6824_acceptance/lec-08/lecture08_v9_training_plan.json",
				"v9_base_features_relpath":   "training/lora_features/mit6824-lec08-v8-feasible-cps56-split12-parts",
				"v9_repair_manifest_relpath": "training/lora_corpus/mit6824-lec08-v9-promoted-repair-v1/manifest.json",
				"v9_merged_features_relpath": "training/lora_features/mit6824-lec08-v9-feasible-cps56-plus-repair-v1",
				"v9_adapter_run_relpath":     "training/lora_runs/mit6824-lec08-v9-feasible-cps56-repair-durationpos-r4-step160",
				"v9_holdout_selected_count":  4,
				"v9_holdout_count":           12,
				"v9_repair_manifest_policy":  "append only promoted repair audio after preprocessing; failed shadow audio is not gold",
				"v10_training_status":        "rejected_holdout_regressed",
				"v10_adapter_run_relpath":    "training/lora_runs/mit6824-lec08-v10-goldmatch-v7clean-r4-step240",
				"v10_decision_relpath":       "training/lora_runs/mit6824-lec08-v10-goldmatch-v7clean-r4-step240/v10_training_decision.json",
				"v10_primary_selected_count": 1,
				"v10_micro6_selected_count":  0,
				"training_readiness_status":  "not_ready_to_train",
				"training_readiness_relpath": "validation/mit6824_acceptance/lec-08/lecture08_training_readiness_v1.json",
				"strict_nonholdout_rows":     90,
				"strict_nonholdout_hours":    0.6581,
				"new_source_ids_vs_v7":       0,
				"threshold_relaxation_used":  false,
			},
			"lecture08_goldmatch_reference": gin.H{
				"id":                          "holdout_highfit_bestof_review_v1",
				"adapter_run_relpath":         "training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200",
				"adapter_relpath":             "training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200/adapter_latest.pt",
				"status":                      "current_training_process_reference_not_production_pass",
				"holdout_count":               12,
				"strict_pass_count":           10,
				"review_only_hard_fail_count": 2,
				"selected_above_5pct_fit":     0,
				"max_abs_drift_ms":            464.671,
				"max_duration_ratio_delta":    0.036012,
				"listening_pack_relpath":      "training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200/holdout_highfit_bestof_review_v1/listening_pack/index.html",
				"selection_rule":              "generated_audio_holdout_beats_training_loss; fit_delta<=0.05; ASR/join/tail hard gates",
			},
		},
		"runner_capabilities": gin.H{
			"smoke_run":                            "ml_service_sync_one_step",
			"smoke_writes_isolated_output":         true,
			"full_training":                        fullTraining,
			"full_training_synchronous_http":       false,
			"canary_loop":                          validationRunner,
			"regression_check":                     validationRunner,
			"production_adapter_overwrite_allowed": false,
		},
		"phase_gates": []gin.H{
			{"phase": "0_silver_smoke", "exit": "adapter loads, loss decreases, inference works, no quality claim"},
			{"phase": "1_gold_seed", "exit": "2-5h human-approved data with fixed holdout"},
			{"phase": "2_local_lora_smoke", "exit": "RTX 5080 smoke passes train/load/infer checks"},
			{"phase": "3_canary_regression", "exit": "canary objective gates and anchors pass"},
			{"phase": "4_episode_shadow", "exit": "full episode final-mix ASR and drift gates pass"},
			{"phase": "5_human_acceptance", "exit": "review pack accepted with no blocking segments"},
			{"phase": "6_opt_in_promotion", "exit": "adapter may be selected by explicit config only"},
		},
		"hard_rejection_reasons": []string{
			"bad_join_quality",
			"high_drift",
			"high_fit_delta",
			"low_asr",
			"low_term_recall",
			"tail_truncation",
		},
		"soft_override_reasons": []string{"flat_f0"},
		"naming_convention": gin.H{
			"pattern": "<domain>-<language>-<tier><hours>-r<rank>-s<step>-<gate>",
			"example": "raft-zh-gold2h-r8-s1200-canary84p-ep-pass",
		},
	}
}

func loraAdapterID(relPath string) string {
	id := strings.TrimSuffix(relPath, filepath.Ext(relPath))
	id = strings.NewReplacer("/", "-", "\\", "-", "_", "-").Replace(id)
	id = strings.Trim(id, "-")
	return strings.ToLower(id)
}

func shortFileSHA256(path string) string {
	info, err := os.Stat(path)
	if err != nil {
		return ""
	}
	// Adapter checkpoints can be hundreds of MB. The registry endpoint is a
	// browsing surface, so avoid turning every page load into a full checkpoint
	// checksum pass. A future write action can compute and persist strong hashes.
	if info.Size() > 1024*1024 {
		return ""
	}
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return ""
	}
	return hex.EncodeToString(hash.Sum(nil))[:16]
}

func resolveDataPathLike(dataRoot, raw string) string {
	value := strings.TrimSpace(raw)
	if value == "" {
		return ""
	}
	value = filepath.ToSlash(value)
	value = strings.TrimPrefix(value, "/data/")
	value = strings.TrimPrefix(value, "data/")
	if filepath.IsAbs(value) {
		return filepath.FromSlash(value)
	}
	return storage.ResolveDataPath(dataRoot, value)
}

func countJSONL(path string) int {
	if path == "" {
		return 0
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return 0
	}
	count := 0
	for _, line := range strings.Split(string(data), "\n") {
		if strings.TrimSpace(line) != "" {
			count++
		}
	}
	return count
}

func intFromJSONNumber(raw any) int {
	switch v := raw.(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	default:
		return 0
	}
}

func stringFromJSON(raw any) string {
	if value, ok := raw.(string); ok {
		return value
	}
	return ""
}
