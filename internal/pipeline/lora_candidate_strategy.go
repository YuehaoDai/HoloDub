package pipeline

import (
	"strings"

	pipettstts "holodub/internal/pipeline/tts"
)

const (
	loraCandidatePolicyVersion         = "main_flow_shadow_v9"
	loraCandidateOfflineBaselineID     = "episode147_v3h_raw_stable_tailfix_human_approved"
	loraCandidateRetryEscalationReason = "retry_escalation_after_default_nonconvergence"
)

var (
	loraCandidateBaseFamilies      = []string{"default"}
	loraCandidateSplitFamilies     = []string{"split_light_vector", "split_mix"}
	loraCandidateHardFailures      = []string{"bad_join_quality", "high_drift", "high_fit_delta", "low_asr", "low_term_recall", "tail_truncation"}
	loraCandidateSoftOverrideRules = []string{"flat_f0"}
)

type loraCandidatePlan struct {
	enabled                       bool
	targetSec                     float64
	textChars                     int
	charsPerSec                   float64
	durationEligible              bool
	textEligible                  bool
	denseEligible                 bool
	nearLongEligible              bool
	shortDenseEligible            bool
	shortTransitionEligible       bool
	shortTermNumberEligible       bool
	splitPlanning                 bool
	executionEnabled              bool
	minTargetSec                  float64
	minTextChars                  int
	denseMinTargetSec             float64
	denseMinCharsPerSec           float64
	nearLongMinTargetSec          float64
	nearLongMinTextChars          int
	shortDenseMinTargetSec        float64
	shortDenseMaxTargetSec        float64
	shortDenseMinCharsPerSec      float64
	shortTransitionMinTargetSec   float64
	shortTransitionMaxTargetSec   float64
	shortTransitionMinCharsPerSec float64
	shortTermNumberMinTargetSec   float64
	shortTermNumberMaxTargetSec   float64
	shortTermNumberMinCharsPerSec float64
	maxPerSegment                 int
}

func (s *Service) buildLoraCandidateShadowPlan(text string, targetSec float64) (map[string]any, bool) {
	if !s.cfg.LoraCandidateLoopEnabled {
		return nil, false
	}
	plan := s.newLoraCandidatePlan(text, targetSec)
	return plan.meta(), true
}

func (s *Service) maybeEscalateLoraCandidateForRetry(meta map[string]any, text string, targetSec float64, attempt int) map[string]any {
	if !s.cfg.LoraCandidateLoopEnabled ||
		!s.cfg.LoraCandidateExecutionEnabled ||
		!s.cfg.LoraCandidateRetryEscalationEnabled {
		return meta
	}
	minAttempt := s.cfg.LoraCandidateRetryEscalationMinAttempt
	if minAttempt < 1 {
		minAttempt = 2
	}
	if attempt < minAttempt {
		return meta
	}
	candidate, ok := loraCandidateMeta(meta)
	if !ok || candidate["enabled"] == true || candidate["execution_skipped_by_attempt_limit"] == true {
		return meta
	}

	chars := pipettstts.CountNonWhitespaceRunes(text)
	charsPerSec := 0.0
	if targetSec > 0 {
		charsPerSec = float64(chars) / targetSec
	}
	if !s.loraCandidateRetryEscalationEligible(targetSec, chars, charsPerSec) {
		return meta
	}

	next := copyDubbingMeta(meta)
	escalated := copyAnyMap(candidate)
	families := append([]string{}, loraCandidateBaseFamilies...)
	alternateFamilies := []string{}
	splitPlanning := s.cfg.LoraSplitPlanningEnabled
	if splitPlanning {
		alternateFamilies = append(alternateFamilies, loraCandidateSplitFamilies...)
		families = append(families, alternateFamilies...)
	}

	escalated["enabled"] = true
	escalated["phase"] = "main_flow_opt_in"
	escalated["execution_mode"] = "candidate_fallback_v1"
	escalated["candidate_execution_enabled"] = true
	escalated["audio_selection_mode"] = "candidate_audio"
	escalated["split_planning"] = splitPlanning
	escalated["split_policy"] = loraCandidateSplitPolicy(splitPlanning, true)
	escalated["candidate_families"] = families
	escalated["alternate_candidate_families"] = alternateFamilies
	escalated["selected_family"] = "default"
	escalated["selection_reason"] = loraCandidateRetryEscalationReason
	escalated["fallback_reason"] = ""

	eligibility := map[string]any{}
	if raw, ok := escalated["eligibility"].(map[string]any); ok {
		eligibility = copyAnyMap(raw)
	}
	eligibility["retry_escalation_eligible"] = true
	reasons := stringSliceFromAny(eligibility["reasons"])
	if !containsStringValue(reasons, loraCandidateRetryEscalationReason) {
		reasons = append(reasons, loraCandidateRetryEscalationReason)
	}
	eligibility["reasons"] = reasons
	escalated["eligibility"] = eligibility
	escalated["retry_escalation"] = map[string]any{
		"enabled":             true,
		"reason":              loraCandidateRetryEscalationReason,
		"attempt":             attempt,
		"min_attempt":         minAttempt,
		"target_sec":          targetSec,
		"text_chars":          chars,
		"chars_per_sec":       charsPerSec,
		"min_target_sec":      s.cfg.LoraCandidateRetryEscalationMinTargetSec,
		"max_target_sec":      s.cfg.LoraCandidateRetryEscalationMaxTargetSec,
		"min_text_chars":      s.cfg.LoraCandidateRetryEscalationMinTextChars,
		"min_chars_per_sec":   s.cfg.LoraCandidateRetryEscalationMinCharsPerSec,
		"source_plan_enabled": candidate["enabled"],
		"source_fallback":     candidate["fallback_reason"],
	}
	next["lora_candidate"] = escalated
	return next
}

func (s *Service) loraCandidateRetryEscalationEligible(targetSec float64, chars int, charsPerSec float64) bool {
	if s.cfg.LoraCandidateRetryEscalationMinTargetSec > 0 &&
		targetSec < s.cfg.LoraCandidateRetryEscalationMinTargetSec {
		return false
	}
	if s.cfg.LoraCandidateRetryEscalationMaxTargetSec > 0 &&
		targetSec > s.cfg.LoraCandidateRetryEscalationMaxTargetSec {
		return false
	}
	if s.cfg.LoraCandidateRetryEscalationMinTextChars > 0 &&
		chars < s.cfg.LoraCandidateRetryEscalationMinTextChars {
		return false
	}
	if s.cfg.LoraCandidateRetryEscalationMinCharsPerSec > 0 &&
		charsPerSec < s.cfg.LoraCandidateRetryEscalationMinCharsPerSec {
		return false
	}
	return true
}

func containsStringValue(values []string, want string) bool {
	for _, value := range values {
		if value == want {
			return true
		}
	}
	return false
}

func (s *Service) newLoraCandidatePlan(text string, targetSec float64) loraCandidatePlan {
	chars := pipettstts.CountNonWhitespaceRunes(text)
	durationEligible := s.cfg.LoraCandidateMinTargetSec <= 0 || targetSec >= s.cfg.LoraCandidateMinTargetSec
	textEligible := s.cfg.LoraCandidateMinTextChars <= 0 || chars >= s.cfg.LoraCandidateMinTextChars
	charsPerSec := 0.0
	if targetSec > 0 {
		charsPerSec = float64(chars) / targetSec
	}
	denseEligible := s.cfg.LoraCandidateDenseMinTargetSec > 0 &&
		s.cfg.LoraCandidateDenseMinCharsPerSec > 0 &&
		targetSec >= s.cfg.LoraCandidateDenseMinTargetSec &&
		charsPerSec >= s.cfg.LoraCandidateDenseMinCharsPerSec
	nearLongEligible := s.cfg.LoraCandidateNearLongMinTargetSec > 0 &&
		s.cfg.LoraCandidateNearLongMinTextChars > 0 &&
		targetSec >= s.cfg.LoraCandidateNearLongMinTargetSec &&
		chars >= s.cfg.LoraCandidateNearLongMinTextChars
	shortDenseEligible := s.cfg.LoraCandidateShortDenseMinTargetSec > 0 &&
		s.cfg.LoraCandidateShortDenseMaxTargetSec > 0 &&
		s.cfg.LoraCandidateShortDenseMinCharsPerSec > 0 &&
		targetSec >= s.cfg.LoraCandidateShortDenseMinTargetSec &&
		targetSec <= s.cfg.LoraCandidateShortDenseMaxTargetSec &&
		charsPerSec >= s.cfg.LoraCandidateShortDenseMinCharsPerSec
	shortTransitionEligible := s.cfg.LoraCandidateShortTransitionMinTargetSec > 0 &&
		s.cfg.LoraCandidateShortTransitionMaxTargetSec > 0 &&
		s.cfg.LoraCandidateShortTransitionMinCharsPerSec > 0 &&
		targetSec >= s.cfg.LoraCandidateShortTransitionMinTargetSec &&
		targetSec <= s.cfg.LoraCandidateShortTransitionMaxTargetSec &&
		charsPerSec >= s.cfg.LoraCandidateShortTransitionMinCharsPerSec &&
		loraCandidateLooksLikeShortTransitionQuestion(text)
	shortTermNumberEligible := s.cfg.LoraCandidateShortTermNumberMinTargetSec > 0 &&
		s.cfg.LoraCandidateShortTermNumberMaxTargetSec > 0 &&
		s.cfg.LoraCandidateShortTermNumberMinCharsPerSec > 0 &&
		targetSec >= s.cfg.LoraCandidateShortTermNumberMinTargetSec &&
		targetSec <= s.cfg.LoraCandidateShortTermNumberMaxTargetSec &&
		charsPerSec >= s.cfg.LoraCandidateShortTermNumberMinCharsPerSec &&
		loraCandidateLooksLikeShortTermNumber(text)
	maxPerSegment := s.cfg.LoraCandidateMaxPerSegment
	if maxPerSegment < 1 {
		maxPerSegment = 2
	}
	eligible := durationEligible || textEligible || denseEligible || nearLongEligible || shortDenseEligible || shortTransitionEligible || shortTermNumberEligible
	return loraCandidatePlan{
		enabled:                       eligible,
		targetSec:                     targetSec,
		textChars:                     chars,
		charsPerSec:                   charsPerSec,
		durationEligible:              durationEligible,
		textEligible:                  textEligible,
		denseEligible:                 denseEligible,
		nearLongEligible:              nearLongEligible,
		shortDenseEligible:            shortDenseEligible,
		shortTransitionEligible:       shortTransitionEligible,
		shortTermNumberEligible:       shortTermNumberEligible,
		splitPlanning:                 eligible && s.cfg.LoraSplitPlanningEnabled,
		executionEnabled:              eligible && s.cfg.LoraCandidateExecutionEnabled,
		minTargetSec:                  s.cfg.LoraCandidateMinTargetSec,
		minTextChars:                  s.cfg.LoraCandidateMinTextChars,
		denseMinTargetSec:             s.cfg.LoraCandidateDenseMinTargetSec,
		denseMinCharsPerSec:           s.cfg.LoraCandidateDenseMinCharsPerSec,
		nearLongMinTargetSec:          s.cfg.LoraCandidateNearLongMinTargetSec,
		nearLongMinTextChars:          s.cfg.LoraCandidateNearLongMinTextChars,
		shortDenseMinTargetSec:        s.cfg.LoraCandidateShortDenseMinTargetSec,
		shortDenseMaxTargetSec:        s.cfg.LoraCandidateShortDenseMaxTargetSec,
		shortDenseMinCharsPerSec:      s.cfg.LoraCandidateShortDenseMinCharsPerSec,
		shortTransitionMinTargetSec:   s.cfg.LoraCandidateShortTransitionMinTargetSec,
		shortTransitionMaxTargetSec:   s.cfg.LoraCandidateShortTransitionMaxTargetSec,
		shortTransitionMinCharsPerSec: s.cfg.LoraCandidateShortTransitionMinCharsPerSec,
		shortTermNumberMinTargetSec:   s.cfg.LoraCandidateShortTermNumberMinTargetSec,
		shortTermNumberMaxTargetSec:   s.cfg.LoraCandidateShortTermNumberMaxTargetSec,
		shortTermNumberMinCharsPerSec: s.cfg.LoraCandidateShortTermNumberMinCharsPerSec,
		maxPerSegment:                 maxPerSegment,
	}
}

func (p loraCandidatePlan) meta() map[string]any {
	families := append([]string{}, loraCandidateBaseFamilies...)
	alternateFamilies := []string{}
	if p.splitPlanning {
		alternateFamilies = append(alternateFamilies, loraCandidateSplitFamilies...)
		families = append(families, alternateFamilies...)
	}

	fallbackReason := "candidate_loop_shadow_only_default_audio"
	selectionReason := "candidate_execution_not_enabled_in_main_flow"
	phase := "main_flow_shadow"
	executionMode := "shadow_only"
	audioSelectionMode := "default_audio"
	candidateExecutionEnabled := false
	if p.executionEnabled {
		fallbackReason = ""
		selectionReason = "candidate_execution_requested"
		phase = "main_flow_opt_in"
		executionMode = "candidate_fallback_v1"
		audioSelectionMode = "candidate_audio"
		candidateExecutionEnabled = true
	}
	if !p.enabled {
		fallbackReason = "below_lora_candidate_threshold"
		selectionReason = "below_lora_candidate_threshold"
		phase = "main_flow_shadow"
		executionMode = "shadow_only"
		audioSelectionMode = "default_audio"
		candidateExecutionEnabled = false
	}

	return map[string]any{
		"schema_version":                      2,
		"policy_version":                      loraCandidatePolicyVersion,
		"offline_baseline_id":                 loraCandidateOfflineBaselineID,
		"enabled":                             p.enabled,
		"phase":                               phase,
		"execution_mode":                      executionMode,
		"candidate_execution_enabled":         candidateExecutionEnabled,
		"audio_selection_mode":                audioSelectionMode,
		"target_sec":                          p.targetSec,
		"text_chars":                          p.textChars,
		"chars_per_sec":                       p.charsPerSec,
		"min_target_sec":                      p.minTargetSec,
		"min_text_chars":                      p.minTextChars,
		"dense_min_target_sec":                p.denseMinTargetSec,
		"dense_min_chars_per_sec":             p.denseMinCharsPerSec,
		"near_long_min_target_sec":            p.nearLongMinTargetSec,
		"near_long_min_text_chars":            p.nearLongMinTextChars,
		"short_dense_min_target_sec":          p.shortDenseMinTargetSec,
		"short_dense_max_target_sec":          p.shortDenseMaxTargetSec,
		"short_dense_min_chars_per_sec":       p.shortDenseMinCharsPerSec,
		"short_transition_min_target_sec":     p.shortTransitionMinTargetSec,
		"short_transition_max_target_sec":     p.shortTransitionMaxTargetSec,
		"short_transition_min_chars_per_sec":  p.shortTransitionMinCharsPerSec,
		"short_term_number_min_target_sec":    p.shortTermNumberMinTargetSec,
		"short_term_number_max_target_sec":    p.shortTermNumberMaxTargetSec,
		"short_term_number_min_chars_per_sec": p.shortTermNumberMinCharsPerSec,
		"max_per_segment":                     p.maxPerSegment,
		"eligibility": map[string]any{
			"duration_eligible":          p.durationEligible,
			"text_eligible":              p.textEligible,
			"dense_eligible":             p.denseEligible,
			"near_long_eligible":         p.nearLongEligible,
			"short_dense_eligible":       p.shortDenseEligible,
			"short_transition_eligible":  p.shortTransitionEligible,
			"short_term_number_eligible": p.shortTermNumberEligible,
			"reasons":                    loraCandidateEligibilityReasons(p.durationEligible, p.textEligible, p.denseEligible, p.nearLongEligible, p.shortDenseEligible, p.shortTransitionEligible, p.shortTermNumberEligible),
		},
		"split_planning":               p.splitPlanning,
		"split_policy":                 loraCandidateSplitPolicy(p.splitPlanning, p.enabled),
		"candidate_families":           families,
		"alternate_candidate_families": alternateFamilies,
		"generation":                   loraCandidateGenerationPolicy(),
		"gate":                         loraCandidateGatePolicy(),
		"episode_assembly":             loraCandidateEpisodeAssemblyPolicy(),
		"quality_baseline":             loraCandidateQualityBaseline(),
		"risk_queue_policy":            loraCandidateRiskQueuePolicy(),
		"regression_anchors":           loraCandidateRegressionAnchors(),
		"fallback_policy":              loraCandidateFallbackPolicy(),
		"hard_rejection_reasons":       append([]string{}, loraCandidateHardFailures...),
		"soft_override_reasons":        append([]string{}, loraCandidateSoftOverrideRules...),
		"override_policy":              "soft_metrics_only",
		"tempo_fit_max_delta":          0.05,
		"production_strategy":          "opt_in_shadow_default_audio",
		"selected_family":              "default",
		"selection_reason":             selectionReason,
		"fallback_reason":              fallbackReason,
	}
}

func loraCandidateEligibilityReasons(durationEligible bool, textEligible bool, denseEligible bool, nearLongEligible bool, shortDenseEligible bool, shortTransitionEligible bool, shortTermNumberEligible bool) []string {
	reasons := []string{}
	if durationEligible {
		reasons = append(reasons, "target_sec_gte_min")
	}
	if textEligible {
		reasons = append(reasons, "text_chars_gte_min")
	}
	if denseEligible {
		reasons = append(reasons, "dense_mid_segment")
	}
	if nearLongEligible {
		reasons = append(reasons, "near_long_dense")
	}
	if shortDenseEligible {
		reasons = append(reasons, "short_dense_segment")
	}
	if shortTransitionEligible {
		reasons = append(reasons, "short_transition_question")
	}
	if shortTermNumberEligible {
		reasons = append(reasons, "short_term_number_anchor")
	}
	if len(reasons) == 0 {
		reasons = append(reasons, "below_target_sec_text_chars_dense_near_long_short_transition_and_term_number_risk")
	}
	return reasons
}

func loraCandidateLooksLikeShortTransitionQuestion(text string) bool {
	compact := strings.NewReplacer(" ", "", "\n", "", "\r", "", "\t", "").Replace(text)
	q := strings.Index(compact, "？")
	qLen := len("？")
	if q < 0 {
		q = strings.Index(compact, "?")
		qLen = len("?")
	}
	if q < 0 {
		return false
	}
	before := compact[:q]
	after := compact[q+qLen:]
	hasQuestionCue := strings.Contains(before, "疑问") ||
		strings.Contains(before, "问题") ||
		strings.Contains(before, "还有什么")
	hasContinuation := strings.Contains(after, "好的") ||
		strings.Contains(after, "好，") ||
		strings.Contains(after, "好,")
	hasContinueVerb := strings.Contains(after, "继续") ||
		strings.Contains(after, "接着") ||
		strings.Contains(after, "进入")
	return hasQuestionCue && hasContinuation && hasContinueVerb
}

func loraCandidateLooksLikeShortTermNumber(text string) bool {
	compact := strings.NewReplacer(" ", "", "\n", "", "\r", "", "\t", "").Replace(text)
	marker := "\u53f7\u4efb\u671f"
	idx := strings.Index(compact, marker)
	if idx <= 0 {
		return false
	}
	prefix := []rune(compact[:idx])
	if len(prefix) == 0 {
		return false
	}
	last := prefix[len(prefix)-1]
	if last >= '0' && last <= '9' {
		return true
	}
	if last >= '\uff10' && last <= '\uff19' {
		return true
	}
	switch last {
	case '\u96f6', '\u3007', '\u4e00', '\u4e8c', '\u4e24', '\u4e09', '\u56db', '\u4e94', '\u516d', '\u4e03', '\u516b', '\u4e5d', '\u5341':
		return true
	default:
		return false
	}
}

func loraCandidateGenerationPolicy() map[string]any {
	return map[string]any{
		"seed":                           1234,
		"temperature":                    0.8,
		"top_p":                          0.8,
		"top_k":                          30,
		"repetition_penalty":             10.0,
		"length_penalty":                 0.0,
		"duration_control_mode":          "embedding",
		"duration_control_min_sec":       0.05,
		"duration_token_rate_policy":     "piecewise",
		"duration_token_rate":            23.4375,
		"duration_piecewise_token_rates": "15:23.4375,23:30,default:32.5",
		"max_text_tokens_per_segment":    600,
		"disable_text_chunking":          true,
		"split_max_mel_headroom":         512,
		"forbidden_default_families":     []string{"split_light_vector_10"},
	}
}

func loraCandidateGatePolicy() map[string]any {
	return map[string]any{
		"max_drift_ms":                500,
		"min_similarity":              0.97,
		"target_similarity":           0.985,
		"min_f0_range":                4.5,
		"target_f0_range":             5.4,
		"min_critical_term_recall":    0.8,
		"target_critical_term_recall": 0.95,
		"max_fit_delta":               0.05,
		"allow_failed_join_quality":   false,
		"hard_failure_reasons":        append([]string{}, loraCandidateHardFailures...),
		"soft_override_reasons":       append([]string{}, loraCandidateSoftOverrideRules...),
	}
}

func loraCandidateEpisodeAssemblyPolicy() map[string]any {
	return map[string]any{
		"duration_anchor":                       "chapter_manifest_or_bgm",
		"duration_lock_required":                true,
		"mix_length_owner":                      "voice_track",
		"baseline_final_video_duration_ms":      4653810,
		"baseline_voice_mix_duration_ms":        4653810,
		"baseline_extracted_audio_duration_ms":  4653824,
		"baseline_extracted_audio_padding_ms":   14,
		"baseline_unmodified_ranges_verified":   true,
		"baseline_final_audio_abs_drift_ms":     14,
		"baseline_final_video_abs_drift_ms":     0,
		"baseline_final_video_duration_source":  "ffprobe",
		"baseline_extracted_audio_duration_src": "ffprobe",
	}
}

func loraCandidateQualityBaseline() map[string]any {
	return map[string]any{
		"id":                         loraCandidateOfflineBaselineID,
		"status":                     "human_approved",
		"episode_id":                 147,
		"review_pack":                "human_review_pack_v3h_raw_stable_tailfix_exact",
		"total_segments":             180,
		"ok_segments":                180,
		"mean_asr":                   0.9781,
		"weighted_asr":               0.9761,
		"full_transcript_similarity": 0.9764,
		"p10_asr":                    0.9498,
		"min_asr":                    0.7807,
		"low_similarity_count":       1,
		"very_low_similarity_count":  0,
		"empty_asr_count":            0,
		"asr_failed_count":           0,
		"weird_asr_count":            0,
		"selected_replacements":      8,
		"join_quality_failures":      0,
		"accepted_low_asr_segments": []map[string]any{
			{
				"sample_id":          "seg10835",
				"similarity":         0.7807,
				"selected_candidate": "split4_tail_correct_atempo",
				"reason":             "human_listening_accepted_tail_fix",
			},
		},
		"prior_canary_baseline": map[string]any{
			"id":                     "canary84_full84_selected_combined_auto_v2_listening_override",
			"mean_abs_drift_ms":      39.16,
			"max_abs_drift_ms":       298.73,
			"mean_asr":               0.9931,
			"min_asr":                0.9625,
			"join_quality_failures":  0,
			"listening_override_seg": "seg10569",
		},
	}
}

func loraCandidateRiskQueuePolicy() []string {
	return []string{
		"listening_override_applied",
		"fit_tempo_delta_gt_0.05",
		"fit_tempo_delta_gt_0.08",
		"fallback_tier_not_strict",
		"min_f0_lt_4.8",
		"similarity_lt_0.985",
		"latin_surface_recall_lt_1.0",
	}
}

func loraCandidateRegressionAnchors() []map[string]any {
	return []map[string]any{
		{
			"sample_id":                "seg10569",
			"required_selected_label":  "shard01_selected",
			"comparison_only_label":    "split_light_vector_10",
			"reason":                   "human_listening_override_preferred_shard01",
			"hard_gate_override_scope": "soft_metrics_only",
		},
		{
			"sample_id":               "seg10686",
			"required_label_contains": "split_mix",
			"reason":                  "long_segment_split_mix_success_path",
		},
		{
			"sample_id": "seg10518",
			"reason":    "pause_boundary_regression_anchor",
		},
		{
			"sample_id":               "seg10694",
			"required_label_contains": "whole_wuhao_space",
			"reason":                  "episode147_human_approved_five_hao_not_five_ri",
		},
		{
			"sample_id":               "seg10700",
			"required_label_contains": "source_atempo_fit",
			"reason":                  "episode147_human_approved_tail_and_qianyirizhi_watch",
		},
		{
			"sample_id":               "seg10701",
			"required_label_contains": "split2_pause_words_1p8",
			"reason":                  "episode147_human_approved_pause_before_yihao_node",
		},
		{
			"sample_id":               "seg10702",
			"required_label_contains": "split5_original_smoother",
			"reason":                  "episode147_human_approved_quality_smoother",
		},
		{
			"sample_id":               "seg10786",
			"required_label_contains": "whole_retry",
			"reason":                  "episode147_human_approved_tail_completion",
		},
		{
			"sample_id":               "seg10808",
			"required_label_contains": "whole_raft_spaced",
			"reason":                  "episode147_human_approved_raft_surface",
		},
		{
			"sample_id":               "seg10835",
			"required_label_contains": "split4_tail_correct_atempo",
			"reason":                  "episode147_human_approved_low_asr_tail_fix",
			"accepted_low_asr":        true,
		},
		{
			"sample_id":               "seg10866",
			"required_label_contains": "split3_no_tail",
			"reason":                  "episode147_human_approved_no_hallucinated_tail",
		},
	}
}

func loraCandidateSplitPolicy(enabled bool, eligible bool) map[string]any {
	policy := map[string]any{
		"enabled":                  enabled,
		"pause_sec":                0.18,
		"max_part_sec":             16.0,
		"max_parts":                4,
		"preserve_semantic_edge":   true,
		"join_quality_required":    true,
		"enable_rescue_strategies": enabled && eligible,
		"part_candidate_families": []string{
			"split_emotext",
			"split_light_vector",
		},
		"mix_family": "split_mix",
		"rescue_strategies": []map[string]any{
			{
				"name":         "balanced_four_part",
				"max_parts":    4,
				"max_part_sec": 10.5,
				"pause_sec":    0.18,
			},
			{
				"name":         "micro_clause_six_part",
				"max_parts":    6,
				"max_part_sec": 8.5,
				"pause_sec":    0.18,
			},
			{
				"name":                  "micro_clause_tight_concat",
				"max_parts":             6,
				"max_part_sec":          8.5,
				"pause_sec":             0.18,
				"concat_pause_override": 0.04,
			},
			{
				"name":         "micro_clause_tight_pause",
				"max_parts":    6,
				"max_part_sec": 8.5,
				"pause_sec":    0.04,
			},
			{
				"name":                  "micro_clause_zero_concat",
				"max_parts":             6,
				"max_part_sec":          8.5,
				"pause_sec":             0.18,
				"concat_pause_override": 0.0,
			},
			{
				"name":         "micro_clause_zero_pause",
				"max_parts":    6,
				"max_part_sec": 8.5,
				"pause_sec":    0.0,
			},
			{
				"name":         "semantic_two_part",
				"max_parts":    2,
				"max_part_sec": 999.0,
				"pause_sec":    0.18,
			},
		},
	}
	if !enabled && eligible {
		policy["disabled_reason"] = "lora_split_planning_disabled"
	}
	return policy
}

func loraCandidateFallbackPolicy() map[string]any {
	return map[string]any{
		"default_audio_on_shadow": true,
		"candidate_failure_fallback_reasons": []string{
			"asr_failed",
			"prosody_failed",
			"split_join_failed",
			"all_candidates_rejected",
			"candidate_execution_not_available",
		},
		"hard_failures_cannot_be_overridden": append([]string{}, loraCandidateHardFailures...),
		"listening_override_scope":           "soft_metrics_only",
	}
}
