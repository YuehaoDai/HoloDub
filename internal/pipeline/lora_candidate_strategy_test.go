package pipeline

import (
	"context"
	"strings"
	"testing"

	"holodub/internal/config"
	"holodub/internal/models"
)

func TestAttachLoraCandidatePlanDefaultOffLeavesMetaUnchanged(t *testing.T) {
	base := map[string]any{"pacing": "normal"}
	svc := &Service{cfg: config.Config{}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(base, "short text", 30)

	if _, ok := got["lora_candidate"]; ok {
		t.Fatalf("lora_candidate meta present when disabled: %+v", got)
	}
	if got["pacing"] != "normal" {
		t.Fatalf("base meta changed: %+v", got)
	}
}

func TestAttachLoraCandidatePlanSkipsShortSegments(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:   true,
		LoraSplitPlanningEnabled:   true,
		LoraCandidateMinTargetSec:  18,
		LoraCandidateMinTextChars:  180,
		LoraCandidateMaxPerSegment: 2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, "short text", 5)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != false {
		t.Fatalf("short segment should be disabled: %+v", meta)
	}
	if meta["fallback_reason"] != "below_lora_candidate_threshold" {
		t.Fatalf("unexpected fallback reason: %+v", meta)
	}
	if meta["selected_family"] != "default" {
		t.Fatalf("short segment should keep default selected family: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != false || meta["audio_selection_mode"] != "default_audio" {
		t.Fatalf("short segment should stay on default audio path: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false || eligibility["text_eligible"] != false {
		t.Fatalf("short segment eligibility = %+v, want both false", eligibility)
	}
	if eligibility["dense_eligible"] != false {
		t.Fatalf("short segment dense eligibility = %+v, want false", eligibility)
	}
}

func TestAttachLoraCandidatePlanEnablesDenseMidSegments(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:         true,
		LoraCandidateExecutionEnabled:    true,
		LoraSplitPlanningEnabled:         true,
		LoraCandidateMinTargetSec:        18,
		LoraCandidateMinTextChars:        180,
		LoraCandidateDenseMinTargetSec:   12,
		LoraCandidateDenseMinCharsPerSec: 5.0,
		LoraCandidateMaxPerSegment:       2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("字", 63), 12.58)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("dense mid segment should be enabled: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("dense mid segment should request candidate audio when execution is enabled: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false || eligibility["text_eligible"] != false || eligibility["dense_eligible"] != true {
		t.Fatalf("dense mid eligibility = %+v, want dense only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"dense_mid_segment"})
	if got := meta["policy_version"]; got != "main_flow_shadow_v9" {
		t.Fatalf("policy version = %v, want main_flow_shadow_v9", got)
	}
}

func TestAttachLoraCandidatePlanEnablesDenseShortSegments(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:         true,
		LoraCandidateExecutionEnabled:    true,
		LoraSplitPlanningEnabled:         true,
		LoraCandidateMinTargetSec:        18,
		LoraCandidateMinTextChars:        180,
		LoraCandidateDenseMinTargetSec:   9,
		LoraCandidateDenseMinCharsPerSec: 5.0,
		LoraCandidateMaxPerSegment:       2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("字", 54), 9.86)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("dense short segment should be enabled: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("dense short segment should request candidate audio: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false || eligibility["text_eligible"] != false || eligibility["dense_eligible"] != true {
		t.Fatalf("dense short eligibility = %+v, want dense only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"dense_mid_segment"})
}

func TestAttachLoraCandidatePlanCatchesSource10733DenseGap(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:         true,
		LoraCandidateExecutionEnabled:    true,
		LoraSplitPlanningEnabled:         true,
		LoraCandidateMinTargetSec:        18,
		LoraCandidateMinTextChars:        180,
		LoraCandidateDenseMinTargetSec:   9,
		LoraCandidateDenseMinCharsPerSec: 4.9,
		LoraCandidateMaxPerSegment:       2,
	}}

	// episode147 source10733 measured 72 speakable chars / 14.55s = 4.95 cps.
	// At the previous 5.0 floor it stayed on the default path and drifted -488ms;
	// direct ML smoke showed split_light_vector at +18ms with ASR 1.0.
	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("x", 72), 14.55)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("source10733-shaped dense segment should be enabled: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("source10733-shaped dense segment should request candidate audio: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false ||
		eligibility["text_eligible"] != false ||
		eligibility["dense_eligible"] != true ||
		eligibility["near_long_eligible"] != false {
		t.Fatalf("source10733-shaped eligibility = %+v, want dense only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"dense_mid_segment"})
	if got := meta["dense_min_chars_per_sec"]; got != 4.9 {
		t.Fatalf("dense floor = %v, want 4.9", got)
	}
}

func TestAttachLoraCandidatePlanEnablesNearLongDenseSegments(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:          true,
		LoraCandidateExecutionEnabled:     true,
		LoraSplitPlanningEnabled:          true,
		LoraCandidateMinTargetSec:         18,
		LoraCandidateMinTextChars:         180,
		LoraCandidateDenseMinTargetSec:    9,
		LoraCandidateDenseMinCharsPerSec:  5.0,
		LoraCandidateNearLongMinTargetSec: 16,
		LoraCandidateNearLongMinTextChars: 70,
		LoraCandidateMaxPerSegment:        2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("x", 76), 17.14)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("near-long dense segment should be enabled: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("near-long segment should request candidate audio: %+v", meta)
	}
	if meta["near_long_min_target_sec"] != 16.0 || meta["near_long_min_text_chars"] != 70 {
		t.Fatalf("near-long thresholds missing from meta: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false ||
		eligibility["text_eligible"] != false ||
		eligibility["dense_eligible"] != false ||
		eligibility["near_long_eligible"] != true {
		t.Fatalf("near-long eligibility = %+v, want near-long only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"near_long_dense"})
}

func TestAttachLoraCandidatePlanCatchesShortTransitionQuestion(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:                   true,
		LoraCandidateExecutionEnabled:              true,
		LoraSplitPlanningEnabled:                   true,
		LoraCandidateMinTargetSec:                  18,
		LoraCandidateMinTextChars:                  180,
		LoraCandidateDenseMinTargetSec:             9,
		LoraCandidateDenseMinCharsPerSec:           4.9,
		LoraCandidateNearLongMinTargetSec:          16,
		LoraCandidateNearLongMinTextChars:          70,
		LoraCandidateShortTransitionMinTargetSec:   5,
		LoraCandidateShortTransitionMaxTargetSec:   10,
		LoraCandidateShortTransitionMinCharsPerSec: 5.0,
		LoraCandidateMaxPerSegment:                 2,
	}}

	text := "关于领导者选举限制这一关键机制，大家还有什么疑问吗？好的，我们就继续讲解。"
	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, text, 6.5)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("short transition question should be enabled: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("short transition question should request candidate audio: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false ||
		eligibility["text_eligible"] != false ||
		eligibility["dense_eligible"] != false ||
		eligibility["near_long_eligible"] != false ||
		eligibility["short_transition_eligible"] != true {
		t.Fatalf("short transition eligibility = %+v, want short transition only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"short_transition_question"})
}

func TestAttachLoraCandidatePlanCatchesShortDenseSegment(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:                   true,
		LoraCandidateExecutionEnabled:              true,
		LoraSplitPlanningEnabled:                   true,
		LoraCandidateMinTargetSec:                  18,
		LoraCandidateMinTextChars:                  180,
		LoraCandidateDenseMinTargetSec:             9,
		LoraCandidateDenseMinCharsPerSec:           4.9,
		LoraCandidateNearLongMinTargetSec:          16,
		LoraCandidateNearLongMinTextChars:          70,
		LoraCandidateShortDenseMinTargetSec:        6,
		LoraCandidateShortDenseMaxTargetSec:        9,
		LoraCandidateShortDenseMinCharsPerSec:      5.5,
		LoraCandidateShortTransitionMinTargetSec:   5,
		LoraCandidateShortTransitionMaxTargetSec:   10,
		LoraCandidateShortTransitionMinCharsPerSec: 5.0,
		LoraCandidateMaxPerSegment:                 2,
	}}

	text := "它收到客户端请求后，立即发送首个Append Entries RPC，对吧？然后呢，哦，"
	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, text, 7.1)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("short dense segment should be enabled: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false ||
		eligibility["text_eligible"] != false ||
		eligibility["dense_eligible"] != false ||
		eligibility["near_long_eligible"] != false ||
		eligibility["short_dense_eligible"] != true {
		t.Fatalf("short dense eligibility = %+v, want short dense only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"short_dense_segment"})
}

func TestAttachLoraCandidatePlanCatchesShortTermNumberAnchor(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:                   true,
		LoraCandidateExecutionEnabled:              true,
		LoraSplitPlanningEnabled:                   true,
		LoraCandidateMinTargetSec:                  18,
		LoraCandidateMinTextChars:                  180,
		LoraCandidateDenseMinTargetSec:             9,
		LoraCandidateDenseMinCharsPerSec:           4.9,
		LoraCandidateNearLongMinTargetSec:          16,
		LoraCandidateNearLongMinTextChars:          70,
		LoraCandidateShortDenseMinTargetSec:        6,
		LoraCandidateShortDenseMaxTargetSec:        9,
		LoraCandidateShortDenseMinCharsPerSec:      5.5,
		LoraCandidateShortTransitionMinTargetSec:   5,
		LoraCandidateShortTransitionMaxTargetSec:   10,
		LoraCandidateShortTransitionMinCharsPerSec: 5.0,
		LoraCandidateShortTermNumberMinTargetSec:   6,
		LoraCandidateShortTermNumberMaxTargetSec:   9,
		LoraCandidateShortTermNumberMinCharsPerSec: 4.8,
		LoraCandidateMaxPerSegment:                 2,
	}}

	// Mirrors episode147 source10694: 7.52s, about 5.05 chars/sec, and
	// "five hao term" must not stay on the default path just because it is short.
	text := strings.Repeat("x", 34) + "\u4e94 \u53f7\u4efb\u671f"
	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, text, 7.52)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("short term-number anchor should be enabled: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("short term-number anchor should request candidate audio: %+v", meta)
	}
	eligibility, ok := meta["eligibility"].(map[string]any)
	if !ok {
		t.Fatalf("missing eligibility: %+v", meta)
	}
	if eligibility["duration_eligible"] != false ||
		eligibility["text_eligible"] != false ||
		eligibility["dense_eligible"] != false ||
		eligibility["near_long_eligible"] != false ||
		eligibility["short_dense_eligible"] != false ||
		eligibility["short_transition_eligible"] != false ||
		eligibility["short_term_number_eligible"] != true {
		t.Fatalf("short term-number eligibility = %+v, want term-number only", eligibility)
	}
	reasons, ok := eligibility["reasons"].([]string)
	if !ok {
		t.Fatalf("missing eligibility reasons: %+v", eligibility)
	}
	assertStringSliceEqual(t, reasons, []string{"short_term_number_anchor"})
	if meta["short_term_number_min_chars_per_sec"] != 4.8 {
		t.Fatalf("term-number threshold missing from meta: %+v", meta)
	}
}

func TestAttachLoraCandidatePlanDoesNotTreatPlainShortQuestionAsTransition(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:                   true,
		LoraCandidateExecutionEnabled:              true,
		LoraSplitPlanningEnabled:                   true,
		LoraCandidateMinTargetSec:                  18,
		LoraCandidateMinTextChars:                  180,
		LoraCandidateDenseMinTargetSec:             9,
		LoraCandidateDenseMinCharsPerSec:           4.9,
		LoraCandidateNearLongMinTargetSec:          16,
		LoraCandidateNearLongMinTextChars:          70,
		LoraCandidateShortTransitionMinTargetSec:   5,
		LoraCandidateShortTransitionMaxTargetSec:   10,
		LoraCandidateShortTransitionMinCharsPerSec: 5.0,
		LoraCandidateMaxPerSegment:                 2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, "这里还有什么问题吗？", 6.5)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != false {
		t.Fatalf("plain short question should stay on default path: %+v", meta)
	}
	eligibility := meta["eligibility"].(map[string]any)
	if eligibility["short_transition_eligible"] != false {
		t.Fatalf("plain short question should not be short-transition eligible: %+v", eligibility)
	}
}

func TestAttachLoraCandidatePlanRecordsSplitFamiliesForLongSegments(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:   true,
		LoraSplitPlanningEnabled:   true,
		LoraCandidateMinTargetSec:  18,
		LoraCandidateMinTextChars:  180,
		LoraCandidateMaxPerSegment: 2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, "long high risk text", 21)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true {
		t.Fatalf("long segment should be enabled: %+v", meta)
	}
	if meta["phase"] != "main_flow_shadow" || meta["execution_mode"] != "shadow_only" {
		t.Fatalf("unexpected shadow plan identity: %+v", meta)
	}
	if meta["policy_version"] != loraCandidatePolicyVersion {
		t.Fatalf("unexpected policy version: %+v", meta)
	}
	if meta["offline_baseline_id"] != loraCandidateOfflineBaselineID {
		t.Fatalf("unexpected offline baseline: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != false || meta["audio_selection_mode"] != "default_audio" {
		t.Fatalf("main flow v0.5 must not replace production audio: %+v", meta)
	}
	if meta["split_planning"] != true {
		t.Fatalf("split planning should be enabled: %+v", meta)
	}
	families, ok := meta["candidate_families"].([]string)
	if !ok {
		t.Fatalf("candidate families missing: %+v", meta)
	}
	assertStringSliceEqual(t, families, []string{"default", "split_light_vector", "split_mix"})
	alternates, ok := meta["alternate_candidate_families"].([]string)
	if !ok {
		t.Fatalf("alternate candidate families missing: %+v", meta)
	}
	assertStringSliceEqual(t, alternates, []string{"split_light_vector", "split_mix"})
	splitPolicy, ok := meta["split_policy"].(map[string]any)
	if !ok {
		t.Fatalf("missing split policy: %+v", meta)
	}
	if splitPolicy["enabled"] != true || splitPolicy["join_quality_required"] != true {
		t.Fatalf("split policy must require join quality: %+v", splitPolicy)
	}
	if splitPolicy["enable_rescue_strategies"] != true {
		t.Fatalf("eligible split policy should expose rescue strategies to ML service: %+v", splitPolicy)
	}
	if splitPolicy["max_parts"] != 4 {
		t.Fatalf("base split max_parts drifted: %+v", splitPolicy)
	}
	rescueStrategies, ok := splitPolicy["rescue_strategies"].([]map[string]any)
	if !ok || len(rescueStrategies) == 0 {
		t.Fatalf("missing rescue strategy contract: %+v", splitPolicy)
	}
	var foundMicroSix bool
	for _, strategy := range rescueStrategies {
		if strategy["name"] == "micro_clause_six_part" {
			foundMicroSix = true
			if strategy["max_parts"] != 6 || strategy["max_part_sec"] != 8.5 {
				t.Fatalf("micro rescue strategy drifted: %+v", strategy)
			}
		}
	}
	if !foundMicroSix {
		t.Fatalf("micro rescue strategy missing: %+v", rescueStrategies)
	}
	generation, ok := meta["generation"].(map[string]any)
	if !ok {
		t.Fatalf("missing generation policy: %+v", meta)
	}
	if generation["duration_token_rate_policy"] != "piecewise" || generation["seed"] != 1234 {
		t.Fatalf("unexpected generation policy: %+v", generation)
	}
	fallbackPolicy, ok := meta["fallback_policy"].(map[string]any)
	if !ok {
		t.Fatalf("missing fallback policy: %+v", meta)
	}
	if fallbackPolicy["default_audio_on_shadow"] != true {
		t.Fatalf("shadow mode must keep default audio fallback: %+v", fallbackPolicy)
	}
	assembly, ok := meta["episode_assembly"].(map[string]any)
	if !ok {
		t.Fatalf("missing episode assembly policy: %+v", meta)
	}
	if assembly["baseline_final_video_duration_ms"] != 4653810 || assembly["baseline_final_video_abs_drift_ms"] != 0 {
		t.Fatalf("episode147 assembly baseline drifted: %+v", assembly)
	}
	if assembly["baseline_extracted_audio_padding_ms"] != 14 {
		t.Fatalf("unexpected AAC/extracted audio padding baseline: %+v", assembly)
	}
	baseline, ok := meta["quality_baseline"].(map[string]any)
	if !ok {
		t.Fatalf("missing quality baseline: %+v", meta)
	}
	if baseline["id"] != loraCandidateOfflineBaselineID {
		t.Fatalf("baseline id drifted: %+v", baseline)
	}
	if baseline["status"] != "human_approved" || baseline["episode_id"] != 147 {
		t.Fatalf("baseline identity drifted: %+v", baseline)
	}
	if baseline["mean_asr"] != 0.9781 || baseline["weighted_asr"] != 0.9761 {
		t.Fatalf("baseline metrics drifted: %+v", baseline)
	}
	if baseline["min_asr"] != 0.7807 || baseline["low_similarity_count"] != 1 {
		t.Fatalf("accepted low-ASR risk drifted: %+v", baseline)
	}
	if baseline["join_quality_failures"] != 0 {
		t.Fatalf("join quality baseline drifted: %+v", baseline)
	}
}

func TestAttachLoraCandidatePlanMarksOptInExecutionWhenEnabled(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:      true,
		LoraCandidateExecutionEnabled: true,
		LoraSplitPlanningEnabled:      true,
		LoraCandidateMinTargetSec:     18,
		LoraCandidateMinTextChars:     180,
		LoraCandidateMaxPerSegment:    2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("risk ", 50), 21)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["phase"] != "main_flow_opt_in" || meta["execution_mode"] != "candidate_fallback_v1" {
		t.Fatalf("unexpected opt-in identity: %+v", meta)
	}
	if meta["candidate_execution_enabled"] != true || meta["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("execution should be requested only for eligible opt-in segments: %+v", meta)
	}
	if meta["selection_reason"] != "candidate_execution_requested" || meta["fallback_reason"] != "" {
		t.Fatalf("unexpected opt-in selection state before runtime: %+v", meta)
	}
	splitPolicy := meta["split_policy"].(map[string]any)
	if splitPolicy["enable_rescue_strategies"] != true {
		t.Fatalf("opt-in execution must pass rescue strategy flag to ML: %+v", splitPolicy)
	}
}

func TestMaybeEscalateLoraCandidateForRetryEnablesShortNonConvergingSegment(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:                   true,
		LoraCandidateExecutionEnabled:              true,
		LoraSplitPlanningEnabled:                   true,
		LoraCandidateMinTargetSec:                  18,
		LoraCandidateMinTextChars:                  180,
		LoraCandidateShortDenseMinTargetSec:        6,
		LoraCandidateShortDenseMaxTargetSec:        9,
		LoraCandidateShortDenseMinCharsPerSec:      5.5,
		LoraCandidateMaxPerSegment:                 2,
		LoraCandidateRetryEscalationEnabled:        true,
		LoraCandidateRetryEscalationMinAttempt:     2,
		LoraCandidateRetryEscalationMinTargetSec:   5,
		LoraCandidateRetryEscalationMaxTargetSec:   12,
		LoraCandidateRetryEscalationMinTextChars:   28,
		LoraCandidateRetryEscalationMinCharsPerSec: 4.5,
	}}
	text := "然后还有一个并发请求，它可能稍晚一点才发出，要求把变量x的值设置为2。"
	base := svc.attachLoraCandidatePlanToDubbingMeta(nil, text, 7.34)
	initial := base["lora_candidate"].(map[string]any)
	if initial["enabled"] != false {
		t.Fatalf("fixture should start below candidate threshold: %+v", initial)
	}

	notYet := svc.maybeEscalateLoraCandidateForRetry(base, text, 7.34, 1)
	notYetMeta := notYet["lora_candidate"].(map[string]any)
	if notYetMeta["enabled"] != false {
		t.Fatalf("attempt before threshold should not escalate: %+v", notYetMeta)
	}

	got := svc.maybeEscalateLoraCandidateForRetry(base, text, 7.34, 2)
	meta := got["lora_candidate"].(map[string]any)
	if meta["enabled"] != true || meta["candidate_execution_enabled"] != true {
		t.Fatalf("retry escalation should enable candidate execution: %+v", meta)
	}
	if meta["selection_reason"] != loraCandidateRetryEscalationReason || meta["fallback_reason"] != "" {
		t.Fatalf("retry escalation reason not recorded: %+v", meta)
	}
	families := meta["candidate_families"].([]string)
	assertStringSliceContains(t, families, "split_light_vector")
	assertStringSliceContains(t, families, "split_mix")
	splitPolicy := meta["split_policy"].(map[string]any)
	if splitPolicy["enable_rescue_strategies"] != true {
		t.Fatalf("retry escalation must enable rescue strategies: %+v", splitPolicy)
	}
	eligibility := meta["eligibility"].(map[string]any)
	if eligibility["retry_escalation_eligible"] != true {
		t.Fatalf("retry escalation eligibility missing: %+v", eligibility)
	}
	reasons := eligibility["reasons"].([]string)
	assertStringSliceContains(t, reasons, loraCandidateRetryEscalationReason)
	retry := meta["retry_escalation"].(map[string]any)
	if retry["attempt"] != 2 || retry["source_fallback"] != "below_lora_candidate_threshold" {
		t.Fatalf("retry escalation evidence missing: %+v", retry)
	}
}

func TestMaybeEscalateLoraCandidateForRetryKeepsLowDensityShortSegmentDefault(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:                   true,
		LoraCandidateExecutionEnabled:              true,
		LoraSplitPlanningEnabled:                   true,
		LoraCandidateMinTargetSec:                  18,
		LoraCandidateMinTextChars:                  180,
		LoraCandidateMaxPerSegment:                 2,
		LoraCandidateRetryEscalationEnabled:        true,
		LoraCandidateRetryEscalationMinAttempt:     2,
		LoraCandidateRetryEscalationMinTargetSec:   5,
		LoraCandidateRetryEscalationMaxTargetSec:   12,
		LoraCandidateRetryEscalationMinTextChars:   28,
		LoraCandidateRetryEscalationMinCharsPerSec: 4.5,
	}}
	text := "这是一个并不拥挤的普通短句。"
	base := svc.attachLoraCandidatePlanToDubbingMeta(nil, text, 8.0)

	got := svc.maybeEscalateLoraCandidateForRetry(base, text, 8.0, 4)
	meta := got["lora_candidate"].(map[string]any)
	if meta["enabled"] != false {
		t.Fatalf("low-density short segment should remain default: %+v", meta)
	}
}

func TestAttachLoraCandidatePlanRecordsSplitDisabledReason(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:   true,
		LoraSplitPlanningEnabled:   false,
		LoraCandidateMinTargetSec:  18,
		LoraCandidateMinTextChars:  180,
		LoraCandidateMaxPerSegment: 2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("risk ", 50), 21)
	meta, ok := got["lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing lora_candidate meta: %+v", got)
	}
	if meta["enabled"] != true || meta["split_planning"] != false {
		t.Fatalf("unexpected split-disabled candidate plan: %+v", meta)
	}
	families, ok := meta["candidate_families"].([]string)
	if !ok {
		t.Fatalf("candidate families missing: %+v", meta)
	}
	assertStringSliceEqual(t, families, []string{"default"})
	splitPolicy, ok := meta["split_policy"].(map[string]any)
	if !ok {
		t.Fatalf("missing split policy: %+v", meta)
	}
	if splitPolicy["disabled_reason"] != "lora_split_planning_disabled" {
		t.Fatalf("missing split disabled reason: %+v", splitPolicy)
	}
	if splitPolicy["enable_rescue_strategies"] != false {
		t.Fatalf("disabled split policy must not enable rescue strategies: %+v", splitPolicy)
	}
}

func TestTTSProvenanceIncludesLoraCandidateShadowPlanWhenEnabled(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:   true,
		LoraSplitPlanningEnabled:   true,
		LoraCandidateMinTargetSec:  18,
		LoraCandidateMinTextChars:  180,
		LoraCandidateMaxPerSegment: 2,
	}}

	updates := svc.buildTTSProvenanceMeta(strings.Repeat("risk ", 50), 21, "voice_profile", nil)
	meta, ok := updates["tts_lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing tts_lora_candidate provenance: %+v", updates)
	}
	if meta["enabled"] != true || meta["selected_family"] != "default" {
		t.Fatalf("unexpected lora candidate provenance: %+v", meta)
	}
	gate, ok := meta["gate"].(map[string]any)
	if !ok {
		t.Fatalf("missing gate policy: %+v", meta)
	}
	if gate["allow_failed_join_quality"] != false {
		t.Fatalf("join quality must remain a hard gate: %+v", gate)
	}
	hardFailures, ok := meta["hard_rejection_reasons"].([]string)
	if !ok {
		t.Fatalf("missing hard rejection reasons: %+v", meta)
	}
	assertStringSliceContains(t, hardFailures, "bad_join_quality")
	assertStringSliceContains(t, hardFailures, "low_asr")
	assertStringSliceContains(t, hardFailures, "tail_truncation")
}

func TestLoraCandidatePlanLocksListeningOverrideAnchors(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:   true,
		LoraSplitPlanningEnabled:   true,
		LoraCandidateMinTargetSec:  18,
		LoraCandidateMinTextChars:  180,
		LoraCandidateMaxPerSegment: 2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("risk ", 50), 21)
	meta := got["lora_candidate"].(map[string]any)
	anchors, ok := meta["regression_anchors"].([]map[string]any)
	if !ok {
		t.Fatalf("missing regression anchors: %+v", meta)
	}
	var found10569 bool
	var found10835 bool
	for _, anchor := range anchors {
		switch anchor["sample_id"] {
		case "seg10569":
			found10569 = true
			if anchor["required_selected_label"] != "shard01_selected" {
				t.Fatalf("seg10569 selected label drifted: %+v", anchor)
			}
			if anchor["comparison_only_label"] != "split_light_vector_10" {
				t.Fatalf("seg10569 tempo-rescue guard drifted: %+v", anchor)
			}
		case "seg10835":
			found10835 = true
			if anchor["required_label_contains"] != "split4_tail_correct_atempo" {
				t.Fatalf("seg10835 selected label drifted: %+v", anchor)
			}
			if anchor["accepted_low_asr"] != true {
				t.Fatalf("seg10835 must remain an explicit accepted-risk anchor: %+v", anchor)
			}
		}
	}
	if !found10569 {
		t.Fatalf("seg10569 anchor missing: %+v", anchors)
	}
	if !found10835 {
		t.Fatalf("seg10835 anchor missing: %+v", anchors)
	}
}

func TestLoraCandidatePlanKeepsTempoRescueOutOfProductionFamilies(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:   true,
		LoraSplitPlanningEnabled:   true,
		LoraCandidateMinTargetSec:  18,
		LoraCandidateMinTextChars:  180,
		LoraCandidateMaxPerSegment: 2,
	}}

	got := svc.attachLoraCandidatePlanToDubbingMeta(nil, strings.Repeat("risk ", 50), 21)
	meta := got["lora_candidate"].(map[string]any)
	families := meta["candidate_families"].([]string)
	if containsString(families, "split_light_vector_10") {
		t.Fatalf("10%% tempo rescue must stay comparison-only, got families=%+v", families)
	}
	generation := meta["generation"].(map[string]any)
	forbidden := generation["forbidden_default_families"].([]string)
	assertStringSliceContains(t, forbidden, "split_light_vector_10")
	gate := meta["gate"].(map[string]any)
	if gate["max_fit_delta"] != 0.05 || meta["tempo_fit_max_delta"] != 0.05 {
		t.Fatalf("tempo threshold drifted: gate=%+v meta=%+v", gate, meta)
	}
	hardFailures := meta["hard_rejection_reasons"].([]string)
	assertStringSliceContains(t, hardFailures, "high_fit_delta")
	softOverrides := meta["soft_override_reasons"].([]string)
	if containsString(softOverrides, "high_fit_delta") {
		t.Fatalf("high_fit_delta must remain a hard failure, not a listening override: %+v", softOverrides)
	}
}

func TestTTSProvenanceDefaultOffOmitsLoraCandidatePlan(t *testing.T) {
	svc := &Service{cfg: config.Config{}}

	updates := svc.buildTTSProvenanceMeta("short text", 30, "voice_profile", nil)
	if _, ok := updates["tts_lora_candidate"]; ok {
		t.Fatalf("lora candidate provenance present when disabled: %+v", updates)
	}
}

func TestPersistTTSProvenanceMergesLoraCandidateRuntime(t *testing.T) {
	svc := &Service{cfg: config.Config{
		LoraCandidateLoopEnabled:      true,
		LoraCandidateExecutionEnabled: true,
		LoraSplitPlanningEnabled:      true,
		LoraCandidateMinTargetSec:     18,
		LoraCandidateMinTextChars:     180,
		LoraCandidateMaxPerSegment:    2,
	}}
	seg := &models.Segment{
		ID: 1,
		Meta: map[string]any{
			ttsLoraCandidateRuntimeMetaKey: map[string]any{
				"selected_family":      "default",
				"audio_selection_mode": "default_audio",
				"selection_reason":     "candidate_execution_not_available",
				"fallback_reason":      "candidate_execution_not_available",
			},
		},
	}

	svc.persistTTSProvenance(context.Background(), seg, strings.Repeat("risk ", 50), 21, "voice_profile", nil)

	meta, ok := seg.Meta["tts_lora_candidate"].(map[string]any)
	if !ok {
		t.Fatalf("missing tts_lora_candidate: %+v", seg.Meta)
	}
	if meta["candidate_execution_enabled"] != true {
		t.Fatalf("execution request should remain visible: %+v", meta)
	}
	if meta["audio_selection_mode"] != "default_audio" || meta["fallback_reason"] != "candidate_execution_not_available" {
		t.Fatalf("runtime selection was not merged: %+v", meta)
	}
	if meta["runtime"] == nil {
		t.Fatalf("runtime missing from candidate provenance: %+v", meta)
	}
	if seg.Meta[ttsLoraCandidateRuntimeMetaKey] == nil {
		t.Fatalf("top-level runtime missing: %+v", seg.Meta)
	}
}

func containsString(got []string, want string) bool {
	for _, item := range got {
		if item == want {
			return true
		}
	}
	return false
}

func assertStringSliceEqual(t *testing.T, got []string, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("slice = %+v, want %+v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("slice = %+v, want %+v", got, want)
		}
	}
}

func assertStringSliceContains(t *testing.T, got []string, want string) {
	t.Helper()
	for _, item := range got {
		if item == want {
			return
		}
	}
	t.Fatalf("slice = %+v, want item %q", got, want)
}
