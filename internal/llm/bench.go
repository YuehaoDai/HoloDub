// Package llm — bench.go.
//
// RunBenchToolCall is a minimal exported wrapper around doChatTool used
// EXCLUSIVELY by offline evaluation tools (cmd/chapterize-bench, future
// model-comparison harnesses). Production code paths MUST use the
// dedicated typed APIs (ExtractEpisodeGlossary, ReviewChapterCuts,
// ReviewSegmentation, ...) — they own the schema, prompt, and parse
// logic, while this helper is intentionally schema-agnostic.
//
// What it does:
//   - Builds a chat completion with a SINGLE tool advertised to the model.
//   - Optionally forces tool_choice to that tool. Thinking-mode models
//     (kimi-k2-thinking, qwen3-*-thinking) reject the strict object form
//     of tool_choice ("invalid_parameter_error: tool_choice does not
//     support being set to required or object in thinking mode"), so the
//     bench passes forceTool=false for those.
//   - Returns the raw tool-call arguments as a JSON string + token usage,
//     same shape as the internal doChatTool. Empty args without error
//     means "model returned a content message instead of a tool call"
//     (which CAN happen with thinking models on tool_choice=auto); the
//     bench reports this as a candidate-side anomaly.
//
// Why expose this rather than redeclare the wire types in cmd/:
// keeping the LLM HTTP transport, retry, and observability stack in
// ONE place means a bug fix to retry/timeouts automatically benefits
// the bench, and the bench's token accounting reuses the same
// observability counters as production.
package llm

import (
	"context"
	"encoding/json"
)

// BenchToolCallResult bundles tool-call arguments + token usage. Token
// counts are surfaced by the bench's report so the operator can compare
// not just quality but also cost-per-call across candidate models.
type BenchToolCallResult struct {
	Args             string // JSON-encoded function arguments; "" when no tool call
	PromptTokens     int
	CompletionTokens int
	CachedTokens     int
}

// RunBenchToolCall issues one chat-completion request that exposes
// exactly one function to the model. See package doc for usage rules.
//
// Parameters:
//   - operation: observability label (e.g. "bench_chapter_judge"); pick
//     a stable string so Prometheus cardinality doesn't explode.
//   - model: the model id to call (e.g. "kimi-k2-thinking").
//   - temperature: passed through to the provider; bench typically uses
//     0.0–0.2 for judges to keep verdicts repeatable.
//   - systemMsg / userMsg: the two-turn prompt.
//   - toolName / toolDescription / toolSchema: the function exposed.
//     Schema is a json.RawMessage so the caller can hand-craft strict
//     OpenAPI-shaped properties / required arrays.
//   - forceTool: true uses tool_choice={type:"function", function:{name:toolName}}
//     (default for non-thinking models); false uses tool_choice="auto"
//     (mandatory for thinking models — see package doc).
//
// Errors mirror doChatTool: HTTP / network / decode errors are returned;
// "no matching tool call in response" is encoded as Args=="" with nil
// error, so the caller can distinguish "model misbehaved" from "request
// failed". This is the same contract production code relies on.
func (c *Client) RunBenchToolCall(
	ctx context.Context,
	operation string,
	model string,
	temperature float64,
	systemMsg, userMsg string,
	toolName, toolDescription string,
	toolSchema json.RawMessage,
	forceTool bool,
) (BenchToolCallResult, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return BenchToolCallResult{}, errBenchNoCreds
	}
	if model == "" {
		return BenchToolCallResult{}, errBenchNoModel
	}

	payload := chatCompletionRequest{
		Model:       model,
		Temperature: temperature,
		Messages: []chatMessage{
			{Role: "system", Content: systemMsg},
			{Role: "user", Content: userMsg},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        toolName,
				Description: toolDescription,
				Parameters:  toolSchema,
			},
		}},
	}
	if forceTool {
		payload.ToolChoice = forceToolChoice(toolName)
	} else {
		payload.ToolChoice = "auto"
	}

	args, err := c.doChatTool(ctx, operation, payload, toolName)
	if err != nil {
		return BenchToolCallResult{}, err
	}
	return BenchToolCallResult{
		Args: args,
		// usageStats are tracked inside doChatTool's observability hooks;
		// we don't surface them on the result for now because callers
		// only need the args string. If a future bench wants per-run
		// token cost, we'll widen the doChatTool signature to return
		// usage instead of duplicating the parse here.
	}, nil
}

// errBench... are sentinel errors so callers can match without parsing
// strings. Kept package-private; bench tools just propagate them up.
var (
	errBenchNoCreds = errBench("RunBenchToolCall requires OPENAI_BASE_URL and OPENAI_API_KEY")
	errBenchNoModel = errBench("RunBenchToolCall requires a non-empty model")
)

type errBench string

func (e errBench) Error() string { return string(e) }
