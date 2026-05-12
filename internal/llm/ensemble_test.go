package llm

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// ensembleStubHandler is the shared mock for RetranslateEnsemble tests.
// It routes incoming /chat/completions requests by inspecting the request
// body:
//
//   - If the payload has tools, treat it as a judge call (use judgeReplies
//     keyed by the *target text* sent for scoring, since the test fixture
//     is keyed on which candidate is being judged).
//   - Otherwise treat it as a retranslate call (use retranslateReplies
//     keyed by payload.Model).
//
// retranslateLatency lets a specific model lag so we can assert parallel
// fanout: if calls were serial, the slow model would dominate wall time.
type ensembleStubHandler struct {
	t                  *testing.T
	retranslateReplies map[string]string  // model -> candidate text
	retranslateErrors  map[string]int     // model -> HTTP status to return (0 = ok)
	retranslateLatency map[string]time.Duration
	judgeReplies       map[string]float64 // candidate text -> fidelity score
	retranslateCalls   atomic.Int32
	judgeCalls         atomic.Int32
	mu                 sync.Mutex
	seenModels         []string
}

func (h *ensembleStubHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	var req chatCompletionRequest
	if err := json.Unmarshal(body, &req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	h.mu.Lock()
	h.seenModels = append(h.seenModels, req.Model)
	h.mu.Unlock()

	if len(req.Tools) > 0 {
		// Judge call. Extract candidate text from the user message; the
		// test fixture writes the candidate's text into the prompt
		// verbatim, so a substring scan locates it.
		h.judgeCalls.Add(1)
		userMsg := ""
		for _, m := range req.Messages {
			if m.Role == "user" {
				userMsg = m.Content
			}
		}
		var matched string
		var fidelity float64
		for cand, score := range h.judgeReplies {
			if strings.Contains(userMsg, cand) {
				matched = cand
				fidelity = score
				break
			}
		}
		if matched == "" {
			// Default judge score when fixture doesn't recognise text;
			// 0.5 is below the typical accept threshold but non-zero so
			// OverallScore() does not collapse the pool.
			fidelity = 0.5
		}
		verdictJSON, _ := json.Marshal(map[string]any{
			"fidelity":  fidelity,
			"fluency":   fidelity,
			"coherence": fidelity,
			"verdict":   "accept",
		})
		resp := buildToolCallResponse("emit_judge_verdict", string(verdictJSON), providerUsage{
			PromptTokens: 200, CompletionTokens: 30,
		})
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
		return
	}

	// Retranslate call.
	h.retranslateCalls.Add(1)
	if delay, ok := h.retranslateLatency[req.Model]; ok && delay > 0 {
		time.Sleep(delay)
	}
	if status, ok := h.retranslateErrors[req.Model]; ok && status != 0 {
		// httpx classifier needs an error response, not a plain content.
		http.Error(w, "stub error for "+req.Model, status)
		return
	}
	text, ok := h.retranslateReplies[req.Model]
	if !ok {
		text = "<missing reply for " + req.Model + ">"
	}
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
			}{Content: text},
			FinishReason: "stop",
		},
	}
	resp.Usage.PromptTokens = 100
	resp.Usage.CompletionTokens = 20
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

func newEnsembleStub(t *testing.T, h *ensembleStubHandler) *httptest.Server {
	t.Helper()
	h.t = t
	return httptest.NewServer(h)
}

func newEnsembleClient(stubURL string) *Client {
	return &Client{
		provider:                 "openai_compatible",
		baseURL:                  stubURL,
		apiKey:                   "sk-test",
		model:                    "deepseek-chat",
		retranslationModel:       "deepseek-chat",
		retranslationTemperature: 0.3,
		judgeModel:               "qwen-turbo",
		httpClient:               &http.Client{Timeout: 5 * time.Second},
		thinkingHTTPClient:       &http.Client{Timeout: 30 * time.Second},
	}
}

func ensembleArgsFixture() EnsembleArgs {
	return EnsembleArgs{
		SourceLanguage:      "en",
		TargetLanguage:      "zh",
		SourceText:          "Hello, this is a test segment.",
		CurrentTrans:        "你好，这是一个测试段落。",
		TargetSec:           4.0,
		ActualSec:           5.2,
		Attempt:             3,
		MaxAttempts:         5,
		DriftThresholdPct:   0.10,
		History:             nil,
		ObservedCharsPerSec: 4.0,
		ContextBefore:       nil,
		NextSourceText:      "",
		TranslationSummary:  "",
		EpisodeSummary:      "",
	}
}

// TestEnsemble_HappyPath_PicksHighestJudgeScore: two models return
// distinct candidates; judge scores deepseek=0.7, qwen=0.9 → qwen wins.
func TestEnsemble_HappyPath_PicksHighestJudgeScore(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{
			"deepseek-chat": "翻译候选 A — deepseek",
			"qwen-plus":     "翻译候选 B — qwen",
		},
		judgeReplies: map[string]float64{
			"翻译候选 A — deepseek": 0.70,
			"翻译候选 B — qwen":     0.90,
		},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)

	res, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"deepseek-chat", "qwen-plus"}, "")
	if err != nil {
		t.Fatalf("ensemble failed: %v", err)
	}
	if res.BestModel != "qwen-plus" {
		t.Fatalf("expected qwen-plus to win on score, got %q", res.BestModel)
	}
	if res.Best != "翻译候选 B — qwen" {
		t.Fatalf("unexpected winning text: %q", res.Best)
	}
	if res.BestVerdict.Fidelity != 0.90 {
		t.Fatalf("expected winning fidelity 0.90, got %v", res.BestVerdict.Fidelity)
	}
	if h.retranslateCalls.Load() != 2 {
		t.Fatalf("expected 2 retranslate calls, got %d", h.retranslateCalls.Load())
	}
	if h.judgeCalls.Load() != 2 {
		t.Fatalf("expected 2 judge calls, got %d", h.judgeCalls.Load())
	}
	if len(res.Candidates) != 2 {
		t.Fatalf("expected 2 candidates returned, got %d", len(res.Candidates))
	}
}

// TestEnsemble_ParallelFanout: assert wall time < sum of per-call
// latencies, i.e. retranslate goroutines actually run concurrently.
// One model lags 200ms, the other 200ms; serial would be ≥ 400ms +
// 2×judge. We allow generous slack for CI variance.
func TestEnsemble_ParallelFanout(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{
			"slow-1": "candidate-1",
			"slow-2": "candidate-2",
		},
		retranslateLatency: map[string]time.Duration{
			"slow-1": 200 * time.Millisecond,
			"slow-2": 200 * time.Millisecond,
		},
		judgeReplies: map[string]float64{
			"candidate-1": 0.6,
			"candidate-2": 0.8,
		},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)
	c.httpClient = &http.Client{Timeout: 10 * time.Second}

	start := time.Now()
	res, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"slow-1", "slow-2"}, "")
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("ensemble failed: %v", err)
	}
	if res.BestModel != "slow-2" {
		t.Fatalf("expected slow-2 to win, got %q", res.BestModel)
	}
	// Serial would be ≥ 400ms (retranslate) + 2×~judge.
	// Parallel should be ≈ 200ms (retranslate) + ≈ judge time. Cap at 350ms
	// to leave room for httptest + JSON overhead on slow CI runners.
	if elapsed >= 350*time.Millisecond {
		t.Fatalf("ensemble appears serial: wall=%v exceeds parallel budget", elapsed)
	}
}

// TestEnsemble_OneCandidateFailsRetranslate: provider returns HTTP 500
// for one model; the other succeeds; ensemble still returns the
// surviving candidate without bubbling the per-model error.
func TestEnsemble_OneCandidateFailsRetranslate(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{
			"good-model": "good translation",
		},
		retranslateErrors: map[string]int{
			"bad-model": http.StatusInternalServerError,
		},
		judgeReplies: map[string]float64{
			"good translation": 0.85,
		},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)
	c.httpClient = &http.Client{Timeout: 2 * time.Second}

	res, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"good-model", "bad-model"}, "")
	if err != nil {
		t.Fatalf("ensemble must tolerate one failure, got: %v", err)
	}
	if res.BestModel != "good-model" {
		t.Fatalf("good-model should win by default, got %q", res.BestModel)
	}
	// Verify the failing candidate's error is preserved in the result for
	// observability without poisoning the winner.
	var sawErr bool
	for _, c := range res.Candidates {
		if c.Model == "bad-model" && c.Err != nil {
			sawErr = true
		}
	}
	if !sawErr {
		t.Fatal("expected bad-model candidate to carry its retranslate error")
	}
}

// TestEnsemble_AllCandidatesFail: every model returns 500 → ensemble
// returns an error wrapping the first underlying failure so the agent
// caller can fall back to single-model retranslate.
func TestEnsemble_AllCandidatesFail(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateErrors: map[string]int{
			"m1": http.StatusInternalServerError,
			"m2": http.StatusBadGateway,
		},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)
	c.httpClient = &http.Client{Timeout: 2 * time.Second}

	_, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"m1", "m2"}, "")
	if err == nil {
		t.Fatal("ensemble must error when every candidate fails")
	}
	if !strings.Contains(err.Error(), "every candidate failed") {
		t.Fatalf("expected 'every candidate failed' wrapper, got: %v", err)
	}
}

// TestEnsemble_ContextCancelDuringRetranslate: cancelling the parent
// context mid-flight must surface a ctx.Err()-wrapped failure rather
// than picking a partial winner.
func TestEnsemble_ContextCancelDuringRetranslate(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{
			"slow-m": "ok",
		},
		retranslateLatency: map[string]time.Duration{
			"slow-m": 500 * time.Millisecond,
		},
		judgeReplies: map[string]float64{"ok": 0.9},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(50 * time.Millisecond)
		cancel()
	}()
	_, err := c.RetranslateEnsemble(ctx,
		ensembleArgsFixture(), []string{"slow-m"}, "")
	if err == nil {
		t.Fatal("ensemble must return error when ctx cancelled")
	}
	if !errors.Is(err, context.Canceled) && !strings.Contains(err.Error(), "context canceled") {
		t.Fatalf("expected context cancellation in error chain, got: %v", err)
	}
}

// TestEnsemble_EmptyModels: degenerate input must fail fast — the
// agent layer should be gating ensemble use, but the API still has
// to be defensive.
func TestEnsemble_EmptyModels(t *testing.T) {
	c := newEnsembleClient("https://example.invalid")
	_, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), nil, "")
	if err == nil {
		t.Fatal("empty models must return error")
	}
}

// TestEnsemble_SingleModel: degenerate-but-valid case used during L2
// smoke; passes through 1 retranslate + 1 judge and returns that
// single candidate as Best. Useful as a feature-flag opt-out without
// a separate code path.
func TestEnsemble_SingleModel(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{"only-model": "only-translation"},
		judgeReplies:       map[string]float64{"only-translation": 0.77},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)

	res, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"only-model"}, "")
	if err != nil {
		t.Fatalf("single-model ensemble failed: %v", err)
	}
	if res.BestModel != "only-model" || res.Best != "only-translation" {
		t.Fatalf("unexpected single-model result: %+v", res)
	}
	if h.retranslateCalls.Load() != 1 || h.judgeCalls.Load() != 1 {
		t.Fatalf("single-model: expected 1+1 calls, got rt=%d jd=%d",
			h.retranslateCalls.Load(), h.judgeCalls.Load())
	}
}

// TestEnsemble_JudgeModelOverride: the override must actually route
// judge calls to that model (not c.judgeModel) — verified by recording
// the model name on each request and asserting we see exactly the
// override for judge calls.
func TestEnsemble_JudgeModelOverride(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{
			"m1": "candidate text",
		},
		judgeReplies: map[string]float64{"candidate text": 0.8},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)
	c.judgeModel = "default-judge" // should be ignored when override is set

	_, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"m1"}, "kimi-k2.5")
	if err != nil {
		t.Fatalf("ensemble failed: %v", err)
	}
	// Verify the judge call used the override.
	h.mu.Lock()
	defer h.mu.Unlock()
	var sawOverride, sawDefault bool
	for _, m := range h.seenModels {
		if m == "kimi-k2.5" {
			sawOverride = true
		}
		if m == "default-judge" {
			sawDefault = true
		}
	}
	if !sawOverride {
		t.Fatalf("judge override 'kimi-k2.5' never sent; saw %v", h.seenModels)
	}
	if sawDefault {
		t.Fatalf("default judge model leaked through override; saw %v", h.seenModels)
	}
	// Verify c.judgeModel was not mutated (race protection sanity check).
	if c.judgeModel != "default-judge" {
		t.Fatalf("c.judgeModel must remain 'default-judge', got %q", c.judgeModel)
	}
}

// TestEnsemble_TieBreakLowerIndexWins: when two candidates score
// identically, the lower-index (earlier in models list) wins. This
// is the documented determinism guarantee.
func TestEnsemble_TieBreakLowerIndexWins(t *testing.T) {
	h := &ensembleStubHandler{
		retranslateReplies: map[string]string{
			"first":  "translation-first",
			"second": "translation-second",
		},
		judgeReplies: map[string]float64{
			"translation-first":  0.80,
			"translation-second": 0.80,
		},
	}
	stub := newEnsembleStub(t, h)
	defer stub.Close()
	c := newEnsembleClient(stub.URL)

	res, err := c.RetranslateEnsemble(context.Background(),
		ensembleArgsFixture(), []string{"first", "second"}, "")
	if err != nil {
		t.Fatalf("ensemble failed: %v", err)
	}
	if res.BestModel != "first" {
		t.Fatalf("on tie, lower-index 'first' must win; got %q", res.BestModel)
	}
}
