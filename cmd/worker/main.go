package main

import (
	"context"
	"log/slog"
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
	slog.Info("worker started",
		"worker_id", cfg.WorkerID,
		"poll_interval", cfg.WorkerPollInterval.String(),
	)

	for {
		if err := taskQueue.PromoteDueDelayed(context.Background(), 100); err != nil {
			slog.Warn("promote delayed tasks failed", "error", err)
		}
		task, err := taskQueue.PopBlocking(context.Background(), cfg.WorkerPollInterval)
		if err != nil {
			slog.Warn("pop task failed", "error", err)
			time.Sleep(2 * time.Second)
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
		if err := service.HandleTask(context.Background(), *task); err != nil {
			slog.Error("task failed",
				"job_id", task.JobID,
				"stage", task.Stage,
				"attempt", task.Attempt,
				"error", err,
			)
		}
	}
}
