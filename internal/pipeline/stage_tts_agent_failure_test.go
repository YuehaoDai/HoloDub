package pipeline

import (
	"errors"
	"testing"

	"holodub/internal/agents"
	"holodub/internal/models"
)

func TestBuildSegmentAgentFailureMetaRecordsRejectedOutput(t *testing.T) {
	out := agents.RunOutput{
		FinalAudioRelPath: "jobs/359/tts/vp0/segment-0007.agent_attempt08.wav",
		FinalActualMs:     31327,
		FinalDecision: agents.Decision{
			Kind:             agents.DecisionAccept,
			Reason:           "clip_overflow",
			HardCapExceeded:  true,
			HardCapDirection: "over",
		},
		FinalLoraCandidateRuntime: map[string]any{
			"audio_selection_mode": "default_audio",
			"fallback_reason":      "candidate_attempt_limit_reached",
			"selected_family":      "default",
		},
		State: agents.State{Attempt: 8},
	}

	updates := buildSegmentAgentFailureMeta(out, 29780, 0.05, errors.New("segment_agent hard-cap escape rejected"))
	failure, ok := updates["tts_agent_failure"].(map[string]any)
	if !ok {
		t.Fatalf("tts_agent_failure missing: %#v", updates)
	}
	if failure["output_not_persisted"] != true {
		t.Fatalf("output_not_persisted = %#v, want true", failure["output_not_persisted"])
	}
	if failure["hard_cap_exceeded"] != true || failure["hard_cap_direction"] != "over" {
		t.Fatalf("hard cap evidence missing: %#v", failure)
	}
	if failure["final_decision"] != "accept" || failure["final_reason"] != "clip_overflow" {
		t.Fatalf("decision evidence missing: %#v", failure)
	}
	if failure["final_abs_drift_ms"] != int64(1547) {
		t.Fatalf("final_abs_drift_ms = %#v, want 1547", failure["final_abs_drift_ms"])
	}
	if failure["rejected_output_relpath"] == "" {
		t.Fatalf("rejected output relpath missing: %#v", failure)
	}
	if failure["lora_fallback_reason"] != "candidate_attempt_limit_reached" {
		t.Fatalf("lora fallback reason missing: %#v", failure)
	}
}

func TestApplySegmentAgentDecisionMetaClearsFailureMeta(t *testing.T) {
	seg := &models.Segment{
		Meta: map[string]any{
			"tts_agent_failure": map[string]any{"error": "previous failure"},
		},
	}
	out := agents.RunOutput{
		FinalActualMs: 1000,
		FinalDecision: agents.Decision{
			Kind:   agents.DecisionAccept,
			Reason: "within_threshold",
		},
	}

	applySegmentAgentDecisionMeta(seg, out, 1000, 0.05)
	if _, ok := seg.Meta["tts_agent_failure"]; ok {
		t.Fatalf("tts_agent_failure should be cleared on success: %#v", seg.Meta)
	}
	if _, ok := seg.Meta["tts_agent"]; !ok {
		t.Fatalf("tts_agent success meta missing: %#v", seg.Meta)
	}
}
