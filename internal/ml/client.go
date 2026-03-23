package ml

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	baseURL    string
	httpClient *http.Client
}

func New(baseURL string) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: 30 * time.Minute,
		},
	}
}

type SeparateRequest struct {
	InputRelPath        string `json:"input_relpath"`
	VocalsOutputRelPath string `json:"vocals_output_relpath"`
	BgmOutputRelPath    string `json:"bgm_output_relpath"`
}

type SeparateResponse struct {
	VocalsRelPath string   `json:"vocals_relpath"`
	BgmRelPath    string   `json:"bgm_relpath"`
	Diagnostics   []string `json:"diagnostics,omitempty"`
}

type SmartSplitRequest struct {
	AudioRelPath       string  `json:"audio_relpath"`
	SourceLanguage     string  `json:"source_language,omitempty"`
	MinSegmentSec      float64 `json:"min_segment_sec"`
	MaxSegmentSec      float64 `json:"max_segment_sec"`
	HardMaxSegmentSec  float64 `json:"hard_max_segment_sec"`
	CloseGapMs         int     `json:"close_gap_ms"`
}

type SmartSegment struct {
	StartMs      int64  `json:"start_ms"`
	EndMs        int64  `json:"end_ms"`
	Text         string `json:"text"`
	SpeakerLabel string `json:"speaker_label"`
	SplitReason  string `json:"split_reason"`
}

type SmartSplitResponse struct {
	Segments    []SmartSegment `json:"segments"`
	Diagnostics []string       `json:"diagnostics,omitempty"`
}

type TTSRequest struct {
	Text              string         `json:"text"`
	TargetDurationSec float64        `json:"target_duration_sec"`
	// MaxAllowedSec is target + trailing gap; the adapter uses it as the hard
	// token ceiling so audio never exceeds (target+gap) and re-translation is
	// only triggered for genuine overflow beyond the available silence.
	MaxAllowedSec     float64        `json:"max_allowed_sec,omitempty"`
	VoiceConfig       map[string]any `json:"voice_config"`
	OutputRelPath     string         `json:"output_relpath"`
	// Adaptive token budget feedback (scheme 2).
	// PrevActualSec and PrevTextChars carry the observed duration and char count
	// from the previous TTS attempt so the adapter can correct tokens_per_char.
	// Both are zero on the first attempt.
	PrevActualSec float64 `json:"prev_actual_sec,omitempty"`
	PrevTextChars int     `json:"prev_text_chars,omitempty"`
}

type TTSResponse struct {
	AudioRelPath     string   `json:"audio_relpath"`
	ActualDurationMs int64    `json:"actual_duration_ms"`
	Diagnostics      []string `json:"diagnostics,omitempty"`
}

func (c *Client) Health(ctx context.Context) (map[string]any, error) {
	var response map[string]any
	if err := c.doJSON(ctx, http.MethodGet, "/healthz", nil, &response); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) Separate(ctx context.Context, request SeparateRequest) (*SeparateResponse, error) {
	var response SeparateResponse
	if err := c.doJSON(ctx, http.MethodPost, "/media/separate", request, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *Client) SmartSplit(ctx context.Context, request SmartSplitRequest) (*SmartSplitResponse, error) {
	var response SmartSplitResponse
	if err := c.doJSON(ctx, http.MethodPost, "/asr/smart_split", request, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *Client) RunTTS(ctx context.Context, request TTSRequest) (*TTSResponse, error) {
	var response TTSResponse
	if err := c.doJSON(ctx, http.MethodPost, "/tts/run", request, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *Client) doJSON(ctx context.Context, method, path string, requestBody any, responseBody any) error {
	var body bytes.Buffer
	if requestBody != nil {
		if err := json.NewEncoder(&body).Encode(requestBody); err != nil {
			return fmt.Errorf("encode request: %w", err)
		}
	}

	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, &body)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")

	response, err := c.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer response.Body.Close()

	if response.StatusCode >= http.StatusBadRequest {
		var payload map[string]any
		_ = json.NewDecoder(response.Body).Decode(&payload)
		return fmt.Errorf("%s %s failed with %d: %v", method, path, response.StatusCode, payload)
	}

	if responseBody == nil {
		return nil
	}
	if err := json.NewDecoder(response.Body).Decode(responseBody); err != nil {
		return fmt.Errorf("decode response: %w", err)
	}
	return nil
}
