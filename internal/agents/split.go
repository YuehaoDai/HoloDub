package agents

import (
	"math"
	"strings"
	"unicode"
)

// SplitDecision captures the result of a Split() call. Empty Children
// (or a single child equal to the input) means "splittable point not
// found — caller should NOT split this segment".
//
// All offsets are in runes (Unicode code points), not bytes — the
// pipeline carries source text as Go strings (UTF-8) but the LLM /
// TTS pipeline thinks in characters. Mixing bytes and runes here would
// produce off-by-one mid-codepoint splits on Japanese / Chinese text,
// which is the entire point of the algorithm.
type SplitDecision struct {
	Children []SplitChild
	// Reason is a low-cardinality label for slog / Prometheus
	// observability (e.g. "punctuation_split", "silence_gap_split",
	// "no_candidate_found"). Always populated.
	Reason string
}

// SplitChild is one piece of a parent segment after splitting. The
// caller computes EndMs / StartMs from the parent's slot and the
// suggested rune offset (charBoundary) by proportional allocation —
// the algorithm here only returns *where* to cut, not *when*.
type SplitChild struct {
	// Text is the source text for this child (parent text sliced at
	// the chosen boundary). Whitespace at the split point is trimmed.
	Text string

	// CharOffset is the start of this child in the parent's source
	// text, measured in runes from the start. 0 for the first child.
	CharOffset int
}

// SplitSourceText is the OPT-407-followup-1 pure split algorithm. It
// looks for a natural break point in `text` and returns a SplitDecision
// describing how to slice it.
//
// Heuristics (first match wins):
//
//  1. Sentence-ending punctuation closest to the midpoint
//     (. ! ? 。 ！ ？ ， 、 …).
//  2. Strong intra-sentence pause closest to the midpoint
//     (, ; : 、 ；).
//  3. Whitespace closest to the midpoint (for unpunctuated text).
//
// Each heuristic only accepts a candidate that splits the text into
// pieces of at least minLenChars runes each. Sub-minimum candidates
// are skipped and the next heuristic is tried; if none qualifies the
// returned SplitDecision has empty Children (the caller should give
// up and either keep the segment or escalate to operator review).
//
// minLenChars is the smallest acceptable piece. Passing 0 falls back
// to a conservative default of 5 — below that the resulting TTS slot
// is too short to ever survive a retranslate.
func SplitSourceText(text string, minLenChars int) SplitDecision {
	if minLenChars <= 0 {
		minLenChars = 5
	}

	runes := []rune(text)
	n := len(runes)
	if n < 2*minLenChars {
		return SplitDecision{Reason: "text_too_short_to_split"}
	}
	mid := n / 2

	sentenceEnders := runeSet([]rune{'.', '!', '?', '。', '！', '？', '…'})
	intraEnders := runeSet([]rune{',', ';', ':', '，', '、', '；', '：'})
	whitespacer := func(r rune) bool { return unicode.IsSpace(r) }

	for _, h := range []struct {
		match  func(r rune) bool
		reason string
	}{
		{func(r rune) bool { _, ok := sentenceEnders[r]; return ok }, "punctuation_split"},
		{func(r rune) bool { _, ok := intraEnders[r]; return ok }, "intra_sentence_split"},
		{whitespacer, "whitespace_split"},
	} {
		idx := findNearestMatch(runes, mid, h.match, minLenChars)
		if idx < 0 {
			continue
		}
		return SplitDecision{
			Children: buildChildren(runes, idx),
			Reason:   h.reason,
		}
	}
	return SplitDecision{Reason: "no_candidate_found"}
}

// findNearestMatch scans outward from `pivot` and returns the index of
// the rune closest to pivot that satisfies `match` AND leaves at least
// `minLen` runes on EACH side (so the resulting children all clear the
// minimum length bar). Returns -1 when no candidate is found.
//
// "Outward" means we check pivot itself, then pivot-1, pivot+1,
// pivot-2, pivot+2, …, stopping at the first hit. This gives the
// nicest balance for the resulting children without sorting / scoring
// every match in the slice.
func findNearestMatch(runes []rune, pivot int, match func(rune) bool, minLen int) int {
	n := len(runes)
	maxRadius := int(math.Max(float64(pivot), float64(n-pivot)))
	for radius := 0; radius <= maxRadius; radius++ {
		left := pivot - radius
		right := pivot + radius
		// Try the left side first when both are available; preserves a
		// slight left bias which empirically matches how humans split
		// sentences (we tend to break after the punctuation we just
		// finished reading).
		if left >= 0 && match(runes[left]) {
			if left >= minLen && (n-left-1) >= minLen {
				return left
			}
		}
		if right < n && right != left && match(runes[right]) {
			if right >= minLen && (n-right-1) >= minLen {
				return right
			}
		}
	}
	return -1
}

// buildChildren slices `runes` at boundaryIdx (the index of the
// punctuation / whitespace rune itself), placing that rune at the END
// of the FIRST child so the punctuation stays with the sentence it
// belongs to. The second child starts at boundaryIdx+1, with any
// leading whitespace trimmed.
func buildChildren(runes []rune, boundaryIdx int) []SplitChild {
	firstEnd := boundaryIdx + 1
	if firstEnd > len(runes) {
		firstEnd = len(runes)
	}
	first := strings.TrimSpace(string(runes[:firstEnd]))

	secondStart := boundaryIdx + 1
	for secondStart < len(runes) && unicode.IsSpace(runes[secondStart]) {
		secondStart++
	}
	second := strings.TrimSpace(string(runes[secondStart:]))

	// If either piece is empty after trimming (e.g. trailing
	// punctuation at the start or end of the input), bail out: the
	// caller's SplitDecision check expects two non-empty pieces.
	if first == "" || second == "" {
		return nil
	}
	return []SplitChild{
		{Text: first, CharOffset: 0},
		{Text: second, CharOffset: secondStart},
	}
}

// runeSet builds a set-like map for fast rune membership tests. Used
// only at function entry; allocating per-call keeps the function
// stateless (and the map is tiny — < 20 entries).
func runeSet(items []rune) map[rune]struct{} {
	out := make(map[rune]struct{}, len(items))
	for _, r := range items {
		out[r] = struct{}{}
	}
	return out
}

// AllocateChildTimings divides the parent's [startMs, endMs] slot
// proportionally to each child's rune count. Returns matching slices
// of (startMs, endMs) — len(out_start) == len(out_end) == len(children).
// The last child's endMs is forced to parentEndMs so any rounding
// error never leaves a gap at the boundary.
//
// Why proportional to char count and not silence-based? At the time
// the agent decides to split it does NOT yet have a re-VAD'd audio of
// the parent slot — the cheap heuristic is "characters take time
// roughly proportionally". The re-translate + re-synth that follows
// will adjust within each child's slot via the normal SegmentAgent
// drift loop.
func AllocateChildTimings(children []SplitChild, parentStartMs, parentEndMs int64) (startMs, endMs []int64) {
	if len(children) == 0 || parentEndMs <= parentStartMs {
		return nil, nil
	}
	totalRunes := 0
	runesPerChild := make([]int, len(children))
	for i, c := range children {
		r := len([]rune(c.Text))
		runesPerChild[i] = r
		totalRunes += r
	}
	if totalRunes == 0 {
		return nil, nil
	}
	totalMs := parentEndMs - parentStartMs
	startMs = make([]int64, len(children))
	endMs = make([]int64, len(children))
	cursor := parentStartMs
	for i, r := range runesPerChild {
		startMs[i] = cursor
		var dur int64
		if i == len(children)-1 {
			dur = parentEndMs - cursor
		} else {
			dur = int64(math.Round(float64(totalMs) * float64(r) / float64(totalRunes)))
		}
		if dur < 1 {
			dur = 1
		}
		endMs[i] = startMs[i] + dur
		cursor = endMs[i]
	}
	// Force the last child's endMs to the parent's end (defensive
	// against rounding drift in the proportional split above).
	endMs[len(children)-1] = parentEndMs
	return startMs, endMs
}
