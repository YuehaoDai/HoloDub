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

// RetranslateWithConstraint re-translates using the configured retranslation model
// (e.g. kimi-k2.5) with a strict character limit derived from targetSec.
// actualSec is the duration the previous TTS attempt produced.
// attempt and maxAttempts are shown to the model so it understands urgency.
func (c *Client) RetranslateWithConstraint(
	ctx context.Context,
	sourceLanguage, targetLanguage, srcText, currentTrans string,
	targetSec, actualSec float64,
	attempt, maxAttempts int,
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

	systemPrompt := fmt.Sprintf(
		"You are a professional dubbing translator optimizing for audio-visual sync.\n\n"+
			"Segment duration: %.1f seconds.\n"+
			"Target language: %s.\n"+
			"Hard character limit: %d characters (speech rate ~%.1f chars/sec).\n\n"+
			"Previous translation (%d chars) produced audio of %.1fs, "+
			"which is %.0f%% %s the %.1fs target.\n"+
			"This is attempt %d of %d.\n\n"+
			"Provide a revised translation that:\n"+
			"1. Does NOT exceed %d characters — strictly enforced.\n"+
			"2. Maintains the core meaning of the original.\n"+
			"3. Sounds natural when spoken aloud.\n\n"+
			"Respond with the revised translation only.",
		targetSec, targetLanguage, limit, rate,
		currentLen, actualSec, pctDiff, direction, targetSec,
		attempt, maxAttempts, limit,
	)

	requestPayload := chatCompletionRequest{
		Model:       model,
		Temperature: c.temperature,
		Messages: []map[string]string{
			{"role": "system", "content": systemPrompt},
			{"role": "user", "content": fmt.Sprintf("Source (%s): %s\nCurrent translation: %s", sourceLanguage, srcText, currentTrans)},
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
