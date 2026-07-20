package pipeline

import (
	"testing"

	"holodub/internal/models"
)

func TestBuildFinalSynthFitAuditMetaPassesWithinFivePercent(t *testing.T) {
	seg := &models.Segment{
		ID:              16283,
		TTSAudioRelPath: "jobs/358/tts/vp0/segment-0026.wav",
		TTSDurationMs:   10_420,
	}

	audit, rejected := buildFinalSynthFitAuditMeta(seg, 10_000, 0.05, "segment_agent")

	if rejected {
		t.Fatalf("rejected = true, want false: %#v", audit)
	}
	if audit["pass"] != true || audit["status"] != "pass" {
		t.Fatalf("audit should pass: %#v", audit)
	}
	if got := auditFloat(audit["final_implied_fit_delta"]); got < 0.0419 || got > 0.0421 {
		t.Fatalf("final_implied_fit_delta = %.6f, want about 0.042", got)
	}
}

func TestBuildFinalSynthFitAuditMetaRejectsHighTempoDelta(t *testing.T) {
	seg := &models.Segment{
		ID:              16285,
		TTSAudioRelPath: "jobs/358/tts/vp0/segment-0028.wav",
		TTSDurationMs:   13_300,
	}

	audit, rejected := buildFinalSynthFitAuditMeta(seg, 10_000, 0.05, "legacy_tts")

	if !rejected {
		t.Fatalf("rejected = false, want true: %#v", audit)
	}
	if audit["pass"] != false || audit["status"] != "fail" {
		t.Fatalf("audit should fail: %#v", audit)
	}
	if audit["reject_reason"] != "high_final_implied_fit_delta" {
		t.Fatalf("reject_reason = %#v", audit["reject_reason"])
	}
	if audit["output_not_persisted"] != true {
		t.Fatalf("output_not_persisted = %#v, want true", audit["output_not_persisted"])
	}
}
