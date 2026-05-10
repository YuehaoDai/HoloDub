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

// TestExtractEpisodeGlossaryHappyPath: provider returns a well-formed
// tool_calls response with three glossary entries, two speakers and a
// reference card; client decodes and returns it without modification.
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
	}
	stub := newGlossaryToolStub(t, want)
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

	got, err := c.ExtractEpisodeGlossary(ctx, "long ASR transcript ...", "en", "zh-CN")
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
}

// TestExtractEpisodeGlossaryEmptyTranscript: blank input is a soft
// short-circuit (nil result, nil error) so the pipeline can keep moving
// without forking the call site on whether ASR produced text.
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

	got, err := c.ExtractEpisodeGlossary(ctx, "   \n\t  ", "en", "zh-CN")
	if err != nil {
		t.Fatalf("blank transcript should not error: %v", err)
	}
	if len(got.Glossary) != 0 || got.ReferenceCard != "" {
		t.Fatalf("blank transcript should yield empty result, got %+v", got)
	}
}

// TestExtractEpisodeGlossaryFallsBackToOpenAIModel: when GLOSSARY_MODEL
// is unset the client must use OpenAIModel instead. We capture the
// outgoing request and inspect the model field.
func TestExtractEpisodeGlossaryFallsBackToOpenAIModel(t *testing.T) {
	var capturedModel string
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req chatCompletionRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		capturedModel = req.Model
		// Minimal valid tool response so caller doesn't error.
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

	if _, err := c.ExtractEpisodeGlossary(ctx, "transcript", "en", "zh-CN"); err != nil {
		t.Fatalf("ExtractEpisodeGlossary: %v", err)
	}
	if capturedModel != "kimi-k2.5" {
		t.Fatalf("want fallback model 'kimi-k2.5', got %q", capturedModel)
	}
}

// TestExtractEpisodeGlossaryNoToolCallTreatedAsFailure: when the LLM
// ignores the tool spec and returns prose content the client MUST return
// an error rather than silently accepting an empty result. Pipeline
// callers are documented to treat any error here as "no glossary, log
// and continue" — but the error itself is required so observability
// counters increment.
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

	_, err := c.ExtractEpisodeGlossary(ctx, "transcript", "en", "zh-CN")
	if err == nil {
		t.Fatal("expected error when provider returns content without tool_calls; got nil")
	}
}
