# LoRA 微调决策框架：何时微调、需要什么数据

> 研究性质文档（2026-07-21）。回答两个核心运营问题：**什么时候需要微调新模型？微调新模型需要什么数据？**
> 答案综合自 `docs/mit6824-dub-production-plan.md`、`docs/lora-adapter-training-sop.md`、
> `docs/mit6824-lecture08-duration-lora-findings.md` 的策略层规定，
> 以及 v1–v10 训练实验、`training_decision.json`、`lecture08_training_readiness_v1.json` 的实证证据。

---

## 一、什么时候需要微调新模型？

### 1.1 决策阶梯（逐级升级，不许跳级）

```
发现问题段
   │
   ▼
它是孤立的吗？（单段、单术语、单次）
   │ 是
   ▼
[L1] 口语文本规则 / 术语表 / 归一化 ──────────► 修完，结束
   │ 不孤立（同类问题反复）
   ▼
[L2] split 规划 / 候选门禁 / 文本压缩 / 救援 ──► 修完，结束
   │
   ▼ 修不动：整集 fit_audit 仍失败
[L3] 受控 lecture-local 时长/韵律实验（可诊断，不晋升）
   │
   ▼ 且满足全部前置条件（见 1.2）
[L4] 课程/领域 adapter 训练（fixed holdout + gold 数据 + 审计）
   │
   ▼ 生成式 holdout 击败受控基线（如 v7 goldmatch）
[L5] candidate_ready 晋升（opt-in，可即时回滚）
```

### 1.2 训练的硬性前置条件（全部满足才允许）

绝大多数问题**不应该**用微调解决。仓库的策略是一个五级阶梯，微调在最后两级：

| 层级 | 触发条件 | 手段 | 工具/配置 |
|---|---|---|---|
| L1 | 孤立发音/数字/格式问题（如 `5号`→`5日`、`5.4.1` 丢点、Raft 读音） | 口语文本规则、术语表、归一化 | spoken_text rules / glossary |
| L2 | 孤立长句截断、拼接问题、单段超时 | split 规划、候选门禁、救援策略 | split_policy / candidate gate / rescue |
| L3 | 单集 `fit_audit` 失败（系统性时长/韵律信号） | **受控 lecture-local 时长/韵律 LoRA 实验**（可诊断修复，但不得直接晋升） | fit_audit → lecture-local experiment |
| L4 | 同一失败族跨多集重复 + 数据量达标 | **课程/领域级 adapter 训练** | mit6824_training_decision.py |
| L5 | adapter 在固定 canary/整集门禁上 outright 失败 | 立即允许训练（豁免 L4 的"≥2 集评审"前置） | canary regression gates |

### 1.2 训练的硬性前置条件（全部满足才允许）

由 `tools/mit6824_training_decision.py` 机器化执行（`data/validation/mit6824_training_decision.json`）：

- **同一失败族 ≥ 3 个具体实例**（`min_family_count: 3`）
- **该失败族出现在 ≥ 2 个已评审 lecture**（`min_repeat_lectures: 2`）
- **已评审 lecture ≥ 2 个**（`min_reviewed_lectures: 2`）
- **人类认可的对齐音频 ≥ 30 分钟**（工程下限 `gold_audio_floor_minutes: 30`）
- **固定 holdout / canary 集，且与训练集零重叠**

默认推荐必须是 `do_not_train_*`。当前课程状态（2026-06-25 快照）：
`reviewed_lectures: 0` → `do_not_train_insufficient_cross_lecture_evidence`。
注意：旧制度下发布的 L1-7 不计入新制度的"已评审"，证据链从 L8 开始重建。

### 1.3 来自 v1–v10 实验的实证教训（策略之外的增量认知）

文档层策略之外，十次 lecture-8 本地训练实验给出了三条血泪规则：

1. **重洗旧数据 ≠ 新证据。** v10 用完全干净的 v7 goldmatch 语料（712 行 / 2.394h，
   审计通过、无泄漏）重训，生成式 holdout 反而退化（primary 1/12，micro6 0/12）。
   结论：**没有新增 gold 数据时不要启动训练**——同样的数据再训一遍只会偏离已验证的 checkpoint。
2. **泄漏的修复包会污染判断。** v9 把 4 行 promoted repair 包追加进训练（含 4 行 holdout 泄漏），
   holdout 选择从 v7 的 10/12 跌到 2/12。审计块：`train_rows:4<80, val_rows:0<6, test_rows:0<7, holdout_leakage:4`。
   结论：**用于评估的修复 holdout 永远不进训练集**。
3. **训练集损失改善不等于生产质量。** v6 duration-position loss 从 0.3578→0.1471，
   但生成式 holdout 只有 3/12 在 5% 内。结论：**checkpoint 只能由生成式音频 holdout 选出，
   不能由 validation loss 或最终 step 选出**。

由此得到一条可执行的总规则：

> **微调的正当触发 = 策略条件（1.2）满足 + 有"实质性新增"的合规数据。
> "实质性新增"的判据：`training_readiness` 工具比较候选包与当前参考语料后，
> `new_source_id_count > 0` 且通过 `training_pack_audit`。**
> （反例：ep180 重建的 90 行 / 0.6581h 全部 90 个 source_id 已在 v7 语料中 → 0 新增 → not_ready_to_train。）

---

## 二、微调新模型需要什么数据？

### 2.0 前置认知：多路由候选机制（为什么它是微调前的必经层）

微调讨论之前必须理解：HoloDub 解决难段的第一武器不是训练，而是**多路由候选生成 + 门禁选择**
（`ml_service/app/services/lora_candidate.py`）。对一个候选段，ML 服务按序尝试三条路由：

| 路由（family） | 做法 | 适用 |
|---|---|---|
| `default` | 单次 LoRA 推理（duration embedding 控制） | 常规长段 |
| `split_light_vector` | split 规划（max_part 16s/170字/cps≤5.6）+ 轻情感向量 | 长而密、需要更平稳韵律 |
| `split_mix` | split 规划 + 逐 part 混合家族选择后组装 | 超长/混合难度 |

每个候选过统一门禁（`GATE_DEFAULTS`）：drift ≤ 500ms、ASR ≥ 0.97（目标 0.985）、
fit delta ≤ 0.05、术语召回 ≥ 0.8（目标 0.95）、F0 范围 ≥ 4.5；拼接救援另有更严门禁
（ASR ≥ 0.995、drift ≤ 50ms、边界 RMS 跳变 ≤ 20dB）。`needs_text_compression` 是规划信号
而非失败：要求先压缩/重译文本再合成（≥4 个 split 变体 + ≥2 种策略才允许判定"文本必须压缩"）。

**这就是为什么"同一失败族重复出现"才允许训练**：候选机制已经覆盖了
时长/切分/韵律/拼接四个维度。候选全挂（`all_candidates_rejected`）且文本压缩也救不回的段，
才构成"模型能力缺口"的证据，才轮到讨论微调。

### 2.1 数据分层（tier）

| Tier | 定义 | 允许用途 |
|---|---|---|
| **silver** | 合成/TTS 生成样本、历史自动通过样本 | smoke、时长标定、目标函数实验；**不得用于晋升** |
| **gold** | 人类明确认可的对齐音频 | 晋升、canary、episode shadow 的准入数据 |

关键判据（SOP）：通过 L7 过滤器的 HoloDub 自生成音频**仍是 silver**，
除非被人耳明确认可。当前 gold 库存：`mit6824-lec07-v25-gold-candidate-v3` = 105 行 / 0.6567h（仅够做过滤标准参考，不够晋升）。

### 2.2 Gold 样本的入库过滤器（L7 标准，逐样本硬条件）

单条样本必须同时满足：

- judge score ≥ 0.8
- final-mix ASR 相似度 ≥ 0.97
- final drift ≤ 500ms 且 drift ratio ≤ 5%
- selected fit tempo delta ≤ 5%
- 文本长度 ≤ 220 字符
- 生产侧新增：`segments.meta` 必须有 `tts_final_fit_audit.pass=true` 才允许进训练 manifest

失败的 shadow 音频**永远不直接入训**：先带动作标签进修复队列
（text_compression / boundary_repair / text_completeness_repair），
修复重生成并通过上述全部门禁后，才可作为 repair adjunct 追加。

### 2.3 训练包整体审计（`tools/mit6824_lora_training_pack_audit.py`，非 smoke 训练前必过）

从 v9/v10 审计块反推出的硬性数值门槛：

- train 行数 ≥ 80；val ≥ 6；test ≥ 7（固定划分）
- 总音频时长 ≥ 0.5 小时
- 所有 selected 样本 fit tempo delta ≤ 0.05
- **零 holdout 泄漏**（任何样本不得同时出现在训练与 canary/episode holdout）
- 不含 failed-shadow 来源
- 语速可行性警告：chars/sec > 5.6 的行会被标记（v7 实验定位失败高密度区在 6.0–6.5 字/秒）

### 2.4 数据量级目标（课程/领域 adapter）

| 阶段 | 量级 | 用途 |
|---|---|---|
| 工程下限 | ≥ 30 分钟 human-approved | 允许启动训练讨论 |
| 晋升目标 | 1–3 小时 gold | `candidate_ready` 评审 |
| 稳定 profile | 2–5 小时 gold | 视为稳定课程/领域 adapter |

覆盖要求：短文本、长文本、拉丁术语、数字、章节引用、自然停顿、尾部完整性 显式覆盖。

### 2.5 模型约束对数据形态的塑造（lecture-8 实证）

- IndexTTS2 有效 GPT code-token 上限 = **1811** → 干净的 40s+ 整句大多被拒/截断。
  **数据应以 split-part 形态组织**（v7 goldmatch：max_part_sec=12、max_part_chars=140、pause_sec=0.18），
  而不是指望模型单次生成可靠的 40s 音频。
- 语速可行性是数据筛选的第一公民：目标 chars/sec 超过安全区（≈5.6）的 split part
  应在合成前被拒或改写（v8 feasible-cps56 实验的由来）。
- 文本完整性（丢字/吞尾）是比时长更深的失败模式：紧时长下模型会省略内容。
  训练数据必须带 **part 级 ASR/尾部完整性验证**，assembly 之前先过 part 门禁。

### 2.6 数据流水线（从源头到 checkpoint 的实际路径）

```
被接受的生产音频+译文（final-mix 通过 / 人审通过）
   │  mit6824 过滤器（judge≥0.8, ASR≥0.97, drift≤500ms&≤5%, fit≤5%, ≤220字）
   ▼
corpus manifest（gold/silver tier 标注，train/val/test 固定划分）
   │  lora_preprocess_indextts2.py（GPT code 提取；1811 token 上限过滤）
   ▼
feature files（train/val/test）→ training_pack_audit（train≥80/val≥6/test≥7/≥0.5h/零泄漏/fit≤0.05）
   │  ml-service LoraTrainingRunner（r4 LoRA；duration_token_rate_policy 随 adapter 记录）
   ▼
checkpoints（adapter_stepN.pt）
   │  固定 holdout 生成式评测（fit≤5% + ASR + 尾部 + join 全过）
   ▼
candidate_ready（registry；仅 opt-in 启用；SHA256 备案）
```

**速率一致性要点（2026-07-21 新增实证）**：训练时的 `duration_token_rate`（如 23.4375）
会成为 adapter 的"速率语义"一部分；推理侧的 piecewise rate 表若与之不符，
时长控制会按比例失真（候选路由实测过产 ≈ 32.5/23.4 ≈ 1.39×）。
→ **训练配置与推理配置必须同源记录、成对晋升**，这是数据之外、但和 adapter 生命周期
绑定的隐性"元数据"要求。

### 2.6 就绪判据（什么时候数据算"够了"）

`tools/mit6824_lora_training_readiness.py` 输出 `not_ready_to_train` 直到：

> 新增的非 holdout、ASR 通过、fit ≤ 5%、人类认可或客观通过的样本
> **实质性地**扩展了当前参考语料（new source ids > 0 且审计通过）。

当前 lecture-8 状态（2026-06-25）：`not_ready_to_train`。
episode 183 部分运行产生的 17 行 runtime-clean 无 ASR 行（0.1101h）**不算**——
要等 final-mix ASR、fit audit、人工评审、量级全部通过。

---

## 三、对当前 MIT 6.824 的直接推论

1. **现在不该训练新 adapter。** readiness 和 training_decision 都说证据不足。
   正确动作是让 episode 183（或后续 L8 run）跑完 → final-mix ASR → fit_audit →
   人工评审，把"已评审 lecture"从 0 变成 1，同时积累 gold 音频。
2. **L8 修复路线已锁定**：v7 goldmatch adapter 是受控基线；下一轮的突破口是
   **TTS 前的文本规划**（text compression / speech-rate feasibility / join-boundary repair），
   不是再训一版 adapter。这与 temporal-plan 原型方向一致。
3. **下一次合法训练的触发画面**：L8/L9/L10 评审后，若同一失败族（例如
   "高密度长句在低 fit 下丢字"）在 ≥2 集各出现 ≥3 例，且 gold 音频累计 ≥30min，
   才启动课程级 adapter 训练；数据以 L7 过滤器 + 修复队列产出为唯一来源。
4. **防止退化三原则**（写进未来每次训练评审）：
   新增数据实质性检查 → 审计零泄漏 → checkpoint 由生成式 holdout 选出。

---

## 附 B：案例研究 —— 2026-07-21 ep183 欠产事件（为什么这次不该训练）

L8 episode 183 重跑中，4 个"墙段"（fit 18-48%）全部在不训练的情况下被攻克：

| 段 | 症状 | 真实根因 | 修复层 |
|---|---|---|---|
| 16262 (24.84s) | fit 18.4% | 重译压缩不足 → 文本过密 | L1 重译+ensemble（7 轮收敛 0.86%） |
| 16395 (44.26s) | LLM 超时 | 瞬时错误 + 超长需 split | L2 候选路由（首发 1ms 漂移） |
| 16364 (6.72s/67字) | fit 45.9% | 极端密度 + tokens_per_char 棘轮 | 人工压缩改写 + fresh 循环（1.7%） |
| 16413 (9.74s) | 双路由夹击 | 速率不稳 | 自动循环（0.08%） |

同期发现的三个工程缺陷（全部**不需要训练**即可修）：
1. `tokens_per_char` 反馈棘轮（observed 无下限 → 上限自锁）
2. duration token rate 表（23.4-32.5 tok/s）与实测（~50 tok/s）不匹配 → 时长目标系统性低估
3. ensemble 胜者选择无时长维度 → 欠产在文本侧修不动

**教训**：表面上是"模型时长控制不行"，实为配置与代码缺陷。若此时训练 duration adapter，
就是在用模型权重修补工程 bug —— 既浪费 gold 数据配额，又掩盖真问题。
这正是决策阶梯 L1→L2→L3→L4 顺序存在的意义：**只有工程层修尽后仍然重复的失败族，
才构成训练证据。** 详见 `docs/mit6824-research-journal-2026-07-21.md` 第 06 节。

---

## 附：关键工件索引

| 工件 | 路径 |
|---|---|
| 训练决策（机器执行） | `tools/mit6824_training_decision.py` → `data/validation/mit6824_training_decision.json` |
| 训练包审计 | `tools/mit6824_lora_training_pack_audit.py` |
| 训练就绪判据 | `tools/mit6824_lora_training_readiness.py` → `data/validation/mit6824_acceptance/lec-08/lecture08_training_readiness_v1.json` |
| 贴合审计（内容门禁） | `tools/mit6824_fit_audit.py` |
| Gold 参考语料 | `data/training/lora_corpus/mit6824-lec08-v7-goldmatch-split12-parts`（712 行 / 2.394h） |
| 受控参考 adapter | `data/training/lora_runs/mit6824-lec08-v7-goldmatch-split12-durationpos-r4-step200/adapter_latest.pt` |
| L7 gold 种子 | `data/training/lora_corpus/mit6824-lec07-v25-gold-candidate-v3`（105 行 / 0.6567h） |
