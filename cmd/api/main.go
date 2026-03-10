package main

import (
	"log/slog"

	apihttp "holodub/internal/http"
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
		slog.Info("auto migration disabled", "component", "api")
	}

	router := apihttp.NewRouter(
		cfg,
		st,
		pipeline.NewService(cfg, st, queue.New(cfg), ml.New(cfg.MLServiceURL), llm.New(cfg)),
	)

	slog.Info("starting api", "addr", cfg.HTTPAddr, "env", cfg.Environment)
	if err := router.Run(cfg.HTTPAddr); err != nil {
		logger.Error("run api failed", "error", err)
		panic(err)
	}
}
