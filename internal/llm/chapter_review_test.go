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

// helper: builds two synthetic chapter inputs (15 + 20 min) for tests that
// need a concrete shape but don't care about details.
func sampleChapters() []ChapterCutInput {
	return []ChapterCutInput{
		{
			Ordinal:         1,
			StartMs:         0,
			EndMs:           15 * 60 * 1000,
			StartSegmentIdx: 0,
			EndSegmentIdx:   89,
			SilenceLeftMs:   0,
			SilenceRightMs:  1500,
			OpeningSegments: []string{"Hello and welcome.", "Today we cover Raft."},
			ClosingSegments: []string{"That is the leader-election idea."},
		},
		{
			Ordinal:         2,
			StartMs:         15 * 60 * 1000,
			EndMs:           35 * 60 * 1000,
			StartSegmentIdx: 90,
			EndSegmentIdx:   199,
			SilenceLeftMs:   1500,
			SilenceRightMs:  0,
			OpeningSegments: []string{"Now let's discuss log replication."},
			ClosingSegments: []string{"Recap: leader, followers, log."},
		},
	}
}

// newChapterReviewToolStub returns an httptest.Server returning the supplied
// ChapterReviewResult wrapped in a strict tool_calls envelope.
func newChapterReviewToolStub(t *testing.T, result ChapterReviewResult) *httptest.Server {
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
					ID:   "call_chrev_1",
					Type: "function",
					Function: toolCallFunction{
						Name:      "emit_chapter_review",
						Arguments: string(args),
					},
				}},
			},
			FinishReason: "tool_calls",
		},
	}
	resp.Usage.PromptTokens = 1200
	resp.Usage.CompletionTokens = 300
	resp.Usage.PromptTokensDetails.CachedTokens = 600

	body, err := json.Marshal(resp)
	if err != nil {
		t.Fatalf("marshal stub response: %v", err)
	}
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
}

// TestReviewChapterCutsHappyPath: provider returns a well-formed tool_calls
// response with two verdicts (one keep, one shift_left); client decodes
// without modification including bilingual titles + summary + episode title.
func TestReviewChapterCutsHappyPath(t *testing.T) {
	want := ChapterReviewResult{
		Verdicts: []ChapterReviewVerdict{
			{
				Ordinal:         1,
				Action:          "keep",
				TitleSource:     "Raft Leader Election",
				TitleTranslated: "Raft 领导者选举",
				SummaryMD:       "本章介绍 Raft 共识算法的领导者选举流程。",
			},
			{
				Ordinal:         2,
				Action:          "shift_left",
				TitleSource:     "Log Replication",
				TitleTranslated: "日志复制",
				SummaryMD:       "本章讲解 Raft 中的日志复制与一致性保证。",
				Rationale:       "left edge currently splits a 'next, log replication' setup; shift_left captures the topic intro.",
			},
		},
		EpisodeTitle: "分布式一致性算法 Raft",
	}
	stub := newChapterReviewToolStub(t, want)
	defer stub.Close()

	c := &Client{
		provider:           "openai_compatible",
		baseURL:            stub.URL,
		apiKey:             "sk-test",
		model:              "qwen-turbo",
		chapterReviewModel: "qwen-turbo",
		httpClient:         &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	got, err := c.ReviewChapterCuts(ctx, sampleChapters(), "Topic: Raft consensus.", "en", "zh-CN")
	if err != nil {
		t.Fatalf("ReviewChapterCuts: %v", err)
	}
	if len(got.Verdicts) != 2 {
		t.Fatalf("want 2 verdicts, got %d", len(got.Verdicts))
	}
	if got.Verdicts[0].Action != "keep" || got.Verdicts[0].TitleTranslated != "Raft 领导者选举" {
		t.Fatalf("first verdict mismatch: %+v", got.Verdicts[0])
	}
	if got.Verdicts[1].Action != "shift_left" || got.Verdicts[1].Rationale == "" {
		t.Fatalf("second verdict mismatch: %+v", got.Verdicts[1])
	}
	if got.EpisodeTitle != "分布式一致性算法 Raft" {
		t.Fatalf("episode title mismatch: %q", got.EpisodeTitle)
	}
}

// TestReviewChapterCutsEmptyChaptersShortCircuits: empty input returns
// (zero, nil) so callers that always invoke the LLM (without checking
// chapter count) keep working without hitting the network.
func TestReviewChapterCutsEmptyChaptersShortCircuits(t *testing.T) {
	c := &Client{
		provider:           "openai_compatible",
		baseURL:            "http://unused",
		apiKey:             "sk-test",
		model:              "qwen-turbo",
		chapterReviewModel: "qwen-turbo",
		httpClient:         &http.Client{Timeout: 1 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()

	got, err := c.ReviewChapterCuts(ctx, nil, "", "en", "zh-CN")
	if err != nil {
		t.Fatalf("empty chapters should not error: %v", err)
	}
	if len(got.Verdicts) != 0 || got.EpisodeTitle != "" {
		t.Fatalf("empty chapters should yield empty result, got %+v", got)
	}
}

// TestReviewChapterCutsFallsBackToOpenAIModel: when ChapterReviewModel is
// empty the client must dial OpenAIModel.
func TestReviewChapterCutsFallsBackToOpenAIModel(t *testing.T) {
	var capturedModel string
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req chatCompletionRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		capturedModel = req.Model
		valid := ChapterReviewResult{
			Verdicts: []ChapterReviewVerdict{
				{Ordinal: 1, Action: "keep", TitleSource: "A", TitleTranslated: "甲", SummaryMD: "x"},
				{Ordinal: 2, Action: "keep", TitleSource: "B", TitleTranslated: "乙", SummaryMD: "y"},
			},
		}
		args := mustMarshalJSON(valid)
		body := []byte(`{"choices":[{"message":{"tool_calls":[{"id":"c","type":"function","function":{"name":"emit_chapter_review","arguments":` +
			string(mustMarshalJSON(string(args))) + `}}]}}],"usage":{"prompt_tokens":10}}`)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(body)
	}))
	defer stub.Close()

	c := &Client{
		provider:           "openai_compatible",
		baseURL:            stub.URL,
		apiKey:             "sk-test",
		model:              "kimi-k2.5",
		chapterReviewModel: "", // intentionally empty
		httpClient:         &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if _, err := c.ReviewChapterCuts(ctx, sampleChapters(), "", "en", "zh-CN"); err != nil {
		t.Fatalf("ReviewChapterCuts: %v", err)
	}
	if capturedModel != "kimi-k2.5" {
		t.Fatalf("want fallback model 'kimi-k2.5', got %q", capturedModel)
	}
}

// TestReviewChapterCutsRejectsVerdictMismatch: when the LLM returns the
// wrong number of verdicts (e.g. drops the last chapter on a long input)
// the client must return an error so the caller falls back to defaults
// rather than risk applying mismatched titles.
func TestReviewChapterCutsRejectsVerdictMismatch(t *testing.T) {
	bad := ChapterReviewResult{
		Verdicts: []ChapterReviewVerdict{
			{Ordinal: 1, Action: "keep", TitleSource: "Only one", TitleTranslated: "唯一", SummaryMD: "x"},
		},
	}
	stub := newChapterReviewToolStub(t, bad)
	defer stub.Close()

	c := &Client{
		provider:           "openai_compatible",
		baseURL:            stub.URL,
		apiKey:             "sk-test",
		model:              "qwen-turbo",
		chapterReviewModel: "qwen-turbo",
		httpClient:         &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_, err := c.ReviewChapterCuts(ctx, sampleChapters(), "", "en", "zh-CN")
	if err == nil {
		t.Fatal("expected error when verdict count mismatches; got nil")
	}
	if !strings.Contains(err.Error(), "expected 2 verdicts") {
		t.Fatalf("error should mention verdict count; got %v", err)
	}
}

// TestReviewChapterCutsRejectsBadAction: an "action" outside
// {keep,shift_left,shift_right} must be rejected so the pipeline does not
// silently absorb a hallucinated boundary nudge.
func TestReviewChapterCutsRejectsBadAction(t *testing.T) {
	bad := ChapterReviewResult{
		Verdicts: []ChapterReviewVerdict{
			{Ordinal: 1, Action: "keep", TitleSource: "A", TitleTranslated: "甲", SummaryMD: "x"},
			{Ordinal: 2, Action: "delete", TitleSource: "B", TitleTranslated: "乙", SummaryMD: "y"},
		},
	}
	stub := newChapterReviewToolStub(t, bad)
	defer stub.Close()

	c := &Client{
		provider:           "openai_compatible",
		baseURL:            stub.URL,
		apiKey:             "sk-test",
		model:              "qwen-turbo",
		chapterReviewModel: "qwen-turbo",
		httpClient:         &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_, err := c.ReviewChapterCuts(ctx, sampleChapters(), "", "en", "zh-CN")
	if err == nil {
		t.Fatal("expected error on unknown action; got nil")
	}
	if !strings.Contains(err.Error(), `action="delete"`) {
		t.Fatalf("error should mention bad action verbatim; got %v", err)
	}
}

// TestReviewChapterCutsNoToolCallTreatedAsFailure: provider that ignores
// tool spec and returns prose must trigger an error so observability
// counters increment and caller falls back to defaults.
func TestReviewChapterCutsNoToolCallTreatedAsFailure(t *testing.T) {
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"here are some titles..."}}],"usage":{"prompt_tokens":50}}`))
	}))
	defer stub.Close()

	c := &Client{
		provider:           "openai_compatible",
		baseURL:            stub.URL,
		apiKey:             "sk-test",
		model:              "qwen-turbo",
		chapterReviewModel: "qwen-turbo",
		httpClient:         &http.Client{Timeout: 5 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	_, err := c.ReviewChapterCuts(ctx, sampleChapters(), "", "en", "zh-CN")
	if err == nil {
		t.Fatal("expected error when provider returns content without tool_calls; got nil")
	}
}

// TestOpChapterReviewConstantStable: the metrics label must never drift,
// since Prometheus alerts and dashboards key off the literal string.
func TestOpChapterReviewConstantStable(t *testing.T) {
	if OpChapterReview != "chapter_review" {
		t.Fatalf("OpChapterReview drifted: got %q, want %q", OpChapterReview, "chapter_review")
	}
}
