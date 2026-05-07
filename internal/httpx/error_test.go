package httpx

import (
	"context"
	"errors"
	"net/http"
	"testing"
	"time"
)

func TestClassifyHTTPStatus(t *testing.T) {
	retryable := []int{408, 425, 429, 500, 502, 503, 504, 599}
	for _, s := range retryable {
		if !ClassifyHTTPStatus(s) {
			t.Errorf("expected status %d to be retryable", s)
		}
	}
	notRetryable := []int{200, 301, 400, 401, 403, 404, 422}
	for _, s := range notRetryable {
		if ClassifyHTTPStatus(s) {
			t.Errorf("expected status %d to NOT be retryable", s)
		}
	}
}

func TestAPIError_String(t *testing.T) {
	e := &APIError{Service: "llm", Operation: "translate", StatusCode: 429, Code: "rate_limited", Message: "slow down"}
	got := e.Error()
	if got == "" {
		t.Fatal("empty error string")
	}
	if e.StatusCode != http.StatusTooManyRequests {
		t.Fatalf("expected status 429, got %d", e.StatusCode)
	}
}

func TestIsRetryable(t *testing.T) {
	if IsRetryable(nil) {
		t.Fatal("nil should not be retryable")
	}
	if !IsRetryable(&APIError{Retryable: true}) {
		t.Fatal("retryable APIError should report retryable")
	}
	if IsRetryable(&APIError{Retryable: false}) {
		t.Fatal("non-retryable APIError should not report retryable")
	}
	if !IsRetryable(context.DeadlineExceeded) {
		t.Fatal("ctx deadline should be retryable")
	}
}

func TestExtractCodeAndMessage(t *testing.T) {
	cases := []struct {
		body            string
		wantCode, wantM string
	}{
		{`{"error":{"message":"rate limit","code":"rate_limited"}}`, "rate_limited", "rate limit"},
		{`{"error":"upstream","message":"foo"}`, "upstream", "foo"},
		{`{"detail":"not found"}`, "", "not found"},
		{`{"detail":{"error":"tts_backend_unsupported","message":"bad"}}`, "tts_backend_unsupported", "bad"},
		{`<html>oops</html>`, "", ""},
	}
	for _, tc := range cases {
		got := extractCodeAndMessage([]byte(tc.body))
		if tc.wantCode == "" && tc.wantM == "" {
			if got != nil {
				t.Errorf("expected nil for %q, got %+v", tc.body, got)
			}
			continue
		}
		if got == nil {
			t.Errorf("expected extraction for %q, got nil", tc.body)
			continue
		}
		if got.code != tc.wantCode {
			t.Errorf("code mismatch for %q: want %q got %q", tc.body, tc.wantCode, got.code)
		}
		if got.message != tc.wantM {
			t.Errorf("message mismatch for %q: want %q got %q", tc.body, tc.wantM, got.message)
		}
	}
}

func TestDo_RetriesRetryableThenSucceeds(t *testing.T) {
	cfg := RetryConfig{
		MaxAttempts: 3,
		BaseBackoff: time.Millisecond,
		MaxBackoff:  10 * time.Millisecond,
	}
	var calls int
	err := Do(context.Background(), cfg, func(ctx context.Context, attempt int) error {
		calls++
		if attempt < 3 {
			return &APIError{Retryable: true}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("expected nil, got %v", err)
	}
	if calls != 3 {
		t.Fatalf("expected 3 calls, got %d", calls)
	}
}

func TestDo_StopsOnNonRetryable(t *testing.T) {
	cfg := RetryConfig{MaxAttempts: 5, BaseBackoff: time.Millisecond}
	var calls int
	target := errors.New("permanent")
	err := Do(context.Background(), cfg, func(ctx context.Context, attempt int) error {
		calls++
		return target
	})
	if !errors.Is(err, target) {
		t.Fatalf("expected target err, got %v", err)
	}
	if calls != 1 {
		t.Fatalf("expected 1 call, got %d", calls)
	}
}

func TestDo_RespectsContextCancel(t *testing.T) {
	cfg := RetryConfig{MaxAttempts: 5, BaseBackoff: 50 * time.Millisecond}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	var calls int
	go func() {
		time.Sleep(10 * time.Millisecond)
		cancel()
	}()
	err := Do(ctx, cfg, func(ctx context.Context, attempt int) error {
		calls++
		return &APIError{Retryable: true}
	})
	if err == nil {
		t.Fatal("expected error, got nil")
	}
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected ctx canceled, got %v", err)
	}
}
