// Package llm — OPT-002 LLM-as-Judge MVP.
//
// JudgeFidelity asks a low-cost LLM to score one (source, translation)
// pair on three independent axes plus an action verdict. The MVP runs
// in observe-only mode (JUDGE_OBSERVE_ONLY=true): scores are persisted
// for analysis but do NOT influence the TTS retry loop. Decision
// integration is OPT-201 (SegmentAgent ReAct refactor).
//
// Why a separate file: Judge has a stable contract independent of the
// translate / review prompt families, and keeping it isolated makes it
// trivial for OPT-201 / OPT-202 (ensemble) to swap or layer.
package llm

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"holodub/internal/observability"
)

// JudgeArgs is the input to one judge call. It intentionally mirrors the
// translation prompt context (episode summary + adjacent segments) so the
// judge sees roughly what the translator saw — judging in isolation would
// over-flag any abbreviation that depends on surrounding context.
type JudgeArgs struct {
	SrcText        string
	TgtText        string
	SrcLang        string
	TgtLang        string
	EpisodeSummary string           // optional; pass job.TranslationSummary when available
	PrevContext    []ContextSegment // optional; preceding 1-2 (src,tgt) pairs
}

// JudgeResult is the structured verdict returned by the judge LLM.
//
// The three sub-scores are 0..1 scalars (1 = best). Issues is a short list
// of human-readable problems (empty when verdict=="accept"). Verdict is
// one of: "accept" | "retry" | "split"; observe-only mode logs but does
// not act on it.
type JudgeResult struct {
	Fidelity  float64  `json:"fidelity"`
	Fluency   float64  `json:"fluency"`
	Coherence float64  `json:"coherence"`
	Issues    []string `json:"issues,omitempty"`
	Verdict   string   `json:"verdict"`
}

// OverallScore returns a single scalar 0..1 used as Segment.JudgeScore.
// Currently equal to Fidelity (the single most important axis); kept as
// a method so OPT-201 can change the aggregation without touching call sites.
func (r JudgeResult) OverallScore() float64 {
	if r.Fidelity > 0 {
		return r.Fidelity
	}
	// Fallback: average of any available sub-score (defends against a
	// provider that only populates fluency / coherence).
	var sum float64
	var n int
	for _, v := range []float64{r.Fluency, r.Coherence} {
		if v > 0 {
			sum += v
			n++
		}
	}
	if n == 0 {
		return 0
	}
	return sum / float64(n)
}

// judgeToolSchema is the strict JSON Schema the judge LLM must satisfy.
// Marshalled once at init() so a typo crashes immediately, not on first
// request — a critical-path scoring function should never silently reject
// input due to a malformed schema.
var judgeToolSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"fidelity":  map[string]any{"type": "number", "minimum": 0, "maximum": 1, "description": "Information preservation: 1.0 = no meaning omitted/added/distorted, 0.0 = wrong meaning."},
		"fluency":   map[string]any{"type": "number", "minimum": 0, "maximum": 1, "description": "Naturalness when spoken aloud in the target language; 1.0 = native-quality, 0.0 = unintelligible."},
		"coherence": map[string]any{"type": "number", "minimum": 0, "maximum": 1, "description": "Consistency with episode summary and adjacent segments (terminology, register)."},
		"issues":    map[string]any{"type": "array", "items": map[string]any{"type": "string"}, "description": "Short concrete problems; leave empty when verdict='accept'."},
		"verdict":   map[string]any{"type": "string", "enum": []string{"accept", "retry", "split"}},
	},
	"required":             []string{"fidelity", "fluency", "coherence", "verdict"},
	"additionalProperties": false,
})

func judgeSystemPrompt(srcLang, tgtLang string) string {
	return fmt.Sprintf(
		"You are a professional dubbing translation judge. You score ONE translated segment on three axes "+
			"(fidelity, fluency, coherence) using the emit_judge_verdict function. Be strict but fair: an over-cautious "+
			"verdict wastes retries; an over-permissive verdict ships errors.\n\n"+
			"[Scoring guide]\n"+
			"- fidelity: 1.0 = every fact / instruction / nuance from %s is preserved in %s; 0.5 = key idea correct "+
			"but a detail is omitted or added; 0.0 = different meaning.\n"+
			"- fluency: 1.0 = sounds natural spoken aloud by a native %s speaker; 0.5 = grammatical but awkward; "+
			"0.0 = unintelligible / robotic.\n"+
			"- coherence: 1.0 = terminology and register match the episode summary and preceding segments; 0.5 = "+
			"acceptable variation; 0.0 = contradicts established usage.\n\n"+
			"[Verdict guide]\n"+
			"- accept: all three scores ≥ 0.7\n"+
			"- retry: any score < 0.5 OR fidelity < 0.7 (retranslate with feedback)\n"+
			"- split: source segment is too long / multi-topic and the translator could not preserve everything; the "+
			"upstream pipeline should split the source and re-translate.\n\n"+
			"[Strictness floor]\n"+
			"You MUST always emit fidelity / fluency / coherence as numbers in [0, 1]. Issues is optional unless the "+
			"verdict is 'retry' or 'split', in which case populate it with one or two short concrete problems.",
		srcLang, tgtLang, tgtLang,
	)
}

// JudgeFidelity scores one (src, tgt) pair. Returns nil, nil when the
// judge model is not configured (judging disabled) so callers can skip
// silently. Never panics; observe-only callers should ignore errors —
// a judge failure must NOT cause the segment to fail.
func (c *Client) JudgeFidelity(ctx context.Context, args JudgeArgs) (*JudgeResult, error) {
	if c.judgeModel == "" {
		return nil, nil
	}
	if c.baseURL == "" || c.apiKey == "" {
		return nil, errors.New("judge requires OPENAI_BASE_URL and OPENAI_API_KEY")
	}
	if strings.TrimSpace(args.SrcText) == "" || strings.TrimSpace(args.TgtText) == "" {
		// Nothing to score; skip without a metric bump.
		return nil, nil
	}

	var userMsg strings.Builder
	if args.EpisodeSummary != "" {
		userMsg.WriteString("[Episode summary - terminology and style guide]\n")
		userMsg.WriteString(args.EpisodeSummary)
		userMsg.WriteString("\n[End of episode summary]\n\n")
	}
	if len(args.PrevContext) > 0 {
		userMsg.WriteString("[Preceding segments - for coherence reference]\n")
		for i, seg := range args.PrevContext {
			label := fmt.Sprintf("-%d", len(args.PrevContext)-i)
			fmt.Fprintf(&userMsg, "(%s) %s: %s\n     %s: %s\n", label, args.SrcLang, seg.SrcText, args.TgtLang, seg.TgtText)
		}
		userMsg.WriteString("\n")
	}
	fmt.Fprintf(&userMsg, "[Segment under judgement]\n%s: %s\n%s: %s\n",
		args.SrcLang, args.SrcText, args.TgtLang, args.TgtText)

	payload := chatCompletionRequest{
		Model:       c.judgeModel,
		Temperature: 0.1, // judges should be near-deterministic
		Messages: []chatMessage{
			{Role: "system", Content: judgeSystemPrompt(args.SrcLang, args.TgtLang)},
			{Role: "user", Content: userMsg.String()},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_judge_verdict",
				Description: "Submit the structured verdict for one translated segment.",
				Parameters:  judgeToolSchema,
			},
		}},
		ToolChoice: forceToolChoice("emit_judge_verdict"),
	}

	rawArgs, err := c.doChatTool(ctx, OpJudge, payload, "emit_judge_verdict")
	if err != nil {
		return nil, fmt.Errorf("judge tool call: %w", err)
	}
	if rawArgs == "" {
		// Provider returned content instead of tool — count as parse failure
		// and report so caller can decide. We do NOT silently fall back to
		// content parsing here: judge results must be schema-validated.
		observability.IncLLMStrictParseFailed(OpJudge)
		return nil, errors.New("judge: no tool call in response")
	}

	var result JudgeResult
	if err := json.Unmarshal([]byte(rawArgs), &result); err != nil {
		observability.IncLLMStrictParseFailed(OpJudge)
		return nil, fmt.Errorf("judge: parse tool args: %w (raw: %.200s)", err, rawArgs)
	}
	if result.Verdict == "" {
		// Schema requires verdict; if it slipped through (weak provider
		// schema enforcement), default to "retry" so the segment is
		// re-examined rather than silently accepted.
		result.Verdict = "retry"
	}
	return &result, nil
}
