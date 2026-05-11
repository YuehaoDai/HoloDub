package rework

// AccumulateCostUSD sums the CostUSDDelta over every entry in `history`.
// Used by Engine to compute the running cost ceiling input for Decide;
// kept as a free function so callers can also pre-compute / mock it in
// tests without spinning up a Store.
//
// MVP semantics: ALL prior dispatched attempts on this episode count
// toward the same ceiling. We do NOT subtract on failure — a retry that
// burned tokens but yielded a worse score still consumed budget.
//
// Future (OPT-407-followup-3): expose a per-tenant ledger so multi-tenant
// deployments can apportion the same physical model spend; for now the
// single-tenant assumption holds.
func AccumulateCostUSD(history []ReworkAttempt) float64 {
	var total float64
	for _, h := range history {
		if !h.Dispatched {
			// Skipped / observe-only attempts cost nothing.
			continue
		}
		if h.CostUSDDelta < 0 {
			// Defensive: never let a malformed entry drive the total
			// below zero (would silently extend an over-budget episode).
			continue
		}
		total += h.CostUSDDelta
	}
	return total
}

// EstimateRetryCostUSD returns a conservative upper-bound estimate of how
// much one rework Action will cost in LLM tokens. Used to charge against
// the per-episode ceiling at dispatch time so the next decision sees the
// committed spend even before the actual retry finishes.
//
// Numbers are deliberately rough — they are NOT used for billing, only
// for the ceiling check. We err on the side of over-charging so the
// engine halts a runaway episode early rather than late. Refined estimates
// can land in OPT-407-followup-1 once we have empirical retry-cost data.
func EstimateRetryCostUSD(action Action) float64 {
	switch action.Type {
	case ActionSegmentRetry:
		// One retranslate (~600 tokens in/out) + one TTS call.
		// Assume kimi-k2.5 at $0.001/1k input + $0.0025/1k output
		// → ~$0.003 per retry. TTS is local, no token cost.
		return 0.003 * float64(max(len(action.SegmentIDs), 1))
	case ActionEscalateToThinking:
		// Thinking models are ~3-5× cost; budget $0.015 per escalation.
		return 0.015
	case ActionReviseWeakestSegments:
		// Top-3 segments × translate + TTS. Each translate is ~$0.003,
		// TTS is local; top-3 cap means at most $0.01 per chapter round.
		return 0.003 * float64(max(len(action.SegmentIDs), 1))
	case ActionBroadcastGlossary:
		// One ExtractEpisodeGlossary call (~5k input, ~1k output)
		// → ~$0.01. Plus retranslate of affected segments — but each
		// retranslate has its own retry attempt that will charge itself.
		// We only charge the glossary call here.
		return 0.01
	case ActionSegmentSplit, ActionAcceptWithBorrow,
		ActionEscalateChapter, ActionEscalateHumanReview,
		ActionEscalateOscillation, ActionHaltCost, ActionNoop:
		// No external LLM cost — pure metadata operations.
		return 0
	}
	return 0
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}
