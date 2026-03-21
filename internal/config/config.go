package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	AppName             string
	Environment         string
	HTTPAddr            string
	LogLevel            string
	LogFormat           string
	DataRoot            string
	DatabaseDriver      string
	DatabaseDSN         string
	RedisAddr           string
	RedisPassword       string
	RedisDB             int
	QueueKey            string
	DelayedQueueKey     string
	DeadLetterQueueKey  string
	StageLeasePrefix    string
	WorkerID            string
	WorkerPollInterval  time.Duration
	StageTimeout        time.Duration
	StageLeaseTTL       time.Duration
	MaxJobRetries       int
	RetryBaseDelay      time.Duration
	DefaultTenantKey    string
	TrustedProxies      []string
	APIAuthToken        string
	RequestRateLimitRPS float64
	RequestRateLimitBurst int
	EnableMetrics       bool
	ModelManifestPath   string
	NotificationTimeout time.Duration
	AutoMigrateOnStart  bool
	MLServiceURL        string
	TranslationProvider string
	OpenAIBaseURL       string
	OpenAIAPIKey        string
	OpenAIModel         string
	OpenAITemperature   float64
	OpenAITimeout       time.Duration

	// Re-translation: triggered when TTS output drifts too far from target duration.
	RetranslationEnabled        bool
	RetranslationDriftThreshold float64 // relative threshold, e.g. 0.06 = 6%
	RetranslationAbsMaxDriftSec float64 // absolute cap in seconds, e.g. 0.8; effective threshold = min(rel, abs/targetSec)
	RetranslationMinDriftThreshold float64 // minimum relative threshold floor, e.g. 0.03 = 3%; prevents impossibly strict targets for very long segments
	RetranslationMaxAttempts    int
	RetranslationMaxBorrowDriftPct float64 // max over-run drift allowed to borrow from gap without re-translating, e.g. 0.12 = 12%
	RetranslationModel          string // e.g. "kimi-k2.5"; reuses OPENAI_BASE_URL/API_KEY

	FFmpegBin  string
	FFprobeBin string

	// TTSConcurrency: max parallel TTS requests per job (1=sequential, 2+=parallel).
	TTSConcurrency int
}

func Load() (Config, error) {
	cfg := Config{
		AppName:             getEnv("APP_NAME", "HoloDub"),
		Environment:         getEnv("APP_ENV", "development"),
		HTTPAddr:            getEnv("HTTP_ADDR", ":8080"),
		LogLevel:            getEnv("LOG_LEVEL", "info"),
		LogFormat:           getEnv("LOG_FORMAT", "text"),
		DataRoot:            getEnv("DATA_ROOT", "./data"),
		DatabaseDriver:      getEnv("DATABASE_DRIVER", "postgres"),
		DatabaseDSN:         getEnv("DATABASE_DSN", "host=localhost user=holodub password=holodub dbname=holodub port=5432 sslmode=disable TimeZone=UTC"),
		RedisAddr:           getEnv("REDIS_ADDR", "localhost:6379"),
		RedisPassword:       getEnv("REDIS_PASSWORD", ""),
		QueueKey:            getEnv("QUEUE_KEY", "holodub:tasks"),
		DelayedQueueKey:     getEnv("DELAYED_QUEUE_KEY", "holodub:tasks:delayed"),
		DeadLetterQueueKey:  getEnv("DEAD_LETTER_QUEUE_KEY", "holodub:tasks:dead"),
		StageLeasePrefix:    getEnv("STAGE_LEASE_PREFIX", "holodub:lease"),
		WorkerID:            getEnv("WORKER_ID", hostnameOrFallback()),
		DefaultTenantKey:    getEnv("DEFAULT_TENANT_KEY", "default"),
		TrustedProxies:      getEnvList("TRUSTED_PROXIES", []string{}),
		APIAuthToken:        getEnv("API_AUTH_TOKEN", ""),
		MLServiceURL:        getEnv("ML_SERVICE_URL", "http://localhost:8000"),
		TranslationProvider: getEnv("TRANSLATION_PROVIDER", "mock"),
		OpenAIBaseURL:       getEnv("OPENAI_BASE_URL", ""),
		OpenAIAPIKey:        getEnv("OPENAI_API_KEY", ""),
		OpenAIModel:         getEnv("OPENAI_MODEL", ""),
		ModelManifestPath:   getEnv("MODEL_MANIFEST_PATH", "config/model-manifest.example.json"),
		FFmpegBin:           getEnv("FFMPEG_BIN", "ffmpeg"),
		FFprobeBin:          getEnv("FFPROBE_BIN", "ffprobe"),
	}

	var err error
	cfg.RedisDB, err = getEnvInt("REDIS_DB", 0)
	if err != nil {
		return Config{}, fmt.Errorf("parse REDIS_DB: %w", err)
	}

	pollSeconds, err := getEnvInt("WORKER_POLL_SECONDS", 5)
	if err != nil {
		return Config{}, fmt.Errorf("parse WORKER_POLL_SECONDS: %w", err)
	}
	cfg.WorkerPollInterval = time.Duration(pollSeconds) * time.Second

	stageTimeoutSeconds, err := getEnvInt("STAGE_TIMEOUT_SECONDS", 900)
	if err != nil {
		return Config{}, fmt.Errorf("parse STAGE_TIMEOUT_SECONDS: %w", err)
	}
	cfg.StageTimeout = time.Duration(stageTimeoutSeconds) * time.Second

	leaseSeconds, err := getEnvInt("STAGE_LEASE_TTL_SECONDS", 1800)
	if err != nil {
		return Config{}, fmt.Errorf("parse STAGE_LEASE_TTL_SECONDS: %w", err)
	}
	cfg.StageLeaseTTL = time.Duration(leaseSeconds) * time.Second

	maxRetries, err := getEnvInt("MAX_JOB_RETRIES", 3)
	if err != nil {
		return Config{}, fmt.Errorf("parse MAX_JOB_RETRIES: %w", err)
	}
	cfg.MaxJobRetries = maxRetries

	retryDelayMs, err := getEnvInt("RETRY_BASE_DELAY_MS", 2000)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRY_BASE_DELAY_MS: %w", err)
	}
	cfg.RetryBaseDelay = time.Duration(retryDelayMs) * time.Millisecond

	openAITimeoutSeconds, err := getEnvInt("OPENAI_TIMEOUT_SECONDS", 90)
	if err != nil {
		return Config{}, fmt.Errorf("parse OPENAI_TIMEOUT_SECONDS: %w", err)
	}
	cfg.OpenAITimeout = time.Duration(openAITimeoutSeconds) * time.Second

	notificationTimeoutSeconds, err := getEnvInt("NOTIFICATION_TIMEOUT_SECONDS", 10)
	if err != nil {
		return Config{}, fmt.Errorf("parse NOTIFICATION_TIMEOUT_SECONDS: %w", err)
	}
	cfg.NotificationTimeout = time.Duration(notificationTimeoutSeconds) * time.Second

	cfg.OpenAITemperature, err = getEnvFloat("OPENAI_TEMPERATURE", 0.2)
	if err != nil {
		return Config{}, fmt.Errorf("parse OPENAI_TEMPERATURE: %w", err)
	}

	cfg.RetranslationEnabled, err = getEnvBool("RETRANSLATION_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_ENABLED: %w", err)
	}

	cfg.RetranslationDriftThreshold, err = getEnvFloat("RETRANSLATION_DRIFT_THRESHOLD", 0.06)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_DRIFT_THRESHOLD: %w", err)
	}

	cfg.RetranslationAbsMaxDriftSec, err = getEnvFloat("RETRANSLATION_ABS_MAX_DRIFT_SEC", 0.8)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_ABS_MAX_DRIFT_SEC: %w", err)
	}

	cfg.RetranslationMinDriftThreshold, err = getEnvFloat("RETRANSLATION_MIN_DRIFT_THRESHOLD", 0.03)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_MIN_DRIFT_THRESHOLD: %w", err)
	}

	cfg.RetranslationMaxAttempts, err = getEnvInt("RETRANSLATION_MAX_ATTEMPTS", 10)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_MAX_ATTEMPTS: %w", err)
	}

	cfg.RetranslationMaxBorrowDriftPct, err = getEnvFloat("RETRANSLATION_MAX_BORROW_DRIFT_PCT", 0.12)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_MAX_BORROW_DRIFT_PCT: %w", err)
	}

	cfg.RetranslationModel = getEnv("RETRANSLATION_MODEL", "kimi-k2.5")

	cfg.TTSConcurrency, err = getEnvInt("TTS_CONCURRENCY", 2)
	if err != nil {
		return Config{}, fmt.Errorf("parse TTS_CONCURRENCY: %w", err)
	}
	if cfg.TTSConcurrency < 1 {
		cfg.TTSConcurrency = 1
	}

	cfg.RequestRateLimitRPS, err = getEnvFloat("REQUEST_RATE_LIMIT_RPS", 20.0)
	if err != nil {
		return Config{}, fmt.Errorf("parse REQUEST_RATE_LIMIT_RPS: %w", err)
	}

	cfg.RequestRateLimitBurst, err = getEnvInt("REQUEST_RATE_LIMIT_BURST", 50)
	if err != nil {
		return Config{}, fmt.Errorf("parse REQUEST_RATE_LIMIT_BURST: %w", err)
	}

	cfg.EnableMetrics, err = getEnvBool("ENABLE_METRICS", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse ENABLE_METRICS: %w", err)
	}

	cfg.AutoMigrateOnStart, err = getEnvBool("AUTO_MIGRATE_ON_START", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse AUTO_MIGRATE_ON_START: %w", err)
	}

	return cfg, nil
}

func getEnv(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func getEnvInt(key string, fallback int) (int, error) {
	value := getEnv(key, strconv.Itoa(fallback))
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return 0, err
	}
	return parsed, nil
}

func getEnvFloat(key string, fallback float64) (float64, error) {
	value := getEnv(key, fmt.Sprintf("%v", fallback))
	parsed, err := strconv.ParseFloat(value, 64)
	if err != nil {
		return 0, err
	}
	return parsed, nil
}

func getEnvBool(key string, fallback bool) (bool, error) {
	value := strings.TrimSpace(getEnv(key, strconv.FormatBool(fallback)))
	parsed, err := strconv.ParseBool(value)
	if err != nil {
		return false, err
	}
	return parsed, nil
}

func getEnvList(key string, fallback []string) []string {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}

	parts := strings.Split(value, ",")
	out := make([]string, 0, len(parts))
	for _, part := range parts {
		trimmed := strings.TrimSpace(part)
		if trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}

func hostnameOrFallback() string {
	hostname, err := os.Hostname()
	if err != nil || strings.TrimSpace(hostname) == "" {
		return "holodub-worker"
	}
	return hostname
}
