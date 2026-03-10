package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"holodub/internal/config"
)

type Client struct {
	provider    string
	baseURL     string
	apiKey      string
	model       string
	temperature float64
	httpClient  *http.Client
}

func New(cfg config.Config) *Client {
	return &Client{
		provider:    strings.ToLower(cfg.TranslationProvider),
		baseURL:     strings.TrimRight(cfg.OpenAIBaseURL, "/"),
		apiKey:      cfg.OpenAIAPIKey,
		model:       cfg.OpenAIModel,
		temperature: cfg.OpenAITemperature,
		httpClient: &http.Client{
			Timeout: cfg.OpenAITimeout,
		},
	}
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
				"role": "system",
				"content": "You translate subtitle segments for dubbing. Keep the meaning, stay natural, keep length close to the source, and respond with plain text only.",
			},
			{
				"role": "user",
				"content": fmt.Sprintf("Source language: %s\nTarget language: %s\nText: %s", sourceLanguage, targetLanguage, text),
			},
		},
	}

	body, err := json.Marshal(requestPayload)
	if err != nil {
		return "", fmt.Errorf("marshal request: %w", err)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return "", fmt.Errorf("build request: %w", err)
	}
	request.Header.Set("Authorization", "Bearer "+c.apiKey)
	request.Header.Set("Content-Type", "application/json")

	response, err := c.httpClient.Do(request)
	if err != nil {
		return "", fmt.Errorf("call translation provider: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode >= http.StatusBadRequest {
		return "", fmt.Errorf("translation provider returned status %d", response.StatusCode)
	}

	var payload chatCompletionResponse
	if err := json.NewDecoder(response.Body).Decode(&payload); err != nil {
		return "", fmt.Errorf("decode translation response: %w", err)
	}
	if len(payload.Choices) == 0 {
		return "", errors.New("translation provider returned no choices")
	}
	return strings.TrimSpace(payload.Choices[0].Message.Content), nil
}
