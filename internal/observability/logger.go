package observability

import (
	"log/slog"
	"os"
	"strings"

	"holodub/internal/config"
)

func NewLogger(cfg config.Config) *slog.Logger {
	level := parseLevel(cfg.LogLevel)
	options := &slog.HandlerOptions{Level: level}

	var handler slog.Handler
	if strings.EqualFold(cfg.LogFormat, "json") {
		handler = slog.NewJSONHandler(os.Stdout, options)
	} else {
		handler = slog.NewTextHandler(os.Stdout, options)
	}
	logger := slog.New(handler)
	slog.SetDefault(logger)
	return logger
}

func parseLevel(input string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(input)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}
