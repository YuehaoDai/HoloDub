// Package llm — OPT-407 LLM USD cost estimation.
//
// ComputeUSD turns the (input_tokens, output_tokens, cached_tokens) triplet
// already collected by ObserveLLMTokens into an estimated dollar amount via
// a hardcoded per-model price table (modelPrices below). The value drives
// two things:
//   1. holodub_llm_cost_usd_total Prometheus counter (cost dashboards).
//   2. The OPT-407 rework engine's per-episode cost ceiling — once a series
//      of retries on one episode burns more than EPISODE_REWORK_COST_CEILING_USD,
//      the engine emits ActionHaltCost and refuses further dispatch.
//
// The price table is intentionally:
//
//   - Hardcoded (NOT loaded from env) — operators rarely change it day-to-day,
//     and an env override would force every container to share a single source
//     of truth that is hard to keep in sync. OPT-407-followup-2 may add
//     MODEL_PRICE_OVERRIDE_JSON if a customer requests it; default is to
//     ship a yearly tracking PR.
//
//   - Conservatively HIGH — when in doubt we round up so the cost ceiling
//     halts an episode early rather than late. Drift between actual and
//     billed cost is acceptable; under-charging would defeat the safety net.
//
// All prices are USD per 1 million tokens, sourced from each provider's
// public pricing page (DashScope / Moonshot / DeepSeek). When a provider
// has multi-tier pricing (input vs output), we use the highest-volume tier
// applicable to our use case (long-context judge calls). Cached-token
// pricing applies to the subset of input tokens served from the provider's
// prefix cache (DeepSeek prompt_cache_hit_tokens / OpenAI cached_tokens).
package llm

// ModelPrice is the per-million-token USD cost for one model. All three
// fields use the same per-1M denominator so the math in ComputeUSD stays
// trivially auditable.
type ModelPrice struct {
	InputPer1M  float64 // input (non-cached) tokens, USD per 1M
	OutputPer1M float64 // output / completion tokens, USD per 1M
	CachedPer1M float64 // input tokens served from provider prefix cache, USD per 1M
}

// modelPrices is the canonical price table. Keep the map literal small and
// alphabetically sorted by model name for easy review. Any model name not
// in this map falls through to the unknownPrice fallback below.
//
// Last reviewed: 2026-05 — re-check at every quarter using each provider's
// public pricing page. Any change to this table SHOULD ship with a CHANGELOG
// entry under the OPT-407 cost-tracking notes.
var modelPrices = map[string]ModelPrice{
	// Alibaba DashScope (Qwen)
	"qwen-turbo":              {InputPer1M: 0.30, OutputPer1M: 0.60, CachedPer1M: 0.06},
	"qwen-plus":               {InputPer1M: 0.80, OutputPer1M: 2.00, CachedPer1M: 0.16},
	"qwen-plus-latest":        {InputPer1M: 0.80, OutputPer1M: 2.00, CachedPer1M: 0.16},
	"qwen-max":                {InputPer1M: 2.40, OutputPer1M: 9.60, CachedPer1M: 0.48},
	"qwen-max-latest":         {InputPer1M: 2.40, OutputPer1M: 9.60, CachedPer1M: 0.48},
	"qwen3-235b-a22b-thinking-2507": {InputPer1M: 2.00, OutputPer1M: 8.00, CachedPer1M: 0.40},

	// Moonshot (Kimi)
	"kimi-k2.5":               {InputPer1M: 0.60, OutputPer1M: 2.50, CachedPer1M: 0.15},
	"kimi-k2-thinking":        {InputPer1M: 1.20, OutputPer1M: 5.00, CachedPer1M: 0.30},

	// DeepSeek
	"deepseek-v3":             {InputPer1M: 0.27, OutputPer1M: 1.10, CachedPer1M: 0.07},
	"deepseek-chat":           {InputPer1M: 0.27, OutputPer1M: 1.10, CachedPer1M: 0.07},
	"deepseek-reasoner":       {InputPer1M: 0.55, OutputPer1M: 2.20, CachedPer1M: 0.14},
}

// unknownPrice is the fallback when a model name is not in modelPrices.
// Set deliberately on the high side so unknown / experimental models still
// charge the rework cost ceiling rather than silently bypass it. Operators
// who want accurate accounting for a new model MUST add it to modelPrices.
var unknownPrice = ModelPrice{
	InputPer1M:  2.00,
	OutputPer1M: 8.00,
	CachedPer1M: 0.40,
}

// ComputeUSD returns the estimated dollar cost of one LLM call given its
// raw token counts. inputTokens is the TOTAL prompt tokens (cached AND
// non-cached); cachedTokens is the subset served from prefix cache. The
// math splits inputTokens into (cached, non-cached) and prices each piece
// at the matching rate.
//
// Returns 0 (NOT an error) when:
//   - the model name is empty (unknown caller),
//   - all three counts are zero (caller forgot to record usage), or
//   - any count is negative (defensive: never let a malformed usage row
//     drive a NEGATIVE cost which would silently extend an over-budget
//     episode's ceiling).
//
// Never panics — observability code paths must stay reliable.
func ComputeUSD(model string, inputTokens, outputTokens, cachedTokens int) float64 {
	if model == "" {
		return 0
	}
	if inputTokens < 0 || outputTokens < 0 || cachedTokens < 0 {
		return 0
	}
	if inputTokens == 0 && outputTokens == 0 && cachedTokens == 0 {
		return 0
	}
	price, ok := modelPrices[model]
	if !ok {
		price = unknownPrice
	}
	// Cached tokens are a SUBSET of input tokens. Clamp so cached never
	// exceeds input — otherwise a buggy provider response could let
	// cached overflow and produce nonsense.
	if cachedTokens > inputTokens {
		cachedTokens = inputTokens
	}
	nonCached := inputTokens - cachedTokens
	const perMillion = 1_000_000.0
	return float64(nonCached)/perMillion*price.InputPer1M +
		float64(outputTokens)/perMillion*price.OutputPer1M +
		float64(cachedTokens)/perMillion*price.CachedPer1M
}
