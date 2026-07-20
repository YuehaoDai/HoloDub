package tts

import (
	"math"
	"strings"
)

type TimingPlanStatus string

const (
	TimingPlanFits     TimingPlanStatus = "fits"
	TimingPlanTooLong  TimingPlanStatus = "too_long"
	TimingPlanTooShort TimingPlanStatus = "too_short"
)

type RateStatus string

const (
	RateNatural RateStatus = "natural"
	RateTooFast RateStatus = "too_fast"
	RateTooSlow RateStatus = "too_slow"
)

const DefaultTimingPlanMaxGroups = 8

type TimingPlanInput struct {
	Text                 string
	TargetLanguage       string
	TargetSec            float64
	CharsPerSecHint      float64
	Pacing               string
	EmotionArousal       float64
	HasEmotionArousal    bool
	HardMaxUnderDriftSec float64
	HardMaxOverDriftSec  float64
	MaxGroups            int
}

type TimingGroup struct {
	Text     string `json:"text"`
	Chars    int    `json:"chars"`
	TargetMs int64  `json:"target_ms"`
}

type TimingPlan struct {
	TargetMs              int64            `json:"target_ms"`
	PredictedMs           int64            `json:"predicted_ms"`
	MinAllowedMs          int64            `json:"min_allowed_ms"`
	MaxAllowedMs          int64            `json:"max_allowed_ms"`
	Chars                 int              `json:"chars"`
	MinChars              int              `json:"min_chars"`
	MaxChars              int              `json:"max_chars"`
	CharsPerSec           float64          `json:"chars_per_sec"`
	RequiredCharsPerSec   float64          `json:"required_chars_per_sec"`
	RateRatio             float64          `json:"rate_ratio"`
	MinNaturalCharsPerSec float64          `json:"min_natural_chars_per_sec"`
	MaxNaturalCharsPerSec float64          `json:"max_natural_chars_per_sec"`
	RateStatus            RateStatus       `json:"rate_status"`
	RateRisk              string           `json:"rate_risk,omitempty"`
	Status                TimingPlanStatus `json:"status"`
	Risk                  string           `json:"risk,omitempty"`
	CompressionRatio      float64          `json:"compression_ratio,omitempty"`
	ExpansionRatio        float64          `json:"expansion_ratio,omitempty"`
	Groups                []TimingGroup    `json:"groups,omitempty"`
}

func DefaultCharsPerSec(lang string) float64 {
	switch strings.ToLower(strings.TrimSpace(lang)) {
	case "zh", "zh-cn", "zh-tw":
		return 4.0
	case "ja":
		return 4.5
	case "ko":
		return 3.8
	case "en":
		return 12.0
	default:
		return 10.0
	}
}

func BuildTimingPlan(in TimingPlanInput) TimingPlan {
	targetSec := in.TargetSec
	if targetSec < 0 {
		targetSec = 0
	}
	targetMs := int64(math.Round(targetSec * 1000))

	cps := in.CharsPerSecHint
	if cps <= 0 {
		cps = DefaultCharsPerSec(in.TargetLanguage)
	}
	if cps <= 0 {
		cps = 10.0
	}

	underCap := in.HardMaxUnderDriftSec
	if underCap < 0 {
		underCap = 0
	}
	overCap := in.HardMaxOverDriftSec
	if overCap < 0 {
		overCap = 0
	}
	minSec := targetSec - underCap
	if minSec < 0 {
		minSec = 0
	}
	maxSec := targetSec + overCap

	chars := CountNonWhitespaceRunes(in.Text)
	predictedSec := float64(chars) / cps
	predictedMs := int64(math.Round(predictedSec * 1000))
	requiredCPS := 0.0
	if targetSec > 0 {
		requiredCPS = float64(chars) / targetSec
	}
	minNaturalCPS, maxNaturalCPS := NaturalRateWindow(cps, in.Pacing, in.EmotionArousal, in.HasEmotionArousal)
	rateStatus := RateNatural
	var rateRisk string
	switch {
	case requiredCPS > maxNaturalCPS:
		rateStatus = RateTooFast
		rateRisk = "required_speech_rate_above_natural_window"
	case requiredCPS > 0 && requiredCPS < minNaturalCPS:
		rateStatus = RateTooSlow
		rateRisk = "required_speech_rate_below_natural_window"
	}
	rateRatio := 0.0
	if cps > 0 {
		rateRatio = requiredCPS / cps
	}
	minAllowedMs := int64(math.Round(minSec * 1000))
	maxAllowedMs := int64(math.Round(maxSec * 1000))
	minChars := int(math.Ceil(minSec * cps))
	maxChars := int(math.Floor(maxSec * cps))
	if minChars < 0 {
		minChars = 0
	}
	if maxChars < 1 {
		maxChars = 1
	}

	status := TimingPlanFits
	var risk string
	var compressionRatio float64
	var expansionRatio float64
	switch {
	case targetSec <= 0:
		status = TimingPlanTooLong
		risk = "zero_target_duration"
	case predictedSec > maxSec:
		status = TimingPlanTooLong
		risk = "text_exceeds_pre_tts_budget"
		compressionRatio = float64(chars) / float64(maxChars)
	case predictedSec < minSec:
		status = TimingPlanTooShort
		risk = "text_under_fills_slot"
		if chars > 0 {
			expansionRatio = float64(minChars) / float64(chars)
		}
	}

	maxGroups := in.MaxGroups
	if maxGroups == 0 {
		maxGroups = DefaultTimingPlanMaxGroups
	}

	return TimingPlan{
		TargetMs:              targetMs,
		PredictedMs:           predictedMs,
		MinAllowedMs:          minAllowedMs,
		MaxAllowedMs:          maxAllowedMs,
		Chars:                 chars,
		MinChars:              minChars,
		MaxChars:              maxChars,
		CharsPerSec:           cps,
		RequiredCharsPerSec:   round3(requiredCPS),
		RateRatio:             round3(rateRatio),
		MinNaturalCharsPerSec: round3(minNaturalCPS),
		MaxNaturalCharsPerSec: round3(maxNaturalCPS),
		RateStatus:            rateStatus,
		RateRisk:              rateRisk,
		Status:                status,
		Risk:                  risk,
		CompressionRatio:      round3(compressionRatio),
		ExpansionRatio:        round3(expansionRatio),
		Groups:                BuildTimingGroups(in.Text, targetMs, maxGroups),
	}
}

func NaturalRateWindow(baseCPS float64, pacing string, arousal float64, hasArousal bool) (float64, float64) {
	if baseCPS <= 0 {
		baseCPS = 10.0
	}
	minMult, maxMult := 0.82, 1.18
	switch strings.ToLower(strings.TrimSpace(pacing)) {
	case "slow":
		minMult, maxMult = 0.70, 1.05
	case "fast":
		minMult, maxMult = 0.95, 1.35
	}
	if hasArousal {
		if arousal < 0 {
			arousal = 0
		}
		if arousal > 1 {
			arousal = 1
		}
		delta := arousal - 0.5
		minMult *= 1 + 0.08*delta
		maxMult *= 1 + 0.16*delta
	}
	minRate := baseCPS * minMult
	maxRate := baseCPS * maxMult
	if minRate < 0.3 {
		minRate = 0.3
	}
	if maxRate < minRate {
		maxRate = minRate
	}
	return minRate, maxRate
}

func BuildTimingGroups(text string, targetMs int64, maxGroups int) []TimingGroup {
	raw := splitBreathGroups(text)
	if len(raw) == 0 {
		return nil
	}
	raw = packShortTimingGroups(raw, 32)
	if maxGroups > 0 && len(raw) > maxGroups {
		raw = packTimingGroups(raw, maxGroups)
	}

	totalChars := 0
	for _, group := range raw {
		totalChars += CountNonWhitespaceRunes(group)
	}
	if totalChars <= 0 {
		return nil
	}

	out := make([]TimingGroup, 0, len(raw))
	var assigned int64
	for i, group := range raw {
		chars := CountNonWhitespaceRunes(group)
		groupMs := int64(math.Round(float64(targetMs) * float64(chars) / float64(totalChars)))
		if i == len(raw)-1 {
			groupMs = targetMs - assigned
		}
		if groupMs < 0 {
			groupMs = 0
		}
		assigned += groupMs
		out = append(out, TimingGroup{
			Text:     group,
			Chars:    chars,
			TargetMs: groupMs,
		})
	}
	return out
}

func splitBreathGroups(text string) []string {
	text = strings.TrimSpace(text)
	if text == "" {
		return nil
	}
	var groups []string
	var current strings.Builder
	for _, r := range text {
		current.WriteRune(r)
		if isTimingBoundary(r) {
			if piece := strings.TrimSpace(current.String()); piece != "" {
				groups = append(groups, piece)
			}
			current.Reset()
		}
	}
	if piece := strings.TrimSpace(current.String()); piece != "" {
		groups = append(groups, piece)
	}
	return groups
}

func isTimingBoundary(r rune) bool {
	switch r {
	case '.', '!', '?', ';', ':', '\n',
		'\u3002', '\uff01', '\uff1f', '\uff1b', '\uff1a':
		return true
	default:
		return false
	}
}

func packTimingGroups(groups []string, maxGroups int) []string {
	if maxGroups <= 0 || len(groups) <= maxGroups {
		return groups
	}
	totalChars := 0
	for _, group := range groups {
		totalChars += CountNonWhitespaceRunes(group)
	}
	targetChars := int(math.Ceil(float64(totalChars) / float64(maxGroups)))
	if targetChars < 1 {
		targetChars = 1
	}

	packed := make([]string, 0, maxGroups)
	var current strings.Builder
	currentChars := 0
	for _, group := range groups {
		if current.Len() > 0 && currentChars >= targetChars && len(packed) < maxGroups-1 {
			packed = append(packed, current.String())
			current.Reset()
			currentChars = 0
		}
		current.WriteString(group)
		currentChars += CountNonWhitespaceRunes(group)
	}
	if current.Len() > 0 {
		packed = append(packed, current.String())
	}
	return packed
}

func packShortTimingGroups(groups []string, minChars int) []string {
	if minChars <= 0 || len(groups) <= 1 {
		return groups
	}
	packed := make([]string, 0, len(groups))
	var current strings.Builder
	currentChars := 0
	flush := func() {
		if current.Len() == 0 {
			return
		}
		packed = append(packed, current.String())
		current.Reset()
		currentChars = 0
	}
	for _, group := range groups {
		current.WriteString(group)
		currentChars += CountNonWhitespaceRunes(group)
		if currentChars >= minChars {
			flush()
		}
	}
	flush()
	return packed
}

func round3(v float64) float64 {
	if v == 0 {
		return 0
	}
	return math.Round(v*1000) / 1000
}
