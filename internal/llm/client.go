package llm

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"

	"holodub/internal/config"
	"holodub/internal/httpx"
	"holodub/internal/observability"
)

type Client struct {
	provider                 string
	baseURL                  string
	apiKey                   string
	model                    string
	temperature              float64
	retranslationModel       string
	retranslationTemperature float64
	thinkingModel            string
	segmentReviewModel       string
	segmentReviewUseTools    bool
	judgeModel               string // OPT-002; "" disables judging
	glossaryModel            string // OPT-402; "" => fall back to model
	chapterReviewModel       string // OPT-403; "" => fall back to model
	thinkingHTTPClient       *http.Client
	httpClient               *http.Client
}

func New(cfg config.Config) *Client {
	retranslationTemp := cfg.RetranslationTemperature
	if retranslationTemp == 0 {
		retranslationTemp = cfg.OpenAITemperature
	}
	thinkingTimeout := time.Duration(cfg.RetranslationThinkingTimeoutSeconds) * time.Second
	if thinkingTimeout <= 0 {
		thinkingTimeout = 600 * time.Second
	}
	return &Client{
		provider:                 strings.ToLower(cfg.TranslationProvider),
		baseURL:                  strings.TrimRight(cfg.OpenAIBaseURL, "/"),
		apiKey:                   cfg.OpenAIAPIKey,
		model:                    cfg.OpenAIModel,
		temperature:              cfg.OpenAITemperature,
		retranslationModel:       cfg.RetranslationModel,
		retranslationTemperature: retranslationTemp,
		thinkingModel:            cfg.RetranslationThinkingModel,
		segmentReviewModel:       cfg.SegmentReviewModel,
		segmentReviewUseTools:    cfg.SegmentReviewUseTools,
		judgeModel:               cfg.JudgeModel,
		glossaryModel:            cfg.GlossaryModel,
		chapterReviewModel:       cfg.ChapterReviewModel,
		thinkingHTTPClient: &http.Client{
			Timeout: thinkingTimeout,
		},
		httpClient: &http.Client{
			Timeout: cfg.OpenAITimeout,
		},
	}
}

// charsPerSec returns the natural spoken character rate for the given language code.
func charsPerSec(lang string) float64 {
	// Empirical speech rates from IndexTTS2 output measurement.
	// Chinese: benchmark showed 9 chars / 2.41 s ≈ 3.7 chars/sec.
	// Using slightly higher 4.0 to keep a small margin in the translation budget.
	switch strings.ToLower(strings.TrimSpace(lang)) {
	case "zh", "zh-cn", "zh-tw":
		return 4.0
	case "ja":
		return 4.5
	case "ko":
		return 3.8
	case "en":
		return 12.0
	default:
		return 10.0
	}
}

// maxChars returns the maximum character count for a translation of the given
// duration in the given target language.
func maxChars(lang string, targetSec float64) int {
	limit := int(math.Ceil(charsPerSec(lang) * targetSec))
	if limit < 1 {
		limit = 1
	}
	return limit
}

func (c *Client) TranslateText(ctx context.Context, sourceLanguage, targetLanguage, text string) (string, error) {
	switch c.provider {
	case "", "mock":
		return mockTranslate(targetLanguage, text), nil
	case "openai_compatible", "openai-compatible":
		return c.translateViaOpenAI(ctx, sourceLanguage, targetLanguage, text)
	default:
		return "", fmt.Errorf("unsupported translation provider %q", c.provider)
	}
}

// TranslateTextWithDuration translates text with an explicit duration constraint
// embedded in the system prompt so the model targets the right character budget.
// charsPerSecHint, when > 0, overrides the language-based default speaking rate
// so the prompt reflects the actual voice profile's measured speed.
// contextBefore provides the immediately preceding translated segments for
// terminology consistency; translationSummary is the episode-level reference card.
func (c *Client) TranslateTextWithDuration(ctx context.Context, sourceLanguage, targetLanguage, text string, targetSec float64, charsPerSecHint float64, contextBefore []ContextSegment, translationSummary string) (string, error) {
	switch c.provider {
	case "", "mock":
		return mockTranslate(targetLanguage, text), nil
	case "openai_compatible", "openai-compatible":
		return c.translateWithDurationViaOpenAI(ctx, sourceLanguage, targetLanguage, text, targetSec, charsPerSecHint, contextBefore, translationSummary)
	default:
		return "", fmt.Errorf("unsupported translation provider %q", c.provider)
	}
}

// RetranslationAttempt records one attempt: text tried and the TTS duration it produced.
type RetranslationAttempt struct {
	Text      string  // translation text used
	ActualSec float64 // TTS output duration in seconds
}

// ContextSegment holds a source+translation pair from an adjacent segment.
// Used to give the LLM local coherence context around the segment being retranslated.
type ContextSegment struct {
	SrcText string // original source-language text
	TgtText string // already-accepted target-language translation
}

// SummarizeTranslation generates a compact episode reference card from a
// representative sample of (source, translation) pairs produced during the
// initial batch translation. The result is stored on the Job and injected
// into every subsequent TTS retranslation prompt to maintain global coherence.
//
// sample should contain 20–30 representative ContextSegment pairs spread
// across the episode. targetLanguage is the dubbed language (e.g. "zh").
func (c *Client) SummarizeTranslation(ctx context.Context, sourceLanguage, targetLanguage string, sample []ContextSegment) (string, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return "", nil // silently skip when no LLM is configured (e.g. mock mode)
	}
	if len(sample) == 0 {
		return "", nil
	}

	var sb strings.Builder
	for i, seg := range sample {
		sb.WriteString(fmt.Sprintf("[%d] %s: %s\n    %s: %s\n", i+1, sourceLanguage, seg.SrcText, targetLanguage, seg.TgtText))
	}

	systemPrompt := fmt.Sprintf(
		"You are a professional dubbing localization consultant.\n\n"+
			"Below is a representative sample of (source, translation) pairs from a dubbing project.\n"+
			"Source language: %s. Target language: %s.\n\n"+
			"%s\n"+
			"Based on these samples, produce a concise REFERENCE CARD (≤200 words) that a translator\n"+
			"can consult to maintain consistency throughout the episode. Include:\n"+
			"1. Genre / setting (e.g. anime, documentary, action film)\n"+
			"2. Key characters or speakers with their names in both languages\n"+
			"3. Recurring terminology, proper nouns, or technical terms with their translations\n"+
			"4. Speaking register and tone (formal/informal, dialect, age group, etc.)\n"+
			"5. Any notable style conventions used in this translation\n\n"+
			"Be specific and concise. Do NOT include the sample texts in your output.",
		sourceLanguage, targetLanguage, sb.String(),
	)

	model := c.model
	if c.retranslationModel != "" {
		model = c.retranslationModel
	}
	payload := chatCompletionRequest{
		Model:       model,
		Temperature: 0.3,
		Messages: []chatMessage{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: "Please produce the reference card now."},
		},
	}
	return c.doChat(ctx, OpSummary, payload)
}

// RetranslateWithConstraint re-translates using the configured retranslation model
// with drift-rate feedback. history contains all previous attempts (text, actualSec).
// driftThresholdPct is the max allowed drift (e.g. 0.06 = 6%).
// useThinking switches to the thinking model with SSE streaming when the normal
// model has stalled (same output for multiple consecutive attempts).
// observedCharsPerSec, when > 0, overrides the language-based default speaking rate
// so the character ceiling reflects the actual voice's measured speed.
// contextBefore contains the preceding 1-2 segments (src+tgt) for local coherence.
// nextSrcText is the source text of the following segment (tone/connector reference).
// translationSummary is the episode-level reference card generated after initial translation.
func (c *Client) RetranslateWithConstraint(
	ctx context.Context,
	sourceLanguage, targetLanguage, srcText, currentTrans string,
	targetSec, actualSec float64,
	attempt, maxAttempts int,
	driftThresholdPct float64,
	history []RetranslationAttempt,
	useThinking bool,
	observedCharsPerSec float64,
	contextBefore []ContextSegment,
	nextSrcText string,
	translationSummary string,
) (string, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return "", errors.New("OPENAI_BASE_URL and OPENAI_API_KEY are required for retranslation")
	}
	model := c.retranslationModel
	if model == "" {
		model = c.model
	}
	if useThinking && c.thinkingModel != "" {
		model = c.thinkingModel
	}
	limit := maxChars(targetLanguage, targetSec)
	rate := charsPerSec(targetLanguage)
	// Use the observed voice-specific rate when available; it overrides the
	// language default and gives the LLM a calibrated character ceiling.
	if observedCharsPerSec > 0 {
		rate = observedCharsPerSec
		limit = int(math.Ceil(targetSec * observedCharsPerSec))
		if limit < 1 {
			limit = 1
		}
	}
	currentLen := len([]rune(currentTrans))
	pctDiff := math.Abs(actualSec-targetSec) / targetSec * 100
	direction := "over"
	if actualSec < targetSec {
		direction = "under"
	}

	// Adjust limit based on observed TTS rate, averaged over ALL history attempts.
	// Using only the current attempt's rate is unstable: a single under-run attempt can
	// show an inflated rate (e.g. 5.27 chars/sec vs the true ~5.0), pushing the ceiling
	// too high (412 chars vs the actual sweet-spot ~395), causing the LLM to overshoot.
	// Weighted average over all data points gives a more stable estimate.
	// When observedCharsPerSec was already provided by the pipeline, only raise the
	// ceiling — never lower it back to the raw history average.
	if currentLen > 0 && actualSec > 0 {
		totalChars := float64(currentLen)
		totalSec := actualSec
		for _, h := range history {
			hc := float64(len([]rune(h.Text)))
			if hc > 0 && h.ActualSec > 0 {
				totalChars += hc
				totalSec += h.ActualSec
			}
		}
		histRate := totalChars / totalSec
		histCeiling := int(math.Ceil(targetSec * histRate))
		if histCeiling > limit {
			limit = histCeiling
		}
	}

	// Build history block for prompt injection.
	// Include every attempt with per-attempt and incremental deltas so the LLM
	// can learn the chars→duration mapping and extrapolate the right target length.
	historyBlock := ""
	if len(history) > 0 {
		historyBlock = "\n\n[Retry history — learn from the pattern]\n"
		for i, h := range history {
			drift := math.Abs(h.ActualSec-targetSec) / targetSec * 100
			dir := "over"
			if h.ActualSec < targetSec {
				dir = "under"
			}
			chars := len([]rune(h.Text))

			deltaChars := ""
			deltaSec := ""
			if i > 0 {
				prevChars := len([]rune(history[i-1].Text))
				dc := chars - prevChars
				ds := h.ActualSec - history[i-1].ActualSec
				sign := "+"
				if dc < 0 {
					sign = ""
				}
				dsSign := "+"
				if ds < 0 {
					dsSign = ""
				}
				deltaChars = fmt.Sprintf(" (Δchars%s%d", sign, dc)
				deltaSec = fmt.Sprintf(", Δsec%s%.2f)", dsSign, ds)
			}
			historyBlock += fmt.Sprintf("  Attempt %d: %d chars%s%s → %.2fs actual (%.1f%% %s target)\n    Text: %s\n",
				i+1, chars, deltaChars, deltaSec, h.ActualSec, drift, dir, h.Text)
		}

		// Trend analysis: estimate chars-per-second from all data points and
		// recommend a concrete target character count for this attempt.
		if len(history) >= 2 {
			// Use first and last attempt to estimate the chars→duration slope.
			firstChars := float64(len([]rune(history[0].Text)))
			lastChars := float64(len([]rune(history[len(history)-1].Text)))
			firstSec := history[0].ActualSec
			lastSec := history[len(history)-1].ActualSec
			secPerChar := 0.0
			if lastChars != firstChars {
				secPerChar = (lastSec - firstSec) / (lastChars - firstChars)
			}

			if secPerChar > 0 && lastSec > targetSec {
				// Over-run: need to reduce chars.
				needed := lastSec - targetSec
				reduceBy := int(math.Ceil(needed / secPerChar))
				recommended := int(lastChars) - reduceBy
				if recommended > 0 {
					historyBlock += fmt.Sprintf(
						"\n  Trend analysis: each char ≈ %.3fs of audio. "+
							"To hit %.1fs target, aim for ~%d chars (reduce by ~%d from last attempt).\n",
						secPerChar, targetSec, recommended, reduceBy)
				}
			} else if lastSec < targetSec {
				// Under-run: need to add chars.
				gap := targetSec - lastSec
				if secPerChar > 0 {
					// Positive slope: adding chars helps.
					addBy := int(math.Ceil(gap / secPerChar))
					recommended := int(lastChars) + addBy
					if recommended <= limit {
						historyBlock += fmt.Sprintf(
							"\n  Trend analysis: each char ≈ %.3fs of audio. "+
								"To hit %.1fs target, aim for ~%d chars (add ~%d chars from last attempt).\n",
							secPerChar, targetSec, recommended, addBy)
					} else {
						historyBlock += fmt.Sprintf(
							"\n  Trend analysis: char limit (%d) may prevent reaching %.1fs target. "+
								"Use deliberate pacing, connective phrases, or elaboration to fill time.\n",
							limit, targetSec)
					}
				} else if secPerChar <= 0 {
					// Negative or zero slope: adding chars is not helping — TTS may be truncating.
					// Advise using fewer, slower-paced sentences instead of packing more chars.
					historyBlock += fmt.Sprintf(
						"\n  Trend analysis: adding more characters is NOT increasing audio length "+
							"(slope=%.3f). The TTS model may be truncating. "+
							"Instead, use shorter sentences with pauses, slower phrasing, or "+
							"split into more distinct clauses to allow natural breathing room. "+
							"Target ~%d chars but with deliberate natural pauses in phrasing.\n",
						secPerChar, int(lastChars))
				}
			}
		}
		historyBlock += "[End of history]\n"
	}

	// Build a direction-aware instruction for the closing requirement #1.
	// Use proportional scaling from the current attempt.
	// Do NOT use aggressive LENGTHEN/SHORTEN language — it causes LLM to oscillate between
	// two extreme translations rather than converging to the target character count.
	var charTargetInstruction string
	if actualSec > targetSec {
		// Over-run: scale down proportionally.
		recommended := int(math.Round(float64(currentLen) * targetSec / actualSec))
		if recommended < 1 {
			recommended = 1
		}
		if recommended > limit {
			recommended = limit
		}
		charDelta := currentLen - recommended
		charTargetInstruction = fmt.Sprintf(
			"Remove approximately %d characters from the current translation "+
				"(reduce from %d to ~%d chars; hard ceiling: %d chars). "+
				"Shorten HOW things are said, NOT what is said — use more concise phrasing, "+
				"shorter synonyms, or drop filler words. Do NOT omit any key information from the source. "+
				"Do NOT rewrite the whole sentence.",
			charDelta, currentLen, recommended, limit)
	} else {
		// Under-run: scale up proportionally, capped at the (already-adjusted) limit.
		recommended := int(math.Round(float64(currentLen) * targetSec / actualSec))
		if recommended > limit {
			recommended = limit
		}
		charDelta := recommended - currentLen
		charTargetInstruction = fmt.Sprintf(
			"Add approximately %d characters to the current translation "+
				"(expand from %d to ~%d chars; hard ceiling: %d chars). "+
				"Elaborate on HOW things are said — use fuller phrasing, explicit connectives, "+
				"or restore nuance implied in the source. Do NOT invent new meaning. "+
				"Do NOT revert to any previous longer version.",
			charDelta, currentLen, recommended, limit)
	}

	systemPrompt := fmt.Sprintf(
		"You are a professional dubbing translator optimizing for audio-visual sync.\n\n"+
			"[Duration constraint]\n"+
			"Segment target duration: %.1f seconds.\n"+
			"Drift limit: %.0f%% — your translation must produce TTS audio within %.0f%% of target.\n"+
			"Target language: %s.\n"+
			"Hard character limit: %d characters (speech rate ~%.1f chars/sec).\n\n"+
			"[Current attempt feedback]\n"+
			"Previous translation (%d chars) produced audio of %.1fs, "+
			"which is %.0f%% %s the %.1fs target — exceeds %.0f%% limit.\n"+
			"This is attempt %d of %d.\n"+
			"%s"+
			"\nProvide a revised translation that:\n"+
			"1. %s\n"+
			"2. Faithfully conveys ALL key information from the source — do NOT omit, distort, or invent meaning.\n"+
			"3. Sounds natural when spoken aloud in %s.\n"+
			"4. Will produce audio as close to %.1fs as possible.\n\n"+
			"Respond with the revised translation only.",
		targetSec, driftThresholdPct*100, driftThresholdPct*100,
		targetLanguage, limit, rate,
		currentLen, actualSec, pctDiff, direction, targetSec, driftThresholdPct*100,
		attempt, maxAttempts,
		historyBlock,
		charTargetInstruction, targetLanguage, targetSec,
	)

	// Build episode-level and local context blocks to append to the system prompt.
	var contextSuffix strings.Builder
	if translationSummary != "" {
		contextSuffix.WriteString("\n\n[Episode reference — maintain consistency with this]\n")
		contextSuffix.WriteString(translationSummary)
		contextSuffix.WriteString("\n[End of episode reference]")
	}
	if len(contextBefore) > 0 || nextSrcText != "" {
		contextSuffix.WriteString("\n\n[Adjacent segments — for coherence and natural flow]\n")
		for i, seg := range contextBefore {
			label := fmt.Sprintf("-%d", len(contextBefore)-i)
			contextSuffix.WriteString(fmt.Sprintf("(%s) %s: %s\n     %s: %s\n", label, sourceLanguage, seg.SrcText, targetLanguage, seg.TgtText))
		}
		contextSuffix.WriteString(fmt.Sprintf("(→ current segment) %s: %s\n", sourceLanguage, srcText))
		if nextSrcText != "" {
			contextSuffix.WriteString(fmt.Sprintf("(+1) %s: %s  ← upcoming (for tone reference only, do NOT translate)\n", sourceLanguage, nextSrcText))
		}
		contextSuffix.WriteString("[End of adjacent segments]")
	}
	if contextSuffix.Len() > 0 {
		systemPrompt += contextSuffix.String()
	}

	requestPayload := chatCompletionRequest{
		Model:       model,
		Temperature: c.retranslationTemperature,
		Messages: []chatMessage{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: fmt.Sprintf(
				"Source (%s):\n%s\n\nCurrent translation (%s) — make minimal edits to THIS text, "+
					"do NOT re-translate from scratch, do NOT revert to any previous version, "+
					"and ensure the result faithfully conveys ALL key information from the source:\n%s",
				sourceLanguage, srcText, targetLanguage, currentTrans)},
		},
	}
	if useThinking {
		return c.doChatStream(ctx, OpRetranslateThinking, requestPayload)
	}
	return c.doChat(ctx, OpRetranslate, requestPayload)
}

func mockTranslate(targetLanguage, text string) string {
	if strings.TrimSpace(text) == "" {
		return ""
	}
	return fmt.Sprintf("[%s] %s", targetLanguage, text)
}

// chatMessage is one OpenAI-compatible chat message. The fields cover both
// the "plain prompt" path (Role + Content) and the function-calling path
// (assistant returning ToolCalls or our caller echoing back ToolCallID).
// All optional fields use omitempty so the wire payload stays minimal and
// byte-stable for prompt-prefix caching purposes (OPT-001).
type chatMessage struct {
	Role       string     `json:"role"`
	Content    string     `json:"content,omitempty"`
	ToolCalls  []toolCall `json:"tool_calls,omitempty"`
	ToolCallID string     `json:"tool_call_id,omitempty"`
}

// toolDef advertises a callable function to the model. Used by OPT-003 to
// turn ad-hoc "respond with JSON" prompts into strict-schema tool calls.
type toolDef struct {
	Type     string      `json:"type"` // always "function"
	Function functionDef `json:"function"`
}

type functionDef struct {
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	Parameters  json.RawMessage `json:"parameters"`
	Strict      bool            `json:"strict,omitempty"`
}

// toolCall is the assistant's response when it picks a tool. The actual
// arguments are a JSON-encoded string (NOT a nested object) per the
// OpenAI / DashScope wire contract — callers must json.Unmarshal them.
type toolCall struct {
	ID       string           `json:"id"`
	Type     string           `json:"type"`
	Function toolCallFunction `json:"function"`
}

type toolCallFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type chatCompletionRequest struct {
	Model          string            `json:"model"`
	Temperature    float64           `json:"temperature"`
	Messages       []chatMessage     `json:"messages"`
	Tools          []toolDef         `json:"tools,omitempty"`
	ToolChoice     any               `json:"tool_choice,omitempty"`
	ResponseFormat map[string]string `json:"response_format,omitempty"`
	Stream         bool              `json:"stream,omitempty"`
}

// forceToolChoice builds the OpenAI-compatible "force this exact function"
// directive used by OPT-003. Some providers also accept "required" (any of
// the declared tools); we always force the specific one to keep parsing
// deterministic.
func forceToolChoice(name string) any {
	return map[string]any{
		"type": "function",
		"function": map[string]string{
			"name": name,
		},
	}
}

// providerUsage carries provider-reported token counts. We accept THREE
// shapes because the OpenAI-compatible ecosystem has not converged:
//   - DeepSeek emits `usage.prompt_cache_hit_tokens`
//   - OpenAI new + DashScope (Qwen) emit `usage.prompt_tokens_details.cached_tokens`
//   - some legacy / alpha paths emit a top-level `usage.cached_tokens`
//
// effectiveCached() returns the max so whichever field is populated becomes
// the effective cached count. Add a new field here when a new provider lands.
type providerUsage struct {
	PromptTokens         int `json:"prompt_tokens"`
	CompletionTokens     int `json:"completion_tokens"`
	TotalTokens          int `json:"total_tokens"`
	CachedTokens         int `json:"cached_tokens"`
	PromptCacheHitTokens int `json:"prompt_cache_hit_tokens"`
	PromptTokensDetails  struct {
		CachedTokens int `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
}

func (u providerUsage) effectiveCached() int {
	return maxInt(maxInt(u.CachedTokens, u.PromptCacheHitTokens), u.PromptTokensDetails.CachedTokens)
}

type chatCompletionResponse struct {
	Choices []struct {
		Message struct {
			Content   string     `json:"content"`
			ToolCalls []toolCall `json:"tool_calls,omitempty"`
		} `json:"message"`
		FinishReason string `json:"finish_reason,omitempty"`
	} `json:"choices"`
	Usage providerUsage `json:"usage"`
}

// usageStats is the normalised token accounting returned by doChatOnce /
// doChatStream so doChat can emit a single observability.ObserveLLMTokens
// call per request, regardless of which upstream cache field was populated.
type usageStats struct {
	PromptTokens     int
	CompletionTokens int
	CachedTokens     int // max(Usage.CachedTokens, Usage.PromptCacheHitTokens)
}

// LLM operation labels used by metrics. Keep this list small and stable;
// Prometheus cardinality grows linearly with len(operations) * len(models).
// Adding a new value requires updating the corresponding doc and rules.
const (
	OpTranslate           = "translate"
	OpRetranslate         = "retranslate"
	OpRetranslateThinking = "retranslate_thinking"
	OpSummary             = "summary"
	OpReview              = "review"
	OpJudge               = "judge"          // OPT-002
	OpGlossary            = "glossary"       // OPT-402 episode glossary extraction
	OpChapterReview       = "chapter_review" // OPT-403 chapter cut review + bilingual title
)

func (c *Client) translateViaOpenAI(ctx context.Context, sourceLanguage, targetLanguage, text string) (string, error) {
	if c.baseURL == "" || c.apiKey == "" || c.model == "" {
		return "", errors.New("OPENAI_BASE_URL, OPENAI_API_KEY and OPENAI_MODEL are required for openai_compatible provider")
	}

	requestPayload := chatCompletionRequest{
		Model:       c.model,
		Temperature: c.temperature,
		Messages: []chatMessage{
			{
				Role:    "system",
				Content: "You translate subtitle segments for dubbing. Keep the meaning, stay natural, keep length close to the source, and respond with plain text only.",
			},
			{
				Role:    "user",
				Content: fmt.Sprintf("Source language: %s\nTarget language: %s\nText: %s", sourceLanguage, targetLanguage, text),
			},
		},
	}

	return c.doChat(ctx, OpTranslate, requestPayload)
}

func (c *Client) translateWithDurationViaOpenAI(ctx context.Context, sourceLanguage, targetLanguage, text string, targetSec float64, charsPerSecHint float64, contextBefore []ContextSegment, translationSummary string) (string, error) {
	if c.baseURL == "" || c.apiKey == "" || c.model == "" {
		return "", errors.New("OPENAI_BASE_URL, OPENAI_API_KEY and OPENAI_MODEL are required for openai_compatible provider")
	}

	rate := charsPerSec(targetLanguage)
	if charsPerSecHint > 0 {
		rate = charsPerSecHint
	}
	limit := int(math.Ceil(rate * targetSec))
	if limit < 1 {
		limit = 1
	}

	systemPrompt := buildTranslateSystemPrompt(targetLanguage, rate, translationSummary)

	// OPT-001-followup-1: per-segment constraints (segment duration, char
	// limit) MUST live in the user message, never in system. The provider
	// prefix cache only matches a byte-identical prefix; if duration changes
	// per segment and lives in system, the cache misses every call. Placing
	// it at the head of the user message keeps system byte-stable per job.
	var userMsg strings.Builder
	userMsg.WriteString("[Per-segment constraints]\n")
	userMsg.WriteString(fmt.Sprintf("Segment duration: %.1f seconds.\n", targetSec))
	userMsg.WriteString(fmt.Sprintf("Hard character limit: %d characters.\n\n", limit))

	// Inject preceding translated segments for local consistency.
	// contextBefore also varies per segment, so it stays in user message.
	if len(contextBefore) > 0 {
		userMsg.WriteString("[Preceding segments — for terminology and style reference]\n")
		for i, seg := range contextBefore {
			label := fmt.Sprintf("-%d", len(contextBefore)-i)
			userMsg.WriteString(fmt.Sprintf("(%s) %s: %s\n     %s: %s\n", label, sourceLanguage, seg.SrcText, targetLanguage, seg.TgtText))
		}
		userMsg.WriteString("\n[Segment to translate now]\n")
	}
	userMsg.WriteString(fmt.Sprintf("Source language: %s\nText: %s", sourceLanguage, text))

	requestPayload := chatCompletionRequest{
		Model:       c.model,
		Temperature: c.temperature,
		Messages: []chatMessage{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: userMsg.String()},
		},
	}

	return c.doChat(ctx, OpTranslate, requestPayload)
}

// buildTranslateSystemPrompt is a pure function that assembles the system
// prompt for translateWithDurationViaOpenAI. Extracted for OPT-001 so
// (1) the byte-stable invariant can be unit-tested directly, and
// (2) downstream call sites (judge, ensemble, retranslate) can share the
// same template piece-by-piece without breaking prefix cache reuse.
//
// OPT-001-followup-1: signature deliberately accepts ONLY per-job
// constants (targetLanguage, rate, translationSummary). Per-segment values
// like duration/char-limit MUST be passed via the user message — putting
// them here would change the prompt byte-by-byte for every segment and
// destroy the provider's prefix cache (observed empirically on DashScope
// qwen-turbo: 0% cache hits on the 10min baseline before this fix).
//
// Within a single job, all per-segment calls receive identical
// (targetLanguage, rate, translationSummary) — this guarantees the
// returned prompt is byte-stable, which is exactly what the provider's
// prefix cache requires to hit.
func buildTranslateSystemPrompt(targetLanguage string, rate float64, translationSummary string) string {
	systemPrompt := fmt.Sprintf(
		"You are a professional dubbing translator. Translate the given source segment accurately and concisely.\n\n"+
			"[Constraints]\n"+
			"Target language: %s.\n"+
			"Speech rate guideline: ~%.1f characters per second (used to derive a per-segment hard character limit, supplied with each segment).\n\n"+
			"[Rules — follow strictly]\n"+
			"1. VERBATIM FIDELITY: Translate only what the speaker actually says. "+
			"Do NOT add explanations, elaborations, summaries, or context that are not in the source. "+
			"If the speaker is incomplete or informal, reflect that — do not 'complete' their thought.\n"+
			"2. LENGTH: Stay within the per-segment hard character limit supplied in the user message. Aim for natural spoken length — "+
			"do NOT pad with filler phrases to fill the time slot.\n"+
			"3. PROPER NOUNS & TECHNICAL TERMS: Do NOT translate algorithm names, protocol names, "+
			"product names, or other proper nouns (e.g. 'Raft' stays 'Raft', 'MapReduce' stays 'MapReduce'). "+
			"Use established translations for standard technical terms where they exist.\n"+
			"4. CONSISTENCY: Use the same translation for the same term throughout. "+
			"Follow any glossary or style guidance provided below.\n"+
			"5. REGISTER: Match the speaker's tone and register (academic lecture → formal academic style).\n"+
			"6. OUTPUT: Respond with the translation only. No explanations, no alternatives, no notes.",
		targetLanguage, rate,
	)

	if translationSummary != "" {
		systemPrompt += "\n\n[Episode reference — use this glossary and style guide]\n" + translationSummary + "\n[End of episode reference]"
	}
	return systemPrompt
}

// llmRetryConfig is the default policy for LLM chat calls. Translation
// providers (DeepSeek, Qwen, Kimi) frequently return 429 / 502 under load —
// budget for that without giving up too quickly.
var llmRetryConfig = httpx.RetryConfig{
	MaxAttempts:    4,
	BaseBackoff:    500 * time.Millisecond,
	MaxBackoff:     4 * time.Second,
	JitterFraction: 0.25,
}

func classifyResult(err error) string {
	if err == nil {
		return "ok"
	}
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return "cancelled"
	}
	if httpx.IsRetryable(err) {
		return "retryable"
	}
	return "permanent"
}

// doChat sends a chat completion request and returns the first choice's
// text. Retries are applied automatically for transient failures (429/5xx,
// network errors). Permanent errors (e.g. 400 invalid request) abort
// immediately.
//
// operation is the metric label (translate / review / summary / ...). It is
// also used in observability spans / logs to attribute cost to a use-case.
// Use the Op* constants — never pass a literal string at the call site.
func (c *Client) doChat(ctx context.Context, operation string, payload chatCompletionRequest) (string, error) {
	started := time.Now()
	var content string
	var usage usageStats
	err := httpx.Do(ctx, llmRetryConfig, func(ctx context.Context, attempt int) error {
		var inner error
		content, usage, inner = c.doChatOnce(ctx, payload)
		return inner
	})
	observability.ObserveExternalCall("llm", operation, classifyResult(err), time.Since(started))
	if err == nil {
		// Only attribute tokens for successful calls — retried-then-failed
		// attempts are already counted as "retryable" / "permanent" via
		// ObserveExternalCall and we don't want to double-charge cost.
		observability.ObserveLLMTokens(payload.Model, operation,
			usage.PromptTokens, usage.CompletionTokens, usage.CachedTokens)
	}
	return content, err
}

// doChatTool sends a chat completion request that REQUIRES the model to call
// a specific function, then returns the function-call arguments JSON string.
//
// Returns "" without error if the model returned a content message instead
// of a tool call (some providers may bypass tool_choice under load) — the
// caller can then either retry or fall back to the prompt path.
//
// Token / latency observability is handled the same way as doChat.
func (c *Client) doChatTool(ctx context.Context, operation string, payload chatCompletionRequest, expectedToolName string) (string, error) {
	started := time.Now()
	var args string
	var usage usageStats
	err := httpx.Do(ctx, llmRetryConfig, func(ctx context.Context, attempt int) error {
		var inner error
		args, usage, inner = c.doChatToolOnce(ctx, payload, expectedToolName)
		return inner
	})
	observability.ObserveExternalCall("llm", operation, classifyResult(err), time.Since(started))
	if err == nil {
		observability.ObserveLLMTokens(payload.Model, operation,
			usage.PromptTokens, usage.CompletionTokens, usage.CachedTokens)
	}
	return args, err
}

func (c *Client) doChatToolOnce(ctx context.Context, payload chatCompletionRequest, expectedToolName string) (string, usageStats, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return "", usageStats{}, fmt.Errorf("marshal request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", usageStats{}, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", usageStats{}, httpx.Wrap("llm", "chat.completions", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= http.StatusBadRequest {
		raw, _ := io.ReadAll(resp.Body)
		return "", usageStats{}, httpx.FromHTTPStatus("llm", "chat.completions", resp.StatusCode, raw)
	}

	var result chatCompletionResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", usageStats{}, fmt.Errorf("decode tool response: %w", err)
	}
	if len(result.Choices) == 0 {
		return "", usageStats{}, errors.New("provider returned no choices")
	}
	usage := usageStats{
		PromptTokens:     result.Usage.PromptTokens,
		CompletionTokens: result.Usage.CompletionTokens,
		CachedTokens:     result.Usage.effectiveCached(),
	}

	// Pick the first matching tool call. Strict tool_choice should yield
	// exactly one, but be defensive: ignore non-matching names so a
	// provider that hallucinates a different tool name doesn't crash us.
	for _, tc := range result.Choices[0].Message.ToolCalls {
		if tc.Function.Name == expectedToolName {
			return tc.Function.Arguments, usage, nil
		}
	}
	// No matching tool call — return empty args, caller decides what to do.
	return "", usage, nil
}

func (c *Client) doChatOnce(ctx context.Context, payload chatCompletionRequest) (string, usageStats, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return "", usageStats{}, fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", usageStats{}, fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", usageStats{}, httpx.Wrap("llm", "chat.completions", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= http.StatusBadRequest {
		raw, _ := io.ReadAll(resp.Body)
		return "", usageStats{}, httpx.FromHTTPStatus("llm", "chat.completions", resp.StatusCode, raw)
	}

	var result chatCompletionResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", usageStats{}, fmt.Errorf("decode translation response: %w", err)
	}
	if len(result.Choices) == 0 {
		return "", usageStats{}, errors.New("translation provider returned no choices")
	}
	usage := usageStats{
		PromptTokens:     result.Usage.PromptTokens,
		CompletionTokens: result.Usage.CompletionTokens,
		CachedTokens:     result.Usage.effectiveCached(),
	}
	return strings.TrimSpace(result.Choices[0].Message.Content), usage, nil
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// doChatStream sends a chat completion request with stream=true and assembles the
// full response by consuming the Server-Sent Events stream.  It collects only
// the "content" delta chunks and silently discards "reasoning_content" chunks
// (Kimi thinking tokens).  This is required for DashScope thinking models
// (e.g. kimi-k2-thinking) which reject non-streaming calls with enable_thinking.
//
// operation is the metric label, used the same way as in doChat. Many
// streaming providers ALSO emit a final SSE chunk containing usage stats
// (DashScope and OpenAI both do); we capture it for ObserveLLMTokens so
// thinking-mode retranslations are not invisible in the cost dashboard.
func (c *Client) doChatStream(ctx context.Context, operation string, payload chatCompletionRequest) (string, error) {
	started := time.Now()
	payload.Stream = true

	body, err := json.Marshal(payload)
	if err != nil {
		observability.ObserveExternalCall("llm", operation, classifyResult(err), time.Since(started))
		return "", fmt.Errorf("marshal stream request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		observability.ObserveExternalCall("llm", operation, classifyResult(err), time.Since(started))
		return "", fmt.Errorf("build stream request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")

	resp, err := c.thinkingHTTPClient.Do(req)
	if err != nil {
		wrapped := httpx.Wrap("llm", operation, err)
		observability.ObserveExternalCall("llm", operation, classifyResult(wrapped), time.Since(started))
		return "", wrapped
	}
	defer resp.Body.Close()

	if resp.StatusCode >= http.StatusBadRequest {
		raw, _ := io.ReadAll(resp.Body)
		statusErr := httpx.FromHTTPStatus("llm", operation, resp.StatusCode, raw)
		observability.ObserveExternalCall("llm", operation, classifyResult(statusErr), time.Since(started))
		return "", statusErr
	}

	// Parse SSE: each line is either "data: {json}" or "data: [DONE]".
	// Each JSON chunk has choices[0].delta which may contain "content" or
	// "reasoning_content".  We collect only "content".  The final chunk often
	// contains a top-level "usage" field with token counts.
	var sb strings.Builder
	var usage usageStats
	scanner := bufio.NewScanner(resp.Body)
	// Allow large lines: thinking models can emit multi-KB usage chunks.
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}
		data := strings.TrimPrefix(line, "data: ")
		if data == "[DONE]" {
			break
		}
		var chunk struct {
			Choices []struct {
				Delta struct {
					Content          string `json:"content"`
					ReasoningContent string `json:"reasoning_content"`
				} `json:"delta"`
			} `json:"choices"`
			Usage *providerUsage `json:"usage,omitempty"`
		}
		if err := json.Unmarshal([]byte(data), &chunk); err != nil {
			continue
		}
		if len(chunk.Choices) > 0 {
			sb.WriteString(chunk.Choices[0].Delta.Content)
		}
		if chunk.Usage != nil {
			// Final chunks may carry usage; keep the latest seen.
			usage = usageStats{
				PromptTokens:     chunk.Usage.PromptTokens,
				CompletionTokens: chunk.Usage.CompletionTokens,
				CachedTokens:     chunk.Usage.effectiveCached(),
			}
		}
	}
	if err := scanner.Err(); err != nil {
		observability.ObserveExternalCall("llm", operation, classifyResult(err), time.Since(started))
		return "", fmt.Errorf("read stream: %w", err)
	}
	result := strings.TrimSpace(sb.String())
	if result == "" {
		emptyErr := errors.New("thinking provider returned empty content")
		observability.ObserveExternalCall("llm", operation, classifyResult(emptyErr), time.Since(started))
		return "", emptyErr
	}
	observability.ObserveExternalCall("llm", operation, "ok", time.Since(started))
	observability.ObserveLLMTokens(payload.Model, operation,
		usage.PromptTokens, usage.CompletionTokens, usage.CachedTokens)
	return result, nil
}

// ── Segmentation review ────────────────────────────────────────────────────────

// SegmentInfo is a compact representation of one ASR segment sent to the
// LLM segmentation-review agent.
type SegmentInfo struct {
	Ordinal     int
	Text        string
	StartMs     int64
	EndMs       int64
	GapAfterMs  int64 // gap to the next segment (0 for the last segment)
	SplitReason string
}

// SegmentReviewSuggestion is one merge recommendation returned by ReviewSegmentation.
// Only "merge" actions are produced; splits require word-level timestamps and are
// performed manually through the UI.
type SegmentReviewSuggestion struct {
	Action     string // always "merge"
	SegmentIDs []uint // IDs of the segments to merge (must be consecutive)
	Reason     string
	Confidence float64 // 0–1
}

// reviewRawSuggestion is the JSON wire shape returned by the LLM both via
// the prompt path AND via the tool-call function arguments. Centralising the
// type lets parseReviewSuggestions() consume both routes identically.
type reviewRawSuggestion struct {
	Ordinals   []int   `json:"ordinals"`
	Reason     string  `json:"reason"`
	Confidence float64 `json:"confidence"`
}

// reviewToolArgs is the argument schema for the emit_segment_suggestions
// tool. Wrapping the array in an object is required by OpenAI / DashScope
// strict-mode tool calling — top-level arrays are not allowed.
type reviewToolArgs struct {
	Suggestions []reviewRawSuggestion `json:"suggestions"`
}

// reviewToolSchema is the JSON Schema sent to the LLM. Marshalled once at
// init() to fail loudly on a typo rather than at first request.
var reviewToolSchema = mustMarshalJSON(map[string]any{
	"type": "object",
	"properties": map[string]any{
		"suggestions": map[string]any{
			"type": "array",
			"items": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"ordinals": map[string]any{
						"type":  "array",
						"items": map[string]any{"type": "integer"},
					},
					"reason":     map[string]any{"type": "string"},
					"confidence": map[string]any{"type": "number"},
				},
				"required":             []string{"ordinals", "reason", "confidence"},
				"additionalProperties": false,
			},
		},
	},
	"required":             []string{"suggestions"},
	"additionalProperties": false,
})

func mustMarshalJSON(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(fmt.Sprintf("static JSON schema marshal failed: %v", err))
	}
	return b
}

// reviewSystemPrompt is the system prompt for the segment_review LLM. It is
// the SAME content for both the prompt-only path and the tool-call path —
// the tool path simply skips the "Respond with a JSON array … return ONLY
// the JSON array" closing instruction, since strict tool calling enforces
// the shape at the protocol level. Returning it from a single function keeps
// the system prompt cache-stable across the two routes (OPT-001 friendly).
func reviewSystemPrompt(sourceLanguage string, toolMode bool) string {
	base := fmt.Sprintf(
		"You are an expert ASR post-processing agent for a video dubbing pipeline.\n"+
			"Source language: %s.\n\n"+
			"You will receive a list of ASR segments.  Each segment has:\n"+
			"  ordinal   – 0-based index (used to identify segments)\n"+
			"  text      – transcribed speech\n"+
			"  duration_ms – segment length in milliseconds\n"+
			"  gap_after_ms – silence gap to the next segment\n"+
			"  split_reason – why the ASR engine created this boundary\n\n"+
			"Your task: identify adjacent segments that should be MERGED because they form\n"+
			"a single natural utterance that was incorrectly split. Common cases:\n"+
			"  - A sentence was split mid-clause due to a brief pause\n"+
			"  - A conjunction or subordinator (but, and, so, because, …) starts the\n"+
			"    following segment, making the split feel unnatural\n"+
			"  - Two very short segments (<1 s each) that together form one coherent phrase\n\n"+
			"Do NOT suggest merges when:\n"+
			"  - There is a clear topic or sentence boundary\n"+
			"  - The gap_after_ms is large (>2000 ms) indicating a deliberate pause\n"+
			"  - The merged segment would exceed ~20 seconds\n\n",
		sourceLanguage,
	)
	if toolMode {
		// Strict tool call path: structure enforced by schema, just describe semantics.
		return base + "Call the emit_segment_suggestions function with your suggestions. " +
			"Return an empty suggestions array if there are no problems. " +
			"Only adjacent segments (consecutive ordinals) may be merged."
	}
	// Legacy prompt path: explicit JSON shape + format directives.
	return base +
		"Respond with a JSON array of suggestions.  Each element:\n" +
		"  { \"ordinals\": [<ordinal_a>, <ordinal_b>], \"reason\": \"<short reason>\", \"confidence\": <0.0-1.0> }\n" +
		"Only adjacent segments may be merged (consecutive ordinals).\n" +
		"If there are no problems, return an empty array: []\n" +
		"Return ONLY the JSON array, no other text."
}

// ReviewSegmentation analyses the ASR-produced segments and returns a list of
// merge suggestions.  It returns an empty slice (no error) when running in
// mock/offline mode or when the LLM finds no problems.
//
// OPT-003: when c.segmentReviewUseTools is true, the call goes through a
// strict function-calling path with a JSON Schema. If the tool call returns
// nothing parseable the call automatically falls back to the legacy
// "respond with a JSON array" prompt — both modes are kept in tree until the
// tool path proves stable across providers.
func (c *Client) ReviewSegmentation(
	ctx context.Context,
	sourceLanguage string,
	segments []SegmentInfo,
) ([]SegmentReviewSuggestion, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return nil, nil // offline / mock mode — no suggestions
	}
	if len(segments) == 0 {
		return nil, nil
	}

	if c.segmentReviewUseTools {
		suggestions, err := c.reviewSegmentationViaTools(ctx, sourceLanguage, segments)
		if err == nil {
			return suggestions, nil
		}
		// Tool path failed (network / unparseable / provider rejected tools).
		// Bump the strict-parse failed counter so a sustained regression is
		// visible on a dashboard, then fall through to the legacy prompt path.
		observability.IncLLMStrictParseFailed(OpReview)
		// Note: error is silently absorbed; ml-service / pipeline already
		// treats segment_review failures as non-fatal (see runSegmentReview).
	}
	return c.reviewSegmentationViaPrompt(ctx, sourceLanguage, segments)
}

func (c *Client) reviewSegmentationViaTools(
	ctx context.Context,
	sourceLanguage string,
	segments []SegmentInfo,
) ([]SegmentReviewSuggestion, error) {
	segJSON2, err := marshalReviewSegments(segments)
	if err != nil {
		return nil, err
	}

	model := c.reviewModel()
	payload := chatCompletionRequest{
		Model:       model,
		Temperature: 0.2,
		Messages: []chatMessage{
			{Role: "system", Content: reviewSystemPrompt(sourceLanguage, true)},
			{Role: "user", Content: string(segJSON2)},
		},
		Tools: []toolDef{{
			Type: "function",
			Function: functionDef{
				Name:        "emit_segment_suggestions",
				Description: "Submit zero or more merge suggestions for adjacent ASR segments that were incorrectly split.",
				Parameters:  reviewToolSchema,
			},
		}},
		ToolChoice: forceToolChoice("emit_segment_suggestions"),
	}

	args, err := c.doChatTool(ctx, OpReview, payload, "emit_segment_suggestions")
	if err != nil {
		return nil, err
	}
	if args == "" {
		return nil, errors.New("tool call returned empty arguments")
	}

	var parsed reviewToolArgs
	if err := json.Unmarshal([]byte(args), &parsed); err != nil {
		return nil, fmt.Errorf("parse tool arguments: %w (raw: %.200s)", err, args)
	}
	return finaliseReviewSuggestions(parsed.Suggestions, segments), nil
}

func (c *Client) reviewSegmentationViaPrompt(
	ctx context.Context,
	sourceLanguage string,
	segments []SegmentInfo,
) ([]SegmentReviewSuggestion, error) {
	segJSON2, err := marshalReviewSegments(segments)
	if err != nil {
		return nil, err
	}

	model := c.reviewModel()
	payload := chatCompletionRequest{
		Model:       model,
		Temperature: 0.2,
		Messages: []chatMessage{
			{Role: "system", Content: reviewSystemPrompt(sourceLanguage, false)},
			{Role: "user", Content: string(segJSON2)},
		},
	}

	raw, err := c.doChat(ctx, OpReview, payload)
	if err != nil {
		return nil, fmt.Errorf("segment review LLM call: %w", err)
	}

	// Strip markdown code fences if the model wraps the JSON
	raw = strings.TrimSpace(raw)
	if strings.HasPrefix(raw, "```") {
		lines := strings.SplitN(raw, "\n", 2)
		if len(lines) == 2 {
			raw = lines[1]
		}
		raw = strings.TrimSuffix(strings.TrimSpace(raw), "```")
	}

	var rawItems []reviewRawSuggestion
	if err := json.Unmarshal([]byte(raw), &rawItems); err != nil {
		return nil, fmt.Errorf("parse segment review response: %w (raw: %.200s)", err, raw)
	}
	return finaliseReviewSuggestions(rawItems, segments), nil
}

func (c *Client) reviewModel() string {
	model := c.segmentReviewModel
	if model == "" {
		model = c.retranslationModel
	}
	if model == "" {
		model = c.model
	}
	return model
}

func marshalReviewSegments(segments []SegmentInfo) ([]byte, error) {
	type segJSON struct {
		Ordinal     int    `json:"ordinal"`
		ID          int    `json:"id"` // placeholder — actual DB IDs added by caller
		Text        string `json:"text"`
		DurationMs  int64  `json:"duration_ms"`
		GapAfterMs  int64  `json:"gap_after_ms"`
		SplitReason string `json:"split_reason"`
	}
	segList := make([]segJSON, len(segments))
	for i, s := range segments {
		segList[i] = segJSON{
			Ordinal:     s.Ordinal,
			ID:          s.Ordinal,
			Text:        s.Text,
			DurationMs:  s.EndMs - s.StartMs,
			GapAfterMs:  s.GapAfterMs,
			SplitReason: s.SplitReason,
		}
	}
	return json.Marshal(segList)
}

func finaliseReviewSuggestions(rawItems []reviewRawSuggestion, segments []SegmentInfo) []SegmentReviewSuggestion {
	ordinalToID := make(map[int]uint, len(segments))
	for _, s := range segments {
		ordinalToID[s.Ordinal] = uint(s.Ordinal) // pipeline caller resolves real DB IDs
	}
	suggestions := make([]SegmentReviewSuggestion, 0, len(rawItems))
	for _, item := range rawItems {
		if len(item.Ordinals) < 2 {
			continue
		}
		ids := make([]uint, len(item.Ordinals))
		for i, ord := range item.Ordinals {
			ids[i] = ordinalToID[ord]
		}
		suggestions = append(suggestions, SegmentReviewSuggestion{
			Action:     "merge",
			SegmentIDs: ids,
			Reason:     item.Reason,
			Confidence: item.Confidence,
		})
	}
	return suggestions
}
