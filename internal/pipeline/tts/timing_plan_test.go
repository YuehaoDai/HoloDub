package tts

import "testing"

func TestBuildTimingPlanFitsWithinHardWindow(t *testing.T) {
	plan := BuildTimingPlan(TimingPlanInput{
		Text:                 "abcdef",
		TargetLanguage:       "en",
		TargetSec:            0.5,
		HardMaxUnderDriftSec: 0.2,
		HardMaxOverDriftSec:  0.2,
	})
	if plan.Status != TimingPlanFits {
		t.Fatalf("status = %s, want %s", plan.Status, TimingPlanFits)
	}
	if plan.CharsPerSec != 12.0 {
		t.Fatalf("chars/sec = %v, want 12.0", plan.CharsPerSec)
	}
}

func TestBuildTimingPlanFlagsTooLongBeforeTTS(t *testing.T) {
	plan := BuildTimingPlan(TimingPlanInput{
		Text:                 "abcdefghijklmnopqrstuvwxyz",
		TargetLanguage:       "en",
		TargetSec:            1.0,
		HardMaxUnderDriftSec: 0.7,
		HardMaxOverDriftSec:  0.3,
	})
	if plan.Status != TimingPlanTooLong {
		t.Fatalf("status = %s, want %s", plan.Status, TimingPlanTooLong)
	}
	if plan.MaxChars != 15 {
		t.Fatalf("max chars = %d, want 15", plan.MaxChars)
	}
	if plan.CompressionRatio <= 1 {
		t.Fatalf("compression ratio = %.3f, want > 1", plan.CompressionRatio)
	}
}

func TestBuildTimingPlanFlagsRequiredRateTooFast(t *testing.T) {
	plan := BuildTimingPlan(TimingPlanInput{
		Text:                 "abcdefghijklmnop",
		TargetLanguage:       "en",
		TargetSec:            1.0,
		CharsPerSecHint:      10.0,
		Pacing:               "normal",
		HardMaxUnderDriftSec: 0.2,
		HardMaxOverDriftSec:  0.2,
	})
	if plan.RateStatus != RateTooFast {
		t.Fatalf("rate status = %s, want %s", plan.RateStatus, RateTooFast)
	}
	if plan.RequiredCharsPerSec != 16 {
		t.Fatalf("required cps = %v, want 16", plan.RequiredCharsPerSec)
	}
	if plan.RateRatio <= 1 {
		t.Fatalf("rate ratio = %.3f, want > 1", plan.RateRatio)
	}
}

func TestBuildTimingPlanFlagsRequiredRateTooSlow(t *testing.T) {
	plan := BuildTimingPlan(TimingPlanInput{
		Text:                 "abcd",
		TargetLanguage:       "en",
		TargetSec:            2.0,
		CharsPerSecHint:      10.0,
		Pacing:               "normal",
		HardMaxUnderDriftSec: 0.2,
		HardMaxOverDriftSec:  0.2,
	})
	if plan.RateStatus != RateTooSlow {
		t.Fatalf("rate status = %s, want %s", plan.RateStatus, RateTooSlow)
	}
	if plan.RequiredCharsPerSec != 2 {
		t.Fatalf("required cps = %v, want 2", plan.RequiredCharsPerSec)
	}
}

func TestNaturalRateWindowUsesPacingAndArousal(t *testing.T) {
	normalMin, normalMax := NaturalRateWindow(10, "normal", 0.5, true)
	fastMin, fastMax := NaturalRateWindow(10, "fast", 0.9, true)
	slowMin, slowMax := NaturalRateWindow(10, "slow", 0.1, true)

	if fastMax <= normalMax {
		t.Fatalf("fast max = %.3f, want > normal max %.3f", fastMax, normalMax)
	}
	if fastMin <= normalMin {
		t.Fatalf("fast min = %.3f, want > normal min %.3f", fastMin, normalMin)
	}
	if slowMax >= normalMax {
		t.Fatalf("slow max = %.3f, want < normal max %.3f", slowMax, normalMax)
	}
	if slowMin >= normalMin {
		t.Fatalf("slow min = %.3f, want < normal min %.3f", slowMin, normalMin)
	}
}

func TestBuildTimingPlanFlagsTooShortBeforeTTS(t *testing.T) {
	plan := BuildTimingPlan(TimingPlanInput{
		Text:                 "x",
		TargetLanguage:       "en",
		TargetSec:            2.0,
		HardMaxUnderDriftSec: 0.7,
		HardMaxOverDriftSec:  0.3,
	})
	if plan.Status != TimingPlanTooShort {
		t.Fatalf("status = %s, want %s", plan.Status, TimingPlanTooShort)
	}
	if plan.ExpansionRatio <= 1 {
		t.Fatalf("expansion ratio = %.3f, want > 1", plan.ExpansionRatio)
	}
}

func TestBuildTimingGroupsAllocatesTargetMs(t *testing.T) {
	groups := BuildTimingGroups(
		"this first breath group is long enough. this second breath group is also long enough? this third breath group is long enough too!",
		3000,
		8,
	)
	if len(groups) != 3 {
		t.Fatalf("len(groups) = %d, want 3", len(groups))
	}
	var sum int64
	for _, group := range groups {
		if group.Text == "" || group.Chars == 0 || group.TargetMs == 0 {
			t.Fatalf("bad group: %+v", group)
		}
		sum += group.TargetMs
	}
	if sum != 3000 {
		t.Fatalf("sum target ms = %d, want 3000", sum)
	}
}

func TestBuildTimingGroupsPacksToMaxGroups(t *testing.T) {
	groups := BuildTimingGroups(
		"first breath group is long enough. second breath group is long enough. third breath group is long enough. fourth breath group is long enough. fifth breath group is long enough. sixth breath group is long enough.",
		6000,
		3,
	)
	if len(groups) != 3 {
		t.Fatalf("len(groups) = %d, want 3", len(groups))
	}
	var sum int64
	for _, group := range groups {
		sum += group.TargetMs
	}
	if sum != 6000 {
		t.Fatalf("sum target ms = %d, want 6000", sum)
	}
}

func TestBuildTimingGroupsMergesTinyBreathGroups(t *testing.T) {
	groups := BuildTimingGroups(
		"short. tiny. this one is long enough to stand alone. another long enough group follows here.",
		5000,
		8,
	)
	if len(groups) != 2 {
		t.Fatalf("len(groups) = %d, want 2: %+v", len(groups), groups)
	}
	if groups[0].Chars < 32 {
		t.Fatalf("first group chars = %d, want merged group >= 32", groups[0].Chars)
	}
	var sum int64
	for _, group := range groups {
		sum += group.TargetMs
	}
	if sum != 5000 {
		t.Fatalf("sum target ms = %d, want 5000", sum)
	}
}
