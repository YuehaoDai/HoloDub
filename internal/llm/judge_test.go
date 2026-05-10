package llm

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// TestJudgeSchemaValid ensures the static judgeToolSchema parses to the
// shape we expect. Catches typos in the schema literal.
func TestJudgeSchemaValid(t *testing.T) {
	var schema map[string]any
	if err := json.Unmarshal(judgeToolSchema, &schema); err != nil {
		t.Fatalf("judgeToolSchema is not valid JSON: %v", err)
	}
	props := schema["properties"].(map[string]any)
	for _, k := range []string{"fidelity", "fluency", "coherence", "verdict"} {
		if _, ok := props[k]; !ok {
			t.Fatalf("schema missing property %q", k)
		}
	}
	required, ok := schema["required"].([]any)
	if !ok || len(required) != 4 {
		t.Fatalf("schema required must list 4 fields, got %v", schema["required"])
	}
}

// TestJudgeDisabledByDefault: empty JudgeModel must short-circuit.
func TestJudgeDisabledByDefault(t *testing.T) {
	c := &Client{
		baseURL:    "https://example.invalid",
		apiKey:     "sk-test",
		judgeModel: "", // disabled
		httpClient: &http.Client{Timeout: 1 * time.Second},
	}
	got, err := c.JudgeFidelity(context.Background(), JudgeArgs{
		SrcText: "hi", TgtText: "你好", SrcLang: "en", TgtLang: "zh",
	})
	if err != nil {
		t.Fatalf("disabled judge must not error, got: %v", err)
	}
	if got != nil {
		t.Fatalf("disabled judge must return nil result, got: %+v", got)
	}
}

// TestJudgeEmptyInputsSkip: empty src or tgt skips without HTTP call.
func TestJudgeEmptyInputsSkip(t *testing.T) {
	called := false
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
	}))
	defer stub.Close()

	c := &Client{
		baseURL:    stub.URL,
		apiKey:     "sk-test",
		judgeModel: "qwen-turbo",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	r1, e1 := c.JudgeFidelity(context.Background(), JudgeArgs{
		SrcText: "", TgtText: "你好", SrcLang: "en", TgtLang: "zh",
	})
	r2, e2 := c.JudgeFidelity(context.Background(), JudgeArgs{
		SrcText: "hi", TgtText: "  ", SrcLang: "en", TgtLang: "zh",
	})
	if e1 != nil || e2 != nil {
		t.Fatalf("empty inputs must not error: e1=%v e2=%v", e1, e2)
	}
	if r1 != nil || r2 != nil {
		t.Fatal("empty inputs must return nil result")
	}
	if called {
		t.Fatal("empty inputs must not trigger HTTP call")
	}
}

// TestJudgeHappyPath: mock LLM returns valid tool call → parsed correctly.
func TestJudgeHappyPath(t *testing.T) {
	verdictJSON := `{"fidelity":0.85,"fluency":0.92,"coherence":0.78,"issues":[],"verdict":"accept"}`
	resp := buildToolCallResponse("emit_judge_verdict", verdictJSON, providerUsage{
		PromptTokens: 250, CompletionTokens: 30,
	})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL:    stub.URL,
		apiKey:     "sk-test",
		judgeModel: "qwen-turbo",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeFidelity(context.Background(), JudgeArgs{
		SrcText: "Hello world", TgtText: "你好世界", SrcLang: "en", TgtLang: "zh-CN",
	})
	if err != nil {
		t.Fatalf("JudgeFidelity: %v", err)
	}
	if got == nil {
		t.Fatal("expected non-nil result")
	}
	if got.Fidelity != 0.85 || got.Fluency != 0.92 || got.Coherence != 0.78 {
		t.Fatalf("score mismatch: %+v", got)
	}
	if got.Verdict != "accept" {
		t.Fatalf("verdict mismatch: %q", got.Verdict)
	}
}

// TestJudgeMissingToolCall: provider returns a content message instead of
// a tool call → JudgeFidelity returns error (judge schema is mandatory).
func TestJudgeMissingToolCall(t *testing.T) {
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"sorry can't comply"}}],"usage":{"prompt_tokens":100}}`))
	}))
	defer stub.Close()

	c := &Client{
		baseURL:    stub.URL,
		apiKey:     "sk-test",
		judgeModel: "qwen-turbo",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeFidelity(context.Background(), JudgeArgs{
		SrcText: "x", TgtText: "y", SrcLang: "en", TgtLang: "zh",
	})
	if err == nil {
		t.Fatalf("expected error when LLM bypasses tool, got result %+v", got)
	}
	if got != nil {
		t.Fatalf("expected nil result on error, got %+v", got)
	}
}

// TestJudgeMissingVerdictDefaultsRetry: schema may slip with weak provider
// strict-mode; missing verdict defaults to "retry" (safer than "accept").
func TestJudgeMissingVerdictDefaultsRetry(t *testing.T) {
	verdictJSON := `{"fidelity":0.5,"fluency":0.6,"coherence":0.7}`
	resp := buildToolCallResponse("emit_judge_verdict", verdictJSON, providerUsage{
		PromptTokens: 250, CompletionTokens: 20,
	})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL:    stub.URL,
		apiKey:     "sk-test",
		judgeModel: "qwen-turbo",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.JudgeFidelity(context.Background(), JudgeArgs{
		SrcText: "x", TgtText: "y", SrcLang: "en", TgtLang: "zh",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got.Verdict != "retry" {
		t.Fatalf("missing verdict should default to retry, got %q", got.Verdict)
	}
}

// TestJudgeOverallScore: verifies OverallScore averaging logic.
func TestJudgeOverallScore(t *testing.T) {
	cases := []struct {
		name string
		r    JudgeResult
		want float64
	}{
		{"fidelity present", JudgeResult{Fidelity: 0.7, Fluency: 0.9, Coherence: 0.8}, 0.7},
		{"fidelity zero, others present", JudgeResult{Fidelity: 0, Fluency: 0.8, Coherence: 0.6}, 0.7},
		{"only fluency", JudgeResult{Fluency: 0.5}, 0.5},
		{"all zero", JudgeResult{}, 0},
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

// buildToolCallResponse is a test helper that constructs an OpenAI-compatible
// chat completion response with a single forced tool call.
func buildToolCallResponse(toolName, args string, usage providerUsage) chatCompletionResponse {
	r := chatCompletionResponse{Usage: usage}
	r.Choices = []struct {
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
					ID:   "call_1",
					Type: "function",
					Function: toolCallFunction{Name: toolName, Arguments: args},
				}},
			},
			FinishReason: "tool_calls",
		},
	}
	return r
}
