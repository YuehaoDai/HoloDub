package pipeline

import (
	"context"
	"fmt"

	"holodub/internal/ml"
	"holodub/internal/models"
)

const ttsLoraCandidateRuntimeMetaKey = "tts_lora_candidate_runtime"

func (s *Service) runTTSWithLoraCandidateFallback(ctx context.Context, request ml.TTSRequest) (*ml.TTSResponse, map[string]any, error) {
	if !loraCandidateExecutionRequested(request.DubbingMeta) {
		resp, err := s.ml.RunTTS(ctx, request)
		if err != nil {
			return resp, nil, err
		}
		runtime := loraCandidateSkippedRuntime(request.DubbingMeta)
		if len(runtime) > 0 {
			runtime["audio_relpath"] = resp.AudioRelPath
			runtime["actual_duration_ms"] = resp.ActualDurationMs
			resp.Diagnostics = append(resp.Diagnostics, loraCandidateRuntimeDiagnostic(runtime))
			return resp, runtime, nil
		}
		return resp, nil, nil
	}

	resp, err := s.ml.RunTTS(ctx, request)
	if err == nil && len(resp.CandidateResult) > 0 {
		runtime := copyAnyMap(resp.CandidateResult)
		resp.Diagnostics = append(resp.Diagnostics, loraCandidateRuntimeDiagnostic(runtime))
		return resp, runtime, nil
	}

	reason := "candidate_execution_not_available"
	candidateErr := ""
	if err != nil {
		reason = "candidate_execution_error"
		candidateErr = err.Error()
	}
	fallbackRequest, runtime := loraCandidateFallbackRequest(request, reason)
	if candidateErr != "" {
		runtime["candidate_error"] = candidateErr
	}
	fallbackResp, fallbackErr := s.ml.RunTTS(ctx, fallbackRequest)
	if fallbackErr != nil {
		runtime["fallback_reason"] = "default_tts_failed_after_candidate_fallback"
		runtime["error"] = fallbackErr.Error()
		return nil, runtime, fallbackErr
	}
	runtime["audio_relpath"] = fallbackResp.AudioRelPath
	runtime["actual_duration_ms"] = fallbackResp.ActualDurationMs
	fallbackResp.Diagnostics = append(fallbackResp.Diagnostics, loraCandidateRuntimeDiagnostic(runtime))
	return fallbackResp, runtime, nil
}

func applyLoraCandidateAttemptLimit(meta map[string]any, attempt int) map[string]any {
	candidate, ok := loraCandidateMeta(meta)
	if !ok || candidate["enabled"] != true || candidate["candidate_execution_enabled"] != true {
		return meta
	}
	maxPerSegment := intFromAny(candidate["max_per_segment"])
	if maxPerSegment < 1 || attempt < maxPerSegment {
		return meta
	}

	next := copyDubbingMeta(meta)
	limited := copyAnyMap(candidate)
	reason := "candidate_attempt_limit_reached"
	limited["candidate_execution_enabled"] = false
	limited["audio_selection_mode"] = "default_audio"
	limited["selected_family"] = "default"
	limited["selection_reason"] = reason
	limited["fallback_reason"] = reason
	limited["candidate_attempt"] = attempt
	limited["execution_skipped_by_attempt_limit"] = true
	next["lora_candidate"] = limited
	return next
}

func loraCandidateSkippedRuntime(meta map[string]any) map[string]any {
	candidate, ok := loraCandidateMeta(meta)
	if !ok || candidate["execution_skipped_by_attempt_limit"] != true {
		return nil
	}
	reason := stringFromAny(candidate["fallback_reason"])
	if reason == "" {
		reason = "candidate_attempt_limit_reached"
	}
	return map[string]any{
		"schema_version":                     1,
		"execution_requested":                false,
		"execution_mode":                     candidate["execution_mode"],
		"audio_selection_mode":               "default_audio",
		"selected_family":                    "default",
		"selection_reason":                   reason,
		"fallback_reason":                    reason,
		"candidate_execution_enabled":        false,
		"candidate_attempt":                  candidate["candidate_attempt"],
		"max_per_segment":                    candidate["max_per_segment"],
		"execution_skipped_by_attempt_limit": true,
		"candidate_families":                 stringSliceFromAny(candidate["candidate_families"]),
		"alternate_candidate_families":       stringSliceFromAny(candidate["alternate_candidate_families"]),
		"hard_rejection_reasons":             stringSliceFromAny(candidate["hard_rejection_reasons"]),
		"soft_override_reasons":              stringSliceFromAny(candidate["soft_override_reasons"]),
		"hard_failures_cannot_be_overridden": stringSliceFromAny(candidate["hard_rejection_reasons"]),
		"listening_override_scope":           "soft_metrics_only",
		"production_strategy":                candidate["production_strategy"],
		"offline_baseline_id":                candidate["offline_baseline_id"],
		"gate_decision":                      map[string]any{"status": "skipped", "reason": reason},
		"join_quality":                       map[string]any{"status": "not_evaluated", "reason": reason},
	}
}

func loraCandidateExecutionRequested(meta map[string]any) bool {
	candidate, ok := loraCandidateMeta(meta)
	if !ok {
		return false
	}
	if candidate["enabled"] != true {
		return false
	}
	if candidate["candidate_execution_enabled"] != true {
		return false
	}
	return candidate["audio_selection_mode"] == "candidate_audio"
}

func loraCandidateFallbackRequest(request ml.TTSRequest, reason string) (ml.TTSRequest, map[string]any) {
	meta := copyDubbingMeta(request.DubbingMeta)
	candidate, _ := loraCandidateMeta(meta)
	if candidate == nil {
		candidate = map[string]any{}
	}
	candidate = copyAnyMap(candidate)

	families := stringSliceFromAny(candidate["candidate_families"])
	runtime := map[string]any{
		"schema_version":       1,
		"execution_requested":  true,
		"execution_mode":       candidate["execution_mode"],
		"audio_selection_mode": "default_audio",
		"selected_family":      "default",
		"selection_reason":     reason,
		"fallback_reason":      reason,
		"candidate_families":   families,
		"gate_decision": map[string]any{
			"status": "skipped",
			"reason": reason,
		},
		"join_quality": map[string]any{
			"status": "not_evaluated",
			"reason": reason,
		},
	}

	candidate["candidate_execution_enabled"] = false
	candidate["audio_selection_mode"] = "default_audio"
	candidate["selected_family"] = "default"
	candidate["selection_reason"] = reason
	candidate["fallback_reason"] = reason
	candidate["runtime"] = runtime
	meta["lora_candidate"] = candidate
	request.DubbingMeta = meta
	return request, runtime
}

func applyLoraCandidateRuntime(seg *models.Segment, runtime map[string]any) {
	if seg == nil || len(runtime) == 0 {
		return
	}
	applySegmentMetaUpdates(seg, map[string]any{
		ttsLoraCandidateRuntimeMetaKey: runtime,
	})
}

func withLoraCandidateRuntimeCount(seg *models.Segment, runtime map[string]any) map[string]any {
	if len(runtime) == 0 {
		return runtime
	}
	next := copyAnyMap(runtime)
	count := loraCandidateRuntimeCountFromSegment(seg)
	if loraCandidateRuntimeConsumesAttempt(runtime) {
		count++
	} else if runtime["execution_skipped_by_attempt_limit"] == true {
		maxPerSegment := intFromAny(runtime["max_per_segment"])
		if maxPerSegment > count {
			count = maxPerSegment
		}
		if previous, ok := loraCandidateRuntimeFromSegment(seg); ok && len(previous) > 0 {
			if loraCandidateRuntimeHasCandidates(previous) {
				next["previous_candidate_runtime"] = copyAnyMap(previous)
			}
		}
	}
	if count > 0 {
		next["candidate_attempt_count"] = count
	}
	return next
}

func loraCandidateRuntimeCountFromSegment(seg *models.Segment) int {
	runtime, ok := loraCandidateRuntimeFromSegment(seg)
	if !ok || len(runtime) == 0 {
		return 0
	}
	if count := intFromAny(runtime["candidate_attempt_count"]); count > 0 {
		return count
	}
	if loraCandidateRuntimeConsumesAttempt(runtime) {
		return 1
	}
	return 0
}

func loraCandidateRuntimeRepresentsExecution(runtime map[string]any) bool {
	if len(runtime) == 0 {
		return false
	}
	if runtime["execution_skipped_by_attempt_limit"] == true {
		return false
	}
	return runtime["execution_requested"] == true
}

func loraCandidateRuntimeConsumesAttempt(runtime map[string]any) bool {
	if !loraCandidateRuntimeRepresentsExecution(runtime) {
		return false
	}
	if loraCandidateRuntimeNeedsTextCompression(runtime) {
		return false
	}
	switch stringFromAny(runtime["fallback_reason"]) {
	case "candidate_execution_not_available",
		"candidate_execution_error",
		"default_tts_failed_after_candidate_fallback":
		return false
	case "all_candidates_rejected":
		return true
	}
	if loraCandidateRuntimeHasCandidates(runtime) {
		return true
	}
	return stringFromAny(runtime["audio_selection_mode"]) == "candidate_audio"
}

func loraCandidateRuntimeNeedsTextCompression(runtime map[string]any) bool {
	if len(runtime) == 0 {
		return false
	}
	if gate, _ := runtime["gate_decision"].(map[string]any); gate != nil && gate["needs_text_compression"] == true {
		return true
	}
	if recommendation, _ := runtime["text_compression_recommendation"].(map[string]any); recommendation != nil && recommendation["needed"] == true {
		return true
	}
	if stringFromAny(runtime["stop_reason"]) == "needs_text_compression_speech_rate" {
		return true
	}
	previous, _ := runtime["previous_candidate_runtime"].(map[string]any)
	return loraCandidateRuntimeNeedsTextCompression(previous)
}

func loraCandidateRuntimeHasCandidates(runtime map[string]any) bool {
	if len(runtime) == 0 {
		return false
	}
	raw, ok := runtime["candidates"]
	if !ok {
		return false
	}
	switch candidates := raw.(type) {
	case []any:
		return len(candidates) > 0
	case []map[string]any:
		return len(candidates) > 0
	default:
		return false
	}
}

func loraCandidateRuntimeFromSegment(seg *models.Segment) (map[string]any, bool) {
	if seg == nil || seg.Meta == nil {
		return nil, false
	}
	raw, ok := seg.Meta[ttsLoraCandidateRuntimeMetaKey]
	if !ok {
		return nil, false
	}
	switch v := raw.(type) {
	case map[string]any:
		return v, true
	default:
		return nil, false
	}
}

func mergeLoraCandidateRuntime(candidate map[string]any, runtime map[string]any) {
	if len(candidate) == 0 || len(runtime) == 0 {
		return
	}
	candidate["runtime"] = runtime
	for _, key := range []string{
		"audio_selection_mode",
		"selected_family",
		"selection_reason",
		"fallback_reason",
		"candidate_execution_enabled",
		"candidate_attempt",
		"candidate_attempt_count",
		"execution_skipped_by_attempt_limit",
	} {
		if v, ok := runtime[key]; ok {
			candidate[key] = v
		}
	}
}

func intFromAny(raw any) int {
	switch v := raw.(type) {
	case int:
		return v
	case int64:
		return int(v)
	case float64:
		return int(v)
	case float32:
		return int(v)
	default:
		return 0
	}
}

func stringFromAny(raw any) string {
	if s, ok := raw.(string); ok {
		return s
	}
	return ""
}

func loraCandidateMeta(meta map[string]any) (map[string]any, bool) {
	if meta == nil {
		return nil, false
	}
	raw, ok := meta["lora_candidate"]
	if !ok {
		return nil, false
	}
	switch v := raw.(type) {
	case map[string]any:
		return v, true
	default:
		return nil, false
	}
}

func copyAnyMap(in map[string]any) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

func stringSliceFromAny(raw any) []string {
	switch xs := raw.(type) {
	case []string:
		return append([]string(nil), xs...)
	case []any:
		out := make([]string, 0, len(xs))
		for _, item := range xs {
			if s, ok := item.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	default:
		return nil
	}
}

func loraCandidateRuntimeDiagnostic(runtime map[string]any) string {
	return fmt.Sprintf(
		"lora_candidate_runtime selected=%v fallback=%v gate=%v",
		runtime["selected_family"],
		runtime["fallback_reason"],
		runtime["gate_decision"],
	)
}
