package llm

import (
	"context"
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestEpisodeJudgeSchemaValid ensures the static episodeJudgeToolSchema parses
// to the shape we expect. Catches typos in the schema literal at build time
// (mustMarshalJSON would panic; this test pins the property names too).
func TestEpisodeJudgeSchemaValid(t *testing.T) {
	var schema map[string]any
	if err := json.Unmarshal(episodeJudgeToolSchema, &schema); err != nil {
		t.Fatalf("episodeJudgeToolSchema is not valid JSON: %v", err)
	}
	props, ok := schema["properties"].(map[string]any)
	if !ok {
		t.Fatal("schema properties missing or wrong type")
	}
	for _, k := range []string{
		"terminology_consistency",
		"register_consistency",
		"narrative_coherence",
		"character_voice_stability",
		"cultural_localization",
		"overall_fidelity",
		"overall_fluency",
		"top_3_weakest_chapters",
		"top_3_weakest_segments",
		"terminology_glossary_observed",
		"verdict",
	} {
		if _, ok := props[k]; !ok {
			t.Fatalf("schema missing property %q", k)
		}
	}
	required, ok := schema["required"].([]any)
	if !ok || len(required) != 8 {
		// 7 axes + verdict = 8 required fields (weakest arrays + glossary
		// + summary are optional so production_ready episodes can omit them)
		t.Fatalf("schema required must list 8 fields (7 axes + verdict), got %v", schema["required"])
	}
	if v, ok := schema["additionalProperties"].(bool); !ok || v {
		t.Fatal("schema additionalProperties must be false")
	}
	weakChapters := props["top_3_weakest_chapters"].(map[string]any)
	if mi, _ := weakChapters["maxItems"].(float64); int(mi) != 3 {
		t.Fatalf("top_3_weakest_chapters.maxItems must be 3, got %v", weakChapters["maxItems"])
	}
	weakSegments := props["top_3_weakest_segments"].(map[string]any)
	if mi, _ := weakSegments["maxItems"].(float64); int(mi) != 3 {
		t.Fatalf("top_3_weakest_segments.maxItems must be 3, got %v", weakSegments["maxItems"])
	}
	// Weakest segment items must require chapter_ordinal so OPT-407 can
	// dispatch a precise segment retranslate without ambiguity.
	segItems := weakSegments["items"].(map[string]any)
	segReq, _ := segItems["required"].([]any)
	hasChapterOrdinal := false
	for _, r := range segReq {
		if s, _ := r.(string); s == "chapter_ordinal" {
			hasChapterOrdinal = true
		}
	}
	if !hasChapterOrdinal {
		t.Fatalf("weakest segment items must require chapter_ordinal, got required=%v", segReq)
	}
}

// TestEpisodeJudgeDisabledByDefault: empty episodeJudgeModel must short-
// circuit without touching the network.
func TestEpisodeJudgeDisabledByDefault(t *testing.T) {
	called := false
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		episodeJudgeModel: "", // disabled
		httpClient:        &http.Client{Timeout: 1 * time.Second},
	}
	got, err := c.JudgeEpisode(context.Background(), EpisodeJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: []EpisodeJudgeSegment{
			{ChapterOrdinal: 1, Ordinal: 1, SourceText: "hi", TargetText: "你好"},
		},
	})
	if err != nil {
		t.Fatalf("disabled episode judge must not error, got: %v", err)
	}
	if got != nil {
		t.Fatalf("disabled episode judge must return nil result, got: %+v", got)
	}
	if called {
		t.Fatal("disabled episode judge must not trigger HTTP call")
	}
}

// TestEpisodeJudgeEmptySegmentsSkip: zero segments skips without HTTP call.
func TestEpisodeJudgeEmptySegmentsSkip(t *testing.T) {
	called := false
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		episodeJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeEpisode(context.Background(), EpisodeJudgeArgs{
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

// TestEpisodeJudgeHappyPath: mock LLM returns a valid 7-axis verdict; we get
// it back parsed correctly (including both weakest arrays + glossary), and
// OverallScore equals overall_fidelity.
func TestEpisodeJudgeHappyPath(t *testing.T) {
	verdictJSON := `{
		"terminology_consistency": 0.88,
		"register_consistency": 0.92,
		"narrative_coherence": 0.90,
		"character_voice_stability": 0.93,
		"cultural_localization": 0.85,
		"overall_fidelity": 0.91,
		"overall_fluency": 0.94,
		"top_3_weakest_chapters": [
			{"ordinal": 3, "issue": "register drifts mid-chapter", "recommended_fix": "re-translate with formal academic tone"}
		],
		"top_3_weakest_segments": [
			{"chapter_ordinal": 5, "ordinal": 12, "issue": "term drift", "recommended_fix": "use 'distributed system'"}
		],
		"terminology_glossary_observed": [
			{"source_term": "consensus", "target_term": "共识"},
			{"source_term": "Raft", "target_term": "Raft", "note": "chapter 2 used 筏式; chapter 5 used Raft — inconsistent"}
		],
		"verdict": "needs_minor_revision",
		"one_paragraph_summary": "整体可用,术语在第二章和第五章不一致,需要小修。"
	}`
	resp := buildToolCallResponse("emit_episode_judge_verdict", verdictJSON, providerUsage{
		PromptTokens: 12000, CompletionTokens: 200,
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
		episodeJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	chapScore := 0.92
	got, err := c.JudgeEpisode(context.Background(), EpisodeJudgeArgs{
		SourceLang: "en", TargetLang: "zh-CN",
		EpisodeID: 142, EpisodeName: "MIT 6.824 lecture",
		EpisodeSummary: "MIT 6.824 distributed systems",
		Chapters: []EpisodeJudgeChapterRow{
			{Ordinal: 1, Title: "Intro", StartMs: 0, EndMs: 600000, ChapterJudgeScore: &chapScore},
			{Ordinal: 2, Title: "Raft", StartMs: 600000, EndMs: 1200000},
		},
		Segments: []EpisodeJudgeSegment{
			{ChapterOrdinal: 1, Ordinal: 1, StartMs: 0, EndMs: 5000, SourceText: "Hello", TargetText: "你好"},
			{ChapterOrdinal: 5, Ordinal: 12, StartMs: 30000, EndMs: 33000, SourceText: "distributed system", TargetText: "分布式系统"},
		},
	})
	if err != nil {
		t.Fatalf("JudgeEpisode: %v", err)
	}
	if got == nil {
		t.Fatal("expected non-nil result")
	}
	if got.OverallFidelity != 0.91 {
		t.Fatalf("overall_fidelity mismatch: got %v", got.OverallFidelity)
	}
	if got.NarrativeCoherence != 0.90 {
		t.Fatalf("narrative_coherence mismatch: got %v", got.NarrativeCoherence)
	}
	if got.CharacterVoiceStability != 0.93 {
		t.Fatalf("character_voice_stability mismatch: got %v", got.CharacterVoiceStability)
	}
	if got.CulturalLocalization != 0.85 {
		t.Fatalf("cultural_localization mismatch: got %v", got.CulturalLocalization)
	}
	if got.Verdict != "needs_minor_revision" {
		t.Fatalf("verdict mismatch: %q", got.Verdict)
	}
	if len(got.Top3WeakestChapters) != 1 || got.Top3WeakestChapters[0].Ordinal != 3 {
		t.Fatalf("weakest chapters: %+v", got.Top3WeakestChapters)
	}
	if len(got.Top3WeakestSegments) != 1 ||
		got.Top3WeakestSegments[0].ChapterOrdinal != 5 ||
		got.Top3WeakestSegments[0].Ordinal != 12 {
		t.Fatalf("weakest segments: %+v", got.Top3WeakestSegments)
	}
	if len(got.TerminologyGlossaryObserved) != 2 {
		t.Fatalf("glossary observed: got %d entries", len(got.TerminologyGlossaryObserved))
	}
	if got.OverallScore() != 0.91 {
		t.Fatalf("OverallScore should equal OverallFidelity, got %v", got.OverallScore())
	}
}

// TestEpisodeJudgeMissingToolCall: provider returns content instead of a
// tool call → JudgeEpisode returns error (episode judge schema is mandatory,
// no silent fallback to prose parsing).
func TestEpisodeJudgeMissingToolCall(t *testing.T) {
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"sorry can't comply"}}],"usage":{"prompt_tokens":100}}`))
	}))
	defer stub.Close()

	c := &Client{
		baseURL:           stub.URL,
		apiKey:            "sk-test",
		episodeJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeEpisode(context.Background(), EpisodeJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: []EpisodeJudgeSegment{
			{ChapterOrdinal: 1, Ordinal: 1, SourceText: "x", TargetText: "y"},
		},
	})
	if err == nil {
		t.Fatalf("expected error when LLM bypasses tool, got result %+v", got)
	}
	if got != nil {
		t.Fatalf("expected nil result on error, got %+v", got)
	}
}

// TestEpisodeJudgeMissingVerdictDefaultsMinorRevision: schema may slip with
// weak provider strict-mode; missing verdict defaults to "needs_minor_revision"
// (safer than "production_ready" — pushes operator review).
func TestEpisodeJudgeMissingVerdictDefaultsMinorRevision(t *testing.T) {
	verdictJSON := `{
		"terminology_consistency": 0.5,
		"register_consistency": 0.6,
		"narrative_coherence": 0.7,
		"character_voice_stability": 0.8,
		"cultural_localization": 0.6,
		"overall_fidelity": 0.6,
		"overall_fluency": 0.7
	}`
	resp := buildToolCallResponse("emit_episode_judge_verdict", verdictJSON, providerUsage{
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
		episodeJudgeModel: "kimi-k2.5",
		httpClient:        &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeEpisode(context.Background(), EpisodeJudgeArgs{
		SourceLang: "en", TargetLang: "zh",
		Segments: []EpisodeJudgeSegment{{ChapterOrdinal: 1, Ordinal: 1, SourceText: "x", TargetText: "y"}},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Verdict != "needs_minor_revision" {
		t.Fatalf("missing verdict should default to needs_minor_revision, got %q", got.Verdict)
	}
}

// TestEpisodeJudgeUserMsgIncludesAllParts: verifies prompt assembly contains
// reference card, glossary, chapter overview rows (with chapter_judge hint),
// and every segment with chapter_ordinal:ordinal tag + (optional) seg_judge.
func TestEpisodeJudgeUserMsgIncludesAllParts(t *testing.T) {
	segScore := 0.42
	chapScore := 0.88
	args := EpisodeJudgeArgs{
		SourceLang: "en", TargetLang: "zh-CN",
		EpisodeID: 142, EpisodeName: "Lecture 5 — Raft",
		EpisodeSummary: "REFERENCE_CARD_TOKEN",
		GlossaryHint:   "GLOSSARY_TOKEN",
		Chapters: []EpisodeJudgeChapterRow{
			{Ordinal: 1, Title: "Intro", TitleTranslated: "引言", StartMs: 0, EndMs: 60000, ChapterJudgeScore: &chapScore},
			{Ordinal: 2, Title: "Raft basics", StartMs: 60000, EndMs: 120000, SummaryMD: "core algorithm walkthrough"},
		},
		Segments: []EpisodeJudgeSegment{
			{ChapterOrdinal: 1, Ordinal: 1, StartMs: 0, EndMs: 4000, SourceText: "Hello", TargetText: "你好", SegJudgeScore: &segScore},
			{ChapterOrdinal: 2, Ordinal: 1, StartMs: 60000, EndMs: 64000, SourceText: "world", TargetText: "世界"},
		},
	}
	got := buildEpisodeJudgeUserMsg(args)
	for _, want := range []string{
		"Lecture 5 — Raft",
		"REFERENCE_CARD_TOKEN",
		"GLOSSARY_TOKEN",
		"Intro",
		"引言",
		"Raft basics",
		"core algorithm walkthrough",
		"chapter_judge=0.88",
		"[c1.s1]",
		"[c2.s1]",
		"seg_judge=0.42",
		"emit_episode_judge_verdict",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("user message missing %q\n---\n%s", want, got)
		}
	}
	if cnt := strings.Count(got, "seg_judge="); cnt != 1 {
		t.Errorf("seg_judge hint should appear exactly once (only c1.s1 has score); got %d", cnt)
	}
	if cnt := strings.Count(got, "chapter_judge="); cnt != 1 {
		t.Errorf("chapter_judge hint should appear exactly once (only c1 has score); got %d", cnt)
	}
}

// TestEpisodeJudgeOverallScore: verifies OverallScore averaging logic and
// fidelity-priority semantics.
func TestEpisodeJudgeOverallScore(t *testing.T) {
	cases := []struct {
		name string
		r    EpisodeJudgeResult
		want float64
	}{
		{
			name: "fidelity present takes priority",
			r: EpisodeJudgeResult{
				TerminologyConsistency:  0.9,
				RegisterConsistency:     0.9,
				NarrativeCoherence:      0.9,
				CharacterVoiceStability: 0.9,
				CulturalLocalization:    0.9,
				OverallFidelity:         0.7, // <- this wins
				OverallFluency:          0.9,
			},
			want: 0.7,
		},
		{
			name: "fidelity zero, others present → average",
			r: EpisodeJudgeResult{
				TerminologyConsistency:  0.6,
				RegisterConsistency:     0.7,
				NarrativeCoherence:      0.7,
				CharacterVoiceStability: 0.7,
				CulturalLocalization:    0.7,
				OverallFidelity:         0,
				OverallFluency:          0.8,
			},
			want: 0.7, // (0.6+0.7+0.7+0.7+0.7+0.8)/6
		},
		{
			name: "all zero",
			r:    EpisodeJudgeResult{},
			want: 0,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := c.r.OverallScore()
			// Tolerance: the average path sums six float64s before
			// dividing, which incurs ~1e-15 round-off; bit-exact
			// comparison would be flaky across architectures.
			if math.Abs(got-c.want) > 1e-9 {
				t.Fatalf("OverallScore want %v (±1e-9), got %v", c.want, got)
			}
		})
	}
}

// TestEpisodeJudgeThinkingModelUsesAutoToolChoice: when the configured episode
// judge model name contains "thinking" (eg. kimi-k2-thinking), the request
// must downgrade tool_choice from the strict object form to "auto" — DashScope
// reasoning endpoints reject the strict form (same constraint OPT-405 glossary
// and OPT-409 chapter judge already encode).
func TestEpisodeJudgeThinkingModelUsesAutoToolChoice(t *testing.T) {
	cases := []struct {
		name     string
		model    string
		wantAuto bool
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
				resp := buildToolCallResponse("emit_episode_judge_verdict",
					`{"terminology_consistency":0.9,"register_consistency":0.9,`+
						`"narrative_coherence":0.9,"character_voice_stability":0.9,`+
						`"cultural_localization":0.9,"overall_fidelity":0.9,"overall_fluency":0.9,`+
						`"verdict":"production_ready"}`,
					providerUsage{PromptTokens: 100, CompletionTokens: 30})
				body, _ := json.Marshal(resp)
				w.Header().Set("Content-Type", "application/json")
				_, _ = w.Write(body)
			}))
			defer stub.Close()

			c := &Client{
				baseURL:            stub.URL,
				apiKey:             "sk-test",
				episodeJudgeModel:  tc.model,
				httpClient:         &http.Client{Timeout: 5 * time.Second},
				thinkingHTTPClient: &http.Client{Timeout: 5 * time.Second},
			}
			_, err := c.JudgeEpisode(context.Background(), EpisodeJudgeArgs{
				SourceLang: "en", TargetLang: "zh",
				Segments: []EpisodeJudgeSegment{{ChapterOrdinal: 1, Ordinal: 1, SourceText: "x", TargetText: "y"}},
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
				if name, _ := fn["name"].(string); name != "emit_episode_judge_verdict" {
					t.Fatalf("expected forced function name 'emit_episode_judge_verdict', got %#v", fn)
				}
			}
		})
	}
}
