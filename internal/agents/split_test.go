package agents

import (
	"strings"
	"testing"
)

func TestSplitSourceText_Sentence(t *testing.T) {
	d := SplitSourceText("Hello world. This is a test sentence.", 5)
	if len(d.Children) != 2 {
		t.Fatalf("expected 2 children, got %+v", d)
	}
	if d.Children[0].Text != "Hello world." {
		t.Errorf("first child want 'Hello world.', got %q", d.Children[0].Text)
	}
	if d.Children[1].Text != "This is a test sentence." {
		t.Errorf("second child want 'This is a test sentence.', got %q", d.Children[1].Text)
	}
	if d.Reason != "punctuation_split" {
		t.Errorf("Reason: want punctuation_split, got %s", d.Reason)
	}
}

func TestSplitSourceText_ChineseFullWidthPunctuation(t *testing.T) {
	d := SplitSourceText("我爱中国，这里有美丽的风景。", 3)
	if len(d.Children) != 2 {
		t.Fatalf("expected 2 children, got %+v", d)
	}
	if !strings.HasSuffix(d.Children[0].Text, "，") && !strings.HasSuffix(d.Children[0].Text, "。") {
		t.Errorf("first child should end with full-width punctuation, got %q", d.Children[0].Text)
	}
}

func TestSplitSourceText_NoPunctuationUsesWhitespace(t *testing.T) {
	d := SplitSourceText("alpha beta gamma delta", 3)
	if len(d.Children) != 2 {
		t.Fatalf("expected 2 children, got %+v", d)
	}
	if d.Reason != "whitespace_split" {
		t.Errorf("Reason: want whitespace_split, got %s", d.Reason)
	}
}

func TestSplitSourceText_TooShortReturnsEmpty(t *testing.T) {
	d := SplitSourceText("hi.", 5)
	if len(d.Children) != 0 {
		t.Errorf("expected no split on short text, got %+v", d)
	}
	if d.Reason != "text_too_short_to_split" {
		t.Errorf("Reason: want text_too_short_to_split, got %s", d.Reason)
	}
}

func TestSplitSourceText_PunctuationAtEdge(t *testing.T) {
	// Punctuation exists but is too close to edge; should fall through
	// to whitespace split, NOT return punctuation_split with a
	// 1-character first child.
	d := SplitSourceText("a, this is a long enough piece of text to need splitting", 5)
	if len(d.Children) != 2 {
		t.Fatalf("expected 2 children, got %+v", d)
	}
	if d.Reason == "punctuation_split" {
		// punctuation match is acceptable IF the children still
		// satisfy min length; check that they do.
		if len([]rune(d.Children[0].Text)) < 5 {
			t.Errorf("first child too short after punctuation split: %q", d.Children[0].Text)
		}
		if len([]rune(d.Children[1].Text)) < 5 {
			t.Errorf("second child too short after punctuation split: %q", d.Children[1].Text)
		}
	}
}

func TestSplitSourceText_IntraSentenceFallback(t *testing.T) {
	// No sentence-ending punctuation, but commas — should pick intra.
	d := SplitSourceText("alpha is one item, beta is another, gamma is third", 4)
	if len(d.Children) != 2 {
		t.Fatalf("expected 2 children, got %+v", d)
	}
	if d.Reason != "intra_sentence_split" && d.Reason != "punctuation_split" {
		t.Errorf("Reason: want intra_sentence_split or punctuation_split, got %s", d.Reason)
	}
}

func TestSplitSourceText_NoCandidate(t *testing.T) {
	// Continuous Japanese kana run with no punctuation or whitespace.
	// Our heuristics should fail to split this (no break candidate).
	d := SplitSourceText("ありがとうございます", 5)
	if len(d.Children) != 0 {
		t.Errorf("expected no split on continuous run, got %+v", d)
	}
}

func TestSplitSourceText_BiasTowardMidpoint(t *testing.T) {
	// Two candidate periods: one near start, one near middle. The
	// algorithm should prefer the one closest to mid.
	d := SplitSourceText("one. two three four five six seven eight. nine ten eleven twelve", 5)
	if len(d.Children) != 2 {
		t.Fatalf("expected 2 children, got %+v", d)
	}
	// The midpoint of the 64-char input is ~32; the second period sits
	// at position ~37, the first at position ~3. We want the second.
	if strings.HasPrefix(d.Children[0].Text, "one.") && !strings.Contains(d.Children[0].Text, "eight.") {
		t.Errorf("split picked first period instead of midpoint period: %+v", d)
	}
}

func TestAllocateChildTimings_Proportional(t *testing.T) {
	children := []SplitChild{
		{Text: "aaaa", CharOffset: 0},
		{Text: "bb", CharOffset: 4},
	}
	startMs, endMs := AllocateChildTimings(children, 0, 6000)
	if len(startMs) != 2 || len(endMs) != 2 {
		t.Fatalf("expected len 2, got %d %d", len(startMs), len(endMs))
	}
	if startMs[0] != 0 {
		t.Errorf("first start: want 0, got %d", startMs[0])
	}
	// 4 of 6 runes → ~4000ms
	if endMs[0] < 3500 || endMs[0] > 4500 {
		t.Errorf("first end (proportional): want ~4000, got %d", endMs[0])
	}
	if endMs[1] != 6000 {
		t.Errorf("last end must equal parent end, got %d", endMs[1])
	}
}

func TestAllocateChildTimings_EmptyOrZero(t *testing.T) {
	s, e := AllocateChildTimings(nil, 0, 1000)
	if s != nil || e != nil {
		t.Errorf("nil children should return nil, got %v %v", s, e)
	}
	s, e = AllocateChildTimings([]SplitChild{{Text: "x"}}, 1000, 1000)
	if s != nil || e != nil {
		t.Errorf("zero-duration parent should return nil, got %v %v", s, e)
	}
}
