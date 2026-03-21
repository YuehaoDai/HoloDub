package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strings"

	"holodub/internal/config"
)

type Client struct {
	provider           string
	baseURL            string
	apiKey             string
	model              string
	temperature        float64
	retranslationModel string
	httpClient         *http.Client
}

func New(cfg config.Config) *Client {
	return &Client{
		provider:           strings.ToLower(cfg.TranslationProvider),
		baseURL:            strings.TrimRight(cfg.OpenAIBaseURL, "/"),
		apiKey:             cfg.OpenAIAPIKey,
		model:              cfg.OpenAIModel,
		temperature:        cfg.OpenAITemperature,
		retranslationModel: cfg.RetranslationModel,
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
func (c *Client) TranslateTextWithDuration(ctx context.Context, sourceLanguage, targetLanguage, text string, targetSec float64) (string, error) {
	switch c.provider {
	case "", "mock":
		return mockTranslate(targetLanguage, text), nil
	case "openai_compatible", "openai-compatible":
		return c.translateWithDurationViaOpenAI(ctx, sourceLanguage, targetLanguage, text, targetSec)
	default:
		return "", fmt.Errorf("unsupported translation provider %q", c.provider)
	}
}

// RetranslationAttempt records one attempt: text tried and the TTS duration it produced.
type RetranslationAttempt struct {
	Text      string  // translation text used
	ActualSec float64 // TTS output duration in seconds
}

// RetranslateWithConstraint re-translates using the configured retranslation model
// with drift-rate feedback. history contains all previous attempts (text, actualSec).
// driftThresholdPct is the max allowed drift (e.g. 0.06 = 6%).
func (c *Client) RetranslateWithConstraint(
	ctx context.Context,
	sourceLanguage, targetLanguage, srcText, currentTrans string,
	targetSec, actualSec float64,
	attempt, maxAttempts int,
	driftThresholdPct float64,
	history []RetranslationAttempt,
) (string, error) {
	if c.baseURL == "" || c.apiKey == "" {
		return "", errors.New("OPENAI_BASE_URL and OPENAI_API_KEY are required for retranslation")
	}
	model := c.retranslationModel
	if model == "" {
		model = c.model
	}
	limit := maxChars(targetLanguage, targetSec)
	rate := charsPerSec(targetLanguage)
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
		observedRate := totalChars / totalSec
		observedCeiling := int(math.Ceil(targetSec * observedRate))
		if observedCeiling > limit {
			limit = observedCeiling
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

	requestPayload := chatCompletionRequest{
		Model:       model,
		Temperature: c.temperature,
		Messages: []map[string]string{
			{"role": "system", "content": systemPrompt},
			{"role": "user", "content": fmt.Sprintf(
				"Source (%s):\n%s\n\nCurrent translation (%s) — make minimal edits to THIS text, "+
					"do NOT re-translate from scratch, do NOT revert to any previous version, "+
					"and ensure the result faithfully conveys ALL key information from the source:\n%s",
				sourceLanguage, srcText, targetLanguage, currentTrans)},
		},
	}
	return c.doChat(ctx, requestPayload)
}

func mockTranslate(targetLanguage, text string) string {
	if strings.TrimSpace(text) == "" {
		return ""
	}
	return fmt.Sprintf("[%s] %s", targetLanguage, text)
}

type chatCompletionRequest struct {
	Model       string                   `json:"model"`
	Temperature float64                  `json:"temperature"`
	Messages    []map[string]string      `json:"messages"`
	ResponseFormat map[string]string     `json:"response_format,omitempty"`
}

type chatCompletionResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage struct {
		PromptTokens     int `json:"prompt_tokens"`
		CompletionTokens int `json:"completion_tokens"`
		TotalTokens      int `json:"total_tokens"`
	} `json:"usage"`
}

func (c *Client) translateViaOpenAI(ctx context.Context, sourceLanguage, targetLanguage, text string) (string, error) {
	if c.baseURL == "" || c.apiKey == "" || c.model == "" {
		return "", errors.New("OPENAI_BASE_URL, OPENAI_API_KEY and OPENAI_MODEL are required for openai_compatible provider")
	}

	requestPayload := chatCompletionRequest{
		Model:       c.model,
		Temperature: c.temperature,
		Messages: []map[string]string{
			{
				"role":    "system",
				"content": "You translate subtitle segments for dubbing. Keep the meaning, stay natural, keep length close to the source, and respond with plain text only.",
			},
			{
				"role":    "user",
				"content": fmt.Sprintf("Source language: %s\nTarget language: %s\nText: %s", sourceLanguage, targetLanguage, text),
			},
		},
	}

	return c.doChat(ctx, requestPayload)
}

func (c *Client) translateWithDurationViaOpenAI(ctx context.Context, sourceLanguage, targetLanguage, text string, targetSec float64) (string, error) {
	if c.baseURL == "" || c.apiKey == "" || c.model == "" {
		return "", errors.New("OPENAI_BASE_URL, OPENAI_API_KEY and OPENAI_MODEL are required for openai_compatible provider")
	}

	limit := maxChars(targetLanguage, targetSec)
	rate := charsPerSec(targetLanguage)

	systemPrompt := fmt.Sprintf(
		"You translate subtitle segments for dubbing.\n\n"+
			"Segment duration: %.1f seconds.\n"+
			"Target language: %s.\n"+
			"Maximum characters allowed: %d (speech rate ~%.1f chars/sec).\n\n"+
			"Rules:\n"+
			"1. Stay within %d characters — hard limit.\n"+
			"2. Keep meaning accurate and natural.\n"+
			"3. If meaning must be shortened to fit, prioritize key information.\n"+
			"4. Respond with the translation only, no explanations.",
		targetSec, targetLanguage, limit, rate, limit,
	)

	requestPayload := chatCompletionRequest{
		Model:       c.model,
		Temperature: c.temperature,
		Messages: []map[string]string{
			{"role": "system", "content": systemPrompt},
			{"role": "user", "content": fmt.Sprintf("Source language: %s\nText: %s", sourceLanguage, text)},
		},
	}

	return c.doChat(ctx, requestPayload)
}

// doChat sends a chat completion request and returns the first choice's text.
func (c *Client) doChat(ctx context.Context, payload chatCompletionRequest) (string, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("marshal request: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("call translation provider: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode >= http.StatusBadRequest {
		return "", fmt.Errorf("translation provider returned status %d", resp.StatusCode)
	}

	var result chatCompletionResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("decode translation response: %w", err)
	}
	if len(result.Choices) == 0 {
		return "", errors.New("translation provider returned no choices")
	}
	return strings.TrimSpace(result.Choices[0].Message.Content), nil
}
