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

// newGlossaryToolStub returns a httptest.Server that mocks the chat
// completions endpoint, returning the given GlossaryResult wrapped in a
// strict tool_calls payload (mirrors the DashScope qwen-turbo wire format
// used in production).
func newGlossaryToolStub(t *testing.T, result GlossaryResult) *httptest.Server {
	t.Helper()
	args := mustMarshalJSON(result)
	resp := chatCompletionResponse{}
	resp.Choices = []struct {
		Message struct {
			Content   string     `json:"content"`
			ToolCalls []toolCall `json:"tool_calls,omitempty"`
		} `json:"message"`
		FinishReason string `json:"finish_reason,omitempty"`
	}{
		{
			Message: struct {
				Content   string     `json:"content"`
				ToolCalls []toolCall `json:"tool_calls,omitempty"`
			}{
				ToolCalls: []toolCall{{
					ID:   "call_glossary_1",
					Type: "function",
					Function: toolCallFunction{
						Name:      "emit_episode_glossary",
						Arguments: string(args),
					},
				}},
			},
			FinishReason: "tool_calls",
		},
	}
	resp.Usage.PromptTokens = 1500
	resp.Usage.CompletionTokens = 200
	resp.Usage.PromptTokensDetails.CachedTokens = 800

	body, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal stub response: %v", err)
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
}

// fixtureSegments produces a deterministic segment slice for tests that
// don't care about the exact text — just that the user message gets
// rendered with [N] indices and the schema accepts it.
func fixtureSegments(n int) []EpisodeSegment {
	out := make([]EpisodeSegment, n)
	for i := 0; i < n; i++ {
		out[i] = EpisodeSegment{
			StartMs:      int64(i) * 5000,
			EndMs:        int64(i)*5000 + 4500,
			Text:         "segment " + string(rune('A'+i%26)),
			SpeakerLabel: "SPK_01",
		}
	}
	return out
}

// TestExtractEpisodeGlossaryHappyPath: provider returns a well-formed
// tool_calls response with three glossary entries, two speakers, a
// reference card AND chapters[]; client decodes and returns it.
func TestExtractEpisodeGlossaryHappyPath(t *testing.T) {
	want := GlossaryResult{
		Glossary: []GlossaryEntry{
			{Source: "Raft", Target: "Raft", Note: "consensus algorithm; do not translate"},
			{Source: "MapReduce", Target: "MapReduce", Note: "do not translate"},
			{Source: "Lamport clock", Target: "Lamport 时钟"},
		},
		Speakers: []SpeakerHint{
			{Label: "SPK_01", DisplayName: "Robert", VoiceRegister: "host/lecturer/measured"},
			{Label: "SPK_02", DisplayName: "", VoiceRegister: "guest/casual"},
		},
		ReferenceCard: "Genre: distributed systems lecture. Topic: consensus.\n\n" +
			"Key entities: Raft, MapReduce, Lamport clock.\n\n" +
			"Register: formal academic, monotone narration.",
		Chapters: []ChapterCut{
			{StartSegmentIdx: 0, EndSegmentIdx: 4,
				TitleSource: "Course Logistics", TitleTranslated: "课程结构",
				SummaryMD: "讲师介绍课程结构、labs、grade。"},
			{StartSegmentIdx: 5, EndSegmentIdx: 9,
				TitleSource: "MapReduce Overview", TitleTranslated: "MapReduce 概述",
				SummaryMD: "讨论 MapReduce 编程模型与典型应用。"},
		},
	}
	stub := newGlossaryToolStub(t, want)
	defer stub.Close()

	c := &Client{
		provider:      "openai_compatible",
		baseURL:       stub.URL,
		apiKey:        "sk-test",
		model:         "kimi-k2.5",
		glossaryModel: "kimi-k2.5",
		httpClient:    &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	got, err := c.ExtractEpisodeGlossary(ctx, fixtureSegments(10), "en", "zh-CN", true)
	if err != nil {
		t.Fatalf("ExtractEpisodeGlossary: %v", err)
	}
	if len(got.Glossary) != 3 {
		t.Fatalf("want 3 glossary entries, got %d", len(got.Glossary))
	}
	if got.Glossary[0].Source != "Raft" || got.Glossary[0].Target != "Raft" {
		t.Fatalf("first entry mismatch: %+v", got.Glossary[0])
	}
	if got.Glossary[2].Target != "Lamport 时钟" {
		t.Fatalf("third entry target mismatch: %q", got.Glossary[2].Target)
	}
	if len(got.Speakers) != 2 || got.Speakers[0].DisplayName != "Robert" {
		t.Fatalf("speakers mismatch: %+v", got.Speakers)
	}
	if !strings.Contains(got.ReferenceCard, "consensus") {
		t.Fatalf("reference card missing topic keyword; got %q", got.ReferenceCard)
	}
	if len(got.Chapters) != 2 {
		t.Fatalf("want 2 chapters, got %d", len(got.Chapters))
	}
	if got.Chapters[0].EndSegmentIdx != 4 || got.Chapters[1].StartSegmentIdx != 5 {
		t.Fatalf("chapter boundaries mismatch: %+v", got.Chapters)
	}
	if got.Chapters[0].TitleTranslated != "课程结构" {
		t.Fatalf("chapter title mismatch: %q", got.Chapters[0].TitleTranslated)
	}
}

// TestExtractEpisodeGlossaryEmptyTranscript: empty/blank segments are a
// soft short-circuit (nil result, nil error) so the pipeline can keep
// moving without forking the call site on whether ASR produced text.
func TestExtractEpisodeGlossaryEmptyTranscript(t *testing.T) {
	c := &Client{
		provider:      "openai_compatible",
		baseURL:       "http://unused",
		apiKey:        "sk-test",
		model:         "qwen-turbo",
		glossaryModel: "qwen-turbo",
		httpClient:    &http.Client{Timeout: 1 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()

	cases := [][]EpisodeSegment{
		nil,
		{},
		{{StartMs: 0, EndMs: 1000, Text: "  "}, {StartMs: 1000, EndMs: 2000, Text: "\n\t"}},
	}
	for i, segs := range cases {
		got, err := c.ExtractEpisodeGlossary(ctx, segs, "en", "zh-CN", true)
		if err != nil {
			t.Fatalf("case %d: blank input should not error: %v", i, err)
		}
		if len(got.Glossary) != 0 || got.ReferenceCard != "" || len(got.Chapters) != 0 {
			t.Fatalf("case %d: blank input should yield empty result, got %+v", i, got)
		}
	}
}

// TestExtractEpisodeGlossaryFallsBackToOpenAIModel: when GLOSSARY_MODEL
// is unset the client must use OpenAIModel instead. We capture the
// outgoing request and inspect the model field. Also verifies that the
// chapterizeEnabled flag flows into the user message (so the model knows
// to fill chapters[]).
func TestExtractEpisodeGlossaryFallsBackToOpenAIModel(t *testing.T) {
	var capturedModel string
	var capturedUserMsg string
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req chatCompletionRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		capturedModel = req.Model
		for _, m := range req.Messages {
			if m.Role == "user" {
				capturedUserMsg = m.Content
			}
		}
		args := mustMarshalJSON(GlossaryResult{ReferenceCard: "ok"})
		body := []byte(`{"choices":[{"message":{"tool_calls":[{"id":"c","type":"function","function":{"name":"emit_episode_glossary","arguments":` +
			string(mustMarshalJSON(string(args))) + `}}]}}],"usage":{"prompt_tokens":10}}`)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		provider:      "openai_compatible",
		baseURL:       stub.URL,
		apiKey:        "sk-test",
		model:         "kimi-k2.5",
		glossaryModel: "", // <- intentionally empty
		httpClient:    &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if _, err := c.ExtractEpisodeGlossary(ctx, fixtureSegments(3), "en", "zh-CN", true); err != nil {
		t.Fatalf("ExtractEpisodeGlossary: %v", err)
	}
	if capturedModel != "kimi-k2.5" {
		t.Fatalf("want fallback model 'kimi-k2.5', got %q", capturedModel)
	}
	if !strings.Contains(capturedUserMsg, "[0] 00:00-00:04") {
		t.Fatalf("user message should carry indexed segment lines; got: %q", capturedUserMsg)
	}
	if !strings.Contains(capturedUserMsg, "chapters[]") {
		t.Fatalf("user message should mention chapters[] when chapterizeEnabled=true; got: %q", capturedUserMsg)
	}
}

// TestExtractEpisodeGlossaryChapterizeDisabledNudgesPrompt: the user
// message must explicitly tell the model "chapterization is disabled"
// when chapterizeEnabled=false, so a model that gets the schema with
// chapters[] still defaults to []. Belt-and-braces alongside the
// system-prompt switch.
func TestExtractEpisodeGlossaryChapterizeDisabledNudgesPrompt(t *testing.T) {
	var capturedUserMsg string
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req chatCompletionRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		for _, m := range req.Messages {
			if m.Role == "user" {
				capturedUserMsg = m.Content
			}
		}
		args := mustMarshalJSON(GlossaryResult{})
		body := []byte(`{"choices":[{"message":{"tool_calls":[{"id":"c","type":"function","function":{"name":"emit_episode_glossary","arguments":` +
			string(mustMarshalJSON(string(args))) + `}}]}}],"usage":{"prompt_tokens":10}}`)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		provider:      "openai_compatible",
		baseURL:       stub.URL,
		apiKey:        "sk-test",
		model:         "qwen-turbo",
		glossaryModel: "qwen-turbo",
		httpClient:    &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if _, err := c.ExtractEpisodeGlossary(ctx, fixtureSegments(3), "en", "zh-CN", false); err != nil {
		t.Fatalf("ExtractEpisodeGlossary: %v", err)
	}
	if !strings.Contains(capturedUserMsg, "chapterization is disabled") {
		t.Fatalf("user message should mention 'chapterization is disabled' when chapterizeEnabled=false; got: %q", capturedUserMsg)
	}
}

// TestExtractEpisodeGlossaryNoToolCallTreatedAsFailure: when the LLM
// ignores the tool spec and returns prose content the client MUST return
// an error rather than silently accepting an empty result.
func TestExtractEpisodeGlossaryNoToolCallTreatedAsFailure(t *testing.T) {
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"sorry, here is some prose"}}],"usage":{"prompt_tokens":50}}`))
	}))
	defer stub.Close()

	c := &Client{
		provider:      "openai_compatible",
		baseURL:       stub.URL,
		apiKey:        "sk-test",
		model:         "qwen-turbo",
		glossaryModel: "qwen-turbo",
		httpClient:    &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_, err := c.ExtractEpisodeGlossary(ctx, fixtureSegments(3), "en", "zh-CN", true)
	if err == nil {
		t.Fatal("expected error when provider returns content without tool_calls; got nil")
	}
}

// TestExtractEpisodeGlossaryThinkingModelUsesAutoToolChoice: DashScope
// thinking-mode models reject tool_choice={type:"function",...} with
// "tool_choice does not support being set to required or object in
// thinking mode". The client must downgrade to tool_choice="auto" for
// those models so the call goes through; non-thinking models keep the
// strict force form so a content-only response becomes a parse error.
func TestExtractEpisodeGlossaryThinkingModelUsesAutoToolChoice(t *testing.T) {
	type capture struct {
		model      string
		toolChoice any
	}
	captureFor := func(m string) capture {
		var got capture
		stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			var req chatCompletionRequest
			_ = json.NewDecoder(r.Body).Decode(&req)
			got.model = req.Model
			got.toolChoice = req.ToolChoice
			args := mustMarshalJSON(GlossaryResult{ReferenceCard: "ok"})
			body := []byte(`{"choices":[{"message":{"tool_calls":[{"id":"c","type":"function","function":{"name":"emit_episode_glossary","arguments":` +
				string(mustMarshalJSON(string(args))) + `}}]}}],"usage":{"prompt_tokens":10}}`)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write(body)
		}))
		defer stub.Close()
		c := &Client{
			provider:      "openai_compatible",
			baseURL:       stub.URL,
			apiKey:        "sk-test",
			model:         m,
			glossaryModel: m,
			httpClient:    &http.Client{Timeout: 5 * time.Second},
		}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if _, err := c.ExtractEpisodeGlossary(ctx, fixtureSegments(3), "en", "zh-CN", true); err != nil {
			t.Fatalf("ExtractEpisodeGlossary(%s): %v", m, err)
		}
		return got
	}

	cases := []struct {
		model      string
		wantString string // when toolChoice should be "auto"
		wantObject bool   // when toolChoice should be the strict object form
	}{
		{model: "kimi-k2-thinking", wantString: "auto"},
		{model: "qwen3-235b-a22b-thinking-2507", wantString: "auto"},
		{model: "kimi-k2.5", wantObject: true},
		{model: "qwen-max-latest", wantObject: true},
		{model: "deepseek-v3", wantObject: true},
	}
	for _, tc := range cases {
		t.Run(tc.model, func(t *testing.T) {
			got := captureFor(tc.model)
			if got.model != tc.model {
				t.Fatalf("captured wrong model: %q", got.model)
			}
			if tc.wantString != "" {
				s, ok := got.toolChoice.(string)
				if !ok || s != tc.wantString {
					t.Fatalf("model %s should use tool_choice=%q (string); got %#v", tc.model, tc.wantString, got.toolChoice)
				}
				return
			}
			if tc.wantObject {
				m, ok := got.toolChoice.(map[string]any)
				if !ok {
					t.Fatalf("model %s should use object tool_choice; got %#v (%T)", tc.model, got.toolChoice, got.toolChoice)
				}
				if m["type"] != "function" {
					t.Fatalf("model %s tool_choice missing type=function; got %#v", tc.model, m)
				}
				fn, _ := m["function"].(map[string]any)
				if fn == nil || fn["name"] != "emit_episode_glossary" {
					t.Fatalf("model %s tool_choice should pin emit_episode_glossary; got %#v", tc.model, m)
				}
			}
		})
	}
}

// TestBuildIndexedTranscriptShape pins the user-message format so the
// model contract stays stable across refactors. Indices match the input
// slice, blanks are skipped without renumbering, mm:ss timestamps roll
// past 60 minutes, and the trailing instruction reflects chapterization
// mode.
func TestBuildIndexedTranscriptShape(t *testing.T) {
	segs := []EpisodeSegment{
		{StartMs: 0, EndMs: 4500, Text: "Hello world", SpeakerLabel: "SPK_01"},
		{StartMs: 5000, EndMs: 9500, Text: "  ", SpeakerLabel: "SPK_01"},        // blank → skipped
		{StartMs: 65 * 60 * 1000, EndMs: 65*60*1000 + 4500, Text: "after 65min"}, // hour roll-over
	}
	got := buildIndexedTranscript(segs, "en", true)
	wantSubstrs := []string{
		"total segments: 3",
		"[0] 00:00-00:04 SPK_01: Hello world",
		"[2] 65:00-65:04 after 65min", // no SpeakerLabel → no prefix
		"chapters[]",
		"[0, 2]",
	}
	for _, sub := range wantSubstrs {
		if !strings.Contains(got, sub) {
			t.Errorf("user message missing substring %q; full message:\n%s", sub, got)
		}
	}
	// Skipped segment must NOT appear (no [1] line).
	if strings.Contains(got, "[1] ") {
		t.Errorf("blank segment should be skipped without renumbering; full message:\n%s", got)
	}
}
