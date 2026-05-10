# HoloDub 优化路线图

> 本文档是 HoloDub 所有长期优化项的 **single source of truth**。
> 与 [`CHANGELOG.md`](../../CHANGELOG.md) 配合：roadmap 跟踪 *规划与进度*，CHANGELOG 只记录 *已 ship 事实*。

---

## 1. 文档说明

### 1.1 OPT-ID 命名

`OPT-NXX`：`N` = 优先级（0/1/2/3），`XX` = 序号。

| 优先级 | 含义 |
|---|---|
| **P0** | 高 ROI，无前置依赖，应立刻排期 |
| **P1** | 紧跟 P0，关键能力补齐 |
| **P2** | 中长期重构，依赖前序项 |
| **P3** | 探索 / 锦上添花 |

**编号一旦分配永不复用**，即使取消的 OPT 也保留 ID 占位（status=cancelled），保证历史 PR / commit 中的引用不失效。

### 1.2 状态机

```
                ┌──── deferred (条件不成熟，留待后续)
                ▼
planned ──→ in_progress ──→ done ──→ archived (release tag 后)
                │
                └──→ cancelled (确认不做)
```

| Status | 含义 | 谁能改 |
|---|---|---|
| `planned` | 已立项未启动 | 任何人 |
| `in_progress` | 有 PR 在跑 | 负责人 |
| `done` | 已 merge 且 L4 灰度通过，CHANGELOG 已加条目 | 负责人 |
| `deferred` | 暂时搁置（前置不满足 / 优先级让位） | 任何人 |
| `cancelled` | 确认不做（方向错 / 被替代） | 项目维护者 |
| `archived` | done 项归档到节 6 | 自动（每次 release tag） |

### 1.3 卡片必填字段

| 字段 | 说明 |
|---|---|
| **Status** | 见上 |
| **Source** | 起源（agent transcript / issue / incident / 用户反馈） |
| **Estimate** | 工作日（粗估，可调整） |
| **Depends on** | 前置 OPT 列表，无则写 `-` |
| **Outcome** | 期待收益，**必须可量化** |
| **Verification** | 通过哪些 metric / golden set / 人工验证 |
| **Rollout** | 灰度策略（参考 [testing-and-rollout.mdc](../../.cursor/rules/testing-and-rollout.mdc) L1→L4） |
| **Related rules** | 哪些 `.cursor/rules/*.mdc` 是必读 |
| **PRs** | 关联 PR 链接，merge 时回填 |

### 1.4 与 CHANGELOG 的关系

- OPT 在 `planned / in_progress` 阶段**不进** CHANGELOG
- `done` 时由负责人在 CHANGELOG `[Unreleased]` 加一条 `Added` / `Changed` / `Fixed`，**必须包含 `(OPT-XXX)` 引用**
- 每次 release tag 后，本文档"节 6"汇总当 release 涵盖的所有 OPT-ID

---

## 2. 工作流闭环

```mermaid
flowchart LR
    Discover["发现优化点"] --> PlanIt["roadmap 新增 OPT-XXX<br/>status=planned"]
    PlanIt --> Start["开 PR<br/>status=in_progress"]
    Start --> Merge["PR merged<br/>L1-L4 灰度通过"]
    Merge --> Done["status=done<br/>CHANGELOG 加条目<br/>引用 OPT-ID"]
    Done --> Tag["release tag<br/>归档到节 6"]
    Tag --> Discover
```

每张 OPT 在生命周期内只有一处真相（本文档），CHANGELOG 只记录 ship 事实，避免双向同步。

---

## 3. 状态总览

> 实时维护。完成项移至节 6 后从此表移除，但 ID 永久保留在节 4/5。

| OPT-ID | 标题 | Pri | Status | Estimate | Depends on |
|---|---|---|---|---|---|
| OPT-001 | Prompt caching | P0 | done (followup-1 promoted P0) | 0.5d | - |
| OPT-001-followup-1 | translate prompt 字节稳定 (move targetSec to user msg) | P0 | planned | 0.5d | OPT-001 |
| OPT-002 | LLM-as-Judge MVP | P0 | done | 2d | - |
| OPT-003 | Function calling 替代 prompt+JSON parse | P0 | done | 1d | - |
| OPT-FOLLOWUP-3 | Drift threshold 长段调优 / judge 短路 retry | P1 | planned | 1d (a) / OPT-201 (b) | OPT-002 |
| OPT-101 | OpenTelemetry GenAI semconv + cost USD trace | P1 | planned | 2d | - |
| OPT-102 | Plan Mode 段审 / TodoWrite 风格 | P1 | planned | 3d | OPT-003 |
| OPT-103 | MCP server 暴露 ml-service | P1 | planned | 1d | - |
| OPT-104 | Agent transcript 持久化 | P1 | planned | 1.5d | OPT-101 |
| OPT-201 | SegmentAgent ReAct 重构 | P2 | planned | 5d | OPT-002, OPT-104 |
| OPT-202 | Speculative ensemble + judge | P2 | planned | 3d | OPT-002 |
| OPT-203 | Streaming TTS + SSE 推送 | P2 | planned | 5d | - |
| OPT-204 | 结构化情感/韵律输出 | P2 | planned | 2d | OPT-003 |
| OPT-205 | Reasoning model 全场景化 | P2 | planned | 1d | - |
| OPT-206 | Skills / Glossary 系统 | P2 | planned | 3d | - |
| OPT-301 | DSPy 自动 prompt 优化 | P3 | planned | 5d | OPT-002, golden set 扩充 |
| OPT-302 | 多模态 ASR backend | P3 | planned | 5d | - |
| OPT-303 | 多租户权限 + tenant_key 强制 | P3 | planned | 5d | - |
| OPT-304 | Settings TOML + profile | P3 | planned | 3d | - |
| OPT-305 | CLI 工具 | P3 | planned | 3d | - |
| OPT-306 | Hot reload prompts in DB | P3 | planned | 2d | OPT-304 |
| OPT-307 | Batch API 选项 | P3 | planned | 2d | OPT-001 |

### 3.1 依赖图（一眼看清"先做什么"）

```mermaid
flowchart TD
    OPT001[OPT-001 Prompt caching]
    OPT002[OPT-002 LLM Judge MVP]
    OPT003[OPT-003 Function calling]
    OPT101[OPT-101 OTEL GenAI semconv]
    OPT102[OPT-102 Plan Mode 段审]
    OPT104[OPT-104 Agent transcript]
    OPT201[OPT-201 SegmentAgent 重构]
    OPT202[OPT-202 Ensemble + judge]
    OPT204[OPT-204 结构化情感韵律]
    OPT301[OPT-301 DSPy 自动 prompt]
    OPT304[OPT-304 Settings TOML]
    OPT306[OPT-306 Hot reload prompts]
    OPT307[OPT-307 Batch API]

    OPT003 --> OPT102
    OPT003 --> OPT204
    OPT002 --> OPT201
    OPT002 --> OPT202
    OPT002 --> OPT301
    OPT101 --> OPT104
    OPT104 --> OPT201
    OPT304 --> OPT306
    OPT001 --> OPT307
```

---

## 4. 详细卡片

### P0：高 ROI 立即排期

#### OPT-001 Prompt caching

- **Status**: done (observability foundation; cache hit ratio target deferred to long-video follow-up)
- **Source**: 优化对话 §三.1
- **Estimate**: 0.5d (实际 ~1.5d，含 DashScope 嵌套字段 bug 修复 + worker /metrics endpoint)
- **Depends on**: -
- **Outcome**: token usage 全链路可观测：每个 LLM 调用按 `{model, operation}` 维度记录 input / output / cached tokens；cache 在 production worker 路径首次确认命中（job 129 judge 调用 cached_tokens=512）。
- **Verification**:
  - `holodub_llm_input_tokens_total{model,operation}` ✓ emit
  - `holodub_llm_output_tokens_total{model,operation}` ✓ emit
  - `holodub_llm_cached_tokens_total{model,operation}` ✓ emit (job 129 first hit)
  - 60s 测试视频 cache 命中率 8.5% (judge model)，0% (translate / retranslate); follow-up 需在 79min 视频验证 ≥40% 目标
- **Rollout**: 完成 L1-L4，default ON
- **Related rules**: [llm-call-standards.mdc#2](../../.cursor/rules/llm-call-standards.mdc), [observability-and-cost.mdc#5](../../.cursor/rules/observability-and-cost.mdc), [incremental-evolution.mdc](../../.cursor/rules/incremental-evolution.mdc)
- **实际改动**:
  - [`internal/llm/client.go`](../../internal/llm/client.go): 新增 `providerUsage` named type with `effectiveCached()` (max of `cached_tokens` / `prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens` 三种 provider 字段); `usageStats`; `Op*` operation constants; `doChat(ctx, operation, payload)` 签名扩展
  - [`internal/observability/metrics.go`](../../internal/observability/metrics.go): 新增 `holodub_llm_input_tokens_total`、`_output_tokens_total`、`_cached_tokens_total` (label: `model, operation`)
  - [`internal/llm/client.go`](../../internal/llm/client.go): 抽出 `buildTranslateSystemPrompt()` 纯函数保证字节稳定 (cache prefix 前提)
  - [`cmd/worker/main.go`](../../cmd/worker/main.go): 新增 `/metrics` HTTP endpoint (env `WORKER_METRICS_ADDR`, default `:8081`)
  - [`docker-compose.yml`](../../docker-compose.yml): worker `ports: ["8081:8081"]`
  - 单测 9 个 (`internal/llm/client_test.go`): 三种 provider 字段、字节稳定、cache prefix 大小、operation constant guard
- **Followups**:
  - **OPT-001-followup-1 (PROMOTED to P0, fix known)**: 10min validation (`tests/quality/baseline-post-p0-10min.json`) found `translate` system prompt is NOT byte-stable per job because `targetSec` (per-segment value) is embedded in the system role. **Fix**: move `targetSec` / `Hard char limit` / `speech rate` from system prompt to user message in `buildTranslateSystemPrompt`. Once fixed, system prompt becomes job-stable (varies only by `targetLanguage` + `translationSummary`) and all translate calls will share a cacheable prefix. Expected hit ratio after fix: ≥30% on long videos (segments naturally cluster on same prompt).
  - OPT-001-followup-2: 解析 `doChatStream` 的 final-chunk usage（thinking model token 当前 metric 显示 0；10min job 7 次 thinking 调用全部丢 token 数据）
  - **OPT-FOLLOWUP-3 (NEW from 10min validation)**: drift threshold 在长段（>20s）上太严格（effective ~3%），导致 11/24 段卡在 retry 漩涡，job cancel at 50% completion。两条思路：(a) 单独提升长段 `RETRANSLATION_MIN_DRIFT_THRESHOLD` 至 0.06；(b) 让 judge verdict='accept' 短路 drift retry（需先做 OPT-201 SegmentAgent 接入决策路径）。两者都依赖 OPT-002 已就位的 judge 信号。
- **PRs**: TBD (落 commit 时补)

---

#### OPT-002 LLM-as-Judge MVP

- **Status**: done (observe-only mode shipped; decision-path integration deferred to OPT-201)
- **Source**: 优化对话 §一.2
- **Estimate**: 2d (实际 ~1d，function calling 由 OPT-003 提前打通)
- **Depends on**: -
- **Outcome**: 每个 TTS 段落异步获得多维评分 (fidelity/fluency/coherence + verdict + issues)；judge 准确捕获 baseline 漂移盲区（job 129 segment 4: source 含 "monitoring" 译文漏掉 → judge 评 0.8/retry，与 baseline 同段在重译漩涡的现象一致）。
- **Verification**:
  - 60s 视频 5/5 segments judged (100% judged_ratio) ✓
  - judge 平均 1.8s/次延迟 (异步不阻塞主流程) ✓
  - 0 strict_parse_failed 在测试期 ✓
  - 抽样 5 个 segment 人工 review verdict 准确 ≥ 4/5 ✓ (segment 4 issue 经人工对照 source/target 确实漏译)
- **Rollout**:
  - L1 / L2 / L3 / L4 完成
  - default JUDGE_MODEL="" (disabled)；启用方式：env JUDGE_MODEL=qwen-turbo + JUDGE_OBSERVE_ONLY=true
  - 决策接入留给 OPT-201
- **Related rules**: [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc), [agent-design.mdc#3](../../.cursor/rules/agent-design.mdc), [observability-and-cost.mdc#5](../../.cursor/rules/observability-and-cost.mdc)
- **实际改动**:
  - [`migrations/004_judge_score.sql`](../../migrations/004_judge_score.sql): `segments.judge_score NUMERIC NULL, judge_meta JSONB NULL` + partial index
  - [`internal/models/models.go`](../../internal/models/models.go): `Segment.JudgeScore *float64, JudgeMeta datatypes.JSON`
  - [`internal/llm/judge.go`](../../internal/llm/judge.go): `JudgeArgs / JudgeResult / JudgeFidelity()` + strict JSON Schema for verdict
  - [`internal/config/config.go`](../../internal/config/config.go): `JudgeModel string` + `JudgeObserveOnly bool` (default true)
  - [`internal/store/store.go`](../../internal/store/store.go): `UpdateSegmentJudgeResult(ctx, id, score, metaJSON)`
  - [`internal/pipeline/stage_tts.go`](../../internal/pipeline/stage_tts.go): `maybeJudgeSegmentAsync()` — 异步 goroutine + detached context (worker SIGTERM 不丢 verdict)
  - [`ui/src/api.ts`](../../ui/src/api.ts) + [`ui/src/components/SegmentTable.vue`](../../ui/src/components/SegmentTable.vue): "AI 评分" 列 (条件渲染，向后兼容)
  - 单测 7 个 (`internal/llm/judge_test.go`): schema 合法性、disabled 短路、empty inputs 跳过、tool path、missing tool fallback、verdict 默认值、OverallScore 算法
- **Followups**:
  - OPT-002-followup-1: golden set 扩充至 ≥50 条人工 confirm 翻译（为 judge 评分 vs 人工的 correlation 计算）
  - OPT-002-followup-2: 决策接入留给 [OPT-201](#opt-201-segmentagent-react-重构) (`JudgeObserveOnly=false`)
- **PRs**: TBD

---

#### OPT-003 Function calling 替代 prompt+JSON parse

- **Status**: done
- **Source**: 优化对话 §一.3
- **Estimate**: 1d
- **Depends on**: -
- **Outcome**: ReviewSegmentation 走 strict-schema function calling，job 128/129 各 1 次 review 调用，0 次 fallback。tool/function calling 基础设施 (Tools/ToolChoice/toolCall struct + doChatTool) 同时为 OPT-002 (judge) 复用。
- **Verification**:
  - `holodub_llm_strict_parse_failed_total{operation="review"}` = 0 在 2 个测试 job (job 128, job 129) 中始终为 0 ✓
  - 旧 prompt + strip-fence 路径保留作 fallback (TestReviewToolFallbackToPrompt 单测验证) ✓
  - Suggestion 质量保持 (job 128/129 都正确识别 segments 9160/9161 应 merge, confidence 0.85) ✓
- **Rollout**:
  - L1-L4 完成
  - default `SEGMENT_REVIEW_USE_TOOLS=false` (新旧并存遵循 incremental-evolution.mdc#1)
  - 启用方式：.env 设 `SEGMENT_REVIEW_USE_TOOLS=true`
- **Related rules**: [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc), [incremental-evolution.mdc#1](../../.cursor/rules/incremental-evolution.mdc)
- **实际改动**:
  - [`internal/llm/client.go`](../../internal/llm/client.go): 新增 `chatMessage / toolDef / functionDef / toolCall / toolCallFunction` named types; `chatCompletionRequest.Tools/ToolChoice` 字段; `forceToolChoice(name)` helper; `doChatTool()` + `doChatToolOnce()`
  - [`internal/llm/client.go`](../../internal/llm/client.go): `ReviewSegmentation` 拆为 `reviewSegmentationViaTools` + `reviewSegmentationViaPrompt` 双路径，自动 fallback + `IncLLMStrictParseFailed("review")` 计数
  - [`internal/llm/client.go`](../../internal/llm/client.go): 新增 `reviewToolSchema` (静态 JSON Schema, init 时 marshal); `reviewSystemPrompt(lang, toolMode)` 双模 prompt
  - [`internal/observability/metrics.go`](../../internal/observability/metrics.go): 新增 `holodub_llm_strict_parse_failed_total{operation}` + `IncLLMStrictParseFailed()`
  - [`internal/config/config.go`](../../internal/config/config.go): `SegmentReviewUseTools bool` (env `SEGMENT_REVIEW_USE_TOOLS`, default false)
  - 5 个 doChat 调用方机械迁移 `[]map[string]string` → `[]chatMessage` (translate / retranslate / summary / review / simple translate)
  - 单测 5 个 (`internal/llm/client_test.go`): dual-mode prompt invariant、schema 合法性、tool path happy/fallback/flag-off
- **PRs**: TBD

---

### P1：紧跟 P0，关键能力补齐

#### OPT-101 OpenTelemetry GenAI semconv + cost USD trace

- **Status**: planned
- **Source**: 优化对话 §三.4
- **Estimate**: 2d
- **Depends on**: -
- **Outcome**:
  - 接入 LangFuse / Phoenix / Honeycomb 任一观测后端无需 relabel
  - 每个 LLM 调用都有 token + cost USD 自动聚合
  - 解决"为什么这个月账单翻倍"的归因
- **Verification**:
  - LangFuse 自托管能看到完整 trace 树（job → stage → segment → llm.call）
  - 每日 cost 聚合曲线与 provider 后台账单偏差 < 5%
- **Rollout**:
  - L1 单元测试 OTEL exporter
  - L2 staging stack 接 LangFuse self-host（已有 Postgres 复用）
  - L3 生产 read-only 跑 1 周
  - L4 接入告警
- **Related rules**: [observability-and-cost.mdc](../../.cursor/rules/observability-and-cost.mdc) (本 OPT 的"宪法")
- **关键改动点**:
  - Go：`internal/observability/` 新增 `otel.go`，初始化 `tracer / meter`，env 配置 `OTEL_EXPORTER_OTLP_ENDPOINT`
  - Go：`internal/observability/cost.go` 新增 `modelPrices` 表 + `RecordLLMCall(model, op, tokens, cached, latency)`
  - Python：`ml_service/app/observability.py` 加 `opentelemetry-instrumentation-fastapi`
  - 现有 `holodub_*` 自定义 metric 保留兼容，新增的全用 OTEL semconv
  - 价格表通过 env override：`MODEL_PRICE_OVERRIDE=deepseek-chat:0.27:1.10:0.027,...`
- **PRs**: TBD

---

#### OPT-102 Plan Mode 段审 / TodoWrite 风格

- **Status**: planned
- **Source**: 优化对话 §二.1
- **Estimate**: 3d
- **Depends on**: OPT-003
- **Outcome**: 把当前的"merge 建议列表"升级为有序整改计划（merge / split / manual_review 混合），用户可一键应用全部 auto，UX 接近 Claude Code 的 plan mode。
- **Verification**:
  - 段审平均完成时间从当前 ~15min 降至 ~5min
  - "未审就直接 confirm"的 job 比例下降（说明用户更愿意走 plan）
- **Rollout**:
  - L1 / L2 / L3 / L4 + UI 灰度（先内部账号、再全量）
- **Related rules**: [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc), [vue-frontend.mdc](../../.cursor/rules/vue-frontend.mdc), [agent-design.mdc#4](../../.cursor/rules/agent-design.mdc) (transcript)
- **关键改动点**:
  - LLM 输出从 `[{ordinals, reason, confidence}]` 升级为 `{plan: [{id, action, ordinals, reason, auto, ...}]}`
  - 新增 `action` 类型：`split` (附 `at_ms`)、`manual_review`（不可自动应用）
  - DB：`segment_suggestions` 加 `action` 已有，新增 `auto_applicable BOOL DEFAULT false`
  - 前端：[`ui/src/components/SegmentReview.vue`](../../ui/src/components/SegmentReview.vue) 加"AI 整改清单 (3/5 已应用)"面板 + "一键 accept all auto" 按钮
- **PRs**: TBD

---

#### OPT-103 MCP server 暴露 ml-service

- **Status**: planned
- **Source**: 优化对话 §二.3
- **Estimate**: 1d
- **Depends on**: -
- **Outcome**: ml-service 的 ASR / TTS / VAD 能力可被 Cursor / Claude Desktop / OpenAI Agents SDK 直接调用；voice profiles 暴露为 MCP Resources（`voice://Host_Voice` URI）。
- **Verification**:
  - Claude Desktop 配置 MCP 后能 list_tools 看到 `transcribe_window` / `run_tts`
  - 能在 Cursor 中用 `@mcp:holodub/transcribe_window` 调用并返回结果
- **Rollout**:
  - L1 本地 stdio MCP server smoke
  - L2 staging 暴露 SSE MCP endpoint
  - L3 内部账号 1 周
  - L4 文档化、加入 README
- **Related rules**: [llm-call-standards.mdc](../../.cursor/rules/llm-call-standards.mdc) (timeout / 错误分类), [python-ml-service.mdc](../../.cursor/rules/python-ml-service.mdc)
- **关键改动点**:
  - 新增 `ml_service/app/mcp_server.py`（用 [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk)），复用现有 `services.asr / services.tts`
  - `ml_service/pyproject.toml` 加依赖 `mcp`
  - Docker：可选 expose 8001 端口（默认关闭）
  - Voice profile 通过 Go API 拉取（HTTP 调 control plane），转译为 MCP resource
- **PRs**: TBD

---

#### OPT-104 Agent transcript 持久化

- **Status**: planned
- **Source**: 优化对话 §二.2
- **Estimate**: 1.5d
- **Depends on**: OPT-101 (token / cost 字段需 OTEL 已就位)
- **Outcome**: 每条 LLM/ML 调用 input/output 落库（zstd 压缩），SQL 查表回答"为什么 segment 38 重译了 7 次"，给算法工程师 / PM / 客诉调试一手数据。
- **Verification**:
  - 79min 视频 transcript 表大小 < 50 MB（压缩后）
  - 任意 segment 的完整决策链能在 < 1s SQL 查出
  - 有 TTL 任务清理 30 天前数据
- **Rollout**:
  - L1 / L2 / L3 / L4
  - 注意 PII：transcript 默认对非 admin 不可见
- **Related rules**: [agent-design.mdc#4](../../.cursor/rules/agent-design.mdc), [observability-and-cost.mdc#7](../../.cursor/rules/observability-and-cost.mdc)
- **关键改动点**:
  - 新增 migration `migrations/00X_agent_transcripts.sql`：

    ```sql
    CREATE TABLE agent_transcripts (
      id BIGSERIAL PRIMARY KEY,
      job_id BIGINT NOT NULL,
      segment_id BIGINT,
      stage TEXT, actor TEXT, tool TEXT,
      input_compressed BYTEA, output_compressed BYTEA,
      decision TEXT,
      latency_ms INT,
      tokens_in INT, tokens_out INT, cached_tokens INT,
      cost_usd NUMERIC,
      created_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX ON agent_transcripts (job_id, created_at);
    ```

  - Go：`internal/store/transcripts.go` 新增写入函数；`internal/observability/cost.go` 加 hook
  - 前端：JobDetail / SegmentTable 加"查看 AI 决策过程"按钮
  - GORM 用 zstd: `zstd:3` 压缩 input/output
- **PRs**: TBD

---

### P2：中长期重构

#### OPT-201 SegmentAgent ReAct 重构

- **Status**: planned
- **Source**: 优化对话 §一.1
- **Estimate**: 5d
- **Depends on**: OPT-002, OPT-104
- **Outcome**: 把 [`internal/pipeline/stage_tts.go:243-422`](../../internal/pipeline/stage_tts.go) 的 180 行手写 retry 循环重构为显式 SegmentAgent + Tool 接口 + 状态机；解锁动态 split 等高级动作；agent 行为可单元测。
- **Verification**:
  - 行为测试覆盖 ≥ 100 种漂移轨迹（参考 [testing-and-rollout.mdc#2](../../.cursor/rules/testing-and-rollout.mdc)）
  - 79min 视频 drift p95 ≤ baseline × 1.05（不允许性能回退）
  - 总 cost USD ≤ baseline × 1.1（agent 多调 judge 不应让总费用爆炸）
- **Rollout**:
  - 严格 L1→L4 + feature flag `SEGMENT_AGENT_ENABLED`
  - 旧 `processOneTTSSegment` 保留 ≥ 4 周
- **Related rules**: [agent-design.mdc](../../.cursor/rules/agent-design.mdc) (本 OPT 的"宪法"), [incremental-evolution.mdc#1](../../.cursor/rules/incremental-evolution.mdc)
- **关键改动点**:
  - 新增 `internal/agents/segment_agent.go`、`internal/agents/dubbing_tools.go`
  - `DubbingTools` 接口：Synthesize / Translate / RetranslateThinking / JudgeFidelity / SplitSegment / AcceptWithBorrow
  - 状态机 struct + `decide(state, obs) Decision` 纯函数
  - `pipeline.processOneTTSSegment` 增加 `if cfg.UseSegmentAgent { return s.runSegmentAgentV2(...) }` 开关
- **PRs**: TBD（建议拆 6 个子 PR）

---

#### OPT-202 Speculative ensemble + judge

- **Status**: planned
- **Source**: 优化对话 §三.3
- **Estimate**: 3d
- **Depends on**: OPT-002
- **Outcome**: 关键段（judge 评分低 / 用户标记重要）同时跑两个不同 model（DeepSeek + Qwen），用 thinking model pairwise 选最优；显著提升质量天花板。
- **Verification**:
  - golden set 上 ensemble 平均 fidelity 比单模型提升 ≥ 5%
  - 仅在 stuck / 关键段触发，避免常态翻倍 cost
  - 总 cost USD 增长 < 15%
- **Rollout**: L1→L4，feature flag `ENSEMBLE_RETRANSLATE_ENABLED`
- **Related rules**: [llm-call-standards.mdc](../../.cursor/rules/llm-call-standards.mdc), [observability-and-cost.mdc](../../.cursor/rules/observability-and-cost.mdc)
- **关键改动点**:
  - 新增 `internal/llm/ensemble.go`：`RetranslateEnsemble(ctx, args, models []string) (best string, scores []JudgeResult, error)`
  - 触发条件：`attemptsWithoutImprovement >= ensembleThreshold` 或 segment.Meta 标记 `important: true`
  - 配置 `ENSEMBLE_MODELS=deepseek-chat,qwen-plus`、`ENSEMBLE_JUDGE_MODEL=...`
- **PRs**: TBD

---

#### OPT-203 Streaming TTS + SSE 推送

- **Status**: planned
- **Source**: 优化对话 §二.5 + §四.1
- **Estimate**: 5d
- **Depends on**: -
- **Outcome**:
  - 前端实时听到合成进度（"30/626 段已生成"），UX 接近 ChatGPT Voice
  - 减少 polling 压力（当前 [`ui/src/composables/usePolling.ts`](../../ui/src/composables/usePolling.ts) 每 N 秒拉一次）
- **Verification**:
  - SSE 连接稳定 ≥ 60 min（长视频）
  - 单段平均到达延迟 < 300ms（worker 完成 → 浏览器收到）
  - 浏览器 polling 请求量降 ≥ 80%
- **Rollout**: L1→L4
- **Related rules**: [vue-frontend.mdc](../../.cursor/rules/vue-frontend.mdc) (生命周期清理), [agent-design.mdc#5](../../.cursor/rules/agent-design.mdc) (取消语义)
- **关键改动点**:
  - Go：`internal/http/router.go` 新增 `GET /jobs/:id/events` (SSE)，复用 webhook notifier 的事件源
  - ml-service：`ml_service/app/routes/tts.py` 新增 `POST /tts/stream`（chunk 流式输出）
  - 前端：新增 `useEventStream.ts` composable，替代部分 polling
- **PRs**: TBD

---

#### OPT-204 结构化情感/韵律输出

- **Status**: planned
- **Source**: 优化对话 §四.3
- **Estimate**: 2d
- **Depends on**: OPT-003
- **Outcome**: 翻译同时输出 `{translation, emotion, pacing, emphasis_words, pause_after}`，IndexTTS2 接收结构化提示后情感/重音/停顿更稳定可控。
- **Verification**:
  - 抽样 50 段人工评分：情感命中率 ≥ 80%
  - 重读词位置准确率 ≥ 70%
- **Rollout**: L1→L4
- **Related rules**: [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc) (function calling)
- **关键改动点**:
  - LLM 翻译 schema 升级（依赖 OPT-003）
  - DB：`segments.meta` JSONB 加 `emotion / pacing / emphasis` 字段
  - ml-service：`ml_service/app/adapters/tts.py` IndexTTS2 调用层接收新字段，转换为 `use_emo_text=False, emo_vector=...`、`emphasis_tokens=...`
- **PRs**: TBD

---

#### OPT-205 Reasoning model 全场景化

- **Status**: planned
- **Source**: 优化对话 §三 (隐含)
- **Estimate**: 1d
- **Depends on**: -
- **Outcome**: 当前 `thinkingModel` 仅在 stuck / non-convergence 触发；扩展到 segment_review、final_summary 等高价值低频场景。
- **Verification**:
  - 段审 confidence 分布右移（≥ 0.8 占比提升）
  - summary 命中术语准确度提升（人工抽样）
- **Rollout**: L1→L4
- **Related rules**: [llm-call-standards.mdc#5](../../.cursor/rules/llm-call-standards.mdc) (模型角色化)
- **关键改动点**:
  - 配置增加 `SEGMENT_REVIEW_USE_THINKING=true`、`SUMMARY_USE_THINKING=true`
  - [`internal/llm/client.go`](../../internal/llm/client.go) 的 `SummarizeTranslation` / `ReviewSegmentation` 加 useThinking 分支
- **PRs**: TBD

---

#### OPT-206 Skills / Glossary 系统

- **Status**: planned
- **Source**: 优化对话 §二.4
- **Estimate**: 3d
- **Depends on**: -
- **Outcome**:
  - 术语表（GlossarySkill）和风格（StyleSkill）作为可挂载知识，job 可挂多个
  - 配合 OPT-001 (prompt cache) 让 glossary 进 stable prefix，零额外成本
- **Verification**:
  - 挂载 100 词术语表后，命中术语翻译一致性 100%
  - 挂载 StyleSkill 后，人工评分风格匹配度 ≥ 80%
- **Rollout**: L1→L4
- **Related rules**: [incremental-evolution.mdc#2](../../.cursor/rules/incremental-evolution.mdc) (DB nullable)
- **关键改动点**:
  - 新增表 `skills (id, type, name, content_jsonb)` + 关联表 `job_skills (job_id, skill_id)`
  - Go：`internal/store/skills.go`, `internal/http` 新增 `/skills` CRUD
  - 翻译时拼装 `glossary + style` 进 system prompt 的 stable prefix（与 OPT-001 协同）
  - 前端：JobDetail 加"挂载知识库"面板
- **PRs**: TBD

---

### P3：探索 / 锦上添花

#### OPT-301 DSPy 自动 prompt 优化

- **Status**: planned
- **Source**: 优化对话 §四.4
- **Estimate**: 5d
- **Depends on**: OPT-002, golden set 扩充至 ≥ 200 条
- **Outcome**: 用 [DSPy](https://github.com/stanfordnlp/dspy) / [TextGrad](https://github.com/zou-group/textgrad) 自动迭代 `TranslateTextWithDuration` 的 system prompt；CI 上 prompt 改动自动跑 regression、超 baseline 才合并。
- **Verification**:
  - DSPy 优化后 prompt 在 holdout set 上 BLEU/COMET/judge_score 提升 ≥ 3%
  - CI 自动 reject 回退 PR
- **Rollout**: 离线优化为主，上线时按普通 prompt 改动走 L1→L4
- **Related rules**: [testing-and-rollout.mdc#3](../../.cursor/rules/testing-and-rollout.mdc) (golden set), [llm-call-standards.mdc](../../.cursor/rules/llm-call-standards.mdc)
- **关键改动点**:
  - 新增 `tests/quality/dspy_optimize.py`
  - 优化产物（新 prompt 文本）通过 OPT-306 的 hot-reload 机制热加载
- **PRs**: TBD

---

#### OPT-302 多模态 ASR backend

- **Status**: planned
- **Source**: 优化对话 §四.2
- **Estimate**: 5d
- **Depends on**: -
- **Outcome**: 加入 Gemini 2.0 Flash / GPT-4o transcribe / ElevenLabs Scribe 选项，对带 PPT 的讲座视频（如 README 的 MIT 6.824 demo）专有名词命中率显著提升。
- **Verification**: 抽样讲座视频上专有名词 WER 从 ~8% 降至 ~2%
- **Rollout**: L1→L4，feature flag 控制
- **Related rules**: [python-ml-service.mdc](../../.cursor/rules/python-ml-service.mdc), [llm-call-standards.mdc](../../.cursor/rules/llm-call-standards.mdc)
- **关键改动点**:
  - 新增 `ml_service/app/adapters/asr_multimodal.py`
  - 视频帧抽取：`ffmpeg -ss N -frames:v 1 -vf scale=448:-1`，每 5s 一帧
  - 配置 `ML_ASR_BACKEND=gemini_multimodal`、`GEMINI_API_KEY`
- **PRs**: TBD

---

#### OPT-303 多租户权限 + tenant_key 强制

- **Status**: planned
- **Source**: 优化对话 §五.3 + [docs/production/scale-roadmap.md](../production/scale-roadmap.md) "Multi-tenant isolation"
- **Estimate**: 5d
- **Depends on**: -
- **Outcome**: `models.Job.TenantKey` 字段已存在但未被强制使用；本 OPT 完成 JWT + tenant 隔离 + 存储前缀隔离 + 配额；商业化前提。
- **Verification**:
  - 不同 tenant token 互相看不到对方 job
  - 存储路径 `data/tenants/<tenant>/jobs/<id>/...` 物理隔离
  - 单 tenant 配额（job 数 / 总时长 / 总 cost）可配置
- **Rollout**: L1→L4，须配 DB migration（向前兼容：现有 job tenant_key 默认 `default`）
- **Related rules**: [incremental-evolution.mdc#2](../../.cursor/rules/incremental-evolution.mdc) (DB), [testing-and-rollout.mdc#9](../../.cursor/rules/testing-and-rollout.mdc) (危险操作)
- **关键改动点**:
  - 新增 JWT 中间件 + `permissions` 字段（read:jobs / write:jobs / admin:tenant）
  - 所有 store 查询自动加 `WHERE tenant_key = ?` (用 GORM scope)
  - migration: tenant_key NOT NULL DEFAULT 'default'
  - 存储路径迁移脚本（dry-run + rollback）
- **PRs**: TBD

---

#### OPT-304 Settings TOML + profile

- **Status**: planned
- **Source**: 优化对话 §五.1
- **Estimate**: 3d
- **Depends on**: -
- **Outcome**: `.env.example` 已 220 行难维护；改用 `holodub.toml` + profile（dev / staging / prod），env var 仍可 override。
- **Verification**:
  - 新旧配置加载结果 100% 等价（自动比对测试）
  - prod profile 内省工具：`holodub config dump --profile prod`
- **Rollout**: L1→L4，旧 .env 路径保留 1 个 release
- **Related rules**: [incremental-evolution.mdc#1](../../.cursor/rules/incremental-evolution.mdc)
- **关键改动点**:
  - 引入 `viper` 或 `koanf`
  - 新增 `holodub.toml.example`
  - [`internal/config/config.go`](../../internal/config/config.go) 加载顺序：toml → env → 默认
- **PRs**: TBD

---

#### OPT-305 CLI 工具

- **Status**: planned
- **Source**: 优化对话 §五.4
- **Estimate**: 3d
- **Depends on**: -
- **Outcome**: `holodub` CLI 复用 internal/，命令包括 `job submit/retry/list`、`voice clone`、`eval run`，对脚本化 / CI 集成 / 批量处理是刚需。
- **Verification**:
  - 等价 curl 调用 100% 可被 CLI 覆盖
  - bash autocomplete 工作
- **Rollout**: L1（独立 binary 不影响主服务）
- **Related rules**: [go-backend.mdc](../../.cursor/rules/go-backend.mdc)
- **关键改动点**:
  - 新增 `cmd/cli/main.go`，用 `cobra`
  - `.goreleaser.yaml` 加 cli binary 构建
- **PRs**: TBD

---

#### OPT-306 Hot reload prompts in DB

- **Status**: planned
- **Source**: 优化对话 §五.2
- **Estimate**: 2d
- **Depends on**: OPT-304
- **Outcome**: 改 prompt / 漂移阈值 / retry 次数无需重编重启；admin UI 可改、SIGHUP 重载或表 watch。
- **Verification**:
  - 改 prompt 后 < 30s 生效
  - 错误 prompt 能立刻回滚到上一版本
- **Rollout**: L1→L4
- **Related rules**: [incremental-evolution.mdc#5](../../.cursor/rules/incremental-evolution.mdc) (回滚预案)
- **关键改动点**:
  - 新增表 `runtime_configs (key, value_jsonb, version, updated_at)`
  - prompt 模板移入 DB；代码读 `RuntimeConfig.Get("prompt.translate.system")`
  - SIGHUP 触发 `RuntimeConfig.Reload()`
- **PRs**: TBD

---

#### OPT-307 Batch API 选项

- **Status**: planned
- **Source**: 优化对话 §三.2
- **Estimate**: 2d
- **Depends on**: OPT-001 (cache 配 batch 综合最省)
- **Outcome**: job 提交时多一档 `priority: batch`，把所有 segment 初翻丢 OpenAI/Anthropic Batch endpoint，24h 内完成、半价。
- **Verification**:
  - batch 模式 cost USD = realtime 模式 × 0.5 ± 5%
  - 完成时间 < 24h 的成功率 ≥ 99%
- **Rollout**: L1→L4
- **Related rules**: [llm-call-standards.mdc](../../.cursor/rules/llm-call-standards.mdc), [agent-design.mdc#6](../../.cursor/rules/agent-design.mdc) (幂等：batch 重复提交不应重复扣费)
- **关键改动点**:
  - [`internal/llm/client.go`](../../internal/llm/client.go) 新增 `submitBatch` / `pollBatch`
  - pipeline `runTranslate` 在 `job.Config.priority == "batch"` 时走 batch 路径
  - 新增 stage `translate_batch_wait`（不会消耗 stage lease，只 sleep + poll）
- **PRs**: TBD

---

## 5. 取消 / 延后项

> 暂无。

---

## 6. 已完成项归档

> 每次 release tag 后，把当 release 期内完成的 OPT 移到这里，附 CHANGELOG 链接 + 实际工时。

### 模板

```markdown
### v1.X.0 (YYYY-MM-DD)

- **OPT-XXX** 标题
  - 实际工时：N 天
  - CHANGELOG: [link to anchor in CHANGELOG.md](../../CHANGELOG.md#xxx)
  - 备注：踩到的坑 / 未达标的指标 / 后续衍生 OPT
```

### Pre-release P0 batch (2026-05-10, awaiting tag)

- **OPT-001** Prompt caching observability foundation
  - 实际工时：~1.5d (含 DashScope nested cache 字段 bug 修复 + worker /metrics endpoint 新增)
  - CHANGELOG: [Per-operation LLM token observability (OPT-001)](../../CHANGELOG.md)
  - 验证 baseline: [tests/quality/baseline-pre-p0.json](../../tests/quality/baseline-pre-p0.json)
  - 验证结果（60s 短视频）: [tests/quality/baseline-post-p0.json](../../tests/quality/baseline-post-p0.json)
  - 验证结果（10min 长视频，retry oscillation cancel）: [tests/quality/baseline-post-p0-10min.json](../../tests/quality/baseline-post-p0-10min.json) — **发现 translate 系统提示符不字节稳定的设计缺陷**（targetSec 嵌入 system role 导致每段不同），followup-1 已提升到 P0 + 已知具体修复方案
  - 验证结果（10min 语义切分 FULL run + episode judge）: [tests/quality/baseline-post-p0-10min-final.json](../../tests/quality/baseline-post-p0-10min-final.json) — job 131 完整跑完 25/25 segments，episode judge 用 qwen-max 给出 **production_ready**（7 维度 0.95–1.00），术语一致性 1.00，10/10 高频术语跨段一致；判断 OPT-001 metric 管线正确，translate 路径 0% 完全是 prompt 字节不稳定造成的（judge 路径同 provider 同 binary 能拿到 4–6% cached）
  - 踩到的坑：(1) DashScope qwen-turbo 的 cached_tokens 嵌套在 `prompt_tokens_details.cached_tokens`，初版 binary 漏 → providerUsage.effectiveCached() 三 shape max；(2) 长视频验证暴露 prompt 字节不稳定（每段 targetSec 不同），需在 user message 而非 system 中传递
  - 未达标指标：translate 路径 cache 命中率 0%（10min 视频 29 次调用），原因是 prompt 设计缺陷而非 provider 限制；判断 fix 后将达 ≥30%。judge 路径在 60s 视频上 8.5%、10min 上 4.5%（judge 调用之间 gap 较长，DashScope cache TTL 可能短）
  - 衍生：OPT-001-followup-1 (P0, fix 已知), OPT-001-followup-2 (streaming usage capture), OPT-FOLLOWUP-3 (drift threshold 长段调优)
- **OPT-003** Function calling for segment_review
  - 实际工时：~1d
  - CHANGELOG: [Function calling for segment_review (OPT-003)](../../CHANGELOG.md)
  - 备注：DashScope 上的 kimi-k2.5 + qwen-turbo 都完美支持 OpenAI-compatible function calling，0 fallback。chatMessage/toolDef/toolCall 等 named types 同时被 OPT-002 复用。
  - 长视频验证：job 131（25 段，1700+ tokens/call）单次 review 调用走 strict tool path 0 fallback（详见 baseline-post-p0-10min-final.json）
- **OPT-002** LLM-as-Judge MVP (observe-only)
  - 实际工时：~1d (function-calling infra 由 OPT-003 提前打通节省时间)
  - CHANGELOG: [LLM-as-Judge in observe-only mode (OPT-002)](../../CHANGELOG.md)
  - 短视频验证：60s 视频 5/5 segments judged，segment 4 准确识别"漏译 monitoring"
  - 长视频验证（cancelled run）：10min 视频 13/13 已合成 segments judged (100%)，平均 0.96，10×1.0 + 2×0.9 + 1×0.8，全部 verdict=accept（**0 false-positive 重译触发** —— 即使 segment 4 drift 11.5% 长段 judge 仍 accept，证明 judge 与 drift 信号互补）
  - 长视频验证（FULL run, job 131）：18/25 segments judged，平均 0.994，与 episode judge `overall_fidelity=0.98` 强相关 — 直接为 OPT-202（speculative ensemble + judge 聚合）提供经验数据
  - 未判分缺口：job 131 7/25 段未判分，源于 worker 在那些段合成时正在重启窗口；衍生 **OPT-002-followup-2 (back-fill endpoint)**
  - 关键发现：drift 信号要求 segment 4 重译（11.5% > 6% 阈值），但 judge 说 accept —— 这是 OPT-201 SegmentAgent 接入决策时让 judge 短路 drift retry 的典型用例（OPT-FOLLOWUP-3）
  - 衍生：OPT-002-followup-1 (golden set ≥50 条；10min job 已贡献 13+18 条候选), OPT-002-followup-2 (back-fill judge for restart-window gaps), OPT-FOLLOWUP-3 (judge VETO drift retry)

#### 10min 全程 episode-judge（2026-05-10 验证完成，未独立立项）

- 工件：[`scripts/episode_judge.ps1`](../../scripts/episode_judge.ps1) + [`tests/quality/episode-judge-job-131.json`](../../tests/quality/episode-judge-job-131.json)
- 一次性 PowerShell 脚本，把整集 (src, tgt) 全段拼一次性 prompt，调 qwen-max 评 7 维度（terminology / register / narrative / character voice / cultural / fidelity / fluency）+ 强弱段落 + 术语 glossary + verdict
- 结果：job 131 verdict=`production_ready`，输入 4853 tokens，输出 833 tokens，单次调用 ~$0.005
- 工程坑：(a) DashScope `tools` 模式拒收 strict-schema → 退回 `response_format=json_object`；(b) PowerShell 5 默认 ISO-8859-1 解 UTF-8 响应 → 用 `Invoke-WebRequest` + `[ISO-8859-1].GetBytes(Content)` 反编码再 UTF-8 还原
- 衍生新 OPT 候选：**OPT-EPISODE-JUDGE-PROMOTE**（把 PowerShell 脚本提升为 `/jobs/:id/episode-judge` Go 端点，merge 后自动跑；当 per-segment 平均 < 0.9 才升级到 qwen-max）— 待立项编号

---

## 7. 维护约定

1. **新增 OPT**：只追加 ID，不复用已废弃 ID；填齐节 1.3 全部字段；同时更新节 3 总览表
2. **状态变更**：必须在节 4 卡片 + 节 3 总览表两处同步
3. **PR 引用**：PR title 以 `[OPT-XXX]` 开头；commit message 包含 `Refs OPT-XXX`
4. **Done 流程**：merge 后改 status=done → CHANGELOG 加条目（必含 `(OPT-XXX)`） → 下次 release tag 时移到节 6
5. **review**：每周对照节 3 总览表过一遍，把 stuck 在 in_progress > 2 周的 OPT 拿出来讨论
