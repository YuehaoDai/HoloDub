package pipeline

import (
	"strings"

	"holodub/internal/models"
)

const (
	asrGuardMetaKey = "asr_guard"
	skipMetaKey     = "dubbing_skip"
)

func shouldSkipDubbing(seg models.Segment) bool {
	if seg.Status == models.SegmentStatusSkipped {
		return true
	}
	if strings.TrimSpace(seg.SourceText) == "" {
		return true
	}

	reason := strings.ToLower(strings.TrimSpace(seg.SplitReason))
	if reason == "non_speech" || strings.HasPrefix(reason, "non_speech:") ||
		reason == "noise" || reason == "sound_effect" {
		return true
	}

	if seg.Meta != nil {
		if skipped, ok := boolFromMeta(seg.Meta[skipMetaKey]); ok && skipped {
			return true
		}
		if raw, ok := seg.Meta[asrGuardMetaKey].(map[string]any); ok {
			if dropped, ok := boolFromMeta(raw["drop"]); ok && dropped {
				return true
			}
			if classification, _ := raw["classification"].(string); classification == "non_speech" {
				return true
			}
		}
	}
	return false
}

func markSegmentSkipped(seg *models.Segment, reason string) {
	if seg == nil {
		return
	}
	if strings.TrimSpace(reason) == "" {
		reason = "non_speech"
	}
	seg.TargetText = ""
	seg.TTSAudioRelPath = ""
	seg.TTSDurationMs = 0
	seg.Status = models.SegmentStatusSkipped
	if seg.Meta == nil {
		seg.Meta = map[string]any{}
	}
	seg.Meta[skipMetaKey] = map[string]any{
		"reason": reason,
		"source": "pipeline_non_speech_guard",
	}
}

func skipReasonForSegment(seg models.Segment) string {
	if strings.TrimSpace(seg.SourceText) == "" {
		return "empty_source_text"
	}
	if strings.TrimSpace(seg.SplitReason) != "" {
		reason := strings.ToLower(strings.TrimSpace(seg.SplitReason))
		if reason == "non_speech" || strings.HasPrefix(reason, "non_speech:") ||
			reason == "noise" || reason == "sound_effect" {
			return reason
		}
	}
	return "non_speech_guard"
}

func boolFromMeta(value any) (bool, bool) {
	switch v := value.(type) {
	case bool:
		return v, true
	case string:
		switch strings.ToLower(strings.TrimSpace(v)) {
		case "true", "1", "yes":
			return true, true
		case "false", "0", "no":
			return false, true
		default:
			return false, false
		}
	default:
		return false, false
	}
}
