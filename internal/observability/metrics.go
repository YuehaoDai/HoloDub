package observability

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	httpRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "holodub_http_requests_total",
		Help: "Total HTTP requests served by the API.",
	}, []string{"method", "path", "status"})

	httpRequestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "holodub_http_request_duration_seconds",
		Help:    "HTTP request duration in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method", "path"})

	stageRunsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "holodub_stage_runs_total",
		Help: "Total pipeline stage runs by stage and status.",
	}, []string{"stage", "status"})

	stageRunDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "holodub_stage_run_duration_seconds",
		Help:    "Pipeline stage run duration in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"stage"})

	deadLettersTotal = promauto.NewCounter(prometheus.CounterOpts{
		Name: "holodub_dead_letters_total",
		Help: "Number of tasks moved into the dead letter queue.",
	})

	externalCallsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "holodub_external_calls_total",
		Help: "Outbound calls to external services (llm, ml) by classification.",
	}, []string{"service", "operation", "result"})

	externalCallDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "holodub_external_call_duration_seconds",
		Help:    "Outbound external call latency in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"service", "operation"})
)

// ObserveExternalCall records the outcome of an outbound HTTP call to a
// dependency (LLM provider, ml-service, …). result is one of:
//   - "ok"           — call succeeded
//   - "retryable"    — failed but classified as retryable (429/5xx/network)
//   - "permanent"    — failed and not retryable (4xx other than 429)
//   - "cancelled"    — context cancelled / deadline exceeded
func ObserveExternalCall(service, operation, result string, duration time.Duration) {
	externalCallsTotal.WithLabelValues(service, operation, result).Inc()
	externalCallDuration.WithLabelValues(service, operation).Observe(duration.Seconds())
}

func ObserveHTTPRequest(method, path string, statusCode int, duration time.Duration) {
	status := strconv.Itoa(statusCode)
	httpRequestsTotal.WithLabelValues(method, path, status).Inc()
	httpRequestDuration.WithLabelValues(method, path).Observe(duration.Seconds())
}

func ObserveStageRun(stage, status string, duration time.Duration) {
	stageRunsTotal.WithLabelValues(stage, status).Inc()
	stageRunDuration.WithLabelValues(stage).Observe(duration.Seconds())
}

func IncDeadLetters() {
	deadLettersTotal.Inc()
}

func MetricsHandler() http.Handler {
	return promhttp.Handler()
}
