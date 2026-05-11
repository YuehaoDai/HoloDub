package llm

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestChapterJudgeSchemaValid ensures the static chapterJudgeToolSchema parses
// to the shape we expect. Catches typos in the schema literal at build time
// (mustMarshalJSON would panic; this test pins the property names too).
func TestChapterJudgeSchemaValid(t *testing.T) {
	var schema map[string]any
	if err := json.Unmarshal(chapterJudgeToolSchema, &schema); err != nil {
		t.Fatalf("chapterJudgeToolSchema is not valid JSON: %v", err)
	}
	props, ok := schema["properties"].(map[string]any)
	if !ok {
		t.Fatal("schema properties missing or wrong type")
	}
	for _, k := range []string{
		"narrative_coherence_within_chapter",
		"speaker_voice_stability_within_chapter",
		"terminology_consistency_within_chapter",
		"register_consistency_within_chapter",
		"overall_fidelity_chapter",
		"overall_fluency_chapter",
		"top_3_weakest_segments",
		"verdict",
	} {
		if _, ok := props[k]; !ok {
			t.Fatalf("schema missing property %q", k)
		}
	}
	required, ok := schema["required"].([]any)
	if !ok || len(required) != 7 {
		t.Fatalf("schema required must list 7 fields, got %v", schema["required"])
	}
	if v, ok := schema["additionalProperties"].(bool); !ok || v {
		t.Fatal("schema additionalProperties must be false")
	}
	weakest := props["top_3_weakest_segments"].(map[string]any)
	if mi, _ := weakest["maxItems"].(float64); int(mi) != 3 {
		t.Fatalf("top_3_weakest_segments.maxItems must be 3, got %v", weakest["maxItems"])
	}
}

// TestChapterJudgeDisabledByDefault: empty chapterJudgeModel must short-
// circuit without touching the network.
func TestChapterJudgeDisabledByDefault(t *testing.T) {
	called := false
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		chapterJudgeModel: "", // disabled
		httpClient:        &http.Client{Timeout: 1 * time.Second},
	}
	got, err := c.JudgeChapter(context.Background(), ChapterJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: []ChapterJudgeSegment{
			{Ordinal: 1, SourceText: "hi", TargetText: "你好"},
		},
	})
	if err != nil {
		t.Fatalf("disabled chapter judge must not error, got: %v", err)
	}
	if got != nil {
		t.Fatalf("disabled chapter judge must return nil result, got: %+v", got)
	}
	if called {
		t.Fatal("disabled chapter judge must not trigger HTTP call")
	}
}

// TestChapterJudgeEmptySegmentsSkip: zero segments skips without HTTP call.
func TestChapterJudgeEmptySegmentsSkip(t *testing.T) {
	called := false
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		chapterJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeChapter(context.Background(), ChapterJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: nil,
	})
	if err != nil {
		t.Fatalf("empty segments must not error: %v", err)
	}
	if got != nil {
		t.Fatal("empty segments must return nil result")
	}
	if called {
		t.Fatal("empty segments must not trigger HTTP call")
	}
}

// TestChapterJudgeHappyPath: mock LLM returns a valid 7-axis verdict; we get
// it back parsed correctly and the OverallScore equals overall_fidelity_chapter.
func TestChapterJudgeHappyPath(t *testing.T) {
	verdictJSON := `{
		"narrative_coherence_within_chapter": 0.92,
		"speaker_voice_stability_within_chapter": 0.88,
		"terminology_consistency_within_chapter": 0.95,
		"register_consistency_within_chapter": 0.90,
		"overall_fidelity_chapter": 0.93,
		"overall_fluency_chapter": 0.91,
		"top_3_weakest_segments": [
			{"ordinal": 5, "issue": "term drift", "recommended_fix": "use 'distributed system'"}
		],
		"verdict": "needs_revision",
		"one_paragraph_summary": "整体可用,只有一段术语不一致。"
	}`
	resp := buildToolCallResponse("emit_chapter_judge_verdict", verdictJSON, providerUsage{
		PromptTokens: 1500, CompletionTokens: 80,
	})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		chapterJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeChapter(context.Background(), ChapterJudgeArgs{
		SourceLang: "en", TargetLang: "zh-CN",
		ChapterOrdinal: 3, ChapterTitle: "Distributed consensus",
		EpisodeSummary: "MIT 6.824 lecture",
		Segments: []ChapterJudgeSegment{
			{Ordinal: 1, StartMs: 0, EndMs: 5000, SourceText: "Hello", TargetText: "你好"},
			{Ordinal: 5, StartMs: 30000, EndMs: 33000, SourceText: "distributed system", TargetText: "分布式系统"},
		},
	})
	if err != nil {
		t.Fatalf("JudgeChapter: %v", err)
	}
	if got == nil {
		t.Fatal("expected non-nil result")
	}
	if got.OverallFidelityChapter != 0.93 {
		t.Fatalf("overall_fidelity_chapter mismatch: got %v", got.OverallFidelityChapter)
	}
	if got.NarrativeCoherenceWithinChapter != 0.92 {
		t.Fatalf("narrative_coherence mismatch: got %v", got.NarrativeCoherenceWithinChapter)
	}
	if got.Verdict != "needs_revision" {
		t.Fatalf("verdict mismatch: %q", got.Verdict)
	}
	if len(got.Top3WeakestSegments) != 1 || got.Top3WeakestSegments[0].Ordinal != 5 {
		t.Fatalf("weakest segments: %+v", got.Top3WeakestSegments)
	}
	if got.OverallScore() != 0.93 {
		t.Fatalf("OverallScore should equal OverallFidelityChapter, got %v", got.OverallScore())
	}
}

// TestChapterJudgeMissingToolCall: provider returns content instead of a
// tool call → JudgeChapter returns error (chapter judge schema is mandatory,
// no silent fallback to prose parsing).
func TestChapterJudgeMissingToolCall(t *testing.T) {
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"sorry can't comply"}}],"usage":{"prompt_tokens":100}}`))
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		chapterJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeChapter(context.Background(), ChapterJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: []ChapterJudgeSegment{
			{Ordinal: 1, SourceText: "x", TargetText: "y"},
		},
	})
	if err == nil {
		t.Fatalf("expected error when LLM bypasses tool, got result %+v", got)
	}
	if got != nil {
		t.Fatalf("expected nil result on error, got %+v", got)
	}
}

// TestChapterJudgeMissingVerdictDefaultsRevision: schema may slip with weak
// provider strict-mode; missing verdict defaults to "needs_revision" (safer
// than "chapter_ready" — pushes operator review).
func TestChapterJudgeMissingVerdictDefaultsRevision(t *testing.T) {
	verdictJSON := `{
		"narrative_coherence_within_chapter": 0.5,
		"speaker_voice_stability_within_chapter": 0.6,
		"terminology_consistency_within_chapter": 0.7,
		"register_consistency_within_chapter": 0.8,
		"overall_fidelity_chapter": 0.6,
		"overall_fluency_chapter": 0.7
	}`
	resp := buildToolCallResponse("emit_chapter_judge_verdict", verdictJSON, providerUsage{
		PromptTokens: 1000, CompletionTokens: 50,
	})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		chapterJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeChapter(context.Background(), ChapterJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: []ChapterJudgeSegment{{Ordinal: 1, SourceText: "x", TargetText: "y"}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Verdict != "needs_revision" {
		t.Fatalf("missing verdict should default to needs_revision, got %q", got.Verdict)
	}
}

// TestChapterJudgeUserMsgIncludesSegments: verifies prompt assembly contains
// every segment with its ordinal + duration + (optional) seg_judge hint, and
// that the optional EpisodeSummary / GlossaryHint are included when provided.
func TestChapterJudgeUserMsgIncludesSegments(t *testing.T) {
	score := 0.42
	args := ChapterJudgeArgs{
		SourceLang: "en", TargetLang: "zh-CN",
		ChapterOrdinal: 2, ChapterTitle: "Raft basics",
		EpisodeSummary: "REFERENCE_CARD_TOKEN",
		GlossaryHint:   "GLOSSARY_TOKEN",
		Segments: []ChapterJudgeSegment{
			{Ordinal: 1, StartMs: 0, EndMs: 4000, SourceText: "Hello", TargetText: "你好", SegJudgeScore: &score},
			{Ordinal: 2, StartMs: 5000, EndMs: 9000, SourceText: "world", TargetText: "世界"},
		},
	}
	got := buildChapterJudgeUserMsg(args)
	for _, want := range []string{
		"Chapter 2",
		"Raft basics",
		"REFERENCE_CARD_TOKEN",
		"GLOSSARY_TOKEN",
		"[seg1]",
		"[seg2]",
		"seg_judge=0.42",
		"emit_chapter_judge_verdict",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("user message missing %q\n---\n%s", want, got)
		}
	}
	if strings.Contains(got, "seg_judge=") &&
		strings.Count(got, "seg_judge=") != 1 {
		t.Errorf("seg_judge hint should appear exactly once (only seg1 has score); got %d",
			strings.Count(got, "seg_judge="))
	}
}

// TestChapterJudgeOverallScore: verifies OverallScore averaging logic and
// fidelity-priority semantics.
func TestChapterJudgeOverallScore(t *testing.T) {
	cases := []struct {
		name string
		r    ChapterJudgeResult
		want float64
	}{
		{
			name: "fidelity present takes priority",
			r: ChapterJudgeResult{
				NarrativeCoherenceWithinChapter:     0.9,
				SpeakerVoiceStabilityWithinChapter:  0.9,
				TerminologyConsistencyWithinChapter: 0.9,
				RegisterConsistencyWithinChapter:    0.9,
				OverallFidelityChapter:              0.7, // <- this wins
				OverallFluencyChapter:               0.9,
			},
			want: 0.7,
		},
		{
			name: "fidelity zero, others present → average",
			r: ChapterJudgeResult{
				NarrativeCoherenceWithinChapter:     0.8,
				SpeakerVoiceStabilityWithinChapter:  0.6,
				TerminologyConsistencyWithinChapter: 0.7,
				RegisterConsistencyWithinChapter:    0.7,
				OverallFidelityChapter:              0,
				OverallFluencyChapter:               0.7,
			},
			want: 0.7, // (0.8+0.6+0.7+0.7+0.7)/5
		},
		{
			name: "all zero",
			r:    ChapterJudgeResult{},
			want: 0,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := c.r.OverallScore()
			if got != c.want {
				t.Fatalf("OverallScore want %v, got %v", c.want, got)
			}
		})
	}
}

// TestChapterJudgeThinkingModelUsesAutoToolChoice: when the configured chapter
// judge model name contains "thinking" (eg. kimi-k2-thinking, qwen3-...
// -thinking-2507), the request must downgrade tool_choice from the strict
// object form to "auto" — DashScope reasoning endpoints reject the strict
// form (same constraint OPT-405 glossary discovered).
func TestChapterJudgeThinkingModelUsesAutoToolChoice(t *testing.T) {
	cases := []struct {
		name      string
		model     string
		wantAuto  bool
	}{
		{"thinking model gets auto", "kimi-k2-thinking", true},
		{"qwen3 thinking gets auto", "qwen3-235b-a22b-thinking-2507", true},
		{"non-thinking model keeps strict", "kimi-k2.5", false},
		{"non-thinking qwen-max keeps strict", "qwen-max-latest", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var captured map[string]any
			stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_ = json.NewDecoder(r.Body).Decode(&captured)
				resp := buildToolCallResponse("emit_chapter_judge_verdict",
					`{"narrative_coherence_within_chapter":0.9,"speaker_voice_stability_within_chapter":0.9,`+
						`"terminology_consistency_within_chapter":0.9,"register_consistency_within_chapter":0.9,`+
						`"overall_fidelity_chapter":0.9,"overall_fluency_chapter":0.9,"verdict":"chapter_ready"}`,
					providerUsage{PromptTokens: 100, CompletionTokens: 30})
				body, _ := json.Marshal(resp)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write(body)
			}))
			defer stub.Close()

			c := &Client{
				baseURL:            stub.URL,
				apiKey:             "sk-test",
				chapterJudgeModel:  tc.model,
				httpClient:         &http.Client{Timeout: 5 * time.Second},
				thinkingHTTPClient: &http.Client{Timeout: 5 * time.Second},
			}
			_, err := c.JudgeChapter(context.Background(), ChapterJudgeArgs{
				SourceLang: "en", TargetLang: "zh",
				Segments: []ChapterJudgeSegment{{Ordinal: 1, SourceText: "x", TargetText: "y"}},
			})
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			tc2, ok := captured["tool_choice"]
			if !ok {
				t.Fatal("request payload missing tool_choice")
			}
			if tc.wantAuto {
				if s, isStr := tc2.(string); !isStr || s != "auto" {
					t.Fatalf("expected tool_choice=\"auto\" for thinking model, got %#v", tc2)
				}
			} else {
				m, isMap := tc2.(map[string]any)
				if !isMap {
					t.Fatalf("expected strict tool_choice object for non-thinking model, got %#v", tc2)
				}
				fn, _ := m["function"].(map[string]any)
				if name, _ := fn["name"].(string); name != "emit_chapter_judge_verdict" {
					t.Fatalf("expected forced function name 'emit_chapter_judge_verdict', got %#v", fn)
				}
			}
		})
	}
}
