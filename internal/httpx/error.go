// Package httpx provides shared error types and retry helpers for the Go
// control plane's outbound HTTP clients (LLM, ML service, future webhooks).
//
// Before this package, clients returned `fmt.Errorf("... status %d")` strings
// that callers could only match by substring. This made it impossible to
// classify failures (transient vs permanent) and impossible to drive retry
// or alerting from structured signals.
package httpx

import (
	"context"
	"errors"
	"fmt"
	"net/http"
)

// APIError is returned by HTTP-backed clients in this repo. Errors satisfy
// errors.Is / errors.As: callers can write
//
//	var apiErr *httpx.APIError
//	if errors.As(err, &apiErr) && apiErr.Retryable { ... }
//
// to drive retry / circuit-breaker logic.
type APIError struct {
	// Service is the logical name of the dependency ("llm", "ml", ...).
	Service string
	// Operation is the verb being attempted ("translate", "tts.run", ...).
	Operation string
	// StatusCode is the HTTP status code, or 0 for non-HTTP failures
	// (DNS error, connection refused, TLS handshake).
	StatusCode int
	// Code is a machine-readable error class derived from the upstream
	// payload, when available (e.g. "rate_limited", "context_length").
	Code string
	// Message is a human-readable summary safe to surface in logs.
	Message string
	// Retryable signals that the same call may succeed if retried.
	Retryable bool
	// Err is the underlying error, if any (network error, decode error).
	Err error
}

func (e *APIError) Error() string {
	switch {
	case e.StatusCode > 0 && e.Code != "":
		return fmt.Sprintf("%s.%s: status=%d code=%s: %s", e.Service, e.Operation, e.StatusCode, e.Code, e.Message)
	case e.StatusCode > 0:
		return fmt.Sprintf("%s.%s: status=%d: %s", e.Service, e.Operation, e.StatusCode, e.Message)
	case e.Err != nil:
		return fmt.Sprintf("%s.%s: %v", e.Service, e.Operation, e.Err)
	default:
		return fmt.Sprintf("%s.%s: %s", e.Service, e.Operation, e.Message)
	}
}

func (e *APIError) Unwrap() error { return e.Err }

// IsRetryable reports whether err describes a transient failure that may
// succeed on retry. Network failures, context deadline exceeded, and
// upstream 408/425/429/5xx responses are considered retryable.
func IsRetryable(err error) bool {
	if err == nil {
		return false
	}
	var apiErr *APIError
	if errors.As(err, &apiErr) {
		return apiErr.Retryable
	}
	// Standalone network / context errors are also retryable.
	if errors.Is(err, context.DeadlineExceeded) {
		return true
	}
	return false
}

// ClassifyHTTPStatus returns whether a given HTTP status code is treated as
// retryable by the default policy.
func ClassifyHTTPStatus(status int) bool {
	switch {
	case status == http.StatusRequestTimeout: // 408
		return true
	case status == http.StatusTooEarly: // 425
		return true
	case status == http.StatusTooManyRequests: // 429
		return true
	case status >= 500 && status <= 599:
		return true
	default:
		return false
	}
}

// Wrap is a small helper to construct an APIError for non-HTTP errors
// (DNS failure, TLS handshake, JSON decode after a 200, etc.).
func Wrap(service, operation string, err error) *APIError {
	return &APIError{
		Service:   service,
		Operation: operation,
		Message:   err.Error(),
		Retryable: true, // network-level errors are conservatively retryable
		Err:       err,
	}
}

// FromHTTPStatus builds an APIError from an HTTP response status and an
// optional decoded error body. The body is only used to extract a human
// message and an error code; callers can pass nil/empty to skip.
func FromHTTPStatus(service, operation string, status int, body []byte) *APIError {
	msg := http.StatusText(status)
	code := ""
	if len(body) > 0 {
		// Best-effort extraction of OpenAI-style {"error":{"message":"...","code":"..."}}
		// without binding the whole package to a specific schema.
		if extracted := extractCodeAndMessage(body); extracted != nil {
			if extracted.code != "" {
				code = extracted.code
			}
			if extracted.message != "" {
				msg = extracted.message
			}
		}
	}
	return &APIError{
		Service:    service,
		Operation:  operation,
		StatusCode: status,
		Code:       code,
		Message:    msg,
		Retryable:  ClassifyHTTPStatus(status),
	}
}

// internal best-effort extraction; a separate file (extract.go) holds it.
