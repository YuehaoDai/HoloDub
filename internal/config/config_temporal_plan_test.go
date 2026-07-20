package config

import "testing"

func TestLoad_TemporalPlanEnabledDefault(t *testing.T) {
	t.Setenv("TEMPORAL_PLAN_ENABLED", "")
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load returned error: %v", err)
	}
	if cfg.TemporalPlanEnabled {
		t.Fatal("TemporalPlanEnabled default = true, want false")
	}
	if cfg.TemporalPlanMinTargetSec != 15.0 {
		t.Fatalf("TemporalPlanMinTargetSec = %v, want 15.0", cfg.TemporalPlanMinTargetSec)
	}
	if cfg.TemporalPlanCharsPerSec != 0 {
		t.Fatalf("TemporalPlanCharsPerSec = %v, want 0", cfg.TemporalPlanCharsPerSec)
	}
	if cfg.LoraCandidateLoopEnabled {
		t.Fatal("LoraCandidateLoopEnabled default = true, want false")
	}
	if cfg.LoraCandidateExecutionEnabled {
		t.Fatal("LoraCandidateExecutionEnabled default = true, want false")
	}
	if cfg.LoraSplitPlanningEnabled {
		t.Fatal("LoraSplitPlanningEnabled default = true, want false")
	}
	if cfg.LoraCandidateMinTargetSec != 18.0 {
		t.Fatalf("LoraCandidateMinTargetSec = %v, want 18.0", cfg.LoraCandidateMinTargetSec)
	}
	if cfg.LoraCandidateMinTextChars != 180 {
		t.Fatalf("LoraCandidateMinTextChars = %v, want 180", cfg.LoraCandidateMinTextChars)
	}
	if cfg.LoraCandidateDenseMinTargetSec != 9.0 {
		t.Fatalf("LoraCandidateDenseMinTargetSec = %v, want 9.0", cfg.LoraCandidateDenseMinTargetSec)
	}
	if cfg.LoraCandidateDenseMinCharsPerSec != 4.9 {
		t.Fatalf("LoraCandidateDenseMinCharsPerSec = %v, want 4.9", cfg.LoraCandidateDenseMinCharsPerSec)
	}
	if cfg.LoraCandidateNearLongMinTargetSec != 16.0 {
		t.Fatalf("LoraCandidateNearLongMinTargetSec = %v, want 16.0", cfg.LoraCandidateNearLongMinTargetSec)
	}
	if cfg.LoraCandidateNearLongMinTextChars != 70 {
		t.Fatalf("LoraCandidateNearLongMinTextChars = %v, want 70", cfg.LoraCandidateNearLongMinTextChars)
	}
	if cfg.LoraCandidateShortDenseMinTargetSec != 6.0 {
		t.Fatalf("LoraCandidateShortDenseMinTargetSec = %v, want 6.0", cfg.LoraCandidateShortDenseMinTargetSec)
	}
	if cfg.LoraCandidateShortDenseMaxTargetSec != 9.0 {
		t.Fatalf("LoraCandidateShortDenseMaxTargetSec = %v, want 9.0", cfg.LoraCandidateShortDenseMaxTargetSec)
	}
	if cfg.LoraCandidateShortDenseMinCharsPerSec != 5.5 {
		t.Fatalf("LoraCandidateShortDenseMinCharsPerSec = %v, want 5.5", cfg.LoraCandidateShortDenseMinCharsPerSec)
	}
	if cfg.LoraCandidateShortTransitionMinTargetSec != 5.0 {
		t.Fatalf("LoraCandidateShortTransitionMinTargetSec = %v, want 5.0", cfg.LoraCandidateShortTransitionMinTargetSec)
	}
	if cfg.LoraCandidateShortTransitionMaxTargetSec != 10.0 {
		t.Fatalf("LoraCandidateShortTransitionMaxTargetSec = %v, want 10.0", cfg.LoraCandidateShortTransitionMaxTargetSec)
	}
	if cfg.LoraCandidateShortTransitionMinCharsPerSec != 5.0 {
		t.Fatalf("LoraCandidateShortTransitionMinCharsPerSec = %v, want 5.0", cfg.LoraCandidateShortTransitionMinCharsPerSec)
	}
	if cfg.LoraCandidateShortTermNumberMinTargetSec != 6.0 {
		t.Fatalf("LoraCandidateShortTermNumberMinTargetSec = %v, want 6.0", cfg.LoraCandidateShortTermNumberMinTargetSec)
	}
	if cfg.LoraCandidateShortTermNumberMaxTargetSec != 9.0 {
		t.Fatalf("LoraCandidateShortTermNumberMaxTargetSec = %v, want 9.0", cfg.LoraCandidateShortTermNumberMaxTargetSec)
	}
	if cfg.LoraCandidateShortTermNumberMinCharsPerSec != 4.8 {
		t.Fatalf("LoraCandidateShortTermNumberMinCharsPerSec = %v, want 4.8", cfg.LoraCandidateShortTermNumberMinCharsPerSec)
	}
	if cfg.LoraCandidateMaxPerSegment != 2 {
		t.Fatalf("LoraCandidateMaxPerSegment = %v, want 2", cfg.LoraCandidateMaxPerSegment)
	}
	if cfg.LoraCandidateRetryEscalationEnabled {
		t.Fatal("LoraCandidateRetryEscalationEnabled default = true, want false")
	}
	if cfg.LoraCandidateRetryEscalationMinAttempt != 2 {
		t.Fatalf("LoraCandidateRetryEscalationMinAttempt = %v, want 2", cfg.LoraCandidateRetryEscalationMinAttempt)
	}
	if cfg.LoraCandidateRetryEscalationMinTargetSec != 5.0 {
		t.Fatalf("LoraCandidateRetryEscalationMinTargetSec = %v, want 5.0", cfg.LoraCandidateRetryEscalationMinTargetSec)
	}
	if cfg.LoraCandidateRetryEscalationMaxTargetSec != 12.0 {
		t.Fatalf("LoraCandidateRetryEscalationMaxTargetSec = %v, want 12.0", cfg.LoraCandidateRetryEscalationMaxTargetSec)
	}
	if cfg.LoraCandidateRetryEscalationMinTextChars != 28 {
		t.Fatalf("LoraCandidateRetryEscalationMinTextChars = %v, want 28", cfg.LoraCandidateRetryEscalationMinTextChars)
	}
	if cfg.LoraCandidateRetryEscalationMinCharsPerSec != 4.5 {
		t.Fatalf("LoraCandidateRetryEscalationMinCharsPerSec = %v, want 4.5", cfg.LoraCandidateRetryEscalationMinCharsPerSec)
	}
	if cfg.SegmentAgentMaxFitDelta != 0.05 {
		t.Fatalf("SegmentAgentMaxFitDelta = %v, want 0.05", cfg.SegmentAgentMaxFitDelta)
	}
}

func TestLoad_TemporalPlanEnabledOverride(t *testing.T) {
	t.Setenv("TEMPORAL_PLAN_ENABLED", "true")
	t.Setenv("TEMPORAL_PLAN_MIN_TARGET_SEC", "20")
	t.Setenv("TEMPORAL_PLAN_CHARS_PER_SEC", "5.25")
	t.Setenv("LORA_CANDIDATE_LOOP_ENABLED", "true")
	t.Setenv("LORA_CANDIDATE_EXECUTION_ENABLED", "true")
	t.Setenv("LORA_SPLIT_PLANNING_ENABLED", "true")
	t.Setenv("LORA_CANDIDATE_MIN_TARGET_SEC", "21")
	t.Setenv("LORA_CANDIDATE_MIN_TEXT_CHARS", "210")
	t.Setenv("LORA_CANDIDATE_DENSE_MIN_TARGET_SEC", "11.5")
	t.Setenv("LORA_CANDIDATE_DENSE_MIN_CHARS_PER_SEC", "5.75")
	t.Setenv("LORA_CANDIDATE_NEAR_LONG_MIN_TARGET_SEC", "15.5")
	t.Setenv("LORA_CANDIDATE_NEAR_LONG_MIN_TEXT_CHARS", "66")
	t.Setenv("LORA_CANDIDATE_SHORT_DENSE_MIN_TARGET_SEC", "5.5")
	t.Setenv("LORA_CANDIDATE_SHORT_DENSE_MAX_TARGET_SEC", "8.5")
	t.Setenv("LORA_CANDIDATE_SHORT_DENSE_MIN_CHARS_PER_SEC", "5.25")
	t.Setenv("LORA_CANDIDATE_SHORT_TRANSITION_MIN_TARGET_SEC", "4.5")
	t.Setenv("LORA_CANDIDATE_SHORT_TRANSITION_MAX_TARGET_SEC", "9.5")
	t.Setenv("LORA_CANDIDATE_SHORT_TRANSITION_MIN_CHARS_PER_SEC", "4.8")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC", "6.5")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MAX_TARGET_SEC", "8.75")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_CHARS_PER_SEC", "4.95")
	t.Setenv("LORA_CANDIDATE_MAX_PER_SEGMENT", "3")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_ENABLED", "true")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_ATTEMPT", "4")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TARGET_SEC", "4.75")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MAX_TARGET_SEC", "11.5")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TEXT_CHARS", "24")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_CHARS_PER_SEC", "4.25")
	t.Setenv("SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE", "true")
	t.Setenv("SEGMENT_AGENT_MAX_FIT_DELTA", "0.04")
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load returned error: %v", err)
	}
	if !cfg.TemporalPlanEnabled {
		t.Fatal("TemporalPlanEnabled override did not flow through")
	}
	if cfg.TemporalPlanMinTargetSec != 20 {
		t.Fatalf("TemporalPlanMinTargetSec override = %v, want 20", cfg.TemporalPlanMinTargetSec)
	}
	if cfg.TemporalPlanCharsPerSec != 5.25 {
		t.Fatalf("TemporalPlanCharsPerSec override = %v, want 5.25", cfg.TemporalPlanCharsPerSec)
	}
	if !cfg.LoraCandidateLoopEnabled {
		t.Fatal("LoraCandidateLoopEnabled override did not flow through")
	}
	if !cfg.LoraCandidateExecutionEnabled {
		t.Fatal("LoraCandidateExecutionEnabled override did not flow through")
	}
	if !cfg.LoraSplitPlanningEnabled {
		t.Fatal("LoraSplitPlanningEnabled override did not flow through")
	}
	if cfg.LoraCandidateMinTargetSec != 21 {
		t.Fatalf("LoraCandidateMinTargetSec override = %v, want 21", cfg.LoraCandidateMinTargetSec)
	}
	if cfg.LoraCandidateMinTextChars != 210 {
		t.Fatalf("LoraCandidateMinTextChars override = %v, want 210", cfg.LoraCandidateMinTextChars)
	}
	if cfg.LoraCandidateDenseMinTargetSec != 11.5 {
		t.Fatalf("LoraCandidateDenseMinTargetSec override = %v, want 11.5", cfg.LoraCandidateDenseMinTargetSec)
	}
	if cfg.LoraCandidateDenseMinCharsPerSec != 5.75 {
		t.Fatalf("LoraCandidateDenseMinCharsPerSec override = %v, want 5.75", cfg.LoraCandidateDenseMinCharsPerSec)
	}
	if cfg.LoraCandidateNearLongMinTargetSec != 15.5 {
		t.Fatalf("LoraCandidateNearLongMinTargetSec override = %v, want 15.5", cfg.LoraCandidateNearLongMinTargetSec)
	}
	if cfg.LoraCandidateNearLongMinTextChars != 66 {
		t.Fatalf("LoraCandidateNearLongMinTextChars override = %v, want 66", cfg.LoraCandidateNearLongMinTextChars)
	}
	if cfg.LoraCandidateShortDenseMinTargetSec != 5.5 {
		t.Fatalf("LoraCandidateShortDenseMinTargetSec override = %v, want 5.5", cfg.LoraCandidateShortDenseMinTargetSec)
	}
	if cfg.LoraCandidateShortDenseMaxTargetSec != 8.5 {
		t.Fatalf("LoraCandidateShortDenseMaxTargetSec override = %v, want 8.5", cfg.LoraCandidateShortDenseMaxTargetSec)
	}
	if cfg.LoraCandidateShortDenseMinCharsPerSec != 5.25 {
		t.Fatalf("LoraCandidateShortDenseMinCharsPerSec override = %v, want 5.25", cfg.LoraCandidateShortDenseMinCharsPerSec)
	}
	if cfg.LoraCandidateShortTransitionMinTargetSec != 4.5 {
		t.Fatalf("LoraCandidateShortTransitionMinTargetSec override = %v, want 4.5", cfg.LoraCandidateShortTransitionMinTargetSec)
	}
	if cfg.LoraCandidateShortTransitionMaxTargetSec != 9.5 {
		t.Fatalf("LoraCandidateShortTransitionMaxTargetSec override = %v, want 9.5", cfg.LoraCandidateShortTransitionMaxTargetSec)
	}
	if cfg.LoraCandidateShortTransitionMinCharsPerSec != 4.8 {
		t.Fatalf("LoraCandidateShortTransitionMinCharsPerSec override = %v, want 4.8", cfg.LoraCandidateShortTransitionMinCharsPerSec)
	}
	if cfg.LoraCandidateShortTermNumberMinTargetSec != 6.5 {
		t.Fatalf("LoraCandidateShortTermNumberMinTargetSec override = %v, want 6.5", cfg.LoraCandidateShortTermNumberMinTargetSec)
	}
	if cfg.LoraCandidateShortTermNumberMaxTargetSec != 8.75 {
		t.Fatalf("LoraCandidateShortTermNumberMaxTargetSec override = %v, want 8.75", cfg.LoraCandidateShortTermNumberMaxTargetSec)
	}
	if cfg.LoraCandidateShortTermNumberMinCharsPerSec != 4.95 {
		t.Fatalf("LoraCandidateShortTermNumberMinCharsPerSec override = %v, want 4.95", cfg.LoraCandidateShortTermNumberMinCharsPerSec)
	}
	if cfg.LoraCandidateMaxPerSegment != 3 {
		t.Fatalf("LoraCandidateMaxPerSegment override = %v, want 3", cfg.LoraCandidateMaxPerSegment)
	}
	if !cfg.LoraCandidateRetryEscalationEnabled {
		t.Fatal("LoraCandidateRetryEscalationEnabled override did not flow through")
	}
	if cfg.LoraCandidateRetryEscalationMinAttempt != 4 {
		t.Fatalf("LoraCandidateRetryEscalationMinAttempt override = %v, want 4", cfg.LoraCandidateRetryEscalationMinAttempt)
	}
	if cfg.LoraCandidateRetryEscalationMinTargetSec != 4.75 {
		t.Fatalf("LoraCandidateRetryEscalationMinTargetSec override = %v, want 4.75", cfg.LoraCandidateRetryEscalationMinTargetSec)
	}
	if cfg.LoraCandidateRetryEscalationMaxTargetSec != 11.5 {
		t.Fatalf("LoraCandidateRetryEscalationMaxTargetSec override = %v, want 11.5", cfg.LoraCandidateRetryEscalationMaxTargetSec)
	}
	if cfg.LoraCandidateRetryEscalationMinTextChars != 24 {
		t.Fatalf("LoraCandidateRetryEscalationMinTextChars override = %v, want 24", cfg.LoraCandidateRetryEscalationMinTextChars)
	}
	if cfg.LoraCandidateRetryEscalationMinCharsPerSec != 4.25 {
		t.Fatalf("LoraCandidateRetryEscalationMinCharsPerSec override = %v, want 4.25", cfg.LoraCandidateRetryEscalationMinCharsPerSec)
	}
	if !cfg.SegmentAgentRejectHardCapEscape {
		t.Fatal("SegmentAgentRejectHardCapEscape override did not flow through")
	}
	if cfg.SegmentAgentMaxFitDelta != 0.04 {
		t.Fatalf("SegmentAgentMaxFitDelta override = %v, want 0.04", cfg.SegmentAgentMaxFitDelta)
	}
}

func TestLoad_TemporalPlanEnabledRejectsBadInput(t *testing.T) {
	t.Setenv("TEMPORAL_PLAN_ENABLED", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid TEMPORAL_PLAN_ENABLED")
	}

	t.Setenv("TEMPORAL_PLAN_ENABLED", "")
	t.Setenv("TEMPORAL_PLAN_MIN_TARGET_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative TEMPORAL_PLAN_MIN_TARGET_SEC")
	}

	t.Setenv("TEMPORAL_PLAN_MIN_TARGET_SEC", "")
	t.Setenv("TEMPORAL_PLAN_CHARS_PER_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative TEMPORAL_PLAN_CHARS_PER_SEC")
	}

	t.Setenv("TEMPORAL_PLAN_CHARS_PER_SEC", "")
	t.Setenv("LORA_CANDIDATE_LOOP_ENABLED", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid LORA_CANDIDATE_LOOP_ENABLED")
	}

	t.Setenv("LORA_CANDIDATE_LOOP_ENABLED", "")
	t.Setenv("LORA_CANDIDATE_EXECUTION_ENABLED", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid LORA_CANDIDATE_EXECUTION_ENABLED")
	}

	t.Setenv("LORA_CANDIDATE_EXECUTION_ENABLED", "")
	t.Setenv("LORA_SPLIT_PLANNING_ENABLED", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid LORA_SPLIT_PLANNING_ENABLED")
	}

	t.Setenv("LORA_SPLIT_PLANNING_ENABLED", "")
	t.Setenv("LORA_CANDIDATE_MIN_TARGET_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_MIN_TARGET_SEC")
	}

	t.Setenv("LORA_CANDIDATE_MIN_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_MIN_TEXT_CHARS", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_MIN_TEXT_CHARS")
	}

	t.Setenv("LORA_CANDIDATE_MIN_TEXT_CHARS", "")
	t.Setenv("LORA_CANDIDATE_DENSE_MIN_TARGET_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_DENSE_MIN_TARGET_SEC")
	}

	t.Setenv("LORA_CANDIDATE_DENSE_MIN_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_DENSE_MIN_CHARS_PER_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_DENSE_MIN_CHARS_PER_SEC")
	}

	t.Setenv("LORA_CANDIDATE_DENSE_MIN_CHARS_PER_SEC", "")
	t.Setenv("LORA_CANDIDATE_NEAR_LONG_MIN_TARGET_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_NEAR_LONG_MIN_TARGET_SEC")
	}

	t.Setenv("LORA_CANDIDATE_NEAR_LONG_MIN_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_NEAR_LONG_MIN_TEXT_CHARS", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_NEAR_LONG_MIN_TEXT_CHARS")
	}

	t.Setenv("LORA_CANDIDATE_NEAR_LONG_MIN_TEXT_CHARS", "")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC")
	}

	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC", "10")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MAX_TARGET_SEC", "9")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject inverted LORA_CANDIDATE_SHORT_TERM_NUMBER target window")
	}

	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MAX_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_CHARS_PER_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_CHARS_PER_SEC")
	}

	t.Setenv("LORA_CANDIDATE_SHORT_TERM_NUMBER_MIN_CHARS_PER_SEC", "")
	t.Setenv("LORA_CANDIDATE_MAX_PER_SEGMENT", "0")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject non-positive LORA_CANDIDATE_MAX_PER_SEGMENT")
	}

	t.Setenv("LORA_CANDIDATE_MAX_PER_SEGMENT", "")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_ENABLED", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid LORA_CANDIDATE_RETRY_ESCALATION_ENABLED")
	}

	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_ENABLED", "")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_ATTEMPT", "0")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject non-positive LORA_CANDIDATE_RETRY_ESCALATION_MIN_ATTEMPT")
	}

	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_ATTEMPT", "")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TARGET_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_RETRY_ESCALATION_MIN_TARGET_SEC")
	}

	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TARGET_SEC", "13")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MAX_TARGET_SEC", "12")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject inverted LORA_CANDIDATE_RETRY_ESCALATION target window")
	}

	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MAX_TARGET_SEC", "")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TEXT_CHARS", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_RETRY_ESCALATION_MIN_TEXT_CHARS")
	}

	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_TEXT_CHARS", "")
	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_CHARS_PER_SEC", "-1")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject negative LORA_CANDIDATE_RETRY_ESCALATION_MIN_CHARS_PER_SEC")
	}

	t.Setenv("LORA_CANDIDATE_RETRY_ESCALATION_MIN_CHARS_PER_SEC", "")
	t.Setenv("SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE", "sometimes")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE")
	}

	t.Setenv("SEGMENT_AGENT_REJECT_HARD_CAP_ESCAPE", "")
	t.Setenv("SEGMENT_AGENT_MAX_FIT_DELTA", "not-a-float")
	if _, err := Load(); err == nil {
		t.Fatal("Load should reject invalid SEGMENT_AGENT_MAX_FIT_DELTA")
	}
}
