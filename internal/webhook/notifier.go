package webhook

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"holodub/internal/config"
	"holodub/internal/models"
)

type Notifier struct {
	httpClient *http.Client
}

type EventPayload struct {
	Event       string            `json:"event"`
	JobID       uint              `json:"job_id"`
	TenantKey   string            `json:"tenant_key"`
	Status      models.JobStatus  `json:"status"`
	Stage       models.JobStage   `json:"stage"`
	Attempt     int               `json:"attempt"`
	OutputRelPath string          `json:"output_relpath,omitempty"`
	ErrorMessage string           `json:"error_message,omitempty"`
	Timestamp   time.Time         `json:"timestamp"`
	Meta        map[string]any    `json:"meta,omitempty"`
}

func New(cfg config.Config) *Notifier {
	return &Notifier{
		httpClient: &http.Client{Timeout: cfg.NotificationTimeout},
	}
}

func (n *Notifier) Notify(ctx context.Context, job models.Job, payload EventPayload) error {
	if job.WebhookURL == "" {
		return nil
	}

	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal webhook payload: %w", err)
	}

	request, err := http.NewRequestWithContext(ctx, http.MethodPost, job.WebhookURL, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build webhook request: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")
	if job.WebhookSecret != "" {
		request.Header.Set("X-HoloDub-Signature", sign(job.WebhookSecret, body))
	}

	response, err := n.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("deliver webhook: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode >= http.StatusBadRequest {
		return fmt.Errorf("webhook returned status %d", response.StatusCode)
	}
	return nil
}

func sign(secret string, payload []byte) string {
	mac := hmac.New(sha256.New, []byte(secret))
	_, _ = mac.Write(payload)
	return hex.EncodeToString(mac.Sum(nil))
}
