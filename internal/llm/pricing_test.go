package llm

import (
	"math"
	"testing"
)

// TestComputeUSD_KnownModel verifies the math against a hand-computed
// reference for one model in modelPrices. Catches accidental table edits.
func TestComputeUSD_KnownModel(t *testing.T) {
	// kimi-k2.5: input 0.60, output 2.50, cached 0.15 (USD per 1M tokens).
	// 1000 input (200 cached, 800 non-cached) + 200 output:
	//   non-cached:   800 / 1e6 * 0.60 = 0.00048
	//   output:       200 / 1e6 * 2.50 = 0.00050
	//   cached:       200 / 1e6 * 0.15 = 0.00003
	//   total:        0.00101
	got := ComputeUSD("kimi-k2.5", 1000, 200, 200)
	want := 0.00101
	if math.Abs(got-want) > 1e-9 {
		t.Fatalf("ComputeUSD want %v, got %v", want, got)
	}
}

// TestComputeUSD_UnknownModelFallback: an unknown model name uses the
// conservatively-high fallback price so the rework cost ceiling still
// guards against runaway spend.
func TestComputeUSD_UnknownModelFallback(t *testing.T) {
	// unknownPrice: input 2.00, output 8.00, cached 0.40.
	// 1M input, 0 cached, 0 output → exactly 2.00 USD.
	got := ComputeUSD("future-model-xyz", 1_000_000, 0, 0)
	if math.Abs(got-2.0) > 1e-9 {
		t.Fatalf("unknown model should fall back to high price, got %v", got)
	}
}

// TestComputeUSD_ZeroAndNegative: any zero / empty input is dropped to 0;
// negative counts also collapse to 0 (defensive — a malformed usage row
// must NOT extend an over-budget episode).
func TestComputeUSD_ZeroAndNegative(t *testing.T) {
	cases := []struct {
		name                              string
		model                             string
		input, output, cached             int
	}{
		{"all_zero", "kimi-k2.5", 0, 0, 0},
		{"empty_model", "", 100, 50, 0},
		{"negative_input", "kimi-k2.5", -1, 50, 0},
		{"negative_output", "kimi-k2.5", 100, -1, 0},
		{"negative_cached", "kimi-k2.5", 100, 50, -10},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := ComputeUSD(c.model, c.input, c.output, c.cached)
			if got != 0 {
				t.Fatalf("expected 0 for %s, got %v", c.name, got)
			}
		})
	}
}

// TestComputeUSD_CachedClamped: cached must NEVER exceed input (defence
// against a buggy provider response). The math should treat the overflow
// as if cachedTokens == inputTokens, NOT panic / produce nonsense.
func TestComputeUSD_CachedClamped(t *testing.T) {
	// kimi-k2.5: 100 input but provider claims 500 cached → treat as 100
	// cached, 0 non-cached, 0 output → 100/1e6 * 0.15 = 0.000015.
	got := ComputeUSD("kimi-k2.5", 100, 0, 500)
	want := 100.0 / 1_000_000.0 * 0.15
	if math.Abs(got-want) > 1e-12 {
		t.Fatalf("cached overflow should clamp to input, want %v got %v", want, got)
	}
}

// TestComputeUSD_OutputOnly: an output-only call (rare but possible if a
// provider drops prompt accounting) still returns a positive cost.
func TestComputeUSD_OutputOnly(t *testing.T) {
	got := ComputeUSD("qwen-turbo", 0, 1000, 0)
	want := 1000.0 / 1_000_000.0 * 0.60 // qwen-turbo OutputPer1M = 0.60
	if math.Abs(got-want) > 1e-12 {
		t.Fatalf("want %v, got %v", want, got)
	}
}

// TestComputeUSD_PriceTableFreshness is a smoke check that ALL the models
// HoloDub uses in production exist in modelPrices. If an operator adds a
// new model to the env (RETRANSLATION_MODEL, JUDGE_MODEL, ...) and forgets
// to update the price table, this test catches it.
func TestComputeUSD_PriceTableFreshness(t *testing.T) {
	productionModels := []string{
		"qwen-turbo", "qwen-plus-latest", "qwen-max",
		"kimi-k2.5", "kimi-k2-thinking",
		"deepseek-v3",
		"qwen3-235b-a22b-thinking-2507",
	}
	for _, m := range productionModels {
		if _, ok := modelPrices[m]; !ok {
			t.Errorf("production model %q is missing from modelPrices — add a row", m)
		}
	}
}
