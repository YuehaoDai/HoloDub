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
	RetranslationTemperature    float64 // temperature for retranslation calls; 0 falls back to OpenAITemperature
	// Thinking-mode fallback: activated when the LLM returns identical text for
	// RetranslationStuckThreshold consecutive attempts.
	RetranslationThinkingModel          string        // e.g. "kimi-k2-thinking"; MUST support stream=true
	RetranslationThinkingTimeoutSeconds int           // timeout in seconds for thinking-mode calls (default 600)
	RetranslationStuckThreshold         int           // consecutive same-char attempts before switching (default 2)
	RetranslationNonConvergenceWindow   int           // trigger thinking after N attempts without drift improvement (default 3)
	RetranslationInitialMaxAttempts     int           // max retranslation attempts for pipeline-triggered (initial) synthesis (default 50)
	RetranslationInitialAbsMaxDriftSec  float64       // stricter absolute drift ceiling for initial synthesis, e.g. 0.75 s
	// VoiceProfileRateAlpha: weight given to new observations when updating EstCharsPerSec
	// via exponential moving average. 0.3 means 30% new data, 70% old estimate.
	VoiceProfileRateAlpha float64

	// ASR segmentation limits passed to the ML service smart_split call.
	HardMaxSegmentSec float64 // absolute ceiling for any segment after post-merge; e.g. 45.0 s
	CloseGapMs        int     // inter-segment gap threshold for close-gap merge pass; e.g. 800 ms

	// Segment review: LLM-powered merge suggestions after asr_smart, before translate.
	SegmentReviewEnabled bool   // default true
	SegmentReviewModel   string // model name; falls back to RetranslationModel then OpenAIModel
	// SegmentReviewUseTools: OPT-003. When true, ReviewSegmentation uses
	// strict-schema function calling instead of "describe JSON in prompt
	// + json.Unmarshal" fallback. Default false during gradual rollout.
	SegmentReviewUseTools bool

	// JudgeModel: OPT-002. Empty = LLM-as-Judge disabled. When non-empty,
	// every TTS segment is scored asynchronously after synthesis.
	JudgeModel string
	// JudgeObserveOnly: OPT-002 MVP. true = score is recorded but does NOT
	// influence retry/break decisions. Flipping to false at L4 rollout
	// hands the judge verdict to the agent loop (see OPT-201).
	JudgeObserveOnly bool

	// JudgeBackfillOnStart: OPT-002-followup-2. true = on worker boot, scan
	// for synthesised segments missing judge_score and dispatch them through
	// maybeJudgeSegmentAsync (bounded concurrency). Closes the gap where
	// segments synthesised during a worker restart window never get judged.
	// Default true; only effective when JudgeModel is also set.
	JudgeBackfillOnStart bool
	// JudgeBackfillLimit: cap on segments scanned per worker boot. Default
	// 500 keeps the wakeup-time judge cost ≤ ~$0.25 with qwen-turbo.
	JudgeBackfillLimit int

	// ChapterJudgeModel: OPT-409. Empty = chapter-level judging disabled.
	// When non-empty, every chapter (= one Job under a multi-chapter Episode)
	// is scored asynchronously after runMerge persists the chapter outputs.
	// Recommended: kimi-k2.5 (~$0.005 per chapter at ~3k input tokens).
	ChapterJudgeModel string
	// ChapterJudgeObserveOnly: OPT-409 MVP. true = chapter judge score is
	// persisted on jobs.chapter_judge_score / chapter_judge_meta but does
	// NOT influence episode_merge or any other downstream decision. OPT-407
	// closed-loop rework will flip this to false once verdict thresholds
	// are calibrated against operator labels.
	ChapterJudgeObserveOnly bool

	// GlossaryModel: OPT-402. The model used by ExtractEpisodeGlossary
	// to derive the canonical episode-level term sheet from the full ASR
	// text. Empty = fall back to OpenAIModel. Recommended: qwen-turbo
	// (cheap, fast, ~3 s on the 10 min baseline). The glossary call is
	// non-blocking on the pipeline; failure leaves Episode.Glossary empty
	// and translate falls back to the no-glossary mode (== current behaviour).
	GlossaryModel string
	// GlossaryEnabled: OPT-402 feature flag. true = ep_glossary_extract
	// stage runs after ep_asr_smart. false = stage is skipped (Episode.
	// Glossary stays empty). Defaults to true on new installs.
	GlossaryEnabled bool

	// ChapterReviewModel: OPT-403 Pass 3 LLM. Reviews the deterministic DP
	// chapter cuts, optionally nudges them by ±1 silence-gap, and mints a
	// bilingual chapter title for each chapter. Empty = fall back to
	// OpenAIModel. Recommended: qwen-turbo (same tier as glossary). The
	// review call is non-blocking — on failure the DP cuts are used as-is
	// and chapter titles default to "Chapter N".
	ChapterReviewModel string
	// ChapterReviewLLMEnabled: OPT-403 feature flag for Pass 3. false =
	// skip LLM review entirely (cuts come straight from DP, titles default
	// to "Chapter N"). true = call ReviewChapterCuts after DP. Defaults
	// to true on new installs.
	ChapterReviewLLMEnabled bool

	// ChapterizeEnabled: OPT-403 master switch. false = always run as a
	// 1-chapter Episode regardless of duration (legacy behaviour). true =
	// the ep_chapterize handler runs after ep_glossary_extract and may
	// fan out long episodes into 2..N chapters. Defaults to true.
	ChapterizeEnabled bool
	// ChapterizeMinChapterMs / ChapterizeTargetChapterMs / ChapterizeMaxChapterMs
	// configure DPOptimalCuts (the OPT-403 deterministic fallback). An episode
	// whose duration is <= MaxChapterMs is short-circuited to a single chapter;
	// longer episodes get split so every chapter falls in [MinChapterMs,
	// MaxChapterMs] and minimises distance from TargetChapterMs. Defaults
	// 18 / 22 / 30 minutes (long-form podcast tuning).
	//
	// OPT-405: when ChapterizeLLMDriven=true the LLM does the chapter cut
	// decision instead, and these knobs are NOT consulted on the happy path
	// — only when the LLM call fails or returns invalid cuts and the pipeline
	// falls back to the DP algorithm.
	ChapterizeMinChapterMs    int64
	ChapterizeTargetChapterMs int64
	ChapterizeMaxChapterMs    int64
	// ChapterizeMinSilenceGapMs is the silence width (in ms) required between
	// two ASR segments to qualify as a chapter-cut candidate. Default 800ms
	// — at less than ~600ms ASR routinely produces unintended boundaries
	// inside a single sentence. Used by both the DP fallback AND OPT-405's
	// snap-to-silence pass (which honours this threshold when looking for a
	// natural cut near the LLM-suggested boundary).
	ChapterizeMinSilenceGapMs int64

	// OPT-405 LLM-driven chapterization knobs.

	// ChapterizeLLMDriven toggles the OPT-405 path. true = ep_glossary_extract
	// produces chapter cuts in the same tool call as the glossary, and
	// ep_chapterize uses them (snapping each end_segment_idx to the nearest
	// silence midpoint, then enforcing the hard min/max guardrails). false =
	// chapterize falls straight back to the OPT-403 DP algorithm. Defaults
	// to true on new installs since OPT-403 review proved the deterministic
	// path produces semantically wrong cuts on long talks.
	ChapterizeLLMDriven bool

	// ChapterizeHardMaxMs is the hard upper bound on a single chapter even
	// when the LLM decided otherwise. Anything longer is force-cut at the
	// nearest silence to the midpoint. Default 45min — long enough to keep
	// natural lecture themes intact, short enough that download / playback
	// remains practical. Set to a very large number to effectively disable.
	ChapterizeHardMaxMs int64

	// ChapterizeHardMinMs is the hard lower bound. Any LLM-emitted chapter
	// shorter than this is merged into its successor (last chapter merges
	// into its predecessor). Default 5min — short enough not to fight the
	// LLM on intentional brief intros / outros, long enough to filter out
	// model hallucinations that emit a 30s chapter for a single sentence.
	ChapterizeHardMinMs int64

	// LoudnormTargetI / LoudnormTargetTP / LoudnormTargetLRA are the EBU R128
	// targets passed to media.LoudnormTwoPass for chapter-level + master-level
	// normalisation. Defaults follow broadcast spec (-23 LUFS / -1 dBTP / 7 LU).
	LoudnormTargetI   float64
	LoudnormTargetTP  float64
	LoudnormTargetLRA float64
	// LoudnormChapterEnabled / LoudnormMasterEnabled toggle the two passes
	// independently. Disabling chapter-level normalisation breaks cross-
	// chapter loudness consistency; disabling the master pass is harmless
	// when chapter-level is on.
	LoudnormChapterEnabled bool
	LoudnormMasterEnabled  bool
	// EpisodeMergeEnabled toggles stage_episode_merge. When false, chapter
	// videos still land in episodes/{ep_id}/chapters/... but no final
	// merge-and-write happens (the EpisodeDetail UI's per-chapter download
	// links keep working).
	EpisodeMergeEnabled bool

	FFmpegBin  string
	FFprobeBin string

	// TTSConcurrency: max parallel TTS requests per job (1=sequential, 2+=parallel).
	TTSConcurrency int

	// WorkerMetricsAddr is the listen address for the worker's /metrics
	// endpoint. Default ":8081"; set to empty string to disable. Worker
	// emits its own LLM token / cost / cache hit metrics that are NOT
	// visible from the api container's /metrics — exposing this lets
	// Prometheus / curl scrape worker-side counters (OPT-001).
	WorkerMetricsAddr string
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

	cfg.RetranslationTemperature, err = getEnvFloat("RETRANSLATION_TEMPERATURE", 0)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_TEMPERATURE: %w", err)
	}

	cfg.RetranslationThinkingModel = getEnv("RETRANSLATION_THINKING_MODEL", "kimi-k2-thinking")

	cfg.RetranslationThinkingTimeoutSeconds, err = getEnvInt("RETRANSLATION_THINKING_TIMEOUT_SECONDS", 600)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_THINKING_TIMEOUT_SECONDS: %w", err)
	}

	cfg.RetranslationStuckThreshold, err = getEnvInt("RETRANSLATION_STUCK_THRESHOLD", 2)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_STUCK_THRESHOLD: %w", err)
	}

	cfg.RetranslationNonConvergenceWindow, err = getEnvInt("RETRANSLATION_NON_CONVERGENCE_WINDOW", 3)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_NON_CONVERGENCE_WINDOW: %w", err)
	}

	cfg.RetranslationInitialMaxAttempts, err = getEnvInt("RETRANSLATION_INITIAL_MAX_ATTEMPTS", 50)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_INITIAL_MAX_ATTEMPTS: %w", err)
	}

	cfg.RetranslationInitialAbsMaxDriftSec, err = getEnvFloat("RETRANSLATION_INITIAL_ABS_MAX_DRIFT_SEC", 0.75)
	if err != nil {
		return Config{}, fmt.Errorf("parse RETRANSLATION_INITIAL_ABS_MAX_DRIFT_SEC: %w", err)
	}

	cfg.VoiceProfileRateAlpha, err = getEnvFloat("VOICE_PROFILE_RATE_ALPHA", 0.3)
	if err != nil {
		return Config{}, fmt.Errorf("parse VOICE_PROFILE_RATE_ALPHA: %w", err)
	}

	cfg.HardMaxSegmentSec, err = getEnvFloat("HARD_MAX_SEGMENT_SEC", 45.0)
	if err != nil {
		return Config{}, fmt.Errorf("parse HARD_MAX_SEGMENT_SEC: %w", err)
	}

	cfg.CloseGapMs, err = getEnvInt("CLOSE_GAP_MS", 800)
	if err != nil {
		return Config{}, fmt.Errorf("parse CLOSE_GAP_MS: %w", err)
	}

	cfg.SegmentReviewEnabled, err = getEnvBool("SEGMENT_REVIEW_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse SEGMENT_REVIEW_ENABLED: %w", err)
	}
	cfg.SegmentReviewModel = getEnv("SEGMENT_REVIEW_MODEL", "")
	cfg.SegmentReviewUseTools, err = getEnvBool("SEGMENT_REVIEW_USE_TOOLS", false)
	if err != nil {
		return Config{}, fmt.Errorf("parse SEGMENT_REVIEW_USE_TOOLS: %w", err)
	}

	cfg.JudgeModel = getEnv("JUDGE_MODEL", "")
	cfg.JudgeObserveOnly, err = getEnvBool("JUDGE_OBSERVE_ONLY", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse JUDGE_OBSERVE_ONLY: %w", err)
	}
	cfg.JudgeBackfillOnStart, err = getEnvBool("JUDGE_BACKFILL_ON_START", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse JUDGE_BACKFILL_ON_START: %w", err)
	}
	cfg.JudgeBackfillLimit, err = getEnvInt("JUDGE_BACKFILL_LIMIT", 500)
	if err != nil {
		return Config{}, fmt.Errorf("parse JUDGE_BACKFILL_LIMIT: %w", err)
	}

	cfg.ChapterJudgeModel = getEnv("CHAPTER_JUDGE_MODEL", "kimi-k2.5")
	cfg.ChapterJudgeObserveOnly, err = getEnvBool("CHAPTER_JUDGE_OBSERVE_ONLY", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTER_JUDGE_OBSERVE_ONLY: %w", err)
	}

	cfg.GlossaryModel = getEnv("GLOSSARY_MODEL", "")
	cfg.GlossaryEnabled, err = getEnvBool("GLOSSARY_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse GLOSSARY_ENABLED: %w", err)
	}

	cfg.ChapterReviewModel = getEnv("CHAPTER_REVIEW_MODEL", "")
	cfg.ChapterReviewLLMEnabled, err = getEnvBool("CHAPTER_REVIEW_LLM_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTER_REVIEW_LLM_ENABLED: %w", err)
	}

	cfg.ChapterizeEnabled, err = getEnvBool("CHAPTERIZE_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_ENABLED: %w", err)
	}
	cfg.ChapterizeMinChapterMs, err = getEnvInt64("CHAPTERIZE_MIN_CHAPTER_MS", 18*60*1000)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_MIN_CHAPTER_MS: %w", err)
	}
	cfg.ChapterizeTargetChapterMs, err = getEnvInt64("CHAPTERIZE_TARGET_CHAPTER_MS", 22*60*1000)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_TARGET_CHAPTER_MS: %w", err)
	}
	cfg.ChapterizeMaxChapterMs, err = getEnvInt64("CHAPTERIZE_MAX_CHAPTER_MS", 30*60*1000)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_MAX_CHAPTER_MS: %w", err)
	}
	cfg.ChapterizeMinSilenceGapMs, err = getEnvInt64("CHAPTERIZE_MIN_SILENCE_GAP_MS", 800)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_MIN_SILENCE_GAP_MS: %w", err)
	}

	cfg.ChapterizeLLMDriven, err = getEnvBool("CHAPTERIZE_LLM_DRIVEN", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_LLM_DRIVEN: %w", err)
	}
	cfg.ChapterizeHardMaxMs, err = getEnvInt64("CHAPTERIZE_HARD_MAX_MS", 45*60*1000)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_HARD_MAX_MS: %w", err)
	}
	cfg.ChapterizeHardMinMs, err = getEnvInt64("CHAPTERIZE_HARD_MIN_MS", 5*60*1000)
	if err != nil {
		return Config{}, fmt.Errorf("parse CHAPTERIZE_HARD_MIN_MS: %w", err)
	}
	if cfg.ChapterizeHardMinMs >= cfg.ChapterizeHardMaxMs {
		return Config{}, fmt.Errorf("CHAPTERIZE_HARD_MIN_MS (%d) must be < CHAPTERIZE_HARD_MAX_MS (%d)",
			cfg.ChapterizeHardMinMs, cfg.ChapterizeHardMaxMs)
	}

	cfg.LoudnormTargetI, err = getEnvFloat("LOUDNORM_TARGET_I", -23.0)
	if err != nil {
		return Config{}, fmt.Errorf("parse LOUDNORM_TARGET_I: %w", err)
	}
	cfg.LoudnormTargetTP, err = getEnvFloat("LOUDNORM_TARGET_TP", -1.0)
	if err != nil {
		return Config{}, fmt.Errorf("parse LOUDNORM_TARGET_TP: %w", err)
	}
	cfg.LoudnormTargetLRA, err = getEnvFloat("LOUDNORM_TARGET_LRA", 7.0)
	if err != nil {
		return Config{}, fmt.Errorf("parse LOUDNORM_TARGET_LRA: %w", err)
	}
	cfg.LoudnormChapterEnabled, err = getEnvBool("LOUDNORM_CHAPTER_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse LOUDNORM_CHAPTER_ENABLED: %w", err)
	}
	cfg.LoudnormMasterEnabled, err = getEnvBool("LOUDNORM_MASTER_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse LOUDNORM_MASTER_ENABLED: %w", err)
	}
	cfg.EpisodeMergeEnabled, err = getEnvBool("EPISODE_MERGE_ENABLED", true)
	if err != nil {
		return Config{}, fmt.Errorf("parse EPISODE_MERGE_ENABLED: %w", err)
	}

	cfg.TTSConcurrency, err = getEnvInt("TTS_CONCURRENCY", 2)
	if err != nil {
		return Config{}, fmt.Errorf("parse TTS_CONCURRENCY: %w", err)
	}
	if cfg.TTSConcurrency < 1 {
		cfg.TTSConcurrency = 1
	}

	cfg.WorkerMetricsAddr = getEnv("WORKER_METRICS_ADDR", ":8081")

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

	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}

	return cfg, nil
}

// IsProduction reports whether the loaded environment is production-like
// (APP_ENV=production or APP_ENV=prod, case-insensitive).
func (c Config) IsProduction() bool {
	switch strings.ToLower(strings.TrimSpace(c.Environment)) {
	case "production", "prod":
		return true
	default:
		return false
	}
}

// Validate enforces invariants that must hold before any service starts.
// Production deployments without an API token are refused outright so we
// never silently accept unauthenticated traffic on the public Internet.
func (c Config) Validate() error {
	if c.IsProduction() && strings.TrimSpace(c.APIAuthToken) == "" {
		return fmt.Errorf(
			"APP_ENV=%s requires API_AUTH_TOKEN to be set; "+
				"generate one with `openssl rand -hex 32` and place it in .env",
			c.Environment,
		)
	}
	if c.DataRoot == "" {
		return fmt.Errorf("DATA_ROOT must not be empty")
	}
	if c.MaxJobRetries < 0 {
		return fmt.Errorf("MAX_JOB_RETRIES must be >= 0, got %d", c.MaxJobRetries)
	}
	return nil
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

// getEnvInt64 parses a 64-bit integer from the named env var, falling back
// to the supplied default when the var is unset or empty. Used by OPT-403's
// CHAPTERIZE_*_MS knobs whose minute-scale defaults overflow int32 on
// 32-bit builds (theoretical, but cheap to be correct).
func getEnvInt64(key string, fallback int64) (int64, error) {
	value := getEnv(key, strconv.FormatInt(fallback, 10))
	parsed, err := strconv.ParseInt(value, 10, 64)
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
