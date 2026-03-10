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
)

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
