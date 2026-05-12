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

// TestDubbingPlanSchemaValid: a minimum guard that the static
// dubbingPlanSchema is well-formed JSON Schema with all required
// fields declared. Catches typos in the schema literal that would
// otherwise crash the first emit_dubbing_plan call in production.
func TestDubbingPlanSchemaValid(t *testing.T) {
	var schema map[string]any
	if err := json.Unmarshal(dubbingPlanSchema, &schema); err != nil {
		t.Fatalf("dubbingPlanSchema is not valid JSON: %v", err)
	}
	props := schema["properties"].(map[string]any)
	for _, k := range []string{"translation", "emotion", "pacing", "emphasis_words", "pause_after_ms"} {
		if _, ok := props[k]; !ok {
			t.Errorf("schema missing property %q", k)
		}
	}
	required, ok := schema["required"].([]any)
	if !ok || len(required) < 3 {
		t.Fatalf("schema must require at least translation+emotion+pacing, got %v", schema["required"])
	}
}

// TestDubbingPlanSystemPromptByteStable: the system prompt MUST be
// byte-identical for the same (targetLanguage, rate, summary) inputs.
// OPT-001 prefix caching depends on byte-identity; a single map
// iteration difference would re-issue the cache key on every segment.
func TestDubbingPlanSystemPromptByteStable(t *testing.T) {
	a := dubbingPlanSystemPrompt("zh", 4.0, "glossary text")
	b := dubbingPlanSystemPrompt("zh", 4.0, "glossary text")
	if a != b {
		t.Fatalf("system prompt is not byte-stable across calls\n--A--\n%s\n--B--\n%s", a, b)
	}
}

// TestTranslateWithDubbingPlan_HappyPath: provider returns a valid
// strict tool call; we parse it back into a DubbingPlan correctly,
// including the optional emphasis_words and pause_after_ms fields.
func TestTranslateWithDubbingPlan_HappyPath(t *testing.T) {
	planJSON := `{
		"translation": "你好，世界。",
		"emotion": {"valence": 0.8, "arousal": 0.3, "label": "calm"},
		"pacing": "normal",
		"emphasis_words": ["你好"],
		"pause_after_ms": 200
	}`
	resp := buildToolCallResponse("emit_dubbing_plan", planJSON, providerUsage{
		PromptTokens: 320, CompletionTokens: 50,
	})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		provider:    "openai_compatible",
		baseURL:     stub.URL,
		apiKey:      "sk-test",
		model:       "qwen-plus",
		temperature: 0.3,
		httpClient:  &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.TranslateWithDubbingPlan(
		context.Background(),
		"en", "zh",
		"Hello, world.",
		3.0, 4.0, nil, "",
	)
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if got.Translation != "你好，世界。" {
		t.Fatalf("translation: got %q", got.Translation)
	}
	if got.Emotion.Label != "calm" || got.Emotion.Valence != 0.8 || got.Emotion.Arousal != 0.3 {
		t.Fatalf("emotion: got %+v", got.Emotion)
	}
	if got.Pacing != "normal" {
		t.Fatalf("pacing: got %q", got.Pacing)
	}
	if len(got.EmphasisWords) != 1 || got.EmphasisWords[0] != "你好" {
		t.Fatalf("emphasis: got %v", got.EmphasisWords)
	}
	if got.PauseAfterMs != 200 {
		t.Fatalf("pause: got %d", got.PauseAfterMs)
	}
}

// TestTranslateWithDubbingPlan_MissingOptionalFields: emphasis_words
// and pause_after_ms are NOT required by the schema; the parser must
// still succeed (returning zero values for the missing fields). This
// is what providers will produce for emotionally-flat segments.
func TestTranslateWithDubbingPlan_MissingOptionalFields(t *testing.T) {
	planJSON := `{
		"translation": "好的。",
		"emotion": {"valence": 0.0, "arousal": 0.2, "label": "neutral"},
		"pacing": "normal"
	}`
	resp := buildToolCallResponse("emit_dubbing_plan", planJSON, providerUsage{})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL: stub.URL, apiKey: "sk-test", model: "qwen-plus",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.TranslateWithDubbingPlan(context.Background(), "en", "zh", "OK.", 1.5, 4.0, nil, "")
	if err != nil {
		t.Fatalf("unexpected err: %v", err)
	}
	if got.Translation != "好的。" {
		t.Fatalf("translation got %q", got.Translation)
	}
	if got.PauseAfterMs != 0 {
		t.Fatalf("missing pause_after_ms must default to 0, got %d", got.PauseAfterMs)
	}
	if len(got.EmphasisWords) != 0 {
		t.Fatalf("missing emphasis_words must be empty, got %v", got.EmphasisWords)
	}
}

// TestTranslateWithDubbingPlan_DefensivePauseClipping: providers
// occasionally violate enum / range constraints under load. The
// parser defensively clips PauseAfterMs to [0,1000] rather than
// fail the segment — degraded prosody is strictly better than a
// stuck pipeline.
func TestTranslateWithDubbingPlan_DefensivePauseClipping(t *testing.T) {
	cases := []struct {
		name   string
		input  int
		want   int
	}{
		{"negative", -50, 0},
		{"too-large", 5000, 1000},
		{"in-range", 800, 800},
		{"zero", 0, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			planJSON := buildSimplePlanJSON("好。", "normal", "calm", tc.input)
			resp := buildToolCallResponse("emit_dubbing_plan", planJSON, providerUsage{})
			body, _ := json.Marshal(resp)
			stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write(body)
			}))
			defer stub.Close()

			c := &Client{
				baseURL: stub.URL, apiKey: "sk-test", model: "qwen-plus",
				httpClient: &http.Client{Timeout: 5 * time.Second},
			}
			got, err := c.TranslateWithDubbingPlan(context.Background(), "en", "zh", "OK.", 1.5, 4.0, nil, "")
			if err != nil {
				t.Fatalf("unexpected err: %v", err)
			}
			if got.PauseAfterMs != tc.want {
				t.Fatalf("PauseAfterMs: want=%d got=%d", tc.want, got.PauseAfterMs)
			}
		})
	}
}

// TestTranslateWithDubbingPlan_EmptyTranslationRejected: a provider
// that returns an empty `translation` field must produce an error —
// the caller has nothing to feed the TTS adapter. This is the one
// case where we DO fail the segment rather than fall back.
func TestTranslateWithDubbingPlan_EmptyTranslationRejected(t *testing.T) {
	planJSON := `{"translation": "", "emotion": {"valence": 0, "arousal": 0, "label": "neutral"}, "pacing": "normal"}`
	resp := buildToolCallResponse("emit_dubbing_plan", planJSON, providerUsage{})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL: stub.URL, apiKey: "sk-test", model: "qwen-plus",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	_, err := c.TranslateWithDubbingPlan(context.Background(), "en", "zh", "OK.", 1.0, 4.0, nil, "")
	if err == nil {
		t.Fatal("empty translation must produce an error")
	}
	if !strings.Contains(err.Error(), "empty translation") {
		t.Fatalf("error message should mention empty translation, got: %v", err)
	}
}

// TestTranslateWithDubbingPlan_ProviderReturnsContent: some providers
// occasionally ignore tool_choice under load and return a plain
// content message. doChatTool returns "" + nil err in that case; the
// dubbing-plan caller must surface this as a real error so the
// pipeline can fall back to plain-text translate. This matches the
// existing OPT-002 judge behaviour.
func TestTranslateWithDubbingPlan_ProviderReturnsContent(t *testing.T) {
	// Build a response with content but no tool_calls (the
	// "tool_choice ignored" case).
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
			}{Content: "plain text bypass"},
			FinishReason: "stop",
		},
	}
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL: stub.URL, apiKey: "sk-test", model: "qwen-plus",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	_, err := c.TranslateWithDubbingPlan(context.Background(), "en", "zh", "OK.", 1.0, 4.0, nil, "")
	if err == nil {
		t.Fatal("provider bypassing tool_choice must produce an error")
	}
}

// TestTranslateWithDubbingPlan_MalformedJSON: provider returns
// invalid JSON in tool_calls.arguments. Parser surfaces a decode
// error (truncated raw payload for log readability).
func TestTranslateWithDubbingPlan_MalformedJSON(t *testing.T) {
	resp := buildToolCallResponse("emit_dubbing_plan", "{not valid json", providerUsage{})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL: stub.URL, apiKey: "sk-test", model: "qwen-plus",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	_, err := c.TranslateWithDubbingPlan(context.Background(), "en", "zh", "OK.", 1.0, 4.0, nil, "")
	if err == nil {
		t.Fatal("malformed JSON must produce a decode error")
	}
	if !strings.Contains(err.Error(), "decode tool args") {
		t.Fatalf("error should mention decode failure, got: %v", err)
	}
}

// TestTryRecoverDubbingPlanJSON_AsciiQuotesInTranslation: the most
// common failure mode in chapter 2 of job 154 — the LLM emits an
// ASCII " inside the translation field, breaking the top-level JSON
// parse. The recovery helper must replace those quotes with Chinese
// typographic quotes and produce a parseable result.
func TestTryRecoverDubbingPlanJSON_AsciiQuotesInTranslation(t *testing.T) {
	// Hand-crafted broken JSON: translation field has unescaped " characters.
	// Building the literal directly so the test reads like the failure mode.
	raw := `{"translation": "他说"是的"我同意", "emotion": {"valence": 0.0, "arousal": 0.3, "label": "neutral"}, "pacing": "normal"}`
	fixed, ok := tryRecoverDubbingPlanJSON(raw)
	if !ok {
		t.Fatalf("recovery should have succeeded for unescaped ASCII quotes; got fixed=%q", fixed)
	}
	var plan DubbingPlan
	if err := json.Unmarshal([]byte(fixed), &plan); err != nil {
		t.Fatalf("fixed JSON still does not parse: %v\nfixed=%q", err, fixed)
	}
	if !strings.Contains(plan.Translation, "「") || !strings.Contains(plan.Translation, "」") {
		t.Fatalf("recovered translation should use Chinese quotes, got %q", plan.Translation)
	}
}

// TestTryRecoverDubbingPlanJSON_NoQuotesReturnsFalse: when the
// translation field has no embedded ASCII " (i.e. the failure was
// elsewhere), recovery must return false so the caller surfaces the
// original parser error rather than pretending it fixed something.
func TestTryRecoverDubbingPlanJSON_NoQuotesReturnsFalse(t *testing.T) {
	// Translation is fine, but pacing has a typo (intentional schema-
	// violation) so the original Unmarshal would still fail on
	// strict-validated callers. The helper must not pretend to fix this.
	raw := `{"translation": "你好世界", "emotion": {"valence": 0.0, "arousal": 0.3, "label": "neutral"}, "pacing": "INVALID"}`
	_, ok := tryRecoverDubbingPlanJSON(raw)
	if ok {
		t.Fatalf("recovery must NOT claim to fix non-quote failures")
	}
}

// TestTryRecoverDubbingPlanJSON_NoTranslationFieldReturnsFalse:
// raw JSON that doesn't even have a translation field cannot be
// recovered. The helper must return false in O(regex) time without
// crashing.
func TestTryRecoverDubbingPlanJSON_NoTranslationFieldReturnsFalse(t *testing.T) {
	raw := `{"emotion": {"valence": 0.0, "arousal": 0.3, "label": "neutral"}, "pacing": "normal"}`
	_, ok := tryRecoverDubbingPlanJSON(raw)
	if ok {
		t.Fatalf("recovery must return false when translation field is missing")
	}
}

// TestTryRecoverDubbingPlanJSON_TruncatedMiddleReturnsFalse:
// translation field is unterminated (no closing quote / delimiter).
// The regex still matches the truncated value, but the resulting
// fixed JSON does not parse cleanly. The sanity check must catch
// this and return false rather than producing corrupted JSON.
func TestTryRecoverDubbingPlanJSON_TruncatedMiddleReturnsFalse(t *testing.T) {
	// Truncated: no closing quote at all → regex falls back to
	// matching another field's quote, sanity check should fail.
	raw := `{"translation": "他说"unfinished... "emotion": {"valence": 0.0, "arousal": 0.3, "label": "neutral"}`
	_, ok := tryRecoverDubbingPlanJSON(raw)
	if ok {
		t.Fatalf("recovery must return false for truncated input that yields invalid result")
	}
}

// TestTranslateWithDubbingPlan_RecoveredAsciiQuotes: end-to-end —
// provider returns malformed JSON with ASCII quotes in translation,
// recovery kicks in and the call succeeds with the cleaned text.
func TestTranslateWithDubbingPlan_RecoveredAsciiQuotes(t *testing.T) {
	rawArgs := `{"translation": "他说"是的，我同意"然后离开", "emotion": {"valence": 0.0, "arousal": 0.3, "label": "neutral"}, "pacing": "normal"}`
	resp := buildToolCallResponse("emit_dubbing_plan", rawArgs, providerUsage{})
	body, _ := json.Marshal(resp)
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		baseURL: stub.URL, apiKey: "sk-test", model: "qwen-plus",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	got, err := c.TranslateWithDubbingPlan(context.Background(), "en", "zh", "He said yes and left.", 4.0, 4.0, nil, "")
	if err != nil {
		t.Fatalf("recovery should make this call succeed, got err: %v", err)
	}
	if !strings.Contains(got.Translation, "「") || !strings.Contains(got.Translation, "」") {
		t.Fatalf("recovered translation should use Chinese quotes, got %q", got.Translation)
	}
}

// buildSimplePlanJSON is a one-line dubbing plan builder for table-
// driven tests. Keeps the test fixtures short.
func buildSimplePlanJSON(translation, pacing, emoLabel string, pauseMs int) string {
	plan := map[string]any{
		"translation": translation,
		"emotion": map[string]any{
			"valence": 0.0, "arousal": 0.0, "label": emoLabel,
		},
		"pacing":         pacing,
		"pause_after_ms": pauseMs,
	}
	b, _ := json.Marshal(plan)
	return string(b)
}
