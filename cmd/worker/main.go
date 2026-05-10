package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"holodub/internal/config"
	"holodub/internal/llm"
	"holodub/internal/ml"
	"holodub/internal/observability"
	"holodub/internal/pipeline"
	"holodub/internal/queue"
	"holodub/internal/storage"
	"holodub/internal/store"
)

func main() {
	cfg, err := config.Load()
	if err != nil {
		panic(err)
	}
	logger := observability.NewLogger(cfg)

	if err := storage.EnsureDataDirs(cfg.DataRoot); err != nil {
		logger.Error("ensure data root failed", "error", err)
		panic(err)
	}

	st, err := store.New(cfg)
	if err != nil {
		logger.Error("open store failed", "error", err)
		panic(err)
	}
	if cfg.AutoMigrateOnStart {
		if err := st.AutoMigrate(); err != nil {
			logger.Error("migrate database failed", "error", err)
			panic(err)
		}
	} else {
		slog.Info("auto migration disabled", "component", "worker")
	}

	taskQueue := queue.New(cfg)
	if err := taskQueue.Ping(context.Background()); err != nil {
		logger.Error("ping redis failed", "error", err)
		panic(err)
	}

	service := pipeline.NewService(cfg, st, taskQueue, ml.New(cfg.MLServiceURL), llm.New(cfg))

	// Root context cancelled on SIGINT/SIGTERM. Every Redis call and every
	// pipeline stage executes with a context derived from this root, so a
	// single Ctrl+C / `docker compose stop` cleanly drains in-flight work
	// and exits the loop instead of being killed mid-stage.
	rootCtx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	// Worker-side /metrics endpoint (OPT-001). LLM token / cost / cache hit
	// counters are emitted from the worker process and would otherwise be
	// invisible from the api container. Keep this best-effort: a bind
	// failure logs a warning but does not stop the worker, since metrics
	// must never block the main flow.
	if addr := strings.TrimSpace(cfg.WorkerMetricsAddr); addr != "" {
		go func() {
			mux := http.NewServeMux()
			mux.Handle("/metrics", observability.MetricsHandler())
			mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte("ok"))
			})
			server := &http.Server{
				Addr:              addr,
				Handler:           mux,
				ReadHeaderTimeout: 5 * time.Second,
			}
			slog.Info("worker metrics endpoint listening", "addr", addr)
			if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
				slog.Warn("worker metrics endpoint exited", "error", err)
			}
		}()
	}

	slog.Info("worker started",
		"worker_id", cfg.WorkerID,
		"poll_interval", cfg.WorkerPollInterval.String(),
	)

	for {
		if rootCtx.Err() != nil {
			slog.Info("worker shutting down", "reason", "signal received")
			return
		}

		if err := taskQueue.PromoteDueDelayed(rootCtx, 100); err != nil && !errors.Is(err, context.Canceled) {
			slog.Warn("promote delayed tasks failed", "error", err)
		}
		task, err := taskQueue.PopBlocking(rootCtx, cfg.WorkerPollInterval)
		if err != nil {
			if errors.Is(err, context.Canceled) {
				slog.Info("worker shutting down", "reason", "redis pop cancelled")
				return
			}
			slog.Warn("pop task failed", "error", err)
			select {
			case <-rootCtx.Done():
				return
			case <-time.After(2 * time.Second):
			}
			continue
		}
		if task == nil {
			continue
		}
		slog.Info("processing task",
			"job_id", task.JobID,
			"stage", task.Stage,
			"attempt", task.Attempt,
			"worker_id", cfg.WorkerID,
		)
		if err := service.HandleTask(rootCtx, *task); err != nil {
			slog.Error("task failed",
				"job_id", task.JobID,
				"stage", task.Stage,
				"attempt", task.Attempt,
				"error", err,
			)
		}
	}
}
