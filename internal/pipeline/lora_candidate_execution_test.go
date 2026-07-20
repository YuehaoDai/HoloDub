package pipeline

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"holodub/internal/agents"
	"holodub/internal/ml"
	"holodub/internal/models"
)

func TestLoraCandidateExecutionRequestedRequiresExplicitCandidateAudio(t *testing.T) {
	if loraCandidateExecutionRequested(nil) {
		t.Fatal("nil meta should not request candidate execution")
	}
	meta := map[string]any{
		"lora_candidate": map[string]any{
			"enabled":                     true,
			"candidate_execution_enabled": true,
			"audio_selection_mode":        "default_audio",
		},
	}
	if loraCandidateExecutionRequested(meta) {
		t.Fatal("default_audio should not request candidate execution")
	}
	meta["lora_candidate"].(map[string]any)["audio_selection_mode"] = "candidate_audio"
	if !loraCandidateExecutionRequested(meta) {
		t.Fatal("candidate_audio opt-in should request candidate execution")
	}
}

func TestLoraCandidateFallbackRequestRecordsUnavailableRuntime(t *testing.T) {
	request := ml.TTSRequest{
		Text:              "long text",
		TargetDurationSec: 21,
		OutputRelPath:     "jobs/1/tts/vp0/segment-0001.wav",
		DubbingMeta: map[string]any{
			"lora_candidate": map[string]any{
				"enabled":                     true,
				"execution_mode":              "candidate_fallback_v1",
				"candidate_execution_enabled": true,
				"audio_selection_mode":        "candidate_audio",
				"selected_family":             "default",
				"candidate_families":          []string{"default", "split_light_vector", "split_mix"},
			},
		},
	}

	got, runtime := loraCandidateFallbackRequest(request, "candidate_execution_not_available")

	if runtime["selected_family"] != "default" || runtime["fallback_reason"] != "candidate_execution_not_available" {
		t.Fatalf("unexpected runtime: %+v", runtime)
	}
	gate := runtime["gate_decision"].(map[string]any)
	if gate["status"] != "skipped" || gate["reason"] != "candidate_execution_not_available" {
		t.Fatalf("unexpected gate decision: %+v", gate)
	}
	candidate := got.DubbingMeta["lora_candidate"].(map[string]any)
	if candidate["candidate_execution_enabled"] != false {
		t.Fatalf("fallback request must disable execution before ML call: %+v", candidate)
	}
	if candidate["audio_selection_mode"] != "default_audio" || candidate["fallback_reason"] != "candidate_execution_not_available" {
		t.Fatalf("unexpected fallback candidate meta: %+v", candidate)
	}
	if candidate["runtime"] == nil {
		t.Fatalf("fallback runtime missing from candidate meta: %+v", candidate)
	}
	if request.DubbingMeta["lora_candidate"].(map[string]any)["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("fallback mutated original request meta: %+v", request.DubbingMeta)
	}
}

func TestMergeLoraCandidateRuntimeIntoProvenance(t *testing.T) {
	candidate := map[string]any{
		"selected_family":      "default",
		"audio_selection_mode": "candidate_audio",
	}
	runtime := map[string]any{
		"selected_family":      "default",
		"audio_selection_mode": "default_audio",
		"selection_reason":     "candidate_execution_not_available",
		"fallback_reason":      "candidate_execution_not_available",
	}

	mergeLoraCandidateRuntime(candidate, runtime)

	if candidate["audio_selection_mode"] != "default_audio" {
		t.Fatalf("audio selection not merged: %+v", candidate)
	}
	if candidate["fallback_reason"] != "candidate_execution_not_available" {
		t.Fatalf("fallback reason not merged: %+v", candidate)
	}
	if candidate["runtime"] == nil {
		t.Fatalf("runtime missing after merge: %+v", candidate)
	}
}

func TestApplyLoraCandidateAttemptLimitDisablesExecutionAtMax(t *testing.T) {
	meta := map[string]any{
		"pacing": "normal",
		"lora_candidate": map[string]any{
			"enabled":                     true,
			"execution_mode":              "candidate_fallback_v1",
			"candidate_execution_enabled": true,
			"audio_selection_mode":        "candidate_audio",
			"selected_family":             "default",
			"candidate_families":          []string{"default", "split_light_vector", "split_mix"},
			"max_per_segment":             2,
		},
	}

	beforeLimit := applyLoraCandidateAttemptLimit(meta, 1)
	if beforeLimit["lora_candidate"].(map[string]any)["candidate_execution_enabled"] != true {
		t.Fatalf("attempt below limit should keep candidate execution: %+v", beforeLimit)
	}

	got := applyLoraCandidateAttemptLimit(meta, 2)
	candidate := got["lora_candidate"].(map[string]any)
	if candidate["candidate_execution_enabled"] != false || candidate["audio_selection_mode"] != "default_audio" {
		t.Fatalf("attempt at limit should disable candidate execution: %+v", candidate)
	}
	if candidate["fallback_reason"] != "candidate_attempt_limit_reached" {
		t.Fatalf("unexpected fallback reason: %+v", candidate)
	}
	if candidate["execution_skipped_by_attempt_limit"] != true || candidate["candidate_attempt"] != 2 {
		t.Fatalf("missing attempt-limit provenance: %+v", candidate)
	}
	if meta["lora_candidate"].(map[string]any)["candidate_execution_enabled"] != true {
		t.Fatalf("attempt limit mutated original meta: %+v", meta)
	}
}

func TestRunTTSWithLoraCandidateAttemptLimitReturnsSkippedRuntime(t *testing.T) {
	request := loraCandidateTestRequest()
	request.DubbingMeta["lora_candidate"].(map[string]any)["max_per_segment"] = 2
	request.DubbingMeta = applyLoraCandidateAttemptLimit(request.DubbingMeta, 2)
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var got ml.TTSRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		candidate := got.DubbingMeta["lora_candidate"].(map[string]any)
		if candidate["candidate_execution_enabled"] != false || candidate["audio_selection_mode"] != "default_audio" {
			t.Fatalf("attempt-limited request should be default_audio: %+v", candidate)
		}
		_ = json.NewEncoder(w).Encode(ml.TTSResponse{
			AudioRelPath:     got.OutputRelPath,
			ActualDurationMs: 21000,
		})
	}))
	defer server.Close()

	svc := &Service{ml: ml.New(server.URL)}
	resp, runtime, err := svc.runTTSWithLoraCandidateFallback(context.Background(), request)
	if err != nil {
		t.Fatalf("runTTSWithLoraCandidateFallback error: %v", err)
	}
	if calls != 1 {
		t.Fatalf("ML calls = %d, want 1", calls)
	}
	if resp.AudioRelPath != request.OutputRelPath {
		t.Fatalf("audio_relpath = %q", resp.AudioRelPath)
	}
	if runtime["fallback_reason"] != "candidate_attempt_limit_reached" {
		t.Fatalf("unexpected runtime: %+v", runtime)
	}
	if runtime["candidate_execution_enabled"] != false || runtime["execution_skipped_by_attempt_limit"] != true {
		t.Fatalf("missing attempt-limit runtime markers: %+v", runtime)
	}
}

func TestLoraCandidateRuntimeCountSurvivesRestarts(t *testing.T) {
	seg := &models.Segment{
		Meta: map[string]any{
			ttsLoraCandidateRuntimeMetaKey: map[string]any{
				"execution_requested": true,
				"fallback_reason":     "all_candidates_rejected",
			},
		},
	}
	if got := loraCandidateRuntimeCountFromSegment(seg); got != 1 {
		t.Fatalf("legacy runtime without count should count as one candidate execution, got %d", got)
	}

	next := withLoraCandidateRuntimeCount(seg, map[string]any{
		"execution_requested": true,
		"fallback_reason":     "all_candidates_rejected",
	})
	if next["candidate_attempt_count"] != 2 {
		t.Fatalf("candidate runtime count not incremented: %+v", next)
	}

	applyLoraCandidateRuntime(seg, next)
	if got := loraCandidateRuntimeCountFromSegment(seg); got != 2 {
		t.Fatalf("persisted runtime count = %d, want 2", got)
	}
}

func TestLoraCandidateRuntimeCountIgnoresUnavailableFallback(t *testing.T) {
	seg := &models.Segment{
		Meta: map[string]any{
			ttsLoraCandidateRuntimeMetaKey: map[string]any{
				"execution_requested":  true,
				"fallback_reason":      "candidate_execution_not_available",
				"audio_selection_mode": "default_audio",
			},
		},
	}
	if got := loraCandidateRuntimeCountFromSegment(seg); got != 0 {
		t.Fatalf("candidate unavailable should not consume retry budget, got %d", got)
	}

	next := withLoraCandidateRuntimeCount(seg, map[string]any{
		"execution_requested": true,
		"fallback_reason":     "all_candidates_rejected",
		"candidates": []any{
			map[string]any{"family": "split_mix"},
		},
	})
	if next["candidate_attempt_count"] != 1 {
		t.Fatalf("first real candidate attempt should count as 1, got runtime=%+v", next)
	}
}

func TestLoraCandidateRuntimeCountIgnoresTextCompressionPlanningSignal(t *testing.T) {
	seg := &models.Segment{
		Meta: map[string]any{
			ttsLoraCandidateRuntimeMetaKey: map[string]any{
				"execution_requested": true,
				"fallback_reason":     "all_candidates_rejected",
				"gate_decision": map[string]any{
					"status":                 "fallback",
					"needs_text_compression": true,
				},
				"text_compression_recommendation": map[string]any{
					"needed":        true,
					"best_drift_ms": 4100.0,
				},
			},
		},
	}
	if got := loraCandidateRuntimeCountFromSegment(seg); got != 0 {
		t.Fatalf("text-compression planning signal should not consume candidate budget, got %d", got)
	}

	next := withLoraCandidateRuntimeCount(seg, map[string]any{
		"execution_requested": true,
		"fallback_reason":     "all_candidates_rejected",
		"stop_reason":         "needs_text_compression_speech_rate",
		"candidates": []any{
			map[string]any{"family": "split_mix"},
		},
	})
	if _, ok := next["candidate_attempt_count"]; ok {
		t.Fatalf("text-compression candidate runtime should not get candidate_attempt_count: %+v", next)
	}
}

func TestAttemptLimitSkippedRuntimePreservesPreviousCandidateRuntime(t *testing.T) {
	previous := map[string]any{
		"execution_requested": true,
		"fallback_reason":     "all_candidates_rejected",
		"candidates": []any{
			map[string]any{
				"family":         "split_mix",
				"reject_reasons": []any{"low_asr"},
			},
		},
	}
	seg := &models.Segment{
		Meta: map[string]any{
			ttsLoraCandidateRuntimeMetaKey: previous,
		},
	}
	runtime := withLoraCandidateRuntimeCount(seg, map[string]any{
		"execution_requested":                false,
		"execution_skipped_by_attempt_limit": true,
		"fallback_reason":                    "candidate_attempt_limit_reached",
		"max_per_segment":                    2,
	})

	if runtime["candidate_attempt_count"] != 2 {
		t.Fatalf("skipped runtime count = %#v, want 2; runtime=%+v", runtime["candidate_attempt_count"], runtime)
	}
	prior, ok := runtime["previous_candidate_runtime"].(map[string]any)
	if !ok {
		t.Fatalf("previous candidate runtime missing: %+v", runtime)
	}
	if prior["fallback_reason"] != "all_candidates_rejected" {
		t.Fatalf("unexpected previous runtime: %+v", prior)
	}
}

func TestTranslationPersistingToolsAttemptLimitKeepsInMemoryCandidateEvidence(t *testing.T) {
	previous := map[string]any{
		"execution_requested": true,
		"fallback_reason":     "all_candidates_rejected",
		"candidates": []any{
			map[string]any{
				"family":         "split_mix",
				"reject_reasons": []any{"bad_join_quality"},
			},
		},
	}
	inner := &scriptedDubbingTools{
		tts: []agents.TTSResult{
			{
				AudioRelPath:         "attempt0.wav",
				ActualDurationMs:     40651,
				LoraCandidateRuntime: previous,
			},
			{
				AudioRelPath:     "attempt1.wav",
				ActualDurationMs: 36910,
				LoraCandidateRuntime: map[string]any{
					"execution_requested":                false,
					"execution_skipped_by_attempt_limit": true,
					"fallback_reason":                    "candidate_attempt_limit_reached",
					"max_per_segment":                    2,
				},
			},
		},
	}
	seg := &models.Segment{ID: 1}
	tools := &translationPersistingTools{inner: inner, seg: seg}

	if _, err := tools.Synthesize(context.Background(), agents.TTSArgs{}); err != nil {
		t.Fatalf("first synth: %v", err)
	}
	delete(seg.Meta, ttsLoraCandidateRuntimeMetaKey)

	got, err := tools.Synthesize(context.Background(), agents.TTSArgs{})
	if err != nil {
		t.Fatalf("second synth: %v", err)
	}
	if got.LoraCandidateRuntime["candidate_attempt_count"] != 2 {
		t.Fatalf("skipped runtime should preserve budget count: %+v", got.LoraCandidateRuntime)
	}
	prior, ok := got.LoraCandidateRuntime["previous_candidate_runtime"].(map[string]any)
	if !ok {
		t.Fatalf("skipped runtime lost previous candidate evidence: %+v", got.LoraCandidateRuntime)
	}
	if prior["fallback_reason"] != "all_candidates_rejected" {
		t.Fatalf("unexpected previous runtime: %+v", prior)
	}
	if !loraCandidateRuntimeHasCandidates(prior) {
		t.Fatalf("previous runtime must keep candidates for offline replay: %+v", prior)
	}
}

func TestRealDubbingToolsAttemptLimitUsesPriorCandidateCountOnly(t *testing.T) {
	requests := make([]ml.TTSRequest, 0, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var got ml.TTSRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		requests = append(requests, got)
		_ = json.NewEncoder(w).Encode(ml.TTSResponse{
			AudioRelPath:     got.OutputRelPath,
			ActualDurationMs: 21000,
			CandidateResult: map[string]any{
				"schema_version":       1,
				"execution_requested":  true,
				"audio_selection_mode": "candidate_audio",
				"selected_family":      "split_mix",
				"fallback_reason":      "",
				"gate_decision":        map[string]any{"status": "selected"},
			},
		})
	}))
	defer server.Close()

	tools := &realDubbingTools{svc: &Service{ml: ml.New(server.URL)}}
	_, err := tools.Synthesize(context.Background(), agents.TTSArgs{
		Text:                    "long text",
		TargetLanguage:          "zh-CN",
		TargetDurationSec:       21,
		MaxAllowedSec:           22,
		OutputRelPath:           "jobs/1/tts/vp0/segment-0001.agent_attempt01.wav",
		PriorLoraCandidateCount: 1,
		Attempt:                 1,
		DubbingMeta: map[string]any{
			"lora_candidate": map[string]any{
				"enabled":                     true,
				"execution_mode":              "candidate_fallback_v1",
				"candidate_execution_enabled": true,
				"audio_selection_mode":        "candidate_audio",
				"selected_family":             "default",
				"candidate_families":          []string{"default", "split_light_vector", "split_mix"},
				"max_per_segment":             2,
			},
		},
	})
	if err != nil {
		t.Fatalf("Synthesize error: %v", err)
	}
	if len(requests) != 1 {
		t.Fatalf("ML requests = %d, want 1", len(requests))
	}
	candidate := requests[0].DubbingMeta["lora_candidate"].(map[string]any)
	if candidate["candidate_execution_enabled"] != true || candidate["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("second real candidate opportunity should stay enabled: %+v", candidate)
	}
}

func TestRunTTSWithLoraCandidateUsesMLCandidateResult(t *testing.T) {
	request := loraCandidateTestRequest()
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/tts/run" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		calls++
		var got ml.TTSRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		if got.TargetLanguage != "zh-CN" {
			t.Fatalf("target_language = %q, want zh-CN", got.TargetLanguage)
		}
		candidate := got.DubbingMeta["lora_candidate"].(map[string]any)
		if candidate["audio_selection_mode"] != "candidate_audio" {
			t.Fatalf("candidate request was pre-disabled: %+v", candidate)
		}
		_ = json.NewEncoder(w).Encode(ml.TTSResponse{
			AudioRelPath:     got.OutputRelPath,
			ActualDurationMs: 21000,
			CandidateResult: map[string]any{
				"schema_version":       1,
				"execution_requested":  true,
				"audio_selection_mode": "candidate_audio",
				"selected_family":      "split_mix",
				"fallback_reason":      "",
				"gate_decision":        map[string]any{"status": "selected"},
			},
		})
	}))
	defer server.Close()

	svc := &Service{ml: ml.New(server.URL)}
	resp, runtime, err := svc.runTTSWithLoraCandidateFallback(context.Background(), request)
	if err != nil {
		t.Fatalf("runTTSWithLoraCandidateFallback error: %v", err)
	}
	if calls != 1 {
		t.Fatalf("ML calls = %d, want 1", calls)
	}
	if resp.AudioRelPath != request.OutputRelPath {
		t.Fatalf("audio_relpath = %q", resp.AudioRelPath)
	}
	if runtime["selected_family"] != "split_mix" || runtime["fallback_reason"] != "" {
		t.Fatalf("unexpected runtime: %+v", runtime)
	}
}

func TestRunTTSWithLoraCandidateFallsBackWhenMLReturnsNoRuntime(t *testing.T) {
	request := loraCandidateTestRequest()
	var calls []ml.TTSRequest
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var got ml.TTSRequest
		if err := json.NewDecoder(r.Body).Decode(&got); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		calls = append(calls, got)
		_ = json.NewEncoder(w).Encode(ml.TTSResponse{
			AudioRelPath:     got.OutputRelPath,
			ActualDurationMs: 21000,
		})
	}))
	defer server.Close()

	svc := &Service{ml: ml.New(server.URL)}
	_, runtime, err := svc.runTTSWithLoraCandidateFallback(context.Background(), request)
	if err != nil {
		t.Fatalf("runTTSWithLoraCandidateFallback error: %v", err)
	}
	if len(calls) != 2 {
		t.Fatalf("ML calls = %d, want 2", len(calls))
	}
	first := calls[0].DubbingMeta["lora_candidate"].(map[string]any)
	second := calls[1].DubbingMeta["lora_candidate"].(map[string]any)
	if first["audio_selection_mode"] != "candidate_audio" {
		t.Fatalf("first request should keep candidate_audio: %+v", first)
	}
	if second["candidate_execution_enabled"] != false || second["audio_selection_mode"] != "default_audio" {
		t.Fatalf("fallback request did not disable candidate execution: %+v", second)
	}
	if runtime["fallback_reason"] != "candidate_execution_not_available" {
		t.Fatalf("unexpected runtime: %+v", runtime)
	}
}

type scriptedDubbingTools struct {
	tts   []agents.TTSResult
	calls int
}

func (s *scriptedDubbingTools) Synthesize(ctx context.Context, args agents.TTSArgs) (agents.TTSResult, error) {
	if err := ctx.Err(); err != nil {
		return agents.TTSResult{}, err
	}
	if s.calls >= len(s.tts) {
		return agents.TTSResult{}, errors.New("scriptedDubbingTools: no more synth results")
	}
	res := s.tts[s.calls]
	s.calls++
	return res, nil
}

func (s *scriptedDubbingTools) RetranslateWithConstraint(ctx context.Context, args agents.RetranslateArgs) (agents.RetranslateResult, error) {
	return agents.RetranslateResult{}, nil
}

func (s *scriptedDubbingTools) RetranslateEnsemble(ctx context.Context, args agents.RetranslateArgs) (agents.EnsembleResult, error) {
	return agents.EnsembleResult{}, agents.ErrEnsembleUnavailable
}

func (s *scriptedDubbingTools) JudgeFidelity(ctx context.Context, args agents.JudgeArgs) (*agents.JudgeResult, error) {
	return nil, nil
}

func loraCandidateTestRequest() ml.TTSRequest {
	return ml.TTSRequest{
		Text:              "long text",
		TargetLanguage:    "zh-CN",
		TargetDurationSec: 21,
		OutputRelPath:     "jobs/1/tts/vp0/segment-0001.wav",
		DubbingMeta: map[string]any{
			"lora_candidate": map[string]any{
				"enabled":                     true,
				"execution_mode":              "candidate_fallback_v1",
				"candidate_execution_enabled": true,
				"audio_selection_mode":        "candidate_audio",
				"selected_family":             "default",
				"candidate_families":          []string{"default", "split_light_vector", "split_mix"},
			},
		},
	}
}

// ── clearSupersededLoraCandidateRuntime (2026-07-21 fit_audit stale-fit fix) ──
//
// A default-route re-synthesis that runs no candidates must drop the
// previous candidate runtime; otherwise fit_audit keeps flagging the fresh
// default audio with a stale rejected candidate's fit (production case:
// ep183 segment 16277).

func TestClearSupersededLoraCandidateRuntime(t *testing.T) {
	t.Run("drops stored runtime via nil delete convention", func(t *testing.T) {
		seg := &models.Segment{
			Meta: map[string]any{
				ttsLoraCandidateRuntimeMetaKey: map[string]any{
					"audio_selection_mode": "default_audio",
					"candidates":           []any{map[string]any{"fit_tempo_delta": 0.45}},
				},
				"other_key": "kept",
			},
		}
		clearSupersededLoraCandidateRuntime(seg)
		v, ok := seg.Meta[ttsLoraCandidateRuntimeMetaKey]
		if !ok {
			t.Fatal("key should be present as nil (delete convention), not missing")
		}
		if v != nil {
			t.Fatalf("expected nil value after clearing, got %#v", v)
		}
		if seg.Meta["other_key"] != "kept" {
			t.Fatal("unrelated meta keys must be preserved")
		}
	})

	t.Run("no-ops without runtime or meta", func(t *testing.T) {
		clearSupersededLoraCandidateRuntime(nil)
		clearSupersededLoraCandidateRuntime(&models.Segment{})
		seg := &models.Segment{Meta: map[string]any{"other_key": 1}}
		clearSupersededLoraCandidateRuntime(seg)
		if _, ok := seg.Meta[ttsLoraCandidateRuntimeMetaKey]; ok {
			t.Fatal("must not create the key when absent")
		}
	})
}
