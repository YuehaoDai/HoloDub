package http

import (
	"crypto/subtle"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"holodub/internal/observability"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"golang.org/x/time/rate"
)

const (
	requestIDContextKey = "request_id"
	tenantContextKey    = "tenant_key"
	apiKeyHeader        = "X-API-Key"
	requestIDHeader     = "X-Request-Id"
	tenantHeader        = "X-Tenant-Key"
)

type visitorLimiter struct {
	limiter  *rate.Limiter
	lastSeen time.Time
}

type rateLimiterStore struct {
	mu      sync.Mutex
	visitors map[string]*visitorLimiter
}

func newRateLimiterStore() *rateLimiterStore {
	return &rateLimiterStore{visitors: map[string]*visitorLimiter{}}
}

func (s *rateLimiterStore) get(key string, rps float64, burst int) *rate.Limiter {
	s.mu.Lock()
	defer s.mu.Unlock()

	visitor, exists := s.visitors[key]
	if !exists {
		visitor = &visitorLimiter{
			limiter:  rate.NewLimiter(rate.Limit(rps), burst),
			lastSeen: time.Now(),
		}
		s.visitors[key] = visitor
		return visitor.limiter
	}

	visitor.lastSeen = time.Now()
	return visitor.limiter
}

func requestIDMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		requestID := c.GetHeader(requestIDHeader)
		if requestID == "" {
			requestID = uuid.NewString()
		}
		c.Set(requestIDContextKey, requestID)
		c.Writer.Header().Set(requestIDHeader, requestID)
		c.Next()
	}
}

func tenantMiddleware(defaultTenant string) gin.HandlerFunc {
	return func(c *gin.Context) {
		tenantKey := c.GetHeader(tenantHeader)
		if tenantKey == "" {
			tenantKey = defaultTenant
		}
		c.Set(tenantContextKey, tenantKey)
		c.Next()
	}
}

func apiKeyAuthMiddleware(token string) gin.HandlerFunc {
	if token == "" {
		return func(c *gin.Context) { c.Next() }
	}

	return func(c *gin.Context) {
		switch c.FullPath() {
		case "/", "/ui", "/ui/*filepath", "/healthz", "/ml-health", "/metrics":
			c.Next()
			return
		}

		provided := c.GetHeader(apiKeyHeader)
		if provided == "" {
			provided = c.Query("api_key")
		}
		if subtle.ConstantTimeCompare([]byte(provided), []byte(token)) != 1 {
			respondError(c, http.StatusUnauthorized, "unauthorized", "missing or invalid API key")
			c.Abort()
			return
		}
		c.Next()
	}
}

func rateLimitMiddleware(rps float64, burst int) gin.HandlerFunc {
	if rps <= 0 || burst <= 0 {
		return func(c *gin.Context) { c.Next() }
	}
	store := newRateLimiterStore()
	return func(c *gin.Context) {
		switch c.FullPath() {
		case "/", "/ui", "/ui/*filepath", "/healthz", "/ml-health", "/metrics":
			c.Next()
			return
		}
		key := c.ClientIP()
		limiter := store.get(key, rps, burst)
		if !limiter.Allow() {
			respondError(c, http.StatusTooManyRequests, "rate_limited", "request rate limit exceeded")
			c.Abort()
			return
		}
		c.Next()
	}
}

func metricsMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		startedAt := time.Now()
		c.Next()
		observability.ObserveHTTPRequest(c.Request.Method, c.FullPath(), c.Writer.Status(), time.Since(startedAt))
	}
}

func loggingMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		startedAt := time.Now()
		c.Next()
		requestID := requestIDFromContext(c)
		slog.Info("http_request",
			"request_id", requestID,
			"method", c.Request.Method,
			"path", c.FullPath(),
			"status", c.Writer.Status(),
			"latency_ms", time.Since(startedAt).Milliseconds(),
			"client_ip", c.ClientIP(),
			"tenant_key", tenantKeyFromContext(c),
		)
	}
}

func requestIDFromContext(c *gin.Context) string {
	value, _ := c.Get(requestIDContextKey)
	requestID, _ := value.(string)
	return requestID
}

func tenantKeyFromContext(c *gin.Context) string {
	value, _ := c.Get(tenantContextKey)
	tenantKey, _ := value.(string)
	return tenantKey
}
