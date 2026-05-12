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
| OPT-001-followup-1 | translate prompt 字节稳定 (move targetSec to user msg) | P0 | done | 0.5d | OPT-001 |
| OPT-002 | LLM-as-Judge MVP | P0 | done | 2d | - |
| OPT-003 | Function calling 替代 prompt+JSON parse | P0 | done | 1d | - |
| OPT-FOLLOWUP-3 | Drift threshold 长段调优 (a) / judge 短路 retry (b) | P1 | (a) done / (b) planned | 1d (a) / OPT-201 (b) | OPT-002 |
| OPT-101 | OpenTelemetry GenAI semconv + cost USD trace | P1 | planned | 2d | - |
| OPT-102 | Plan Mode 段审 / TodoWrite 风格 | P1 | planned | 3d | OPT-003 |
| OPT-103 | MCP server 暴露 ml-service | P1 | planned | 1d | - |
| OPT-104 | Agent transcript 持久化 | P1 | planned | 1.5d | OPT-101 |
| OPT-401 | Episode / Chapter 数据模型（长视频三层级基础） | P1 | done | 3d | - |
| OPT-402 | Pipeline 重构：episode-level stages + glossary_extract | P1 | done | 3d | OPT-401 |
| OPT-403 | Chapterize 算法 + fan-out 多 chapter job | P1 | done | 5d | OPT-401, OPT-402 |
| OPT-201 | SegmentAgent ReAct 重构 | P2 | planned | 5d | OPT-002, OPT-104 |
| OPT-202 | Speculative ensemble + judge | P2 | planned | 3d | OPT-002 |
| OPT-203 | Streaming TTS + SSE 推送 | P2 | planned | 5d | - |
| OPT-204 | 结构化情感/韵律输出 | P2 | planned | 2d | OPT-003 |
| OPT-205 | Reasoning model 全场景化 | P2 | planned | 1d | - |
| OPT-206 | Skills / Glossary 系统 | P2 | planned | 3d | - |
| OPT-404 | Episode merge + 跨 chapter 一致性广播 | P2 | done | 3d | OPT-403 |
| OPT-405 | LLM-Driven Chapterization（语义切分替代 DP） | P2 | done | 3d | OPT-403, OPT-402 |
| OPT-405.1 | Multi-Model Chapterize Benchmark（kimi-k2.5 baseline） | P2 | done | 1d | OPT-405 |
| OPT-406 | Episode-level Judge productize（兼容 OPT-EPISODE-JUDGE-PROMOTE） | P2 | done | 2d | OPT-404, OPT-409 |
| OPT-407 | Closed-loop rework engine（三级 verdict → 返工调度） | P2 | done | 5d | OPT-409, OPT-406（OPT-201 软依赖：用现有 RetryJob，未走 SegmentAgent）|
| OPT-408 | Multi-episode 调度 + GPU 公平性 | P2 | planned | 3d | OPT-403 |
| OPT-409 | Chapter-level Judge（原 OPT-405 计划，2026-05-11 重编号；OPT-405 ID 已被 LLM-Driven Chapterization 占用） | P2 | done | 2d | OPT-403, OPT-002 |
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

    %% 长视频三层级线（OPT-401..409）
    subgraph long_video["长视频三层级（Episode → Chapter → Segment）"]
        direction TB
        OPT401[OPT-401 数据模型]
        OPT402[OPT-402 Episode pipeline + glossary]
        OPT403[OPT-403 Chapterize + fan-out（DP）]
        OPT404[OPT-404 Episode merge + 一致性广播]
        OPT405[OPT-405 LLM-Driven Chapterize]
        OPT405_1[OPT-405.1 Multi-Model Bench]
        OPT406[OPT-406 Episode Judge]
        OPT407[OPT-407 Closed-loop rework]
        OPT408[OPT-408 多 episode 调度 + GPU 公平]
        OPT409[OPT-409 Chapter Judge]
    end

    OPT401 --> OPT402
    OPT401 --> OPT403
    OPT402 --> OPT403
    OPT402 --> OPT405
    OPT403 --> OPT404
    OPT403 --> OPT405
    OPT403 --> OPT408
    OPT403 --> OPT409
    OPT405 --> OPT405_1
    OPT404 --> OPT406
    OPT002 --> OPT409
    OPT409 --> OPT406
    OPT409 --> OPT407
    OPT406 --> OPT407
    OPT201 --> OPT407
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
  - **OPT-001-followup-1 (DONE 2026-05-10)**: `buildTranslateSystemPrompt` 已去除 `targetSec` / `limit` 参数，二者改在 user message 末尾以 "Hard duration constraint" 行注入；system prompt 现在仅随 `targetLanguage` + `translationSummary` 变化，job 内字节稳定。`internal/llm/client_test.go` 加反向断言 (`TestSystemPromptStable`) + 新增 `TestTranslateUserMsgContainsPerSegmentConstraints`。验证：60s 视频 cache hit 21.4%（segment 数 7，受 cluster 数限制），10min 验证待补 baseline-post-p0-opt402-10min.json。详见节 6。
  - **OPT-001-followup-2 (DONE 2026-05-11)**: `doChatStream` 已在 `internal/llm/client.go:969-979` 解析 SSE 最终 chunk 的 `usage` 字段（DashScope `chunk.Usage != nil` 时取 `prompt_tokens / completion_tokens / cached_tokens`），并在 line 992-993 透传给 `observability.ObserveLLMTokens(payload.Model, operation, ...)`。验证：跑 `cmd/chapterize-bench` 含 thinking model 的 baseline 后，`worker:8081/metrics` `holodub_llm_input_tokens_total{model="kimi-k2-thinking"}` > 0 不再为 0。本条目此前因 OPT-001 收尾时同步落地、roadmap 漏标，2026-05-11 OPT-409 巡检时补标。
  - **OPT-FOLLOWUP-3 (a) DONE 2026-05-10 / (b) 仍 planned**：(a) `internal/pipeline/tts/budget.go` 加 `AdaptiveMinDriftThreshold` pure function：targetSec ≥ 20s 时把 floor 抬到 0.06、≥ 10s 抬到 0.05、≤ 5s 保持 0.03，杜绝长段 retry 漩涡；6 个单测覆盖边界。`stage_tts.go` 调用点接入。`.env` 长警告已删。(b) 让 judge verdict='accept' 短路 drift retry 仍需 OPT-201 SegmentAgent 决策路径就位。
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
  - **OPT-002-followup-2 (DONE 2026-05-11)**: worker 启动 judge back-fill — 跑完 OPT-409 同 PR 落地。`internal/store/store.go` `ListSegmentsAwaitingJudge(ctx, limit)` + 单测；`internal/pipeline/judge_backfill.go` `(*Service).BackfillSegmentJudges(ctx, limit, concurrency)` bounded concurrency；`cmd/worker/main.go` 服务初始化后 spawn 15s 延迟 goroutine；env `JUDGE_BACKFILL_ON_START=true / JUDGE_BACKFILL_LIMIT=500`。staging 验证：worker 重启后未判分段 5908 → 5408（500 段补齐 in 12s）。详见 §6 archive。
  - OPT-002-followup-3: backfill 路径补 PrevContext（当前传 nil 简化首版）
  - OPT-002-followup-4: 决策接入留给 [OPT-201](#opt-201-segmentagent-react-重构) (`JudgeObserveOnly=false`)
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

#### OPT-401 Episode / Chapter 数据模型（长视频三层级基础）

- **Status**: done (2026-05-10; 1-chapter shortcut 完整兼容历史 100+ job)
- **Source**: 业务对话 2026-05（长视频分章节处理需求）
- **Estimate**: 3d (实际 ~2d)
- **Depends on**: -
- **背景与命名**：当前 `Job` 一对一对应一个完整视频的处理流程；处理 60+ 分钟长视频时 ASR/翻译/TTS 全部串行、单点失败影响范围大、retry 漩涡阻塞整个 job。引入三层级：
  | 层级 | 含义 | 对应 DB 实体 |
  |---|---|---|
  | **Episode** | 用户上传的整个原始视频（顶层，新增） | 新表 `episodes` |
  | **Chapter** | 自动切出的 20-30 min 子片段；执行单元 | 复用 `Job`，加 `episode_id / chapter_ordinal / chapter_start_ms / chapter_end_ms` 列 |
  | **Segment** | 现有 ASR 段（最小粒度，不变） | `segments` |
- **Outcome**:
  - 现有所有单视频 Job 自动等价于"1-chapter episode"，DB 迁移加 default episode 行；零行为变化
  - Episode 层暴露 `GET /episodes/:id` API 看进度（chapters_pending / chapters_done / episode_status）
  - 为 OPT-402..408 提供数据基础
- **Verification**:
  - migration 在含 100+ historical job 的 staging DB 上向前/向后迁移 100% 等价
  - 现有 `POST /jobs` API 行为不变（自动建 episode + chapter_ordinal=1）
  - `GET /episodes/:id/chapters` 返回正确顺序
- **Rollout**:
  - L1 单测 model + transition
  - L2 staging 跑 1 周（所有新 job 自动走 episode wrapping）
  - L3 production read-only 验证迁移
  - L4 启用 episode-level UI
- **Related rules**: [incremental-evolution.mdc#2](../../.cursor/rules/incremental-evolution.mdc) (DB nullable + 默认值), [go-backend.mdc](../../.cursor/rules/go-backend.mdc)
- **关键改动点**:
  - migration `migrations/00X_episodes.sql`：

    ```sql
    CREATE TABLE episodes (
      id BIGSERIAL PRIMARY KEY,
      tenant_key TEXT NOT NULL DEFAULT 'default',
      name TEXT,
      source_video_relpath TEXT NOT NULL,
      source_language TEXT, target_language TEXT,
      duration_ms BIGINT,
      total_chapters INT NOT NULL DEFAULT 1,
      glossary_jsonb JSONB,           -- OPT-402 写入
      reference_card TEXT,            -- OPT-402 写入（episode-wide style/register）
      status TEXT NOT NULL,           -- pending | chaptering | dispatched | running | merging | judging | reworking | completed | failed
      episode_judge_score NUMERIC,    -- OPT-406 写入
      episode_judge_meta JSONB,       -- OPT-406 写入
      output_relpath TEXT,            -- 最终拼出来的视频
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at TIMESTAMPTZ
    );
    ALTER TABLE jobs
      ADD COLUMN episode_id BIGINT REFERENCES episodes(id),
      ADD COLUMN chapter_ordinal INT NOT NULL DEFAULT 1,
      ADD COLUMN chapter_start_ms BIGINT NOT NULL DEFAULT 0,
      ADD COLUMN chapter_end_ms BIGINT NOT NULL DEFAULT 0;
    -- back-fill: 每个现有 job 创建一个 episode_id
    INSERT INTO episodes (id, source_video_relpath, source_language, target_language,
                          status, output_relpath, created_at)
      SELECT id, input_relpath, source_language, target_language,
             CASE WHEN status = 'completed' THEN 'completed' ELSE 'running' END,
             output_relpath, created_at FROM jobs;
    UPDATE jobs SET episode_id = id;  -- 1:1 mapping for existing data
    ALTER TABLE jobs ALTER COLUMN episode_id SET NOT NULL;
    CREATE INDEX ON jobs (episode_id, chapter_ordinal);
    ```

  - Go：新增 `internal/models/episode.go` (`Episode struct` + `EpisodeStatus` 类型 + `Transition()` 状态机)
  - Go：`Job` struct 加 `EpisodeID uint`, `ChapterOrdinal int`, `ChapterStartMs int64`, `ChapterEndMs int64`
  - Go：`internal/store/episodes.go` (CRUD + 子句 join chapter list)
  - Go：`internal/http/router.go` 新增 `GET /episodes`, `GET /episodes/:id`, `GET /episodes/:id/chapters`
  - 前端：`ui/src/api.ts` 加 Episode 类型；新页面 `EpisodeDetail.vue`（chapter 进度网格）
- **风险与待决策**:
  - 是否同时给 `segments` 表加 `episode_id` 冗余列以加速跨 chapter 查询？建议否：通过 `JOIN jobs ON segments.job_id = jobs.id` 即可，减少 denormalization
- **PRs**: TBD（建议拆 3 个：migration / model+store / api+ui）

---

#### OPT-402 Pipeline 重构：episode-level stages + glossary_extract

- **Status**: done (2026-05-10; 1-chapter 路径生效；OPT-403 落地后 N-chapter 分支也已通)
- **Source**: 业务对话 2026-05
- **Estimate**: 3d (实际 ~2d, 借力已就位的 OPT-003 function calling 基础设施)
- **Depends on**: OPT-401
- **背景**：当前 `media → separate → asr_smart → segment_review → translate → tts_duration → merge` 全链在单个 Job 上执行。要支持长视频按章节切，必须把"对整个 episode 只需做一次"的 stages 上提到 episode 级，避免每个 chapter 重复跑一遍 separate / ASR。
- **新 pipeline 模型**：

  ```
  Episode-level stages (跑在整个原视频上):
    ├─ media            (复用)
    ├─ separate         (复用 — 整 episode 一次 separate，BGM/vocals 整流通过)
    ├─ asr_smart        (复用 — 整 episode 一次 ASR，避免边界字漏听)
    ├─ glossary_extract (新增 — LLM 扫全文 ASR 提 episode-level glossary + reference card)
    └─ chapterize       (新增 — OPT-403 实现，输出 chapter 边界并 fan-out chapter jobs)

  Chapter-level stages (跑在每个 chapter job 上, 多 job 并行):
    ├─ segment_review   (复用，但作用域缩小到 chapter)
    ├─ translate        (复用，每段都拿 episode-level glossary)
    ├─ tts_duration     (复用)
    └─ chapter_merge    (复用现有 merge 逻辑，但产物是 chapter 视频)

  Episode-level final stages (新增):
    ├─ episode_merge    (OPT-404 — 把 N 个 chapter 视频按时间序 concat)
    ├─ episode_judge    (OPT-406 — 全篇评估)
    └─ maybe_rework     (OPT-407 — 决定是否触发返工)
  ```
- **Outcome**:
  - 短视频（<20min，1 chapter）行为完全等价于现状（pipeline 自动短路 episode → chapter 区分）
  - 长视频 separate/ASR 只跑一次（避免 5x 重复 GPU 推理）
  - Episode-level glossary 给所有 chapter 翻译做术语 anchor，跨 chapter 一致性的基础
- **Verification**:
  - 60s 烟测视频 wall time 不超过现有 baseline ± 5%
  - 30min 视频跑通：separate 1 次、ASR 1 次、glossary_extract 1 次、chapter jobs N=2，chapter 间术语一致 ≥ 95%
  - 现有 baselines (`tests/quality/baseline-pre-p0.json` etc.) 重跑通过
- **Rollout**:
  - L1 单测各 stage transition
  - L2 staging：所有 episode 强制走 1-chapter 路径（验证短路）
  - L3 staging：开启 N-chapter 路径（依赖 OPT-403）
  - L4 production
- **Related rules**: [incremental-evolution.mdc#1](../../.cursor/rules/incremental-evolution.mdc) (新旧并存), [go-backend.mdc](../../.cursor/rules/go-backend.mdc), [llm-call-standards.mdc](../../.cursor/rules/llm-call-standards.mdc) (glossary_extract 走 function calling)
- **关键改动点**:
  - `internal/models/models.go`：新增 `EpisodeStage` 枚举 + `EpisodeStageOrder`，与 `JobStage` 平行
  - `internal/pipeline/`：新增 `episode_stages.go`，把 media/separate/asr_smart 改为接 episode 而非 job
  - `internal/pipeline/stage_glossary_extract.go` 新增：用 OPT-003 function calling，schema:

    ```json
    {
      "glossary": [{"source": "MapReduce", "target": "MapReduce", "note": "保留英文"}],
      "speakers": [{"source_label": "speaker_0", "display_name": "Robert Morris", "voice_register": "academic"}],
      "reference_card_md": "**Genre**: ...\n**Register**: ..."
    }
    ```
  - `internal/llm/client.go` 加 `ExtractEpisodeGlossary(ctx, asrFullText, srcLang, tgtLang) (GlossaryResult, error)`
  - 任务调度：episode 任务和 chapter 任务用同一个 redis queue，但 stage 字段区分（worker 看 stage 决定 actor）
  - DB：`episodes.glossary_jsonb` + `episodes.reference_card` 由 glossary_extract stage 写入
  - 现有 `Job.TranslationSummary` 字段保留兼容（chapter job 可继承自 `Episode.reference_card`）
- **风险与待决策**:
  - **glossary_extract 在 chapterize 前还是后？** 推荐**前**：先有全 episode glossary 才能让所有 chapter 翻译用同一份术语；如果放后，第 1 个 chapter 拿不到术语。代价是 glossary 质量略逊（chapter 边界还没切，topic 维度信息少），但通过 reference_card 可补
  - **glossary 提取的 LLM 成本**：1 小时讲座 ASR 约 8K tokens × qwen-turbo ≈ $0.005，可忽略
- **PRs**: TBD（建议拆 4 个）
- **实际改动**:
  - `internal/models/models.go` 新增 `EpisodeStage` 枚举 + `EpisodeStageOrder` (`ep_media → ep_separate → ep_asr_smart → ep_glossary_extract → ep_chapterize`)；`TaskPayload` 加 `EpisodeID / EpisodeStage`
  - migration `migrations/006_episode_pipeline.sql` 加 `episodes.vocals_rel_path / bgm_rel_path / asr_done_at / glossary_done_at`
  - `internal/llm/glossary.go` 新增 `ExtractEpisodeGlossary` 走 strict tool call (`emit_episode_glossary` schema)
  - `internal/pipeline/pipeline.go` 加 `EnqueueEpisodeStage` + `handleEpisodeStage` 路由；`stage_glossary_extract.go` 新文件
  - `stage_tts.go` 把 episode glossary + reference card 注入 `RetranslateWithConstraint` 提示
  - `internal/store/store.go` 加 `UpdateEpisodeMediaFromChapter`（1-chapter 短路双写）+ `UpdateEpisodeGlossary`
  - 前端 `EpisodeDetail.vue` 加 episode-stage 进度块 + 术语表
- **实际工时**: ~2d (借力 OPT-003 function-calling 基础设施)
- **验证**:
  - 60s/10min/79min 三档烟测；1-chapter 短路 → media/separate/ASR 后双写 episode 字段，自动入 ep_glossary_extract 队列
  - 长视频证据：[tests/quality/opt402-79min-episode-139.json](../../tests/quality/opt402-79min-episode-139.json) — episode 139 (79min MIT 6.824 lecture) ASR 4.5s 完成、glossary 提取 3.8s 完成，提取出 6 条术语 + 301 字符 reference card
  - 完整 archive 见 §6 `OPT-402` 条

---

#### OPT-403 Chapterize 算法 + fan-out 多 chapter job

- **Status**: done (2026-05-10; 79min episode 142 切出 8 chapters，所有切点 silence ≥850ms)
- **Source**: 业务对话 2026-05
- **Estimate**: 5d
- **Depends on**: OPT-401, OPT-402
- **背景**：长视频切分必须同时满足三个约束：
  1. **硬约束**：每章 [`CHAPTER_MIN_MIN`, `CHAPTER_MAX_MIN`]（默认 [20, 30] min）；最后一章可放宽下限
  2. **音频约束**：切点必须落在静默 ≥ 1.5s 的位置（保证 episode_merge concat 无爆音）
  3. **语义约束**：在硬+音频约束允许的窗口内，优先选 LLM 标记的 topic shift 点
- **算法**（两段式）：
  - **Pass 1 — 候选点提取（无 LLM）**：扫 ASR 输出，对每个相邻 segment 间 gap，记录 `{at_ms, silence_duration_ms, sentence_boundary, paragraph_boundary}`
  - **Pass 2 — DP 全局最优**：动态规划求一组切点 `cuts = [c_1, ..., c_{N-1}]`，最大化总 score:

    ```
    score(c_i) = w_silence  * normalize(silence_duration_ms)
               + w_topic    * topic_shift_prob (LLM)
               + w_balance  * 1 / (1 + |chapter_len_i - target_len|)
               - w_short    * I[chapter_len_i < CHAPTER_MIN_MIN]
    ```

    约束：每段 ∈ [min, max]；切点必须是候选点
  - **Pass 3 — LLM 校核（可选）**：把 pass 2 的方案 + 各切点前后 ±60s 文本喂 LLM，问 "这些切点是否合理？是否有更好的位置？"，作为人工审核前的兜底
- **Outcome**:
  - 79min 测试视频自动切为 3 chapter（避免 4×20min 这种偏短切法）
  - 切点全部命中静默 ≥ 1.5s 的位置（merge 时无需 crossfade）
  - chapter 间术语一致性 ≥ 95%（glossary 已在 OPT-402 提取）
  - fan-out 后 N 个 chapter job 并行入队、Worker 自然消费
- **Verification**:
  - 5 个不同长度（30/45/60/75/90 min）测试视频跑 chapterize：所有切点 silence ≥ 1.5s ✓，chapter 数 ∈ {2..4} ✓
  - 切点准确性人评：≥ 80% 切点位置 ≥ 4/5 评分（0=极差，5=完美）
  - 79min 视频端到端 wall time ≤ 现有单 job × 0.5（并行 3 chapter 应大幅缩短）
- **Rollout**:
  - L1 单测算法（含 corner case：全无 silence、全是 silence、视频过短）
  - L2 staging：N=2 chapter 视频（约 30min）端到端
  - L3 staging：N=3..4（45-90min）
  - L4 production，feature flag `CHAPTERIZE_ENABLED`，可一键退化为单 chapter
- **Related rules**: [agent-design.mdc#3](../../.cursor/rules/agent-design.mdc) (LLM 校核作为 tool), [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc) (function calling for boundary suggest), [incremental-evolution.mdc#1](../../.cursor/rules/incremental-evolution.mdc)
- **关键改动点**:
  - 新增 `internal/pipeline/stage_chapterize.go`
  - 新增 `internal/chapterize/algorithm.go`（纯函数 + 完整单测）
  - 新增 `internal/chapterize/llm_review.go`（OPT-003 风格 tool call）
  - 配置：`CHAPTER_MIN_MIN=20`, `CHAPTER_MAX_MIN=30`, `CHAPTER_TARGET_MIN=25`, `CHAPTER_MIN_SILENCE_MS=1500`
  - 权重 `CHAPTERIZE_WEIGHT_SILENCE=0.4`, `_TOPIC=0.4`, `_BALANCE=0.2`, `_SHORT_PENALTY=2.0`
  - **Fan-out 实现**：chapterize 完成后，pipeline 在事务内创建 N 个 chapter job (`episode_id` = 当前, `chapter_ordinal` = 1..N, `chapter_start/end_ms` = DP 切点)，每个 job 入队 `segment_review` stage（注意：所有 chapter job 共享 episode 的 ASR、separate、glossary，不重跑）
  - 每个 chapter job 启动时从 `episodes.glossary_jsonb / reference_card` 读 episode-level 上下文，注入 translate 提示
  - 短视频短路：episode 时长 < `CHAPTER_MIN_MIN × 2` 直接生成 1 个 chapter，跳过 DP
- **风险与待决策**:
  - **DP 复杂度**：N 个候选点 × M 个章节 = O(N²M)；典型 N=200, M=4 即 16 万次比较，<10ms
  - **LLM 校核是否默认开启**：建议默认开（成本极低，1 次调用 ~$0.002），但可 env 关掉
  - **Speaker 跨 chapter 同一性**：当前 ASR diarization 在 episode 级跑（OPT-402），speaker_label 全 episode 一致；chapter job 直接用现有 binding 即可
  - **Audio 实际切割**：chapter job 不需要物理切音频，TTS / merge 仍按 ASR segment 时间戳（相对 chapter_start_ms）操作；仅 merge 阶段产物是 chapter 视频片段
- **PRs**: TBD（建议拆 5 个：算法 + LLM 校核 + stage 实现 + fan-out + UI 显示）
- **实际改动**:
  - `internal/chapterize/algo.go` 新增 Pass 1 `ExtractCandidates`（基于 ASR 静默间隙）+ Pass 2 `DPOptimalCuts`（动态规划，min/target/max 18/22/30min 约束 + 静默偏好惩罚）+ `BuildChapterRanges`，配 13 个测试覆盖空输入 / 单候选 / 边界条件 / 79min 合成长视频
  - `internal/llm/chapter_review.go` 新增 Pass 3 `ReviewChapterCuts` strict tool call（`emit_chapter_review` schema），生成中英双语 chapter title + summary，配 6 个 mock 测试
  - `internal/pipeline/stage_chapterize.go` 新增 `runEpisodeChapterize` orchestrator + `runFanOutChapters` 7 步 fan-out（slice 媒体 / 更新 ch1 范围与路径 / 创建 ch2..N sibling jobs / 段落重映射并平移时间戳 / 更新 episode 元数据 / 重新入队 SegmentReview）
  - `internal/store/store.go` 加 `ListSegmentsByEpisode / ReassignSegmentsToChaptersAndShift / CreateChapterJob / UpdateChapterMetadata / UpdateChapterRange / UpdateEpisodeChapters / UpdateEpisodeOutput / UpdateLoudnormStats`
  - `internal/models/models.go` 加 `Job.ChapterTitle / ChapterTitleTranslated / ChapterSummaryMD` + `Episode.OutputLayoutVersion / ChaptersManifestRelPath / LoudnormStats` + `JobStatusAwaitingChapterize` 状态
  - `internal/media/ffmpeg.go` 加 `SliceVideoAtRange / LoudnormTwoPass / ConcatChapterVideos`
  - migration `migrations/007_chapter_metadata.sql`
- **实际工时**: ~3d
- **验证**:
  - 算法基线：[docs/opt-403/baseline-opt403-79min.json](../../docs/opt-403/baseline-opt403-79min.json) — 79min 合成 episode (79 segments / 55s avg seg / 1.6s avg gap) → DP 切出 3 chapter（24.55 / 25.47 / 24.56 min），mean 24.86min（target 22min, 偏差 ≤15.8%），所有 chapter 时长 ∈ [18min, 30min] 区间，候选采样数 78、所有切点 silence ≥850ms（默认阈值 800ms）
  - 真实场景：episode 142 (79min, MIT 6.824) → 8 chapters，最终被 OPT-405 LLM 切分接管为默认路径，DP 仅作 fallback
  - 完整 archive 见 §6 `OPT-403` 条

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

#### OPT-404 Episode merge + 跨 chapter 一致性广播

- **Status**: done (2026-05-10; 1-chapter hardlink shortcut + N-chapter concat + chapters.json 全部上线)
- **Source**: 业务对话 2026-05
- **Estimate**: 3d
- **Depends on**: OPT-403
- **背景**：OPT-403 fan-out 完成后，所有 chapter job 并行跑。本 OPT 解决两件事：(1) 所有 chapter 跑完后如何拼成最终 episode 视频；(2) 如何在并行翻译过程中"广播"额外术语 / 风格修正给其他 chapter（OPT-407 闭环反馈的载体）。
- **Outcome**:
  - 新增 `episode_merge` stage：等待所有 chapter job 状态为 `completed`，按 `chapter_ordinal` 顺序 ffmpeg concat 视频流（含视频 + dubbed audio + BGM），写入 `episodes.output_relpath`
  - 新增 `glossary broadcast` 机制：episode 拥有 `glossary_version` 单调递增计数器；chapter 翻译时记录其使用的 glossary_version；OPT-407 触发 glossary 修正时 +1 版本，所有 chapter 中 version 落后的段进入 `pending_rework` 状态
  - Episode 状态机：`chaptering → dispatched → running → all_chapters_done → merging → judging → (rework → ... 循环) → completed`
- **Verification**:
  - 3-chapter 测试视频：所有 chapter 正确按时序拼接，无音频跳跃
  - 故意触发 glossary update：受影响 chapter segment 重译并复用既有 voice profile，最终 episode 输出术语统一
  - episode_merge 失败时（如某 chapter 卡住）状态正确停在 `running`（不进 merging），不丢数据
- **Rollout**: L1→L4，feature flag `EPISODE_MERGE_ENABLED`
- **Related rules**: [incremental-evolution.mdc#3](../../.cursor/rules/incremental-evolution.mdc) (输出路径区分维度), [agent-design.mdc#5](../../.cursor/rules/agent-design.mdc) (取消 / 部分失败语义)
- **关键改动点**:
  - 新增 `internal/pipeline/stage_episode_merge.go`
  - 新增 `internal/store/episode_progress.go`（轮询/事件触发，判断 all-chapters-done）
  - `Episode` 加 `glossary_version INT NOT NULL DEFAULT 1`；`Job` 加 `glossary_version_used INT`
  - 输出路径规范：`data/episodes/{episode_id}/output.mp4`（包含 episode_id 和最终 mux 结果，与 chapter 视频 `data/jobs/{job_id}/merge.mp4` 区分）
  - 触发器：每个 chapter job 进入 `completed` 时通知 episode coordinator 检查是否全部完成
  - 异常处理：某 chapter `failed` → episode 进入 `partial_failed`，等用户决策（rerun chapter 或 abandon）
- **风险与待决策**:
  - **是否引入"软等待"** vs 真正的事件驱动：先用轮询（worker poll cycle 5s），后续 OPT-203 SSE 完成后切到事件
  - **glossary broadcast 的 cost ceiling**：每次 broadcast 重译 N 段，可能 N=几十；OPT-407 实施时需要明确单次 broadcast 上限段数
- **PRs**: TBD
- **实际改动**:
  - `internal/episode/chapters_manifest.go` 新增 `ChaptersManifest` 结构（schema_version=1）+ `WriteChaptersJSON`（原子写、缩进、自动时戳）+ `Validate / SortChapters / ReadChaptersJSON`，配 7 个测试覆盖空 / 错版 / 乱序 / 缺文件等场景
  - `internal/pipeline/stage_episode_merge.go` 新增 `runEpisodeMerge`（1-chapter hardlink shortcut + N-chapter `ConcatChapterVideos` + 可选 master EBU R128 pass + 写 chapters.json + 更新 Episode.OutputRelPath/ChaptersManifestRelPath/OutputLayoutVersion=2 + 转 Completed 状态）+ `maybeEnqueueEpisodeMerge`（chapter 完成时 idempotent trigger）
  - `internal/pipeline/pipeline.go` `runMerge` 输出路径切到统一 layout `episodes/{ep_id}/chapters/vp{vp}/ch{ord:02d}.mp4`（旧 `jobs/{id}/output/...` 仅作为 layout v1 兜底），加 `runChapterLoudnorm` + `persistChapterLoudnormStats` 用 flat key `vp{N}_chXX` 写到 `Episode.LoudnormStats` 避免与 master pass 冲突
  - `internal/http/router_episode_downloads.go` 新增三个下载端点 `GET /episodes/:id/download/final`、`GET /episodes/:id/chapters.json`、`GET /jobs/:id/download/final`，全部从 DB 读 relpath 不自行拼路径
  - 前端 `ui/src/components/EpisodeDetail.vue` 增加 layout v1/v2 badge、loudnorm-applied 标识、chapterize / episode_merge 进度 pill、双语 chapter title 渲染（translated > source > "Chapter N"）+ 章节级下载按钮
  - `cmd/migrate-output/main.go` 新增 138 历史 episode 一次性 back-fill 工具（--dry-run/--use-hardlink/--keep-old/--episode-ids/--limit/--record）
  - **Glossary broadcast / OPT-407 闭环 rework 部分**未在本 OPT 落地，留给 OPT-407
- **实际工时**: ~2d (与 OPT-403 同 PR)
- **验证**:
  - 算法基线：[docs/opt-403/baseline-opt403-79min.json](../../docs/opt-403/baseline-opt403-79min.json)（OPT-403 + OPT-404 共享）
  - Back-fill 报告：[docs/opt-403/opt403-backfill-dry-run.json](../../docs/opt-403/opt403-backfill-dry-run.json) — 74 episodes scanned，44 可迁移 (31 GB hardlink, 不重编码), 30 因历史 chapter `output_relpath` 为空需手动处置，总耗时 ~200ms
  - 真实场景：episode 142 (79min, 8 chapters) 端到端通过 chapter merge → episode merge → chapters.json → UI 章节卡渲染
  - 完整 archive 见 §6 `OPT-404` 条

---

#### OPT-405 LLM-Driven Chapterization（语义切分替代 DP）

- **Status**: done (2026-05-11; kimi-k2.5 baseline 锁定，DP 自动降级为 fallback)
- **Source**: 业务对话 2026-05-11（用户对 OPT-403 DP 切分质量不满意，要求"先从语义上做一个大致的主题划分，对时长要求反而不应该限制太多"）
- **Estimate**: 3d (实际 ~2d，借力 OPT-402 glossary 提取的 strict tool call 基础设施)
- **Depends on**: OPT-403（fan-out + DP 兜底）, OPT-402（glossary 提取入口同 LLM 调用合并）
- **历史说明**：本 ID 原计划 = "Chapter-level Judge"；2026-05-11 重定义为 LLM-Driven Chapterization。原 Chapter Judge 计划重编号到 [OPT-409](#opt-409-chapter-level-judge)，详见节 7 维护约定 + 节 6 archive 末尾说明。
- **背景**：OPT-403 的 DP 切分（quadratic deviation from target + silence reward）能保证时长均匀，但完全不看语义；用户在 episode 142（79min）上发现 chapter 内"主题没说完就切掉、下一章接着补尾巴"，要求把整段 ASR 喂给 LLM 做语义优先的切分。
- **Outcome**:
  - `ExtractEpisodeGlossary`（`internal/llm/glossary.go`）的 strict tool call schema 扩 `chapters[]` 字段，与 glossary / speakers / reference_card 在同一次 LLM 调用里产出（避免双跑）
  - 新增 `internal/chapterize/llm_apply.go` 包含三个纯函数：
    - `ValidateLLMPlan`：拒收乱序 / 越界 / 重叠的 segment index
    - `SnapBoundariesToSilences`：把 LLM 给的切点吸到最近的 ASR 静默缝（≥ `CHAPTERIZE_MIN_SILENCE_GAP_MS`，避免在词中间切）
    - `EnforceHardConstraints`：< `CHAPTERIZE_HARD_MIN_MS` 的章节合并入邻居；> `CHAPTERIZE_HARD_MAX_MS` 的章节按内部最宽静默二分
  - `internal/pipeline/stage_chapterize.go` `runEpisodeChapterize` 优先尝试 `tryLLMChapterPlan`，失败 / LLM 计划被拒才 fallback 到 OPT-403 DP
  - 新增 DB 列 `episodes.llm_chapters JSONB`（migration `migrations/008_llm_chapters.sql`）持久化原始 LLM 计划，方便后续审计 / OPT-405.1 bench 复现
  - 副产物（`internal/llm/client.go` 修复）：`doChatToolOnce` 检测到 model 名含 `thinking` 自动切换到 `c.thinkingHTTPClient`（10 min 超时），`glossary.go` 检测到 thinking model 自动把 `tool_choice` 从 `forceToolChoice("emit_episode_glossary")` 降为 `"auto"`（DashScope thinking endpoint 拒收 strict tool_choice）
- **Verification**:
  - 端到端：episode 142 (79min, 176 segments) → kimi-k2.5 切出 8 chapters，全部通过 validate / snap / hard-constraint，无 fallback 触发
  - LLM-as-judge 评分：boundary coherence + title quality + topic completeness 三维平均 4.76 / 5（OPT-405.1 baseline 见下条）
  - 单元测试：`internal/chapterize/llm_apply_test.go` 覆盖空计划 / 单 chapter / 越界 / 时长超限 / 静默吸附 5 大类边界
  - 兼容性：`CHAPTERIZE_LLM_DRIVEN=false` 时所有 OPT-403 DP 行为 100% 不变（feature flag 灰度可一键退化）
- **Rollout**: L4 production, default `CHAPTERIZE_LLM_DRIVEN=true`, `GLOSSARY_MODEL=kimi-k2.5`（OPT-405.1 baseline 锁定）
- **Related rules**: [agent-design.mdc#3](../../.cursor/rules/agent-design.mdc) (LLM tool call), [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc) (function calling), [incremental-evolution.mdc#1](../../.cursor/rules/incremental-evolution.mdc) (feature flag + DP fallback)
- **关键改动点**:
  - `internal/llm/glossary.go`：`emit_episode_glossary` schema 扩 `chapters[]`；新增 `isThinkingModelName` 帮助函数；`ToolChoice` 在 thinking model 上降为 `"auto"`
  - `internal/llm/client.go`：`doChatToolOnce` 在 thinking model 上切到 `c.thinkingHTTPClient`（10 min timeout）
  - `internal/chapterize/llm_apply.go` 新文件 + `internal/chapterize/llm_apply_test.go`
  - `internal/pipeline/stage_chapterize.go`：`runEpisodeChapterize` 加 `tryLLMChapterPlan`；DP 降级为 fallback
  - `internal/pipeline/stage_glossary_extract.go`：把 LLM 返回的 `chapters` marshal 成 `llmChaptersJSON` 持久化
  - `internal/store/store.go`：`UpdateEpisodeGlossary` 接收 `llmChaptersJSON []byte` 一并写入
  - `internal/models/models.go`：`Episode.LLMChapters datatypes.JSON`
  - `internal/config/config.go`：`ChapterizeLLMDriven bool / ChapterizeHardMaxMs int64 / ChapterizeHardMinMs int64`
  - migration `migrations/008_llm_chapters.sql`：`ALTER TABLE episodes ADD COLUMN IF NOT EXISTS llm_chapters JSONB`
  - env：`CHAPTERIZE_LLM_DRIVEN=true / CHAPTERIZE_HARD_MAX_MS=2700000 / CHAPTERIZE_HARD_MIN_MS=300000`，`GLOSSARY_MODEL=kimi-k2.5`
- **风险与待决策**:
  - **LLM 切分比 DP 慢**：单次调用增加 ~10-30s（取决于 transcript 长度 + 模型），在 episode-level 一次性，可接受
  - **kimi-k2.5 替代风险**：见 OPT-405.1 baseline，clear-win 0.70 分领先，但若未来 DashScope 改价格 / 下线，bench 工具可秒级换主用模型
  - **DP fallback 永远要保留**：避免 LLM 调用 503 / token 超限时整条流水线挂掉
- **PRs**: TBD

---

#### OPT-405.1 Multi-Model Chapterize Benchmark

- **Status**: done (2026-05-11; baseline 锁定 kimi-k2.5 = 4.76 / 5)
- **Source**: 业务对话 2026-05-11（OPT-405 落地后用户问"有没有改善方案"，建议先做多模型对比测试）
- **Estimate**: 1d (实际 ~1d，含 kimi-k2-thinking 超时调试 + 6 模型完整跑批)
- **Depends on**: OPT-405（被测对象）
- **背景**：OPT-405 落地后只测了 kimi-k2.5，缺乏可量化的"为什么是 kimi-k2.5"证据。需要一个可复用的离线 benchmark：fix dataset / fix judge / multi-candidate / multi-run，输出排行榜，未来换 chapter 模型时秒级复评。
- **Outcome**:
  - 新增 CLI `cmd/chapterize-bench`（4 文件 ~1.4k 行）：`main.go` orchestrate + 缓存命中跳过；`runner.go` 单模型单 run 抽取 + validate + snap + hard-constraint + 静态指标；`judge.go` 调度 LLM-as-judge `score_chapter_cuts` strict tool call；`report.go` 渲染 markdown 排行榜 + 机器可读 JSON
  - 新增 `internal/llm/bench.go` `Client.RunBenchToolCall` 通用入口，让离线工具复用主流水线的 retry / observability / timeout / thinking-model 透传逻辑
  - 评判维度（0-5 各项，平均为 total）：
    - boundary_coherence（每个 chapter 内主题封闭、不外溢）
    - title_quality（title + title_translated 与 chapter 实际内容契合）
    - topic_completeness（每章是否把主题"说明白"了，而不是说一半切走）
  - **Baseline 结论**：episode 142（79min, 176 segments）× 6 candidates × 3 runs × 1 judge (kimi-k2-thinking) →
    | 排名 | Model | Total | 备注 |
    |---|---|---|---|
    | 1 | **kimi-k2.5** | **4.76** | 8 chapters, 稳定 |
    | 2 | qwen-max-latest | 4.06 | 6 chapters, 主题略粗 |
    | 3 | deepseek-v3 | 3.81 | 7 chapters, title 准确度一般 |
    | 4 | qwen-plus-latest | 3.27 | 5 chapters, 主题颗粒度过粗 |
    | 5 | kimi-k2-thinking | 3.05 | 7 chapters, judge 跑超时多 |
    | 6 | qwen3-235b-a22b-thinking-2507 | 2.18 | 4 chapters, 切太少 |
- **Verification**:
  - 6 模型全跑通，artefacts 完整存于 [`docs/opt-405/bench-baseline-2026-05-11/`](../../docs/opt-405/bench-baseline-2026-05-11/)（`report.md` / `report.json` / `raw/{model}-run{i}.json` / `judge/{model}-judgment.json` / `chapters-{model}.txt`）
  - 缓存机制：重跑时 `judge/{model}-judgment.json` 命中即跳过，节省 ~70% 重跑时间
  - thinking model 超时回归：本 OPT 暴露的 OPENAI_TIMEOUT_SECONDS=90 不够长问题已在 OPT-405 副产物里修掉（`doChatToolOnce` 自动切 `c.thinkingHTTPClient`）
- **Rollout**: 离线工具，无 production rollout；`docs/opt-405/bench-README.md` 写明用法
- **Related rules**: [agent-design.mdc#3](../../.cursor/rules/agent-design.mdc) (LLM-as-judge), [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc) (strict tool schema), [testing-and-rollout.mdc#3](../../.cursor/rules/testing-and-rollout.mdc) (固定 dataset baseline)
- **关键改动点**:
  - `cmd/chapterize-bench/main.go / runner.go / judge.go / report.go` 新增
  - `internal/llm/bench.go` 新增 `RunBenchToolCall`
  - `docs/opt-405/bench-README.md` 新增使用说明
  - `docs/opt-405/bench-baseline-2026-05-11/` baseline artefacts
- **风险与待决策**:
  - **Judge 模型本身偏见**：kimi-k2-thinking 评 kimi-k2.5 是否有"同源偏好"？mitigation：换 qwen3-thinking 或 deepseek 跨家族 judge 复跑一次（待立 follow-up）
  - **单 episode dataset 偏少**：只跑了 79min lecture，新闻 / 访谈 / 综艺类型未覆盖；待 dataset 扩到 5 类型再 lock-in
- **PRs**: TBD

---

#### OPT-406 Episode-level Judge productize

- **Status**: done (2026-05-11)
- **Source**: 业务对话 2026-05 + 节 6 "OPT-EPISODE-JUDGE-PROMOTE" 候选
- **Estimate**: 2d → **实际 ~0.5d**（与 OPT-409 同结构高复用）
- **Depends on**: OPT-404, OPT-409（原 OPT-405 = Chapter Judge 已重编号，详见 §3 备注）
- **背景**：`scripts/episode_judge.ps1` 已经在 job 131 验证完整可用。本 OPT 把它从一次性 PowerShell 提升为 Go API，做为 `episode_merge` 完成后自动触发的 stage。重点关注**跨章节**维度（segment / chapter judge 都看不到）：
  - 跨 chapter 术语漂移（chapter 1 用"分布式系统"，chapter 3 用"distributed systems"留英文 → 标记不一致）
  - 跨 chapter 角色 voice 漂移（讲师在 chapter 2 突然像换人）
  - 整体叙事弧线 coherence
- **Outcome**:
  - 新增 episode-level judge dispatch 自动跑在 `runEpisodeMerge` 末尾（Episode → Completed 之后）
  - 写入 `episodes.episode_judge_score / episode_judge_meta`
  - **MVP 简化**：单模型 `EPISODE_JUDGE_MODEL=kimi-k2.5`（与 OPT-409 chapter judge 同模型；自动升级到 qwen-max 留 OPT-406-followup-2）
  - 验收：observe-only 模式（`EPISODE_JUDGE_OBSERVE_ONLY=true`）；不阻塞 merge 完成；OPT-407 闭环 rework 上线后会消费此分数
- **Verification**:
  - **L1**：`go vet ./...` clean；`go test ./internal/llm/... -run TestEpisodeJudge` 8 case 全绿（schema valid / disabled / empty / happy path / 缺 verdict 默认 / user msg 组装 / overall score / thinking model tool_choice 降级）；`go test ./internal/store/... -run TestUpdateEpisodeJudgeResult` 1 case 绿（partial UPDATE 不触碰 Status / OutputRelPath / ChaptersManifestRelPath / ReferenceCard）；GOOS=linux GOARCH=amd64 双 binary 编译通过
  - **L2 staging**：episode 131（1-chapter，p0-10min-semantic-validation）触发 retry stage=ep_episode_merge → worker 9 s 内完成 LLM 调用并写入 `episode_judge_score=0.95`，meta JSONB 含 7 axes 全 ≥ 0.95 + verdict=`production_ready` + 8 个 cross-chapter glossary 观察（`MapReduce` → `MapReduce`、`fault tolerance` → `容错`、`distributed systems` → `分布式系统` 等），与 PowerShell 版 [scripts/episode_judge.ps1](../../scripts/episode_judge.ps1) 同 episode 输出一致
  - **L3 跨章 drift correlation 待补**：与 OPT-409 chapter correlation 共用 episode 142/141/140 窗口，等 multi-chapter episode 走通 merge 后做（不阻塞本 OPT 完成）
- **Rollout**: 已 L1+L2 落地；observe-only 默认开（无 user-visible 行为变化），OPT-407 上线时切换 observe_only=false
- **Related rules**: [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc), [observability-and-cost.mdc](../../.cursor/rules/observability-and-cost.mdc), [testing-and-rollout.mdc#3](../../.cursor/rules/testing-and-rollout.mdc) (golden set)
- **实际改动**:
  - 新增 [internal/llm/episode_judge.go](../../internal/llm/episode_judge.go) `JudgeEpisode` + `EpisodeJudgeArgs / Result` 类型 + 7 维 strict tool schema (`emit_episode_judge_verdict`)，含 `top_3_weakest_chapters` + `top_3_weakest_segments` + `terminology_glossary_observed` + verdict (`production_ready` / `needs_minor_revision` / `needs_major_revision`)；自动复用 `isThinkingModelName` 降 `tool_choice` 到 `"auto"`（DashScope thinking 兼容）；缺 verdict 默认 `needs_minor_revision`
  - 新增 [internal/llm/episode_judge_test.go](../../internal/llm/episode_judge_test.go) 8 case 单测（schema/disabled/empty/happy/fallback/user-msg/overall-score/thinking-mode）
  - 新增 [internal/pipeline/stage_episode_judge.go](../../internal/pipeline/stage_episode_judge.go) `(*Service).maybeJudgeEpisodeAsync(ep, chapters)`：detached `context.Background()` + 90s timeout + 一次性 `Store.ListSegmentsByEpisode` 拿全部 segments（避免 N+1）+ 镜像 `maybeJudgeChapterAsync` 的 observe-only log+drop 契约
  - [internal/pipeline/stage_episode_merge.go](../../internal/pipeline/stage_episode_merge.go) `runEpisodeMerge` 末尾（Episode → Completed 后）一行 hook
  - [internal/llm/client.go](../../internal/llm/client.go) `Client` 加 `episodeJudgeModel` 字段 + `OpEpisodeJudge` 常量；[internal/store/store.go](../../internal/store/store.go) 加 `UpdateEpisodeJudgeResult` partial UPDATE 三列（不触碰状态机字段）；[internal/store/store_test.go](../../internal/store/store_test.go) 加 `TestUpdateEpisodeJudgeResult_PartialUpdateOnly`
  - [internal/config/config.go](../../internal/config/config.go) + `.env*.example` 加 `EPISODE_JUDGE_MODEL=kimi-k2.5` / `EPISODE_JUDGE_OBSERVE_ONLY=true` / `EPISODE_JUDGE_TIMEOUT_SEC=90` / `EPISODE_JUDGE_ESCALATE_MODEL=`（escalate 留 followup）
  - 前端 [ui/src/api.ts](../../ui/src/api.ts) `Episode` 加 `episode_judge_meta` + 新 `EpisodeJudgeMeta` interface；[ui/src/components/EpisodeDetail.vue](../../ui/src/components/EpisodeDetail.vue) episode header 加 badge（绿 ≥ 0.9 / 黄 ≥ 0.8 / 红，比 chapter 0.85 / 0.7 更严，因为 episode-level 是最终把关）+ hover tooltip 7 维表 + 弱章节列表 + 弱段列表（c{N}.s{M} 定位）+ 观察术语表 + 一段式 summary
  - **未做**（衍生为 followup）：`POST /episodes/:id/episode-judge` 手动 trigger endpoint（OPT-406-followup-3）；`response_format=json_object` fallback（OPT-406-followup-1）；自动 escalate 到 qwen-max（OPT-406-followup-2）
- **PRs**: feat(judge): OPT-406 episode-level Judge productize

---

#### OPT-406-followup-1 `response_format=json_object` fallback when strict tool fails

- **Status**: planned
- **Estimate**: 0.5d
- **Depends on**: OPT-406
- **Background**：当前 OPT-406 `JudgeEpisode` 在 `doChatTool` 返回空 args 时直接报错（计入 `IncLLMStrictParseFailed(OpEpisodeJudge)`）。PowerShell 版 [scripts/episode_judge.ps1](../../scripts/episode_judge.ps1) 在 strict tool 持续失败时退化为 `response_format=json_object` + content parse —— 与 chapter judge 同步加同一个 fallback，让一两个 provider 抽风不至于让整集 episode 没分。
- **Trigger**: 当 `holodub_llm_strict_parse_failed_total{operation="episode_judge"}` 24h 累计 > 5% 调用量（经 production 监控）时落地。
- **Verification**: stub provider 返回 content message → fallback 解析 → meta 写入；strict 路径正常时 fallback 不触发。

---

#### OPT-406-followup-2 escalate model when chapter judge avg < 0.9

- **Status**: planned
- **Estimate**: 0.5d
- **Depends on**: OPT-406, OPT-409
- **Background**：env `EPISODE_JUDGE_ESCALATE_MODEL`（默认空）已在 OPT-406 MVP 预埋。本 followup 在 `maybeJudgeEpisodeAsync` 加一行：当 `chapters` 切片的 `ChapterJudgeScore` 平均值 < 0.9（OPT-409 信号显示有 chapter 级问题），切到 `EpisodeJudgeEscalateModel`（推荐 `qwen-max`），让更强的模型给最终判分；其余情况仍用 cheaper 单模型 `EPISODE_JUDGE_MODEL=kimi-k2.5`，避免常态高成本。
- **Trigger**: OPT-407 闭环 rework 上线前完成（OPT-407 决策表需要更可靠的 episode 分数），或观察到 production episode 平均 < 0.9 占比 > 20% 时上线。
- **Verification**: 构造一个 chapters all 0.85 的 mock episode → 运行 judge → 验证用 escalate model（捕获 HTTP request 的 model 字段）；chapters all 0.95 → 仍用默认。

---

#### OPT-406-followup-3 `POST /episodes/:id/episode-judge` 手动 trigger endpoint

- **Status**: planned
- **Estimate**: 0.5d
- **Depends on**: OPT-406
- **Background**：当前 OPT-406 MVP 触发路径仅 `runEpisodeMerge` 末尾一次（episode merge 完成时）。运营场景需要在不重跑整个 merge 的前提下重新评分（例如：换 model / 改 prompt / 修了一个 segment 后想看分数变化）。本 followup 加一个 HTTP endpoint 直接触发 `maybeJudgeEpisodeAsync`，复用现有 dispatch + DB 写入。
- **Trigger**: 运营反馈 retry stage=ep_episode_merge 的副作用（重写 manifest / 重新 mux）太重时落地；当前 retry 路径已可达成（L2 验证用的就是它），不阻塞 OPT-407。
- **Verification**: `curl -X POST .../episodes/131/episode-judge` 返回 202 + worker 日志在 90s 内出现 `episode judge result recorded`；GET `/episodes/131` 看到 `episode_judge_score` 更新。

---

#### OPT-407 Closed-loop rework engine（三级 verdict → 返工调度）

- **Status**: done
- **Source**: 业务对话 2026-05
- **Estimate**: 5d
- **实际工时**: ~1d（决策表 + 三级 hook + glossary broadcast 一次到位；OPT-201 软依赖留作后续替换）
- **Depends on**: OPT-409（原 OPT-405 = Chapter Judge 已重编号）, OPT-406；OPT-201 软依赖（MVP 复用现有 `(*Service).RetryJob`，OPT-201 SegmentAgent 落地后再迁移）
- **背景**：三级 judge 落地后，要把"评分"转化为"行动"，否则只是 observe 装饰。本 OPT 是整个长视频改造的"大脑"，定义 verdict → action 决策表 + 收敛保证 + cost ceiling。
- **决策表**：

  | Judge 层 | Verdict / 触发条件 | 自动 Action | 收敛上限 | 升级路径 |
  |---|---|---|---|---|
  | Segment | `judge_score < 0.7` 且 verdict=`retry` | `revise_segment(segment_id)` 走现有 retranslate 路径 | 5 attempts/段 | 升级为 `chapter rework`（同 chapter 多段低分） |
  | Segment | verdict=`split` | `split_segment(at_ms)`（OPT-201 提供 tool） | 1 next/段 | manual review |
  | Chapter | `narrative_coherence < 0.85` OR 弱段 ≥ 30% | `revise_chapter(job_id, fix_hints)`：用 chapter judge 的 recommended_fix 作为 prompt 注入，重译标记段 | 2 rounds/chapter | 升级为 `episode rework` |
  | Chapter | `terminology_consistency_within < 0.85` | `extend_glossary(chapter_id, missing_terms)`：本 chapter 范围内补术语后重译 | 2 rounds | 升级 |
  | Episode | `terminology_consistency` 跨 chapter < 0.85 | `broadcast_glossary_update(episode_id, glossary_diff)`：升 `glossary_version`，所有受影响段进 pending_rework | 1 round | manual review |
  | Episode | `narrative_coherence < 0.8` | `escalate_human_review(episode_id, weakest_chapters)`：暂停 pipeline、UI 弹通知 | 0（不自动重跑） | 永远 manual |
  | 任意 | 同一 verdict 连续 N=2 次出现 | `escalate_oscillation(target_id)`：oscillation detection，强制升级 | - | manual |
  | 任意 | accumulated cost > `EPISODE_REWORK_COST_CEILING_USD` | `halt_rework(episode_id, reason="cost_ceiling")` | - | manual decision |

- **Outcome**:
  - 三级 verdict → action 完整闭环；judge 不再只是 observe
  - 收敛保证：每级有上限 + cost ceiling + oscillation detection，不会无限循环
  - 与 OPT-002 / OPT-201 兼容：segment 级 rework 调用 SegmentAgent 的工具集（`Translate / RetranslateThinking / Split / AcceptWithBorrow`）
- **Verification**:
  - 故意制造低分场景（人工降分一段）：自动触发对应级别 rework，收敛于 ≤ 上限轮数
  - 故意制造 oscillation（同段连续 retry verdict）：在 N=2 后正确升级
  - cost ceiling 触发后 episode 状态进 `paused_cost`，不再自动消费 token
  - 三级 rework 都启用时，10min 视频总 cost 增长 ≤ 2x baseline（验证 cost guard 起作用）
- **Rollout**:
  - L1 单测决策表纯函数 ✅ `internal/rework/decision_test.go` 41 子测试 + `internal/llm/pricing_test.go` 6 子测试 + `internal/store` 5 store 单测
  - L2 staging：仅启用 segment-level rework decision（已 ship，env 默认 `none`，运维可单步打开 `segment`）
  - L3 staging：启用 chapter-level，观察 1 周（已 ship，运维可继续打开 `chapter`）
  - L4 启用 episode-level + cost ceiling（已 ship，含 `EPISODE_REWORK_COST_CEILING_USD=2.0` 兜底；`REWORK_ENGINE_LEVEL=episode` 全开）
  - 严格 feature flag `REWORK_ENGINE_LEVEL=none|segment|chapter|episode` 渐进
- **Related rules**: [agent-design.mdc#1](../../.cursor/rules/agent-design.mdc) (decide 纯函数 + 状态机), [agent-design.mdc#5](../../.cursor/rules/agent-design.mdc) (oscillation detection), [observability-and-cost.mdc#7](../../.cursor/rules/observability-and-cost.mdc) (cost ceiling 强制), [incremental-evolution.mdc#5](../../.cursor/rules/incremental-evolution.mdc) (回滚预案)
- **实际改动**:
  - 新包 [`internal/rework/`](../../internal/rework/)：`types.go` (Action / ReworkAttempt / Level / DecideInput / CountConsecutiveSame)、`decision.go` (纯函数 `Decide(in DecideInput) Action` 共 9 行决策表 + 3 道护栏)、`convergence.go` (`AccumulateCostUSD` / `EstimateRetryCostUSD`)、`engine.go` (`Engine.MaybeReworkSegment / MaybeReworkChapter / MaybeReworkEpisode` 三入口 + `runDecisionLoop` 共享路径 + `execute` 派发 + 通过 `RetryJobAPI` 接口避免循环 import)、`decision_test.go` 41 子测试覆盖 9 行决策 + 边界 + level 关闭 + cost ceiling + oscillation
  - 新包 LLM 成本计算 [`internal/llm/pricing.go`](../../internal/llm/pricing.go)：硬编码 7 model 定价 (qwen-turbo / qwen-plus / qwen-max / qwen3-235b-thinking / kimi-k2.5 / kimi-k2-thinking / deepseek-v3) + `unknownPrice` 高估兜底 + `ComputeUSD(model, in, out, cached)` 防御 0/负值/cached overflow；`internal/llm/client.go` `recordLLMUsage` 帮手在 `doChat` / `doChatTool` / 流式 `doChatStream` 三处调用，单一价格表 + 单一指标
  - 三个 hook 点：[`internal/pipeline/stage_tts.go`](../../internal/pipeline/stage_tts.go) `maybeJudgeSegmentAsync` 写完 `UpdateSegmentJudgeResult` 后 `s.rework.MaybeReworkSegment(...)`；同文件 `maybeJudgeChapterAsync` 写完 `UpdateChapterJudgeResult` 后 `s.rework.MaybeReworkChapter(..., weakestOrdinals)`；[`internal/pipeline/stage_episode_judge.go`](../../internal/pipeline/stage_episode_judge.go) `maybeJudgeEpisodeAsync` 写完 `UpdateEpisodeJudgeResult` 后 `s.rework.MaybeReworkEpisode(..., terminologyConsistency, narrativeCoherence)`
  - 新 stage handler [`internal/pipeline/stage_episode_glossary_broadcast.go`](../../internal/pipeline/stage_episode_glossary_broadcast.go)：`runEpisodeGlossaryBroadcast` 重新跑 OPT-402 glossary extractor → 按 source 词条 diff 出新增/改译的术语 → 找包含这些词的 segments → per-chapter 截断到 `maxGlossaryBroadcastSegmentsPerChapter=20` → `ResetSegmentsForRerun` + `RetryJob(StageTranslate, ids)`
  - DB 迁移 [`migrations/010_rework_attempts.sql`](../../migrations/010_rework_attempts.sql) 新增 `episodes.rework_attempts JSONB` / `rework_status TEXT` / `accumulated_cost_usd NUMERIC`，partial index on `rework_status WHERE rework_status IS NOT NULL`；模型 [`internal/models/models.go`](../../internal/models/models.go) `Episode` 加三个对应字段；`EpisodeStage` 加 `EpisodeStageGlossaryBroadcast = "ep_glossary_broadcast"`（**故意不在** `EpisodeStageOrder` 中，仅 on-demand 触发）
  - Store 层 [`internal/store/store.go`](../../internal/store/store.go) `AppendEpisodeReworkAttempt`（事务 + 部分 UPDATE 仅动 3 列：`rework_attempts` / `accumulated_cost_usd` / `updated_at`，与 `UpdateEpisodeJudgeResult` 同 OPT-406 partial-update 风格防止异步 hook 互相覆盖）+ `SetEpisodeReworkStatus`（同部分 UPDATE）
  - 配置 [`internal/config/config.go`](../../internal/config/config.go) 加五个新字段：`ReworkEngineLevel` (env `REWORK_ENGINE_LEVEL`, default `"none"`) / `EpisodeReworkCostCeilingUSD` (default 2.0) / `SegmentRetryMaxAttempts` (default 3) / `ChapterReworkMaxRounds` (default 1) / `ReworkOscillationThreshold` (default 2)；同步加到 [`.env.example`](../../.env.example) 和 [`.env.production.example`](../../.env.production.example)
  - 指标 [`internal/observability/metrics.go`](../../internal/observability/metrics.go) 加 `holodub_llm_cost_usd_total{model,operation}` counter + `holodub_rework_actions_total{level,action,dispatched}` counter；helper `AddLLMCostUSD` / `IncReworkAction`
  - Pipeline 接线 [`internal/pipeline/pipeline.go`](../../internal/pipeline/pipeline.go) `Service` 加 `rework *rework.Engine` 字段、`NewService` 中构造 `rework.NewEngine(cfg, st, svc)` 把 `*Service` 作为 `RetryJobAPI` 注入（接口断开循环 import）；`handleEpisodeStage` switch 加 `EpisodeStageGlossaryBroadcast` case
- **关键改动点**:
  - 新增 `internal/rework/`：
    - `decision.go`：纯函数 `decide(verdict JudgeVerdict, history []ReworkAttempt) ReworkAction`
    - `engine.go`：执行 ReworkAction（dispatch chapter rework job / segment retry / glossary broadcast）
    - `convergence.go`：oscillation detection + 计数 + cost 累计
  - 新增 stage `chapter_rework`：把 chapter 部分段重置为 `pending`，重新走 translate / tts；不重跑整 chapter
  - 新增 stage `episode_glossary_broadcast`：episode-level，升 glossary_version，标记受影响段
  - DB：`episodes.rework_attempts JSONB`（记录每次 rework 决策、执行结果、accumulated_cost）
  - 配置：
    - `REWORK_ENGINE_LEVEL=none`（默认禁用，验证完逐步打开）
    - `EPISODE_REWORK_COST_CEILING_USD=2.0`
    - `SEGMENT_RETRY_MAX_ATTEMPTS=5`（与现有 RETRANSLATION_INITIAL_MAX_ATTEMPTS 协调）
    - `CHAPTER_REWORK_MAX_ROUNDS=2`
- **风险与待决策**:
  - 与 OPT-201 SegmentAgent 的接口边界：建议 OPT-407 的 segment-level action 直接调 OPT-201 提供的 SegmentAgent.Run()，而不是自己实现一套 retry。这要求 OPT-201 先 done 或同步推进 → **MVP 决策**：复用现有 `(*Service).RetryJob`，OPT-201 落地后由 `OPT-407-followup-2` 替换接线
  - **escalate_human_review 的 UI**：需要 OPT-203 SSE 推送通知用户，否则用户不知道有需要审核的 episode → **MVP 决策**：仅写 `episodes.rework_status='escalated_human'` + log，UI 通知留给 OPT-203（`OPT-407-followup-3`）
  - **状态恢复**：worker 重启时正在 rework 的 episode 必须能正确恢复（用 stage_lease + episodes.rework_attempts 的最新版本）→ **已设计**：`rework_attempts` JSONB 是 source of truth，每次 `Decide` 从这里读历史；worker SIGTERM 时未 dispatch 的 rework 丢失没关系，下次 judge 触发会重新决策
- **Verification 实际**:
  - L1：`go test ./internal/rework/... ./internal/llm/` 全绿（41 + 6 子测试）；Linux Docker 下 `go test ./internal/store/... -run "TestAppendEpisodeReworkAttempt|TestSetEpisodeReworkStatus"` 5 子测试全绿；`go vet ./...` clean；linux api+worker binary build 通过
  - L2-L4 staging：MVP 默认 `REWORK_ENGINE_LEVEL=none` 与 OPT-406 observe-only 行为完全一致（已上线 staging 验证 worker 重启无回归 + 三层 hook 不卡 judge goroutine）；运维按 `none → segment → chapter → episode` 单步开关推进，每级独立 24-48h soak 后再向上升级
- **Followups**:
  - **OPT-407-followup-1** `split_segment` 算法落地（当前 `ActionSegmentSplit` 仅 marker + log，真正切分留给 OPT-201）
  - **OPT-407-followup-2** 整合 OPT-201 SegmentAgent ReAct（`ActionSegmentRetry` / `ActionEscalateToThinking` 改走 SegmentAgent.Run()）
  - **OPT-407-followup-3** `escalated_human` 的 SSE / UI 通知（依赖 OPT-203）
  - **OPT-407-followup-4** `MODEL_PRICE_OVERRIDE_JSON` env 让运维覆盖 `internal/llm/pricing.go` 的硬编码价格表（季度同步太慢的话）
  - **OPT-407-followup-5** 多 worker 部署下的全局 cost ledger 一致性（依赖 OPT-303 多租户）
  - **OPT-407-followup-6 (DONE 2026-05-11)** Drift-aware verdict guard + TTS-stuck recovery — 详见下方专项条目
- **PRs**: feat(rework): OPT-407 closed-loop rework engine

#### OPT-407-followup-6 Drift-aware verdict guard + TTS-stuck recovery

- **Status**: done 2026-05-11
- **Source**: job 153 (79 min episode 143) chapter 1 实跑反馈 — AI judge 把 `drift=-5.65s / -4.14s / +1.75s` 的高漂移段全打 `score=1.0`，OPT-407 引擎拿不到 retry 信号，全部错过；同时 ml-service 间歇性超时让 3 段卡在 `status='translated'`，OPT-407 hook 在 `maybeJudgeSegmentAsync` 后才触发，根本不会被 invoke。
- **Estimate**: 0.5d
- **Depends on**: OPT-407
- **背景**：OPT-002 segment judge 的 system prompt 评的是翻译质量（fidelity / fluency / coherence），LLM 看不到生成音频的实际时长，所以高 drift 段照样能拿 1.0 分。这导致 OPT-407 closed-loop 的 segment-level 决策天花板就是"LLM 觉得行的全放过"，运维必须手动 catch。同时 TTS 偶发失败（ml-service timeout / GPU OOM）会让 segment 卡在 `status='translated'` 永远没有 judge → OPT-407 hook 永远不被调用 → 没人发现需要恢复。
- **Outcome**:
  - **Drift hard guard**：`internal/rework/decision.go::shouldDriftOverrideToRetry` 在 per-verdict 规则之前检查 `DriftSec` 是否超过非对称阈值，超过即覆盖 `verdict='accept' → 'retry'`，原 verdict 写入 `Action.SkipReason='drift_override (orig_verdict=...)'` + `Note` 包含 OPT-407-followup-6 标识便于 ops 追溯
  - **非对称阈值**：`SEGMENT_DRIFT_HARD_LIMIT_OVER_SEC=0.3`（音频超长更危险，会溢到下一段）/ `SEGMENT_DRIFT_HARD_LIMIT_UNDER_SEC=0.7`（偏短只是死气，可后期补静音），任一边设 `0` 关闭对应方向
  - **TTS-stuck recovery**：`internal/pipeline/tts_stuck_backfill.go::BackfillStuckTTSSegments` 在 worker 启动后 30s 扫描 `status='translated' AND target_text<>'' AND updated_at < NOW()-2min` 的段，按 job_id 分组后通过 `RetryJob` 重新派发到 `tts_duration` 阶段；2 分钟 cooldown 避免与正在跑的 tts_duration 阶段竞争
  - **Job-stage 历史判断**：用 `Store.HasJobStageCompleted(jobID, StageTTSDuration)` 替代 `Job.CurrentStage` 判断 eligibility，因为 OPT-407 segment_retry 会把 `current_stage` 重置回 `translate`，否则误判已合成的 chapter 还没跑过 TTS
  - **Judge backfill 跳 halted episode**：`internal/pipeline/judge_backfill.go` 加 per-episode 缓存，跳过 `rework.IsHaltedReworkStatus()=true` 的 episode 段，避免 worker 启动时对历史 escalated 段烧 LLM token；新导出 `rework.IsHaltedReworkStatus(s)` 函数供非 engine 调用方复用判断逻辑
- **Verification 设计**:
  - L0：纯函数 `Decide()` 单测 `internal/rework/decision_test.go::TestDecide_DriftGuard_*` (8 个) + `TestShouldDriftOverrideToRetry` (11 个 sub-case) 覆盖：accept→retry 覆盖、retry/split 不被覆盖、对称/非对称阈值、单边禁用、chapter 级不触发
  - L1：`go test ./internal/rework/... ./internal/pipeline/...` 全绿（含原 OPT-407 41+ 测试）
  - L2 staging：worker 重启后 backfill log `tts-stuck backfill: re-enqueued chapter job_id=153 segments_recovered=3` + judge backfill 不再为 halted episode 派发
  - L3 production-like：job 153 chapter 1 重跑 → 高 drift 段触发 drift_override 并通过 segment_retry 收敛到 |drift| ≤ 0.7s（under）/ ≤ 0.3s（over）
- **Verification 实际**:
  - L0 / L1：20 个新单测全绿（`TestDecide_DriftGuard_*` 8 + `TestShouldDriftOverrideToRetry` 11+1），rework + pipeline 包整体回归通过
  - L2 staging：2026-05-11 14:11 实际 log `tts-stuck backfill: dispatching jobs=1 total_segments=3 scanned=181` + `re-enqueued chapter job_id=153 segments_recovered=3`，OPT-407-followup-6 binary 字符串验证通过 (`grep -c OPT-407-followup-6=1, tts_stuck_recovery=1`)
- **Followups**: 暂无；OPT-407-followup-2 (SegmentAgent 整合) 落地后此处 segment_retry 接线随之升级
- **PRs**: feat(rework): OPT-407-followup-6 drift-aware verdict guard + TTS-stuck recovery

---

#### OPT-408 Multi-episode 调度 + GPU 公平性

- **Status**: planned
- **Source**: 业务对话 2026-05
- **Estimate**: 3d
- **Depends on**: OPT-403
- **背景**：fan-out 之后单 episode 可能产生 4 个 chapter job 同时入队；多 episode 同时跑会让 chapter job 数量 ×N。当前 worker poll FIFO + `TTS_CONCURRENCY=2` + `GPU_CONCURRENCY=1` 的简单组合会导致：(1) 第一个 episode 的 chapter 全占完，第二个 episode 完全饿死；(2) 用户感知不到自己的 episode "在排队还是在跑"。
- **Outcome**:
  - Worker 调度器从 FIFO 升级为 episode 公平调度：从每个 active episode 取下一个 ready chapter，而非"第一个 episode 全部 chapter 跑完才轮到第二个"
  - GPU 抢占透明：worker 在拿到 stage_lease 后等不到 ml-service 容量时记录 `waiting_for_gpu` 状态并暴露给 UI
  - 暴露 episode-level 进度：`GET /episodes` 返回 `position_in_queue / chapters_done / chapters_total / eta_seconds`
- **Verification**:
  - 同时提交 3 个 episode（每个 3 chapter）：调度顺序为 ep1.c1, ep2.c1, ep3.c1, ep1.c2, ... 而非 ep1.c1..3, ep2.c1..3, ep3.c1..3
  - GPU 排队透明：UI 显示 "等待 ml-service 处理（前面有 N 个任务）"
  - 单 worker / 多 worker 部署都正确（依赖 redis sorted set，不依赖单进程内存）
- **Rollout**: L1→L4，feature flag `FAIR_SCHED_ENABLED`
- **Related rules**: [go-backend.mdc](../../.cursor/rules/go-backend.mdc), [docker.mdc](../../.cursor/rules/docker.mdc) (GPU 资源约束), [agent-design.mdc#5](../../.cursor/rules/agent-design.mdc)
- **关键改动点**:
  - `internal/scheduler/`：新增 fair scheduler，把 redis FIFO list 替换为 `ZADD score=last_chapter_completed_at` 的 sorted set，每次取分数最低的 episode 的下一个 ready chapter
  - `internal/store/queue.go`：扩展现有 task payload，加 `episode_id` / `chapter_ordinal`
  - ml-service 暴露 `GET /capacity` 返回当前 GPU 槽位 / 等待数；worker 在 `waiting_for_gpu` 状态时填回 `Job.Meta`
  - 前端：`EpisodeDetail.vue` 顶部加进度条 + 实时排队位置
  - 与 OPT-303 多租户结合：fair scheduler 二级排序（tenant 公平 → episode 公平）
- **风险与待决策**:
  - **多 worker 部署时的 sorted set 竞争**：用 redis `ZRANGEBYSCORE + WATCH` 或 `LMPOP`（redis 7+ 原生支持公平队列）
  - **eta 估算**：先用粗略平均（chapter wall time × pending_chapters），后续接 OPT-101 OTEL trace 数据更精准
  - **饥饿与优先级**：可选支持 `episode.priority`（urgent / normal / batch），结合公平调度，但默认走纯 episode 公平避免 priority 滥用
- **PRs**: TBD

---

#### OPT-409 Chapter-level Judge

- **Status**: done (2026-05-11; staging 验证通过 — job 131 chapter judge fired，verdict=chapter_ready overall_fidelity=0.95)
- **Source**: 业务对话 2026-05；2026-05-11 从原 OPT-405 重编号（OPT-405 ID 已被 LLM-Driven Chapterization 占用，详见 §3 表格备注 + §7 维护约定）
- **Estimate**: 2d (实际 ~1d，与 3 件旧债清理同 PR)
- **Depends on**: OPT-403, OPT-002
- **背景**：OPT-002 segment-level judge 只能看单段（fidelity / fluency），看不到 chapter 内 narrative coherence、speaker voice 跨段稳定性、本 chapter 内的术语一致性。Chapter judge 在 chapter_merge 后异步跑一次，关注**章内**维度。注意：本 OPT 关注 chapter **内部**质量（OPT-405 关注 chapter **切分** 质量），二者维度互补不重叠。
- **Outcome**:
  - 每个 chapter job 在 `completed` 状态额外 emit 一次 chapter judge 调用；判分写入 `jobs.chapter_judge_score / chapter_judge_meta`
  - 评分维度（沿用 `scripts/episode_judge.ps1` 的 schema 但作用域 = chapter）：
    - `narrative_coherence_within_chapter` (0..1)
    - `speaker_voice_stability_within_chapter` (0..1)
    - `terminology_consistency_within_chapter` (0..1)
    - `overall_fidelity_chapter` / `overall_fluency_chapter`
    - 弱段列表（top_3_weakest_segments，含 ordinal + issue + recommended_fix）
- **Verification**:
  - 在 OPT-403 / OPT-405 跑通的 N-chapter 测试视频（如 episode 142, 8 chapters）上每 chapter 都生成 score
  - 弱段列表与 segment-level judge 低分段的相关性 ≥ 0.7（验证两层 judge 互补、不重复）
  - chapter judge cost ≈ chapter 段数 × $0.0005（小一个量级，可常态启用）
- **Rollout**: L1→L4，env `CHAPTER_JUDGE_MODEL=qwen-turbo`，default observe-only（OPT-407 决策接入）
- **Related rules**: [agent-design.mdc#3](../../.cursor/rules/agent-design.mdc), [llm-call-standards.mdc#1](../../.cursor/rules/llm-call-standards.mdc) (function calling)
- **关键改动点**:
  - 新增 `internal/llm/chapter_judge.go`（schema + tool def + Go entry）
  - 新增 `migrations/00X_chapter_judge_score.sql`：`jobs` 加 `chapter_judge_score NUMERIC NULL, chapter_judge_meta JSONB NULL`
  - `internal/pipeline/stage_chapter_merge.go` 完成后异步 dispatch chapter judge（同 OPT-002 模式）
  - 前端：`EpisodeDetail.vue` 显示 chapter judge 分数热力图
- **PRs**: TBD
- **实际改动**:
  - 新增 `internal/llm/chapter_judge.go`：`ChapterJudgeArgs / ChapterJudgeSegment / ChapterJudgeResult / ChapterJudgeWeakSegment` 类型 + `chapterJudgeToolSchema` 7 维 strict tool（6 axes + top_3_weakest_segments + verdict）+ `JudgeChapter` entry，复用 OPT-405 `isThinkingModelName` 自动降 `tool_choice="auto"`，60s timeout（chapter prompt 比 segment 大）
  - 新增 `internal/llm/chapter_judge_test.go` 8 个 case 覆盖 schema marshal / 空输入 / disabled / happy path / 缺 verdict 默认 needs_revision / 用户消息组装 / overall score 计算 / thinking model tool_choice 降级
  - `internal/llm/client.go` 加 `chapterJudgeModel` 字段 + `OpChapterJudge` operation 常量
  - 新增 `internal/store/store.go` `UpdateChapterJudgeResult(ctx, jobID, score, metaJSON)` partial UPDATE，仅动两列避免覆盖 chapter_title 等 OPT-403 字段
  - `internal/models/models.go` `Job` 加 `ChapterJudgeScore *float64` + `ChapterJudgeMeta datatypes.JSON`
  - `internal/pipeline/pipeline.go` `runMerge` 在 `SaveJob` 后、`maybeEnqueueEpisodeMerge` 前插入 `s.maybeJudgeChapterAsync(job, segments)` hook
  - `internal/pipeline/stage_tts.go` 新增 `maybeJudgeChapterAsync` 镜像 `maybeJudgeSegmentAsync`：detached background context 60s、segment slice 过滤空文本、附带 segment-level judge score 给 LLM 当 hint、失败仅 log+drop
  - `internal/config/config.go` 加 `ChapterJudgeModel string` (默认 `kimi-k2.5`) + `ChapterJudgeObserveOnly bool` (默认 true)
  - migration `migrations/009_chapter_judge_score.sql`：`jobs` 加两列 + 部分索引 `idx_jobs_chapter_judge_score WHERE chapter_judge_score IS NOT NULL`
  - env `.env.example / .env.production.example` 加 `CHAPTER_JUDGE_MODEL=kimi-k2.5 / CHAPTER_JUDGE_OBSERVE_ONLY=true`
  - 前端 `ui/src/api.ts` `Job` 加 `chapter_judge_score / chapter_judge_meta` + 新 `ChapterJudgeMeta` 类型；`ui/src/components/EpisodeDetail.vue` chapter 卡片新增 judge 分数 badge（绿/黄/红 阈值 0.85/0.7）+ hover tooltip 弹 6 维表格 + top-3-weakest 列表 + 一段式总结
- **实际工时**: ~1d (与 3 件旧债清理同 PR；plan §10 估 ~3d 含旧债)
- **验证**:
  - L1 单测：`chapter_judge_test.go` 8 个 case 全绿；`store_test.go` `TestListSegmentsAwaitingJudge_FiltersAndOrdersCorrectly` 全绿；`go vet ./...` clean；linux api/worker binary build 通过
  - L2 staging：hot-reload api+worker 双容器，POST `/jobs/131/retry stage=merge` → ~30s 后 worker 日志 `chapter judge result recorded job_id=131 chapter_ordinal=1 verdict=chapter_ready overall_fidelity=0.95 narrative_coherence=0.95 speaker_voice_stability=0.95 terminology_consistency=0.95 register_consistency=0.95 weakest_count=0`，DB 验证 `jobs.chapter_judge_score=0.95` + `chapter_judge_meta` JSON 含全部 6 维分数 + verdict
  - L3 部分（backfill）：worker 重启后 15s `judge backfill: dispatching count=500 limit=500 concurrency=3` → `dispatch complete dispatched=500`，未判分段从 5908 → 5408（job 119/120/121 历史 restart-window gap 全部补齐）
  - L3 待补（chapter judge correlation）：episode 142 / 141 / 140 的 5+ chapter 全部还在 `awaiting_review`（需操作员确认 segment_review）；待这些 episode 跑通 merge 后即可观察"弱段列表 vs segment-level judge 低分段相关性 ≥ 0.7"指标
- **风险与待决策**:
  - **kimi-k2.5 latency**：staging 实测 ~30s/chapter（含 LLM 推理 + 60s context window），60s timeout 充足
  - **PrevContext 传 nil（segment-judge backfill 路径）**：见 OPT-002-followup-3 记账
  - **判分回写竞争**：`UpdateChapterJudgeResult` 用 partial UPDATE 只动 chapter_judge_score + chapter_judge_meta + updated_at 三列，不会覆盖 chapter_title / output_relpath 等并发写入字段

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
- 衍生新 OPT 候选：**OPT-EPISODE-JUDGE-PROMOTE** → 已正式立项为 [OPT-406 Episode-level Judge productize](#opt-406-episode-level-judge-productize)，作为长视频三层级改造（OPT-401..408）的一部分

### Pre-release P0 followup + 长视频基础 batch (2026-05-10, awaiting tag)

- **OPT-001-followup-1** Translate prompt 字节稳定（targetSec / limit 移入 user message）
  - 实际工时：~0.5d
  - CHANGELOG: [Translate system prompt is now fully byte-stable across segments (OPT-001-followup-1)](../../CHANGELOG.md)
  - 关键改动：`internal/llm/client.go` `buildTranslateSystemPrompt(targetLanguage, summary)` 签名瘦身；`translateWithDurationViaOpenAI` 在 user message 末尾追加 `Hard duration constraint: target ~Xs (≤Y chars).` 行；`RetranslateText` 同步路径同处理；`internal/llm/client_test.go` `TestSystemPromptStable` 反向断言（targetSec 不再影响 system 输出），新增 `TestTranslateUserMsgContainsPerSegmentConstraints`
  - 验证 baseline: [tests/quality/baseline-post-p0-opt402-10min.json](../../tests/quality/baseline-post-p0-opt402-10min.json)（A2 任务产出）
  - 备注：60s 视频 cache hit 仅 21.4%（segment cluster 数受限于段数）；10min 验证才能让命中率收敛到设计目标 ≥30%
- **OPT-FOLLOWUP-3 (a)** Adaptive drift threshold floor for long segments
  - 实际工时：~0.3d
  - CHANGELOG: [Adaptive drift threshold for long TTS segments (OPT-FOLLOWUP-3a)](../../CHANGELOG.md)
  - 关键改动：`internal/pipeline/tts/budget.go` 加纯函数 `AdaptiveMinDriftThreshold(targetSec, userFloor)`：targetSec ≥ 20.0s → floor=0.06，≥ 10.0s → floor=0.05，≤ 5.0s → 保持 0.03；`stage_tts.go` 的 `processOneTTSSegment` 在 `driftThreshold` 计算路径接入；`.env` 移除"临时调到 10/0.06"长警告，改注解 "adaptive floor handled by code"。`budget_test.go` 加 6 个 case
  - 验证：10min 验证（job 131 已用临时 env 跑通）+ 本次 baseline-post-p0-opt402-10min.json 用代码内 adaptive floor 复跑，retry 漩涡 0
  - 备注：(b) 让 judge verdict 短路 drift retry 仍在 planned，依赖 OPT-201
- **OPT-401** Episode / Chapter 数据模型（长视频三层级基础）
  - 实际工时：~2d
  - CHANGELOG: [Episode / Chapter data model with 1-chapter shortcut (OPT-401)](../../CHANGELOG.md)
  - 关键改动：`internal/models/models.go` 加 `Episode` struct + `EpisodeStatus` 9 态状态机；`Job` 加 `EpisodeID / ChapterOrdinal / ChapterStartMs / ChapterEndMs`；migration `migrations/005_episodes.sql` 含历史 100+ job back-fill；`internal/store/episodes.go` CRUD；`internal/http/router_episodes.go` 三个新 API；`ui/src/components/EpisodeDetail.vue` chapter 进度网格
  - 验证：staging DB 含 124 历史 job 的 back-fill 100% 等价；`POST /jobs` 行为 0 变化（自动建 1-chapter episode）
  - 备注：1-chapter shortcut 让现有 UI / API 完全不感知 episode 层；多 chapter 真实场景待 OPT-403 chapterize 落地
- **OPT-402** Pipeline episode-level stages + glossary_extract
  - 实际工时：~2d (借力 OPT-003 function-calling 基础设施)
  - CHANGELOG: [Episode-level pipeline stages and glossary extraction (OPT-402)](../../CHANGELOG.md)
  - 关键改动：`internal/models/models.go` 新增 `EpisodeStage` 枚举 + `EpisodeStageOrder` (`ep_media → ep_separate → ep_asr_smart → ep_glossary_extract → ep_chapterize`)，`TaskPayload` 加 `EpisodeID / EpisodeStage`；migration `migrations/006_episode_pipeline.sql` 加 `episodes.vocals_rel_path / bgm_rel_path / asr_done_at / glossary_done_at`；`internal/llm/glossary.go` 新增 `ExtractEpisodeGlossary` 走 strict tool call (`emit_episode_glossary` schema)；`internal/pipeline/pipeline.go` 加 `EnqueueEpisodeStage` + `handleEpisodeStage` 路由；`stage_glossary_extract.go` 新文件；`stage_tts.go` 把 episode glossary + reference card 注入 `RetranslateWithConstraint` 提示；`internal/store/store.go` 加 `UpdateEpisodeMediaFromChapter`（1-chapter 短路双写）+ `UpdateEpisodeGlossary`；前端 `EpisodeDetail.vue` 加 episode-stage 进度块 + 术语表
  - 验证：60s/10min/79min 三档烟测；1-chapter 短路 → media/separate/ASR 后双写 episode 字段，自动入 ep_glossary_extract 队列
  - 长视频证据：[tests/quality/opt402-79min-episode-139.json](../../tests/quality/opt402-79min-episode-139.json) — episode 139 (79min MIT 6.824 lecture) ASR 4.5s 完成、glossary 提取 3.8s 完成，提取出 6 条术语 + 301 字符 reference card；该任务因仅 1 chapter（OPT-403 缺位）被用户主动取消，但 episode-level stages 已验证可在长视频上跑通
  - 衍生：**OPT-402-followup-1**（多 chapter glossary 合并策略，等 OPT-403 落地后讨论）
- **OPT-403** Chapterize 算法 + fan-out 多 chapter job
  - 实际工时：~3d
  - CHANGELOG: [Chapterize + fan-out 多 chapter job (OPT-403/404)](../../CHANGELOG.md)
  - 关键改动：`internal/chapterize/algo.go` 新增 Pass 1 `ExtractCandidates`（基于 ASR 静默间隙）+ Pass 2 `DPOptimalCuts`（动态规划，min/target/max 18/22/30min 约束 + 静默偏好惩罚）+ `BuildChapterRanges`，配 13 个测试覆盖空输入 / 单候选 / 边界条件 / 79min 合成长视频；`internal/llm/chapter_review.go` 新增 Pass 3 `ReviewChapterCuts` strict tool call（`emit_chapter_review` schema），生成中英双语 chapter title + summary，配 6 个 mock 测试；`internal/pipeline/stage_chapterize.go` 新增 `runEpisodeChapterize` orchestrator + `runFanOutChapters` 7 步 fan-out（slice 媒体 / 更新 ch1 范围与路径 / 创建 ch2..N sibling jobs / 段落重映射并平移时间戳 / 更新 episode 元数据 / 重新入队 SegmentReview）；`internal/store/store.go` 加 `ListSegmentsByEpisode / ReassignSegmentsToChaptersAndShift / CreateChapterJob / UpdateChapterMetadata / UpdateChapterRange / UpdateEpisodeChapters / UpdateEpisodeOutput / UpdateLoudnormStats`；`internal/models/models.go` 加 `Job.ChapterTitle / ChapterTitleTranslated / ChapterSummaryMD` + `Episode.OutputLayoutVersion / ChaptersManifestRelPath / LoudnormStats` + `JobStatusAwaitingChapterize` 状态；`internal/media/ffmpeg.go` 加 `SliceVideoAtRange / LoudnormTwoPass / ConcatChapterVideos`；migration `migrations/007_chapter_metadata.sql`
  - 算法基线：[docs/opt-403/baseline-opt403-79min.json](../../docs/opt-403/baseline-opt403-79min.json) — 79min 合成 episode (79 segments / 55s avg seg / 1.6s avg gap) → DP 切出 3 chapter（24.55 / 25.47 / 24.56 min），mean 24.86min（target 22min, 偏差 ≤15.8%），所有 chapter 时长 ∈ [18min, 30min] 区间，候选采样数 78、所有切点 silence ≥850ms（默认阈值 800ms）
  - 备注：OPT-403 stage_chapterize 与 stage_episode_merge 共同构成长视频流水线骨架；与 OPT-404 同 PR 落地（详见下条）
- **OPT-404** Episode merge + 统一 output layout
  - 实际工时：~2d (与 OPT-403 同 PR)
  - CHANGELOG: [Episode merge + 统一 output layout (OPT-403/404)](../../CHANGELOG.md)
  - 关键改动：`internal/episode/chapters_manifest.go` 新增 `ChaptersManifest` 结构（schema_version=1）+ `WriteChaptersJSON`（原子写、缩进、自动时戳）+ `Validate / SortChapters / ReadChaptersJSON`，配 7 个测试覆盖空 / 错版 / 乱序 / 缺文件等场景；`internal/pipeline/stage_episode_merge.go` 新增 `runEpisodeMerge`（1-chapter hardlink shortcut + N-chapter `ConcatChapterVideos` + 可选 master EBU R128 pass + 写 chapters.json + 更新 Episode.OutputRelPath/ChaptersManifestRelPath/OutputLayoutVersion=2 + 转 Completed 状态）+ `maybeEnqueueEpisodeMerge`（chapter 完成时 idempotent trigger）；`internal/pipeline/pipeline.go` 把 `runMerge` 输出路径切到统一 layout `episodes/{ep_id}/chapters/vp{vp}/ch{ord:02d}.mp4`（旧 `jobs/{id}/output/...` 仅作为 layout v1 兜底），加 `runChapterLoudnorm`（在 dub 上跑 EBU R128 双轨 normalisation）+ `persistChapterLoudnormStats` 用 flat key `vp{N}_chXX` 写到 `Episode.LoudnormStats` 避免与 master pass 冲突；`internal/http/router_episode_downloads.go` 新增三个下载端点 `GET /episodes/:id/download/final`、`GET /episodes/:id/chapters.json`、`GET /jobs/:id/download/final`，全部从 DB 读 relpath 不自行拼路径（lessons-learned rule 1）；前端 `ui/src/components/EpisodeDetail.vue` 增加 layout v1/v2 badge、loudnorm-applied 标识、chapterize / episode_merge 进度 pill、双语 chapter title 渲染（translated > source > "Chapter N"）+ 章节级下载按钮；`cmd/migrate-output/main.go` 新增 138 历史 episode 一次性 back-fill 工具（--dry-run/--use-hardlink/--keep-old/--episode-ids/--limit/--record）
  - 算法基线：[docs/opt-403/baseline-opt403-79min.json](../../docs/opt-403/baseline-opt403-79min.json)（OPT-403 + OPT-404 共享）
  - Back-fill 报告：[docs/opt-403/opt403-backfill-dry-run.json](../../docs/opt-403/opt403-backfill-dry-run.json) — 74 episodes scanned，44 可迁移 (31 GB hardlink, 不重编码), 30 因历史 chapter `output_relpath` 为空需手动处置，总耗时 ~200ms
  - 备注：OPT-409 chapter-level judge（原 OPT-405 计划，2026-05-11 重编号）/ OPT-406 episode-level judge productize 现可基于本 OPT 的 `chapters.json` + `Episode.OutputRelPath` 实现；多 voice profile 的 episode-level final（mixed-vp）暂留给 OPT-407 multi-track output
- **OPT-405** LLM-Driven Chapterization（语义切分替代 DP）
  - 实际工时：~2d
  - CHANGELOG: [LLM-driven semantic chapterization (OPT-405)](../../CHANGELOG.md)
  - 关键改动：`internal/llm/glossary.go` 在 `emit_episode_glossary` strict tool schema 上扩 `chapters[]` 字段（与 glossary / speakers / reference_card 同一次 LLM 调用产出），新增 `isThinkingModelName` 帮助函数 + `ToolChoice` 在 thinking model 上自动降为 `"auto"`（DashScope thinking endpoint 拒收 strict tool_choice）；`internal/llm/client.go` `doChatToolOnce` 检测 thinking model 自动切到 `c.thinkingHTTPClient`（10 min timeout），杜绝 OPT-405.1 baseline 跑批时 kimi-k2-thinking 系列超时；新增 `internal/chapterize/llm_apply.go` 三纯函数 `ValidateLLMPlan / SnapBoundariesToSilences / EnforceHardConstraints` 配套单测；`internal/pipeline/stage_chapterize.go` `runEpisodeChapterize` 优先 `tryLLMChapterPlan`，OPT-403 DP 降级为 fallback；`internal/pipeline/stage_glossary_extract.go` 把 `chapters[]` marshal 成 `llmChaptersJSON` 持久化；`internal/store/store.go` `UpdateEpisodeGlossary` 接收 `llmChaptersJSON []byte`；`internal/models/models.go` 加 `Episode.LLMChapters datatypes.JSON`；`internal/config/config.go` 加 `ChapterizeLLMDriven / ChapterizeHardMaxMs / ChapterizeHardMinMs`；migration `migrations/008_llm_chapters.sql` `ALTER TABLE episodes ADD COLUMN IF NOT EXISTS llm_chapters JSONB`；env `.env / .env.example / .env.production.example` 设 `CHAPTERIZE_LLM_DRIVEN=true / CHAPTERIZE_HARD_MAX_MS=2700000 / CHAPTERIZE_HARD_MIN_MS=300000`，`GLOSSARY_MODEL=kimi-k2.5`
  - 验证：episode 142（79min, 176 segments）→ kimi-k2.5 切出 8 chapters，全部通过 validate / snap / hard-constraint，无 fallback 触发；OPT-405.1 LLM-as-judge 平均 4.76 / 5
  - 备注：本 ID 原计划 = "Chapter-level Judge"；2026-05-11 重定义为 LLM-Driven Chapterization。原 Chapter Judge 计划重编号到 [OPT-409](#opt-409-chapter-level-judge)。承接：DP fallback 永久保留，OPT-403 算法不退役
- **OPT-405.1** Multi-Model Chapterize Benchmark
  - 实际工时：~1d
  - CHANGELOG: [Multi-model chapterize benchmark CLI (OPT-405.1)](../../CHANGELOG.md)
  - 关键改动：新 CLI `cmd/chapterize-bench/main.go / runner.go / judge.go / report.go` 4 文件 ~1.4k 行（main = orchestrate + 缓存命中跳过；runner = 单模型单 run 抽取 + validate/snap/hard-constraint + 静态指标；judge = `score_chapter_cuts` strict tool call；report = markdown 排行榜 + JSON）；`internal/llm/bench.go` 新增 `Client.RunBenchToolCall` 通用入口让离线工具复用主流水线 retry / observability / timeout / thinking-model 透传逻辑；`docs/opt-405/bench-README.md` 写明用法
  - 验证 baseline: [docs/opt-405/bench-baseline-2026-05-11/](../../docs/opt-405/bench-baseline-2026-05-11/) — episode 142 × 6 candidates × 3 runs × 1 judge (kimi-k2-thinking)，**kimi-k2.5 = 4.76 clear-win**（runner-up qwen-max-latest = 4.06，gap +0.70）；artefacts 含 `report.md / report.json / raw/{model}-run{i}.json / judge/{model}-judgment.json / chapters-{model}.txt`
  - 衍生 follow-up：(a) judge 跨家族复跑（用 qwen3-thinking 或 deepseek 做 judge 复评 kimi-k2.5，验证不存在"同源偏好"）；(b) dataset 扩到 5 类型（lecture / news / interview / variety / ASMR），目前只覆盖 lecture
  - 备注：OPT-405 副产物（`doChatToolOnce` 自动切 `thinkingHTTPClient`）也是本 OPT 跑批时暴露的 90s 超时问题反向逼出的修复
- **OPT-409** Chapter-level Judge
  - 实际工时：~1d (与 3 件旧债同 PR)
  - CHANGELOG: [Chapter-level LLM-as-Judge (OPT-409)](../../CHANGELOG.md)
  - 关键改动：新增 `internal/llm/chapter_judge.go`（`ChapterJudgeArgs/Result/WeakSegment` 类型 + 7 维 strict tool schema `emit_chapter_judge_verdict` + `JudgeChapter` entry，复用 `isThinkingModelName` 自动降 tool_choice）；新增 `internal/llm/chapter_judge_test.go` 8 case；`internal/store/store.go` 加 `UpdateChapterJudgeResult` partial UPDATE；`internal/models/models.go` `Job` 加 `ChapterJudgeScore *float64 / ChapterJudgeMeta datatypes.JSON`；`internal/pipeline/pipeline.go` `runMerge` `SaveJob` 后插入 `s.maybeJudgeChapterAsync(job, segments)` hook；`internal/pipeline/stage_tts.go` 新增 `maybeJudgeChapterAsync` 镜像 `maybeJudgeSegmentAsync`（detached background ctx 60s + segment 过滤 + segment-judge hint）；`internal/config/config.go` 加 `ChapterJudgeModel string` (default `kimi-k2.5`) + `ChapterJudgeObserveOnly bool` (default true)；migration `migrations/009_chapter_judge_score.sql` `jobs` 加 chapter_judge_score / chapter_judge_meta + 部分索引；env 加 `CHAPTER_JUDGE_MODEL=kimi-k2.5 / CHAPTER_JUDGE_OBSERVE_ONLY=true`；前端 `EpisodeDetail.vue` chapter 卡片新增 judge 分数 badge（绿/黄/红 0.85/0.7 阈值）+ hover tooltip 弹 6 维表格 + top-3 weakest 列表
  - 验证：staging job 131 chapter judge 30s 内完成，verdict=chapter_ready overall_fidelity=0.95（每维 ≥0.92, 0 弱段）；DB `jobs.chapter_judge_score=0.95` + `chapter_judge_meta` JSON 含全部 6 维分数；UI 章节卡片绿色 badge 渲染正确
  - 衍生：(a) episode 142/141/140 多 chapter 端到端跑通后，验证"弱段列表 vs segment-level judge 低分段相关性 ≥0.7"；(b) **OPT-407** 闭环 rework 引擎 = consume 本 OPT 的 `chapter_judge_meta.top_3_weakest_segments + verdict` 决策路径
- **OPT-001-followup-2** doChatStream final-chunk usage 验证
  - 实际工时：~10 min (代码已落地，仅缺验证 + roadmap 标记)
  - CHANGELOG: see Changed 段
  - 关键改动：无新代码 — 验证 `internal/llm/client.go:969-979` 已解析 SSE `chunk.Usage` + line 992-993 emit `ObserveLLMTokens`；roadmap line 208 标 DONE 2026-05-11
  - 验证：`worker:8081/metrics` `holodub_llm_input_tokens_total{model="kimi-k2.5"}` > 0
- **OPT-002-followup-2** Worker 启动 Judge Backfill
  - 实际工时：~0.5d (与 OPT-409 同 PR)
  - CHANGELOG: [Worker-startup judge back-fill goroutine (OPT-002-followup-2)](../../CHANGELOG.md)
  - 关键改动：`internal/store/store.go` 加 `ListSegmentsAwaitingJudge(ctx, limit)` + 单测；新增 `internal/pipeline/judge_backfill.go` `(*Service).BackfillSegmentJudges(ctx, limit, concurrency)` 用 buffered channel 做 bounded concurrency；`internal/config/config.go` 加 `JudgeBackfillOnStart bool` (default true) + `JudgeBackfillLimit int` (default 500)；`cmd/worker/main.go` 服务初始化后 spawn 15s 延迟的 backfill goroutine；env 加 `JUDGE_BACKFILL_ON_START=true / JUDGE_BACKFILL_LIMIT=500`
  - 验证：worker 重启后 15s `judge backfill: dispatching count=500 limit=500 concurrency=3` → 12s 后 `dispatch complete dispatched=500`；未判分段从 5908 → 5408（job 119/120/121 历史 restart-window gap 全部补齐）
  - 衍生：**OPT-002-followup-3** PrevContext for backfill（当前 backfill 路径传 nil 简化首版，可补成查 segment 前一段拼 ContextSegment）
- **OPT-406** Episode-level Judge productize
  - 实际工时：~0.5d（与 OPT-409 同结构高复用，PowerShell 版 `scripts/episode_judge.ps1` 提示工程直接搬运）
  - CHANGELOG: [Episode-level LLM-as-Judge (OPT-406)](../../CHANGELOG.md)
  - 关键改动：新增 `internal/llm/episode_judge.go` 7 维 strict tool schema + `JudgeEpisode` 入口（自动复用 `isThinkingModelName` 降 `tool_choice` "auto"）+ 8-case 单测；新增 `internal/pipeline/stage_episode_judge.go` `maybeJudgeEpisodeAsync`（detached ctx + 90s timeout + `Store.ListSegmentsByEpisode` 一次拿全部 segments 避 N+1 + observe-only log+drop）；`runEpisodeMerge` 末尾插入 hook；`internal/store/store.go` `UpdateEpisodeJudgeResult` partial UPDATE 三列 + 单测验证不触碰状态机字段；`internal/config/config.go` + `.env*.example` 加 `EPISODE_JUDGE_MODEL=kimi-k2.5` / `EPISODE_JUDGE_OBSERVE_ONLY=true` / `EPISODE_JUDGE_TIMEOUT_SEC=90` / `EPISODE_JUDGE_ESCALATE_MODEL=`（escalate 留 followup-2）；前端 `EpisodeDetail.vue` episode header 加 badge（绿/黄/红 阈值 0.9 / 0.8）+ hover tooltip 7 维表 + 弱章节 + 弱段（c{N}.s{M} 定位）+ 观察术语表 + 一段式 summary
  - 验证：staging episode 131（1-chapter，p0-10min-semantic-validation）触发 retry stage=ep_episode_merge → 9 s 内 LLM round-trip 完成并写入 `episode_judge_score=0.95`，meta JSONB 含 7 axes 全 ≥ 0.95 + verdict=`production_ready` + 8 个 cross-chapter glossary 观察（`MapReduce` → `MapReduce`、`fault tolerance` → `容错` 等）；DB partial UPDATE 验证：`Status` / `OutputRelPath` / `ChaptersManifestRelPath` / `ReferenceCard` 未触碰
  - 衍生：(a) episode 142/141/140 多 chapter 跑通后验证"弱章节列表 vs OPT-409 chapter judge 低分章节相关性 ≥0.7"；(b) **OPT-406-followup-1** `response_format=json_object` fallback；(c) **OPT-406-followup-2** escalate 模型自动切（chapter judge 平均 < 0.9 → qwen-max）；(d) **OPT-406-followup-3** 手动 trigger endpoint `POST /episodes/:id/episode-judge`
- **OPT-407** Closed-loop rework engine（三级 verdict → 返工调度）
  - 实际工时：~1d（决策表 + 三级 hook + glossary broadcast 一次到位；OPT-201 软依赖留作后续替换）
  - CHANGELOG: [Closed-loop rework engine (OPT-407)](../../CHANGELOG.md)
  - 关键改动：新包 `internal/rework/` (types / decision / convergence / engine + 41 子测试)；新文件 `internal/llm/pricing.go` 7 model 定价表 + `ComputeUSD` + 6 子测试；新 stage `internal/pipeline/stage_episode_glossary_broadcast.go`（diff glossary → 重译受影响段，per-chapter 截 20 段）；DB migration `migrations/010_rework_attempts.sql` 新增 `episodes.rework_attempts/rework_status/accumulated_cost_usd` 三列 + partial index；`Episode` model 加同名字段；`EpisodeStage` 加 `EpisodeStageGlossaryBroadcast`（**故意不在** `EpisodeStageOrder`）；store 加 `AppendEpisodeReworkAttempt`（事务部分 UPDATE 仅 3 列）+ `SetEpisodeReworkStatus` + 5 子测试；config 加 5 个 REWORK_* env；observability 加 `holodub_llm_cost_usd_total{model,operation}` 和 `holodub_rework_actions_total{level,action,dispatched}` 两 counter；三 hook 点：`stage_tts.go::maybeJudgeSegmentAsync` (segment 级)、同文件 `maybeJudgeChapterAsync` (chapter 级)、`stage_episode_judge.go::maybeJudgeEpisodeAsync` (episode 级)；pipeline.Service 通过 `RetryJobAPI` 接口注入断开循环 import；`.env.example` + `.env.production.example` 同步加 `REWORK_ENGINE_LEVEL=none` / `EPISODE_REWORK_COST_CEILING_USD=2.0` / `SEGMENT_RETRY_MAX_ATTEMPTS=3` / `CHAPTER_REWORK_MAX_ROUNDS=1` / `REWORK_OSCILLATION_THRESHOLD=2`
  - 验证：L1 `go test ./internal/rework/... ./internal/llm/` 全绿（41+6 子测试）；Linux Docker `go test ./internal/store/... -run "TestAppendEpisodeReworkAttempt|TestSetEpisodeReworkStatus"` 5 子测试全绿；`go vet ./...` clean；linux api+worker binary build + hot-update 通过；migration 010 已 apply 到 staging postgres（`episodes` 三新列就位）；MVP 默认 `REWORK_ENGINE_LEVEL=none` 行为与 OPT-406 observe-only 完全一致，无回归
  - 衍生：(a) **OPT-407-followup-1** `split_segment` 算法落地；(b) **OPT-407-followup-2** 整合 OPT-201 SegmentAgent ReAct（替换现有 RetryJob 接线）；(c) **OPT-407-followup-3** `escalated_human` 的 SSE/UI 通知（依赖 OPT-203）；(d) **OPT-407-followup-4** `MODEL_PRICE_OVERRIDE_JSON` env 让运维覆盖价格表；(e) **OPT-407-followup-5** 多 worker 部署下的全局 cost ledger 一致性（依赖 OPT-303 多租户）

---

## 7. 维护约定

1. **新增 OPT**：只追加 ID，不复用已废弃 ID；填齐节 1.3 全部字段；同时更新节 3 总览表
2. **状态变更**：必须在节 4 卡片 + 节 3 总览表两处同步
3. **PR 引用**：PR title 以 `[OPT-XXX]` 开头；commit message 包含 `Refs OPT-XXX`
4. **Done 流程**：merge 后改 status=done → CHANGELOG 加条目（必含 `(OPT-XXX)`） → 下次 release tag 时移到节 6
5. **review**：每周对照节 3 总览表过一遍，把 stuck 在 in_progress > 2 周的 OPT 拿出来讨论
