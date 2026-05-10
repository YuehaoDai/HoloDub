package llm

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// TestUsageParseDeepSeek verifies the DeepSeek-flavoured
// `prompt_cache_hit_tokens` field is correctly captured.
func TestUsageParseDeepSeek(t *testing.T) {
	body := `{
        "choices":[{"message":{"content":"hello"}}],
        "usage":{"prompt_tokens":1024,"completion_tokens":12,"total_tokens":1036,"prompt_cache_hit_tokens":768}
    }`
	var resp chatCompletionResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := resp.Usage.effectiveCached(); got != 768 {
		t.Fatalf("want cached=768, got %d", got)
	}
	if resp.Usage.PromptTokens != 1024 || resp.Usage.CompletionTokens != 12 {
		t.Fatalf("want prompt=1024 completion=12, got prompt=%d completion=%d",
			resp.Usage.PromptTokens, resp.Usage.CompletionTokens)
	}
}

// TestUsageParseDashScope verifies the DashScope-flavoured
// `usage.prompt_tokens_details.cached_tokens` nested field. This is
// the OPT-001 critical path for Qwen on aliyuncs.com — initial impl
// only checked the top-level `cached_tokens` and silently missed all
// hits. The test asserts the nested field IS what populates the metric.
func TestUsageParseDashScope(t *testing.T) {
	body := `{
        "choices":[{"message":{"content":"hello"}}],
        "usage":{
            "prompt_tokens":2048,
            "completion_tokens":12,
            "total_tokens":2060,
            "prompt_tokens_details":{"cached_tokens":1024}
        }
    }`
	var resp chatCompletionResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := resp.Usage.effectiveCached(); got != 1024 {
		t.Fatalf("want cached=1024 from prompt_tokens_details, got %d", got)
	}
}

// TestUsageParseOpenAILegacy verifies the legacy top-level `cached_tokens`
// field (early OpenAI cache rollout / alpha endpoints).
func TestUsageParseOpenAILegacy(t *testing.T) {
	body := `{
        "choices":[{"message":{"content":"hello"}}],
        "usage":{"prompt_tokens":2048,"completion_tokens":24,"total_tokens":2072,"cached_tokens":1500}
    }`
	var resp chatCompletionResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := resp.Usage.effectiveCached(); got != 1500 {
		t.Fatalf("want cached=1500, got %d", got)
	}
}

// TestUsageParseAllThreeShapes verifies max(...) when an experimental
// provider somehow populates all three fields. Helps catch regressions
// where one of the fields is dropped from providerUsage.
func TestUsageParseAllThreeShapes(t *testing.T) {
	body := `{
        "choices":[{"message":{"content":"hello"}}],
        "usage":{
            "prompt_tokens":1000,
            "completion_tokens":10,
            "cached_tokens":600,
            "prompt_cache_hit_tokens":800,
            "prompt_tokens_details":{"cached_tokens":900}
        }
    }`
	var resp chatCompletionResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := resp.Usage.effectiveCached(); got != 900 {
		t.Fatalf("want max=900, got %d", got)
	}
}

// TestUsageParseNoCache verifies a zero-cache response parses to cached=0.
func TestUsageParseNoCache(t *testing.T) {
	body := `{
        "choices":[{"message":{"content":"hi"}}],
        "usage":{"prompt_tokens":50,"completion_tokens":3}
    }`
	var resp chatCompletionResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := resp.Usage.effectiveCached(); got != 0 {
		t.Fatalf("want cached=0, got %d", got)
	}
}

// TestUsageParseDashScopeNestedZero is the actual-payload regression test:
// DashScope qwen-turbo returns prompt_tokens_details with cached_tokens=0
// when the prompt is below the cache-eligibility threshold (~256 tokens).
// The struct must parse this without crashing.
func TestUsageParseDashScopeNestedZero(t *testing.T) {
	body := `{
        "choices":[{"message":{"content":"hi"}}],
        "usage":{"prompt_tokens":205,"completion_tokens":2,"total_tokens":207,"prompt_tokens_details":{"cached_tokens":0}}
    }`
	var resp chatCompletionResponse
	if err := json.Unmarshal([]byte(body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got := resp.Usage.effectiveCached(); got != 0 {
		t.Fatalf("want cached=0 (sub-threshold), got %d", got)
	}
	if resp.Usage.PromptTokens != 205 {
		t.Fatalf("want prompt=205, got %d", resp.Usage.PromptTokens)
	}
}

// TestSystemPromptStable is the OPT-001 cache-friendliness invariant:
// for the same job (same target language, same chars-per-sec rate, same
// episode summary), buildTranslateSystemPrompt MUST return a byte-identical
// string. Otherwise the provider's prefix cache will miss every segment
// and OPT-001 yields zero benefit.
//
// OPT-001-followup-1: the signature only accepts per-job constants now —
// targetSec / limit live in the user message — so this test additionally
// asserts the function CANNOT be passed per-segment values.
func TestSystemPromptStable(t *testing.T) {
	const summary = "Genre: technical lecture. Speakers: SPK_01 (host). Term map: Raft -> Raft, MapReduce -> MapReduce."

	p1 := buildTranslateSystemPrompt("zh-CN", 4.0, summary)
	p2 := buildTranslateSystemPrompt("zh-CN", 4.0, summary)
	if p1 != p2 {
		t.Fatalf("system prompt should be byte-stable for identical inputs:\nhash1=%s\nhash2=%s",
			hashPrompt(p1), hashPrompt(p2))
	}

	// Sanity: changing summary must change prompt.
	p3 := buildTranslateSystemPrompt("zh-CN", 4.0, summary+" extra")
	if p1 == p3 {
		t.Fatal("changing summary did not change system prompt")
	}

	// Sanity: changing targetLanguage must change prompt.
	p5 := buildTranslateSystemPrompt("ja", 4.5, summary)
	if p1 == p5 {
		t.Fatal("changing targetLanguage did not change system prompt")
	}

	// Sanity: changing rate must change prompt (rate is per-job, derived
	// from the voice profile — different jobs may use different rates,
	// but within a job the rate is constant).
	p6 := buildTranslateSystemPrompt("zh-CN", 5.5, summary)
	if p1 == p6 {
		t.Fatal("changing rate did not change system prompt")
	}

	// OPT-001-followup-1 reverse-assertion: the per-segment-stable system
	// prompt must NOT mention any specific segment duration or char limit.
	// If a future maintainer accidentally re-adds them, the test fails loud.
	for _, banned := range []string{
		"Segment duration:",
		"Hard character limit:",
		"7.5 seconds",
		"30 characters",
	} {
		if strings.Contains(p1, banned) {
			t.Fatalf("system prompt must not contain per-segment text %q "+
				"(OPT-001-followup-1: would break prefix cache)", banned)
		}
	}
}

// TestSystemPromptCachePrefixSize sanity-checks that the stable prefix
// (everything before the per-segment user message) is large enough to
// realistically trigger the provider's prefix cache. DashScope / OpenAI
// typically need ≥256 / ≥1024 tokens of stable prefix; 1 char ≈ 0.25
// tokens for English, so we want ≥1024 chars (~256 tokens worst-case)
// to guarantee any provider can cache.
func TestSystemPromptCachePrefixSize(t *testing.T) {
	const summary = "Genre: technical lecture. Speakers: SPK_01 (host)."
	p := buildTranslateSystemPrompt("zh-CN", 4.0, summary)
	if len(p) < 1024 {
		t.Fatalf("system prompt is %d bytes; cache may not trigger on smaller-prefix providers (need ≥1024)", len(p))
	}
	// Also assert episode reference is the LAST piece of the system prompt
	// (so it caches independently of per-call content).
	if !strings.HasSuffix(strings.TrimSpace(p), "[End of episode reference]") {
		t.Fatal("episode reference must be at the END of system prompt for cache stability")
	}
}

// TestTranslateUserMsgContainsPerSegmentConstraints verifies the OPT-001-
// followup-1 contract: per-segment duration & character limit MUST appear
// in the user message body sent to the LLM. If they silently disappear
// the model loses critical sync information; if they migrate back into
// system the cache breaks. We capture the actual chat payload via a stub
// server and inspect both messages.
func TestTranslateUserMsgContainsPerSegmentConstraints(t *testing.T) {
	type capture struct {
		System string
		User   string
	}
	got := make([]capture, 0, 3)

	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req chatCompletionRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode request: %v", err)
		}
		var sys, usr string
		for _, m := range req.Messages {
			switch m.Role {
			case "system":
				sys = m.Content
			case "user":
				usr = m.Content
			}
		}
		got = append(got, capture{System: sys, User: usr})
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":100}}`))
	}))
	defer stub.Close()

	c := &Client{
		provider:   "openai_compatible",
		baseURL:    stub.URL,
		apiKey:     "sk-test",
		model:      "qwen-turbo",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	type call struct {
		text      string
		targetSec float64
	}
	calls := []call{
		{text: "Hello world", targetSec: 3.5},
		{text: "Another segment here", targetSec: 9.0},
		{text: "Final one", targetSec: 18.25},
	}
	for _, ca := range calls {
		if _, err := c.translateWithDurationViaOpenAI(
			ctx, "en", "zh-CN", ca.text, ca.targetSec, 4.0,
			nil, "Genre: tech.",
		); err != nil {
			t.Fatalf("translateWithDurationViaOpenAI[%s]: %v", ca.text, err)
		}
	}
	if len(got) != len(calls) {
		t.Fatalf("want %d captured calls, got %d", len(calls), len(got))
	}

	// 1. Every user message contains the per-segment constraints block
	//    with the actual numbers; if they disappeared we'd lose audio sync.
	for i, ca := range calls {
		want := []string{
			"[Per-segment constraints]",
			"Segment duration:",
			"Hard character limit:",
		}
		for _, sub := range want {
			if !strings.Contains(got[i].User, sub) {
				t.Fatalf("call[%d] user missing %q; user=%q", i, sub, got[i].User)
			}
		}
		// Numerical proof: the chosen targetSec appears verbatim with one
		// decimal place. Catches any future helper that quietly rounds.
		wantSec := fmt.Sprintf("%.1f seconds", ca.targetSec)
		if !strings.Contains(got[i].User, wantSec) {
			t.Fatalf("call[%d] user missing duration %q; user=%q", i, wantSec, got[i].User)
		}
	}

	// 2. System message MUST be byte-identical across calls (this is the
	//    whole point of OPT-001-followup-1; if it isn't, prefix cache misses
	//    every segment).
	for i := 1; i < len(got); i++ {
		if got[i].System != got[0].System {
			t.Fatalf("system prompt drifted between calls 0 and %d; "+
				"hash0=%s hash%d=%s", i, hashPrompt(got[0].System),
				i, hashPrompt(got[i].System))
		}
	}

	// 3. System MUST NOT contain per-segment numbers from any call.
	for _, banned := range []string{"3.5 seconds", "9.0 seconds", "18.3 seconds", "Segment duration:", "Hard character limit:"} {
		if strings.Contains(got[0].System, banned) {
			t.Fatalf("system contains per-segment text %q (broke cache invariant)", banned)
		}
	}
}


// TestOperationConstants guards against a future maintainer renaming or
// removing the metric labels. Changing these breaks existing dashboards /
// recording rules.
func TestOperationConstants(t *testing.T) {
	cases := map[string]string{
		OpTranslate:           "translate",
		OpRetranslate:         "retranslate",
		OpRetranslateThinking: "retranslate_thinking",
		OpSummary:             "summary",
		OpReview:              "review",
		OpJudge:               "judge",
		OpGlossary:            "glossary",
		OpChapterReview:       "chapter_review",
	}
	for got, want := range cases {
		if got != want {
			t.Fatalf("operation constant changed: got %q want %q", got, want)
		}
	}
}

func hashPrompt(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:8])
}

// ── OPT-003: ReviewSegmentation tool-call path tests ──────────────────────

// TestReviewSystemPromptDualMode confirms the tool-mode and prompt-mode
// system prompts share the SAME prefix (cache-friendly) but diverge on the
// closing instruction. A regression on either contract breaks OPT-001 cache
// reuse OR OPT-003 tool-call enforcement.
func TestReviewSystemPromptDualMode(t *testing.T) {
	toolP := reviewSystemPrompt("en", true)
	promptP := reviewSystemPrompt("en", false)
	if toolP == promptP {
		t.Fatal("tool-mode and prompt-mode system prompts must differ on closing instruction")
	}
	// Cache-friendly invariant: both share the long fixed preamble.
	commonPrefix := commonPrefixOf(toolP, promptP)
	if len(commonPrefix) < 800 {
		t.Fatalf("shared prefix %d bytes too small (want ≥800 to keep prefix cache hot across modes)", len(commonPrefix))
	}
	if !strings.Contains(toolP, "emit_segment_suggestions") {
		t.Fatal("tool-mode prompt must reference the tool name")
	}
	if !strings.Contains(promptP, "JSON array") {
		t.Fatal("prompt-mode prompt must describe the JSON array format")
	}
}

func commonPrefixOf(a, b string) string {
	n := len(a)
	if len(b) < n {
		n = len(b)
	}
	for i := 0; i < n; i++ {
		if a[i] != b[i] {
			return a[:i]
		}
	}
	return a[:n]
}

// TestReviewToolSchemaIsValidJSON sanity-checks the static reviewToolSchema
// JSON Schema parses to the expected shape — guards against typos.
func TestReviewToolSchemaIsValidJSON(t *testing.T) {
	var schema map[string]any
	if err := json.Unmarshal(reviewToolSchema, &schema); err != nil {
		t.Fatalf("reviewToolSchema is not valid JSON: %v", err)
	}
	if schema["type"] != "object" {
		t.Fatal("schema root must be type=object")
	}
	props, ok := schema["properties"].(map[string]any)
	if !ok {
		t.Fatal("schema must have properties map")
	}
	if _, ok := props["suggestions"]; !ok {
		t.Fatal("schema must declare suggestions property")
	}
}

// newReviewToolStub returns a httptest.Server that mocks DashScope's
// chat/completions endpoint, returning the given suggestions wrapped in a
// tool_calls response.
func newReviewToolStub(t *testing.T, suggestions []reviewRawSuggestion) *httptest.Server {
	t.Helper()
	args := mustMarshalJSON(reviewToolArgs{Suggestions: suggestions})
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
					ID:   "call_1",
					Type: "function",
					Function: toolCallFunction{
						Name:      "emit_segment_suggestions",
						Arguments: string(args),
					},
				}},
			},
			FinishReason: "tool_calls",
		},
	}
	resp.Usage.PromptTokens = 800
	resp.Usage.CompletionTokens = 30
	resp.Usage.PromptTokensDetails.CachedTokens = 256

	body, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal stub response: %v", err)
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
}

// TestReviewToolPathHappyPath: tool_calls returned -> parsed correctly.
func TestReviewToolPathHappyPath(t *testing.T) {
	stub := newReviewToolStub(t, []reviewRawSuggestion{
		{Ordinals: []int{0, 1}, Reason: "split mid-clause", Confidence: 0.85},
		{Ordinals: []int{3, 4}, Reason: "conjunction starts next", Confidence: 0.7},
	})
	defer stub.Close()

	c := &Client{
		provider:              "openai_compatible",
		baseURL:               stub.URL,
		apiKey:                "sk-test",
		model:                 "qwen-turbo",
		segmentReviewUseTools: true,
		httpClient:            &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	got, err := c.ReviewSegmentation(ctx, "en", []SegmentInfo{
		{Ordinal: 0, Text: "Hello", StartMs: 0, EndMs: 1000},
		{Ordinal: 1, Text: "world", StartMs: 1000, EndMs: 2000},
		{Ordinal: 2, Text: "foo", StartMs: 2500, EndMs: 3500},
		{Ordinal: 3, Text: "bar", StartMs: 4000, EndMs: 5000},
		{Ordinal: 4, Text: "baz", StartMs: 5100, EndMs: 6000},
	})
	if err != nil {
		t.Fatalf("ReviewSegmentation: %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("want 2 suggestions, got %d", len(got))
	}
	if got[0].Action != "merge" || got[0].Confidence != 0.85 {
		t.Fatalf("first suggestion mismatch: %+v", got[0])
	}
}

// TestReviewToolFallbackToPrompt: tool path returns content (no tool_call),
// caller falls back to legacy prompt parser.
func TestReviewToolFallbackToPrompt(t *testing.T) {
	calls := 0
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.Header().Set("Content-Type", "application/json")
		// First call (tool path): return content but NO tool_calls.
		// Second call (prompt fallback): return JSON array as content.
		if calls == 1 {
			_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"sorry, I'll just write text"}}],"usage":{"prompt_tokens":100}}`))
			return
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"[{\"ordinals\":[1,2],\"reason\":\"close\",\"confidence\":0.9}]"}}],"usage":{"prompt_tokens":120}}`))
	}))
	defer stub.Close()

	c := &Client{
		provider:              "openai_compatible",
		baseURL:               stub.URL,
		apiKey:                "sk-test",
		model:                 "qwen-turbo",
		segmentReviewUseTools: true,
		httpClient:            &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	got, err := c.ReviewSegmentation(ctx, "en", []SegmentInfo{
		{Ordinal: 1, Text: "a", StartMs: 0, EndMs: 1000},
		{Ordinal: 2, Text: "b", StartMs: 1100, EndMs: 2000},
	})
	if err != nil {
		t.Fatalf("ReviewSegmentation: %v", err)
	}
	if len(got) != 1 || got[0].Confidence != 0.9 {
		t.Fatalf("want 1 fallback-parsed suggestion, got %+v", got)
	}
	if calls != 2 {
		t.Fatalf("expected 2 HTTP calls (tool then prompt), got %d", calls)
	}
}

// TestReviewToolFlagOff: when SegmentReviewUseTools=false, tool path is
// never invoked — verifies feature flag respect.
func TestReviewToolFlagOff(t *testing.T) {
	calls := 0
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var req chatCompletionRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		if len(req.Tools) > 0 {
			t.Errorf("flag OFF should not send tools; got %d", len(req.Tools))
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"[]"}}],"usage":{"prompt_tokens":100}}`))
	}))
	defer stub.Close()

	c := &Client{
		provider:              "openai_compatible",
		baseURL:               stub.URL,
		apiKey:                "sk-test",
		model:                 "qwen-turbo",
		segmentReviewUseTools: false,
		httpClient:            &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	got, err := c.ReviewSegmentation(ctx, "en", []SegmentInfo{
		{Ordinal: 0, Text: "a", StartMs: 0, EndMs: 1000},
		{Ordinal: 1, Text: "b", StartMs: 1100, EndMs: 2000},
	})
	if err != nil {
		t.Fatalf("ReviewSegmentation: %v", err)
	}
	if len(got) != 0 {
		t.Fatalf("expected 0 suggestions for empty []; got %d", len(got))
	}
	if calls != 1 {
		t.Fatalf("expected 1 HTTP call (prompt only), got %d", calls)
	}
}
