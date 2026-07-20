package pipeline

import (
	"context"
	"fmt"
	"log/slog"
	"math"

	"holodub/internal/models"
)

const ttsFinalFitAuditMetaKey = "tts_final_fit_audit"

func (s *Service) auditFinalSynthFitBeforePersist(ctx context.Context, seg *models.Segment, targetMs int64, maxFitDelta float64, source string) error {
	if seg == nil {
		return nil
	}
	audit, rejected := buildFinalSynthFitAuditMeta(seg, targetMs, maxFitDelta, source)
	if len(audit) == 0 {
		return nil
	}
	updates := map[string]any{
		ttsFinalFitAuditMetaKey: audit,
	}
	applySegmentMetaUpdates(seg, updates)
	if s != nil && s.store != nil && seg.ID != 0 {
		if err := s.store.UpdateSegmentMeta(ctx, seg.ID, updates); err != nil {
			slog.Warn("failed to persist final TTS fit audit",
				"segment_id", seg.ID,
				"source", source,
				"error", err,
			)
		}
	}
	if rejected {
		return fmt.Errorf(
			"tts final fit audit rejected (segment %d, source %s, implied_fit_delta %.4f, max %.4f)",
			seg.ID,
			source,
			auditFloat(audit["final_implied_fit_delta"]),
			maxFitDelta,
		)
	}
	return nil
}

func buildFinalSynthFitAuditMeta(seg *models.Segment, targetMs int64, maxFitDelta float64, source string) (map[string]any, bool) {
	if seg == nil {
		return nil, false
	}
	actualMs := seg.TTSDurationMs
	audit := map[string]any{
		"schema_version":            "holodub.tts_final_fit_audit.v1",
		"source":                    source,
		"segment_id":                seg.ID,
		"target_ms":                 targetMs,
		"actual_ms":                 actualMs,
		"audio_relpath":             seg.TTSAudioRelPath,
		"max_fit_delta":             maxFitDelta,
		"pass":                      true,
		"output_not_persisted":      false,
		"reject_reason":             "",
		"final_implied_fit_delta":   0.0,
		"final_implied_fit_percent": 0.0,
	}
	if maxFitDelta <= 0 {
		audit["status"] = "disabled"
		return audit, false
	}
	if targetMs <= 0 || actualMs <= 0 {
		audit["status"] = "skipped_missing_duration"
		return audit, false
	}
	delta := math.Abs(float64(actualMs)/float64(targetMs) - 1.0)
	audit["status"] = "pass"
	audit["final_implied_fit_delta"] = delta
	audit["final_implied_fit_percent"] = delta * 100.0
	if delta <= maxFitDelta+1e-9 {
		return audit, false
	}
	audit["status"] = "fail"
	audit["pass"] = false
	audit["output_not_persisted"] = true
	audit["reject_reason"] = "high_final_implied_fit_delta"
	return audit, true
}

func auditFloat(v any) float64 {
	switch x := v.(type) {
	case float64:
		return x
	case float32:
		return float64(x)
	case int:
		return float64(x)
	case int64:
		return float64(x)
	default:
		return 0
	}
}
