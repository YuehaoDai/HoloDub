package pipeline

import (
	"context"
	"log/slog"

	"holodub/internal/models"
	pipettstts "holodub/internal/pipeline/tts"
)

const (
	voiceConfigSourceProfile       = "voice_profile"
	voiceConfigSourceEpisodeVocals = "episode_vocals_fallback"
	voiceConfigSourceJobVocals     = "job_vocals_fallback"
	voiceConfigSourceGlobalDefault = "global_default_fallback"
)

func spokenTextForTTS(text string) pipettstts.SpokenTextResult {
	result := pipettstts.NormalizeSpokenText(text)
	if result.Text == "" {
		result.Text = text
	}
	return result
}

func (s *Service) persistTTSProvenance(ctx context.Context, seg *models.Segment, displayText string, targetSec float64, voiceSource string, voiceConfig map[string]any) {
	if seg == nil || seg.ID == 0 {
		return
	}
	updates := s.buildTTSProvenanceMeta(displayText, targetSec, voiceSource, voiceConfig)
	if runtime, ok := loraCandidateRuntimeFromSegment(seg); ok {
		if candidate, ok := updates["tts_lora_candidate"].(map[string]any); ok {
			mergeLoraCandidateRuntime(candidate, runtime)
		}
		updates[ttsLoraCandidateRuntimeMetaKey] = runtime
	}
	if len(updates) == 0 {
		return
	}
	applySegmentMetaUpdates(seg, updates)
	if s == nil || s.store == nil {
		return
	}
	if err := s.store.UpdateSegmentMeta(ctx, seg.ID, updates); err != nil {
		slog.Warn("failed to persist tts provenance",
			"segment_id", seg.ID,
			"voice_config_source", voiceSource,
			"error", err,
		)
	}
}

func (s *Service) buildTTSProvenanceMeta(displayText string, targetSec float64, voiceSource string, voiceConfig map[string]any) map[string]any {
	updates := buildTTSProvenanceMeta(displayText, voiceSource, voiceConfig)
	if candidatePlan, ok := s.buildLoraCandidateShadowPlan(displayText, targetSec); ok {
		updates["tts_lora_candidate"] = candidatePlan
	}
	return updates
}

func buildTTSProvenanceMeta(displayText string, voiceSource string, voiceConfig map[string]any) map[string]any {
	spoken := spokenTextForTTS(displayText)
	updates := map[string]any{
		"tts_text_normalized": spoken.Changed(),
		"voice_config_source": voiceSource,
	}
	if spoken.Changed() {
		updates["tts_spoken_text"] = spoken.Text
		updates["tts_text_rewrites"] = spoken.Rewrites
	} else {
		updates["tts_spoken_text"] = nil
		updates["tts_text_rewrites"] = nil
	}
	if samples := sampleRelPathsFromVoiceConfig(voiceConfig); len(samples) > 0 {
		updates["voice_config_sample_relpaths"] = samples
	} else {
		updates["voice_config_sample_relpaths"] = nil
	}
	return updates
}

func applySegmentMetaUpdates(seg *models.Segment, updates map[string]any) {
	if seg.Meta == nil {
		seg.Meta = map[string]any{}
	}
	for k, v := range updates {
		if v == nil {
			delete(seg.Meta, k)
		} else {
			seg.Meta[k] = v
		}
	}
}

func sampleRelPathsFromVoiceConfig(voiceConfig map[string]any) []string {
	if voiceConfig == nil {
		return nil
	}
	raw, ok := voiceConfig["sample_relpaths"]
	if !ok {
		return nil
	}
	switch samples := raw.(type) {
	case []string:
		return append([]string(nil), samples...)
	case []any:
		out := make([]string, 0, len(samples))
		for _, item := range samples {
			if s, ok := item.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}
