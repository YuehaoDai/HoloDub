package tts

import "testing"

func TestNormalizeSpokenTextRaftServerLabels(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "server suffix",
			in:   "服务器S3被选为leader",
			want: "三号节点被选为leader",
		},
		{
			name: "multiple standalone labels",
			in:   "S1拥有日志，S2也拥有日志",
			want: "一号节点拥有日志，二号节点也拥有日志",
		},
		{
			name: "labels with conjunction",
			in:   "S1和S2同时成为leader",
			want: "一号节点和二号节点同时成为leader",
		},
		{
			name: "server prefix",
			in:   "S3服务器收到请求",
			want: "三号节点收到请求",
		},
		{
			name: "two digit label",
			in:   "S10向S21发送AppendEntries RPC",
			want: "十号节点向二十一号节点发送AppendEntries RPC",
		},
		{
			name: "zero label",
			in:   "S0是测试节点",
			want: "零号节点是测试节点",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NormalizeSpokenText(tt.in)
			if got.Text != tt.want {
				t.Fatalf("Text = %q, want %q", got.Text, tt.want)
			}
			if len(got.Rewrites) == 0 {
				t.Fatal("expected rewrite provenance")
			}
		})
	}
}

func TestNormalizeSpokenTextKeepsTechnicalTerms(t *testing.T) {
	in := "AppendEntries RPC、InstallSnapshot RPC、Raft、SSD都保持原样。"
	got := NormalizeSpokenText(in)
	if got.Text != in {
		t.Fatalf("Text = %q, want unchanged %q", got.Text, in)
	}
	if len(got.Rewrites) != 0 {
		t.Fatalf("expected no rewrites, got %+v", got.Rewrites)
	}
}

func TestNormalizeSpokenTextChineseServerLabels(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "server with chinese numeral suffix",
			in:   "服务器一的日志包含第5项",
			want: "一号节点的日志包含第5项",
		},
		{
			name: "server with arabic numeral suffix",
			in:   "服务器2当选领导者",
			want: "二号节点当选领导者",
		},
		{
			name: "ordinal server",
			in:   "第三个服务器也拥有日志",
			want: "三号节点也拥有日志",
		},
		{
			name: "server count",
			in:   "假设我们有五个服务器",
			want: "假设我们有五个节点",
		},
		{
			name: "plain server stays unchanged",
			in:   "服务器需要持久化日志",
			want: "服务器需要持久化日志",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NormalizeSpokenText(tt.in)
			if got.Text != tt.want {
				t.Fatalf("Text = %q, want %q", got.Text, tt.want)
			}
		})
	}
}

func TestNormalizeSpokenTextTechnicalV3ClarityRules(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "term hao label keeps hao audible",
			in:   "命令对应的任期编号恰好就是5号任期。",
			want: "命令对应的任期编号恰好就是五 号任期。",
		},
		{
			name: "term hao label from chinese numeral keeps hao audible",
			in:   "命令对应的任期编号恰好就是五号任期。",
			want: "命令对应的任期编号恰好就是五 号任期。",
		},
		{
			name: "section number speaks every point",
			in:   "请参照5.4.1节继续执行。",
			want: "请参照第五点四点一节继续执行。",
		},
		{
			name: "standalone section number speaks every point",
			in:   "这里的5.4.1很关键。",
			want: "这里的五点四点一很关键。",
		},
		{
			name: "number list inserts bridge before node",
			in:   "它刚刚记录到5、6。一号节点仍然存在一个问题。",
			want: "它刚刚记录到五和六。随后，一号节点仍然存在一个问题。",
		},
		{
			name: "number list inserts bridge before server label",
			in:   "它刚刚记录到5、6。服务器S1仍然存在一个问题。",
			want: "它刚刚记录到五和六。随后，一号节点仍然存在一个问题。",
		},
		{
			name: "cjk arabic number list speaks chinese numerals",
			in:   "这里存在日志索引1、2、3这三个位置。",
			want: "这里存在日志索引一、二、三这三个位置。",
		},
		{
			name: "figure dash becomes a comma pause",
			in:   "第二个图2——后面的说明继续。",
			want: "第二个图二，后面的说明继续。",
		},
		{
			name: "previous log field becomes natural clause",
			in:   "前一日志索引字段以及前一日志任期字段很重要。",
			want: "前一条日志的索引字段以及前一条日志的任期字段很重要。",
		},
		{
			name: "previous log phrase avoids unnatural split",
			in:   "前一日志中日和志不能断开。",
			want: "前一条日志中日和志不能断开。",
		},
		{
			name: "polyphone longer phrase",
			in:   "这些操作都要长得多。",
			want: "这些操作耗时都要更久。",
		},
		{
			name: "latin term before cjk gets a space",
			in:   "Raft算法中的Append Entries RPC请求。",
			want: "Raft 算法中的Append Entries RPC 请求。",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := NormalizeSpokenText(tt.in)
			if got.Text != tt.want {
				t.Fatalf("Text = %q, want %q", got.Text, tt.want)
			}
			if len(got.Rewrites) == 0 {
				t.Fatal("expected rewrite provenance")
			}
		})
	}
}

func TestNormalizeSpokenTextAvoidsFalsePositiveInsideASCIIWords(t *testing.T) {
	tests := []string{
		"TLS1.3握手",
		"MS2不是Raft服务器标签",
		"XS3Y",
		"Class2",
		"S3a",
	}
	for _, in := range tests {
		got := NormalizeSpokenText(in)
		if got.Text != in {
			t.Fatalf("NormalizeSpokenText(%q) = %q, want unchanged", in, got.Text)
		}
	}
}
