package tts

import (
	"strconv"
	"strings"
	"unicode"
)

// SpokenTextRewrite records one deterministic text rewrite that is applied
// only to the text sent to TTS. Display subtitles keep the original text.
type SpokenTextRewrite struct {
	Rule        string `json:"rule"`
	Original    string `json:"original"`
	Replacement string `json:"replacement"`
	StartRune   int    `json:"start_rune"`
	EndRune     int    `json:"end_rune"`
}

type SpokenTextResult struct {
	Text     string
	Rewrites []SpokenTextRewrite
}

func (r SpokenTextResult) Changed() bool {
	return len(r.Rewrites) > 0 && r.Text != ""
}

// NormalizeSpokenText converts display-safe technical shorthand into a form
// that Mandarin TTS reads predictably. It deliberately keeps the scope narrow:
// S1/S2/S3 style Raft server labels are verbalized, while ordinary English
// technical terms such as RPC, Raft, AppendEntries, InstallSnapshot and SSD
// are left untouched.
func NormalizeSpokenText(input string) SpokenTextResult {
	if input == "" {
		return SpokenTextResult{}
	}

	runes := []rune(input)
	out := make([]rune, 0, len(runes))
	rewrites := make([]SpokenTextRewrite, 0)

	for i := 0; i < len(runes); {
		if replacement, originalEnd, rule, ok := normalizeSectionNumber(runes, i); ok {
			appendRewrite(&out, &rewrites, rule, runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if replacement, originalEnd, rule, ok := normalizeFigureReference(runes, i); ok {
			appendRewrite(&out, &rewrites, rule, runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if replacement, originalEnd, ok := normalizeTermHaoLabel(runes, i); ok {
			appendRewrite(&out, &rewrites, "renqi_hao_to_spaced_hao_term", runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if replacement, originalEnd, ok := normalizeNumberListBeforeNode(runes, i); ok {
			appendRewrite(&out, &rewrites, "number_list_sentence_pause_before_node_v3", runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if replacement, originalEnd, ok := normalizeCJKArabicNumberList(runes, i); ok {
			appendRewrite(&out, &rewrites, "cjk_arabic_number_list_to_chinese_numerals_v1", runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if replacement, originalEnd, rule, ok := normalizePreviousLogPhrase(runes, i); ok {
			appendRewrite(&out, &rewrites, rule, runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if matchRunes(runes, i, "操作都要长得多") {
			originalEnd := i + len([]rune("操作都要长得多"))
			appendRewrite(&out, &rewrites, "duration_polyphone_chang_to_haoshi_gengjiu_v3", runes, i, originalEnd, "操作耗时都要更久")
			i = originalEnd
			continue
		}
		if replacement, originalEnd, ok := normalizeTechnicalLatinSuffix(runes, i); ok {
			appendRewrite(&out, &rewrites, "technical_latin_cjk_suffix_spacing_v3", runes, i, originalEnd, replacement)
			i = originalEnd
			continue
		}
		if matchRunes(runes, i, "第") {
			if value, numberEnd, ok := parseChineseNumberToken(runes, i+1); ok && matchRunes(runes, numberEnd, "个服务器") {
				originalEnd := numberEnd + len([]rune("个服务器"))
				original := string(runes[i:originalEnd])
				replacement := chineseNumber(value) + "号节点"
				out = append(out, []rune(replacement)...)
				rewrites = append(rewrites, SpokenTextRewrite{
					Rule:        "chinese_ordinal_server_label",
					Original:    original,
					Replacement: replacement,
					StartRune:   i,
					EndRune:     originalEnd,
				})
				i = originalEnd
				continue
			}
		}
		if value, numberEnd, ok := parseChineseNumberToken(runes, i); ok && matchRunes(runes, numberEnd, "个服务器") {
			originalEnd := numberEnd + len([]rune("个服务器"))
			original := string(runes[i:originalEnd])
			replacement := chineseNumber(value) + "个节点"
			out = append(out, []rune(replacement)...)
			rewrites = append(rewrites, SpokenTextRewrite{
				Rule:        "chinese_server_count",
				Original:    original,
				Replacement: replacement,
				StartRune:   i,
				EndRune:     originalEnd,
			})
			i = originalEnd
			continue
		}
		if matchRunes(runes, i, "服务器") {
			numberStart := i + len([]rune("服务器"))
			if value, numberEnd, ok := parseChineseNumberToken(runes, numberStart); ok {
				original := string(runes[i:numberEnd])
				replacement := chineseNumber(value) + "号节点"
				out = append(out, []rune(replacement)...)
				rewrites = append(rewrites, SpokenTextRewrite{
					Rule:        "chinese_server_label",
					Original:    original,
					Replacement: replacement,
					StartRune:   i,
					EndRune:     numberEnd,
				})
				i = numberEnd
				continue
			}
		}

		r := runes[i]
		if (r == 'S' || r == 's') && !hasASCIIWordBefore(runes, i) {
			j := i + 1
			for j < len(runes) && unicode.IsDigit(runes[j]) {
				j++
			}
			if j > i+1 && !hasASCIIWordAfter(runes, j) {
				digits := string(runes[i+1 : j])
				if value, err := strconv.Atoi(digits); err == nil && value >= 0 && value <= 99 {
					replacement := chineseNumber(value) + "号节点"
					originalStart := i
					originalEnd := j
					if hasServerPrefix(out) {
						out = out[:len(out)-3]
						originalStart = i - 3
					}
					if hasServerSuffix(runes, j) {
						originalEnd = j + 3
						j = originalEnd
					}
					original := string(runes[originalStart:originalEnd])
					out = append(out, []rune(replacement)...)
					rewrites = append(rewrites, SpokenTextRewrite{
						Rule:        "raft_server_label",
						Original:    original,
						Replacement: replacement,
						StartRune:   originalStart,
						EndRune:     originalEnd,
					})
					i = j
					continue
				}
			}
		}
		out = append(out, r)
		i++
	}

	return SpokenTextResult{
		Text:     string(out),
		Rewrites: rewrites,
	}
}

func hasServerPrefix(out []rune) bool {
	if len(out) < 3 {
		return false
	}
	return string(out[len(out)-3:]) == "服务器"
}

func appendRewrite(out *[]rune, rewrites *[]SpokenTextRewrite, rule string, runes []rune, start int, end int, replacement string) {
	original := string(runes[start:end])
	if original == replacement {
		*out = append(*out, []rune(original)...)
		return
	}
	*out = append(*out, []rune(replacement)...)
	*rewrites = append(*rewrites, SpokenTextRewrite{
		Rule:        rule,
		Original:    original,
		Replacement: replacement,
		StartRune:   start,
		EndRune:     end,
	})
}

func normalizeSectionNumber(runes []rune, index int) (string, int, string, bool) {
	if matchRunes(runes, index, "第") {
		parts, numberEnd, ok := parseDottedNumber(runes, index+1)
		if ok && matchRunes(runes, numberEnd, "节") {
			return "第" + chinesePointNumber(parts) + "节", numberEnd + len([]rune("节")), "section_number_to_spoken_points", true
		}
	}
	if hasASCIIWordBefore(runes, index) {
		return "", index, "", false
	}
	parts, numberEnd, ok := parseDottedNumber(runes, index)
	if !ok || hasASCIIWordAfter(runes, numberEnd) {
		return "", index, "", false
	}
	if matchRunes(runes, numberEnd, "节") {
		return "第" + chinesePointNumber(parts) + "节", numberEnd + len([]rune("节")), "section_number_to_spoken_points", true
	}
	return chinesePointNumber(parts), numberEnd, "section_number_to_spoken_points", true
}

func normalizeFigureReference(runes []rune, index int) (string, int, string, bool) {
	if hasASCIIWordBefore(runes, index) || index >= len(runes) {
		return "", index, "", false
	}
	if runes[index] != '图' && runes[index] != '表' {
		return "", index, "", false
	}
	value, numberEnd, ok := parseChineseNumberToken(runes, index+1)
	if !ok || hasASCIIWordAfter(runes, numberEnd) {
		return "", index, "", false
	}
	replacement := string(runes[index]) + chineseNumber(value)
	rule := "figure_digit_to_chinese_numeral"
	end := numberEnd
	if end < len(runes) && isDashRune(runes[end]) {
		for end < len(runes) && isDashRune(runes[end]) {
			end++
		}
		replacement += "，"
		rule = "figure_dash_to_comma_pause"
	}
	return replacement, end, rule, true
}

func normalizeTermHaoLabel(runes []rune, index int) (string, int, bool) {
	if hasASCIIWordBefore(runes, index) {
		return "", index, false
	}
	value, numberEnd, ok := parseChineseNumberToken(runes, index)
	if !ok || !matchRunes(runes, numberEnd, "号任期") {
		return "", index, false
	}
	originalEnd := numberEnd + len([]rune("号任期"))
	return chineseNumber(value) + " 号任期", originalEnd, true
}

func normalizeNumberListBeforeNode(runes []rune, index int) (string, int, bool) {
	if hasASCIIWordBefore(runes, index) {
		return "", index, false
	}
	first, firstEnd, ok := parseChineseNumberToken(runes, index)
	if !ok || !matchRunes(runes, firstEnd, "、") {
		return "", index, false
	}
	second, secondEnd, ok := parseChineseNumberToken(runes, firstEnd+len([]rune("、")))
	if !ok || !matchRunes(runes, secondEnd, "。") {
		return "", index, false
	}
	lookahead := secondEnd + len([]rune("。"))
	nodeLabel, nodeEnd, ok := parseNodeLabelToken(runes, lookahead)
	if !ok {
		return "", index, false
	}
	return chineseNumber(first) + "和" + chineseNumber(second) + "。随后，" + nodeLabel, nodeEnd, true
}

func normalizeCJKArabicNumberList(runes []rune, index int) (string, int, bool) {
	if index >= len(runes) || hasASCIIWordBefore(runes, index) || !unicode.IsDigit(runes[index]) {
		return "", index, false
	}
	if !hasNearbyCJKBefore(runes, index, 6) {
		return "", index, false
	}

	parts := make([]int, 0, 4)
	j := index
	for {
		start := j
		for j < len(runes) && unicode.IsDigit(runes[j]) {
			j++
		}
		if start == j {
			return "", index, false
		}
		value, err := strconv.Atoi(string(runes[start:j]))
		if err != nil || value < 0 || value > 99 {
			return "", index, false
		}
		parts = append(parts, value)
		if j >= len(runes) || runes[j] != '、' {
			break
		}
		if j+1 >= len(runes) || !unicode.IsDigit(runes[j+1]) {
			return "", index, false
		}
		j++
	}
	if len(parts) < 2 || hasASCIIWordAfter(runes, j) || !hasNearbyCJKAfter(runes, j, 6) {
		return "", index, false
	}

	labels := make([]string, 0, len(parts))
	for _, part := range parts {
		labels = append(labels, chineseNumber(part))
	}
	return strings.Join(labels, "、"), j, true
}

func parseNodeLabelToken(runes []rune, index int) (string, int, bool) {
	if value, nodeNumberEnd, ok := parseChineseNumberToken(runes, index); ok && matchRunes(runes, nodeNumberEnd, "号节点") {
		end := nodeNumberEnd + len([]rune("号节点"))
		return chineseNumber(value) + "号节点", end, true
	}
	if matchRunes(runes, index, "服务器") {
		numberStart := index + len([]rune("服务器"))
		if value, numberEnd, ok := parseChineseNumberToken(runes, numberStart); ok {
			return chineseNumber(value) + "号节点", numberEnd, true
		}
		if label, end, ok := parseASCIIServiceNodeLabel(runes, numberStart); ok {
			return label, end, true
		}
	}
	return parseASCIIServiceNodeLabel(runes, index)
}

func parseASCIIServiceNodeLabel(runes []rune, index int) (string, int, bool) {
	if index >= len(runes) || (runes[index] != 'S' && runes[index] != 's') || hasASCIIWordBefore(runes, index) {
		return "", index, false
	}
	j := index + 1
	for j < len(runes) && unicode.IsDigit(runes[j]) {
		j++
	}
	if j <= index+1 || hasASCIIWordAfter(runes, j) {
		return "", index, false
	}
	value, err := strconv.Atoi(string(runes[index+1 : j]))
	if err != nil || value < 0 || value > 99 {
		return "", index, false
	}
	return chineseNumber(value) + "号节点", j, true
}

func normalizePreviousLogPhrase(runes []rune, index int) (string, int, string, bool) {
	if !matchRunes(runes, index, "前一日志") {
		return "", index, "", false
	}
	prefixEnd := index + len([]rune("前一日志"))
	for _, field := range []string{"索引", "任期"} {
		suffix := field + "字段"
		if matchRunes(runes, prefixEnd, suffix) {
			return "前一条日志的" + suffix, prefixEnd + len([]rune(suffix)), "previous_log_field_to_clause_v3", true
		}
	}
	return "前一条日志", prefixEnd, "previous_log_phrase_to_clause_v3", true
}

func normalizeTechnicalLatinSuffix(runes []rune, index int) (string, int, bool) {
	for _, term := range []string{"Append Entries RPC", "Append Entries", "Raft", "RAFT", "raft", "RPC"} {
		if !matchRunes(runes, index, term) {
			continue
		}
		end := index + len([]rune(term))
		if strings.EqualFold(term, "raft") && matchRunes(runes, end, "服务器") {
			continue
		}
		if end < len(runes) && isCJK(runes[end]) {
			return term + " ", end, true
		}
	}
	return "", index, false
}

func parseDottedNumber(runes []rune, index int) ([]int, int, bool) {
	parts := make([]int, 0, 3)
	j := index
	for {
		if j >= len(runes) || !unicode.IsDigit(runes[j]) {
			return nil, index, false
		}
		start := j
		for j < len(runes) && unicode.IsDigit(runes[j]) {
			j++
		}
		value, err := strconv.Atoi(string(runes[start:j]))
		if err != nil || value < 0 || value > 99 {
			return nil, index, false
		}
		parts = append(parts, value)
		if j >= len(runes) || runes[j] != '.' {
			break
		}
		j++
	}
	if len(parts) < 2 {
		return nil, index, false
	}
	return parts, j, true
}

func chinesePointNumber(parts []int) string {
	labels := make([]string, 0, len(parts))
	for _, part := range parts {
		labels = append(labels, chineseNumber(part))
	}
	return strings.Join(labels, "点")
}

func isDashRune(r rune) bool {
	return r == '-' || r == '—' || r == '–'
}

func isCJK(r rune) bool {
	return r >= '\u4e00' && r <= '\u9fff'
}

func hasNearbyCJKBefore(runes []rune, index int, window int) bool {
	start := index - window
	if start < 0 {
		start = 0
	}
	for i := index - 1; i >= start; i-- {
		if isCJK(runes[i]) {
			return true
		}
		if isASCIIAlpha(runes[i]) {
			return false
		}
	}
	return false
}

func hasNearbyCJKAfter(runes []rune, index int, window int) bool {
	end := index + window
	if end > len(runes) {
		end = len(runes)
	}
	for i := index; i < end; i++ {
		if isCJK(runes[i]) {
			return true
		}
		if isASCIIAlpha(runes[i]) {
			return false
		}
	}
	return false
}

func matchRunes(runes []rune, index int, needle string) bool {
	needleRunes := []rune(needle)
	if index < 0 || index+len(needleRunes) > len(runes) {
		return false
	}
	for offset, r := range needleRunes {
		if runes[index+offset] != r {
			return false
		}
	}
	return true
}

func parseChineseNumberToken(runes []rune, index int) (int, int, bool) {
	if index >= len(runes) {
		return 0, index, false
	}
	if unicode.IsDigit(runes[index]) {
		j := index
		for j < len(runes) && unicode.IsDigit(runes[j]) {
			j++
		}
		value, err := strconv.Atoi(string(runes[index:j]))
		if err != nil || value < 0 || value > 99 {
			return 0, index, false
		}
		return value, j, true
	}

	j := index
	for j < len(runes) && isChineseNumberRune(runes[j]) {
		j++
	}
	if j == index {
		return 0, index, false
	}
	value, ok := parseChineseNumber(string(runes[index:j]))
	if !ok || value < 0 || value > 99 {
		return 0, index, false
	}
	return value, j, true
}

func isChineseNumberRune(r rune) bool {
	switch r {
	case '零', '一', '二', '两', '三', '四', '五', '六', '七', '八', '九', '十':
		return true
	default:
		return false
	}
}

func parseChineseNumber(text string) (int, bool) {
	values := map[rune]int{
		'零': 0,
		'一': 1,
		'二': 2,
		'两': 2,
		'三': 3,
		'四': 4,
		'五': 5,
		'六': 6,
		'七': 7,
		'八': 8,
		'九': 9,
	}
	runes := []rune(text)
	if len(runes) == 1 {
		if runes[0] == '十' {
			return 10, true
		}
		value, ok := values[runes[0]]
		return value, ok
	}
	tenIndex := -1
	for i, r := range runes {
		if r == '十' {
			tenIndex = i
			break
		}
	}
	if tenIndex < 0 {
		return 0, false
	}
	tens := 1
	if tenIndex > 0 {
		value, ok := values[runes[tenIndex-1]]
		if !ok || value == 0 {
			return 0, false
		}
		tens = value
	}
	ones := 0
	if tenIndex+1 < len(runes) {
		value, ok := values[runes[tenIndex+1]]
		if !ok {
			return 0, false
		}
		ones = value
	}
	return tens*10 + ones, true
}

func hasServerSuffix(runes []rune, index int) bool {
	if index+3 > len(runes) {
		return false
	}
	return string(runes[index:index+3]) == "服务器"
}

func hasASCIIWordBefore(runes []rune, index int) bool {
	if index <= 0 {
		return false
	}
	prev := runes[index-1]
	return isASCIIAlpha(prev) || isASCIIDigit(prev)
}

func hasASCIIWordAfter(runes []rune, index int) bool {
	if index >= len(runes) {
		return false
	}
	next := runes[index]
	return isASCIIAlpha(next) || isASCIIDigit(next)
}

func isASCIIAlpha(r rune) bool {
	return (r >= 'A' && r <= 'Z') || (r >= 'a' && r <= 'z')
}

func isASCIIDigit(r rune) bool {
	return r >= '0' && r <= '9'
}

func chineseNumber(n int) string {
	digits := []string{"零", "一", "二", "三", "四", "五", "六", "七", "八", "九"}
	if n >= 0 && n < 10 {
		return digits[n]
	}
	if n == 10 {
		return "十"
	}
	if n < 20 {
		return "十" + digits[n%10]
	}
	tens := n / 10
	ones := n % 10
	if ones == 0 {
		return digits[tens] + "十"
	}
	return digits[tens] + "十" + digits[ones]
}
