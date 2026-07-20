package main

import (
	"testing"

	"gorm.io/datatypes"
)

func TestCleanSegmentMetaRemovesLoraCandidateRuntime(t *testing.T) {
	meta := datatypes.JSONMap{
		"dubbing":                    map[string]any{"pacing": "steady"},
		"tts_lora_candidate":         map[string]any{"selected_family": "split_mix"},
		"tts_lora_candidate_runtime": map[string]any{"selected_family": "split_mix"},
		"tts_spoken_text":            "old spoken text",
		"voice_config_source":        "global_default_fallback",
	}

	clean := cleanSegmentMeta(meta)

	if _, ok := clean["tts_lora_candidate"]; ok {
		t.Fatalf("tts_lora_candidate was not removed: %#v", clean)
	}
	if _, ok := clean["tts_lora_candidate_runtime"]; ok {
		t.Fatalf("tts_lora_candidate_runtime was not removed: %#v", clean)
	}
	if _, ok := clean["tts_spoken_text"]; ok {
		t.Fatalf("tts_spoken_text was not removed: %#v", clean)
	}
	if _, ok := clean["voice_config_source"]; ok {
		t.Fatalf("voice_config_source was not removed: %#v", clean)
	}
	if _, ok := clean["dubbing"]; !ok {
		t.Fatalf("dubbing meta should be preserved: %#v", clean)
	}
}

func TestShadowSegmentMetaRecordsSourceLineage(t *testing.T) {
	meta := shadowSegmentMeta(datatypes.JSONMap{"dubbing": map[string]any{"pacing": "steady"}}, 170, 10694)

	if got := meta["shadow_source_job_id"]; got != uint(170) {
		t.Fatalf("shadow_source_job_id = %#v, want 170", got)
	}
	if got := meta["shadow_source_segment_id"]; got != uint(10694) {
		t.Fatalf("shadow_source_segment_id = %#v, want 10694", got)
	}
	if _, ok := meta["dubbing"]; !ok {
		t.Fatalf("dubbing meta should be preserved: %#v", meta)
	}
}
