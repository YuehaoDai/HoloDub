package pipeline

import (
	"encoding/json"
	"log/slog"

	"holodub/internal/models"
	pipettstts "holodub/internal/pipeline/tts"
)

func (s *Service) charsPerSecHintForTTS(targetLanguage string, profile *models.VoiceProfile) float64 {
	if s.cfg.TemporalPlanCharsPerSec > 0 {
		return s.cfg.TemporalPlanCharsPerSec
	}
	if profile != nil && profile.EstCharsPerSec != nil && *profile.EstCharsPerSec > 0 {
		return *profile.EstCharsPerSec
	}
	return pipettstts.DefaultCharsPerSec(targetLanguage)
}

func (s *Service) attachTemporalPlanToDubbingMeta(
	base map[string]any,
	text string,
	targetLanguage string,
	targetSec float64,
	charsPerSecHint float64,
	jobID uint,
	segmentID uint,
) map[string]any {
	meta := base
	if s.cfg.TemporalPlanEnabled && (s.cfg.TemporalPlanMinTargetSec <= 0 || targetSec >= s.cfg.TemporalPlanMinTargetSec) {
		meta = copyDubbingMeta(meta)
		plan := pipettstts.BuildTimingPlan(pipettstts.TimingPlanInput{
			Text:                 text,
			TargetLanguage:       targetLanguage,
			TargetSec:            targetSec,
			CharsPerSecHint:      charsPerSecHint,
			Pacing:               timingPacing(meta),
			EmotionArousal:       timingArousal(meta),
			HasEmotionArousal:    hasTimingArousal(meta),
			HardMaxUnderDriftSec: s.cfg.HardMaxUnderDriftSec,
			HardMaxOverDriftSec:  s.cfg.HardMaxOverDriftSec,
			MaxGroups:            pipettstts.DefaultTimingPlanMaxGroups,
		})
		meta["timing"] = plan
		if plan.Status != pipettstts.TimingPlanFits || plan.RateStatus != pipettstts.RateNatural {
			slog.Warn("tts temporal plan preflight risk",
				"job_id", jobID,
				"segment_id", segmentID,
				"status", plan.Status,
				"risk", plan.Risk,
				"rate_status", plan.RateStatus,
				"rate_risk", plan.RateRisk,
				"target_ms", plan.TargetMs,
				"predicted_ms", plan.PredictedMs,
				"min_allowed_ms", plan.MinAllowedMs,
				"max_allowed_ms", plan.MaxAllowedMs,
				"chars", plan.Chars,
				"min_chars", plan.MinChars,
				"max_chars", plan.MaxChars,
				"chars_per_sec", plan.CharsPerSec,
				"required_chars_per_sec", plan.RequiredCharsPerSec,
				"rate_ratio", plan.RateRatio,
				"min_natural_chars_per_sec", plan.MinNaturalCharsPerSec,
				"max_natural_chars_per_sec", plan.MaxNaturalCharsPerSec,
			)
		}
	}
	return s.attachLoraCandidatePlanToDubbingMeta(meta, text, targetSec)
}

func (s *Service) attachLoraCandidatePlanToDubbingMeta(base map[string]any, text string, targetSec float64) map[string]any {
	candidateMeta, ok := s.buildLoraCandidateShadowPlan(text, targetSec)
	if !ok {
		return base
	}
	meta := copyDubbingMeta(base)
	meta["lora_candidate"] = candidateMeta
	return meta
}

func copyDubbingMeta(in map[string]any) map[string]any {
	out := make(map[string]any, len(in)+1)
	for k, v := range in {
		out[k] = v
	}
	return out
}

func timingPacing(meta map[string]any) string {
	if meta == nil {
		return ""
	}
	if pacing, ok := meta["pacing"].(string); ok {
		return pacing
	}
	return ""
}

func timingArousal(meta map[string]any) float64 {
	arousal, _ := timingArousalValue(meta)
	return arousal
}

func hasTimingArousal(meta map[string]any) bool {
	_, ok := timingArousalValue(meta)
	return ok
}

func timingArousalValue(meta map[string]any) (float64, bool) {
	if meta == nil {
		return 0, false
	}
	emotion, ok := meta["emotion"].(map[string]any)
	if !ok {
		return 0, false
	}
	switch v := emotion["arousal"].(type) {
	case float64:
		return v, true
	case float32:
		return float64(v), true
	case int:
		return float64(v), true
	case int64:
		return float64(v), true
	case json.Number:
		f, err := v.Float64()
		return f, err == nil
	default:
		return 0, false
	}
}
