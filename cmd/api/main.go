package main

import (
	"context"
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
		// OPT-401: bring legacy databases up to the Episode/Chapter schema
		// (no-op when every Job already has a non-zero episode_id). Runs
		// inside a single transaction so partial failure leaves the DB
		// untouched.
		if err := st.RunBackfillIfNeeded(context.Background()); err != nil {
			logger.Error("episode back-fill failed", "error", err)
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

	if cfg.APIAuthToken == "" && !cfg.IsProduction() {
		logger.Warn(
			"API_AUTH_TOKEN is not set; protected routes are open to all clients. "+
				"This is allowed only because APP_ENV is not 'production'.",
			"environment", cfg.Environment,
		)
	}

	slog.Info("starting api", "addr", cfg.HTTPAddr, "env", cfg.Environment)
	if err := router.Run(cfg.HTTPAddr); err != nil {
		logger.Error("run api failed", "error", err)
		panic(err)
	}
}
