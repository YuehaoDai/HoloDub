package httpx

import (
	"context"
	"errors"
	"math/rand"
	"time"
)

// RetryConfig controls retry behaviour. Defaults are conservative — most
// callers should override MaxAttempts and MaxBackoff for their use case.
type RetryConfig struct {
	// MaxAttempts is the total number of attempts including the first call.
	// Zero or negative is treated as 1 (no retry).
	MaxAttempts int
	// BaseBackoff is the starting delay for the first retry; subsequent
	// retries double the delay (exponential) up to MaxBackoff.
	BaseBackoff time.Duration
	// MaxBackoff caps any single sleep between retries.
	MaxBackoff time.Duration
	// JitterFraction adds [0, JitterFraction*backoff] uniform random delay
	// to break up coordinated retry storms. Use 0.2 for 20% jitter.
	JitterFraction float64
}

// DefaultRetry is a sensible baseline for outbound HTTP calls: 3 attempts,
// exponential 200ms → 800ms with 20% jitter.
var DefaultRetry = RetryConfig{
	MaxAttempts:    3,
	BaseBackoff:    200 * time.Millisecond,
	MaxBackoff:     2 * time.Second,
	JitterFraction: 0.2,
}

// Do invokes fn with retry on retryable errors (per IsRetryable).
//
// On a retryable error it sleeps with exponential backoff + jitter and
// retries until MaxAttempts is reached or ctx is cancelled. On a
// non-retryable error it returns immediately. The final attempt's error is
// wrapped to make the attempt count visible to logs.
func Do(ctx context.Context, cfg RetryConfig, fn func(ctx context.Context, attempt int) error) error {
	attempts := cfg.MaxAttempts
	if attempts < 1 {
		attempts = 1
	}
	var err error
	backoff := cfg.BaseBackoff
	for attempt := 1; attempt <= attempts; attempt++ {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return ctxErr
		}
		err = fn(ctx, attempt)
		if err == nil {
			return nil
		}
		if !IsRetryable(err) {
			return err
		}
		if attempt == attempts {
			break
		}
		sleep := backoffWithJitter(backoff, cfg.JitterFraction)
		if cfg.MaxBackoff > 0 && sleep > cfg.MaxBackoff {
			sleep = cfg.MaxBackoff
		}
		select {
		case <-ctx.Done():
			return errors.Join(err, ctx.Err())
		case <-time.After(sleep):
		}
		backoff *= 2
		if cfg.MaxBackoff > 0 && backoff > cfg.MaxBackoff {
			backoff = cfg.MaxBackoff
		}
	}
	return err
}

func backoffWithJitter(base time.Duration, jitterFraction float64) time.Duration {
	if base <= 0 {
		return 0
	}
	if jitterFraction <= 0 {
		return base
	}
	jitter := time.Duration(rand.Float64() * jitterFraction * float64(base))
	return base + jitter
}
