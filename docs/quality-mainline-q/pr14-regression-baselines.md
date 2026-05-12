# PR-14 / Phase 5: 三档 Baseline 回归对比

> 质量主线 1-Quarter 路线图（OPT-201 → 202 → 204）收尾验证。
> 在 PR-1 ~ PR-13 全部 merge 后、灰度结束默认开之前必须跑完三档对比。

## 1. 目标

防止"小样本看着没问题、79min episode 一上就抖"。每档至少跑一次，
任一指标恶化 > 20% **必须解释或回退**（`testing-and-rollout.mdc` §7）。

## 2. 三档输入

| 档位 | 视频 / Episode | 基线 JSON | 用途 |
|---|---|---|---|
| L1 烟测（60s） | `test_60s.mp4` | [`tests/quality/baseline-pre-p0.json`](../../tests/quality/baseline-pre-p0.json) | 快速跑通，发现回归门槛之上的硬错 |
| L2 中段（10min） | `test_10min_sem.mp4` | [`tests/quality/baseline-post-p0-10min-final.json`](../../tests/quality/baseline-post-p0-10min-final.json) | 验证多段长视频在 SegmentAgent + ensemble + 韵律下不退化 |
| L3 长片（79min） | `test_full.mp4`（MIT 6.824 Lec1） | [`tests/quality/opt402-79min-episode-139.json`](../../tests/quality/opt402-79min-episode-139.json) | 验证 wall_time / cost / drift p95 不退化 |

## 3. 工具

新增 `tests/quality/run_baseline_diff.py` —— 与 `run_regression.py` 区分：

| 工具 | 触发场景 | 输出 |
|---|---|---|
| `run_regression.py` | CI smoke / 每个 PR | 绝对阈值 pass/fail |
| `run_baseline_diff.py`（本 PR） | 阶段灰度收尾 | 相对 baseline 的 regression % + warn/fail |

```powershell
# 模式 1: 从已跑完的 job 拉一份 baseline-shaped 报告
python tests/quality/run_baseline_diff.py collect `
  --api-base-url http://localhost:8080 `
  --job-id 999 `
  --output reports/q-baselines/current-60s-jobs-999.json

# 模式 2: 对比 baseline，恶化 > 20% 即 FAIL（exit code 1）
python tests/quality/run_baseline_diff.py diff `
  --baseline tests/quality/baseline-pre-p0.json `
  --current reports/q-baselines/current-60s-jobs-999.json

# 模式 2 一步合一: 当场拉 job + diff
python tests/quality/run_baseline_diff.py diff `
  --baseline tests/quality/baseline-pre-p0.json `
  --api-base-url http://localhost:8080 `
  --job-id 999
```

工具兜底：metric 在两边都存在且非 0 才比；缺一边只输出 `n/a` 不算
失败。这样老 baseline（捕获于 OPT-001 之前、`accumulated_cost_usd`
为 0）也能继续用。

## 4. 阈值（默认在脚本里硬编码）

| 指标 | 方向 | 阈值 |
|---|---|---|
| `drift_p50_sec` | 越小越好 | +20% FAIL |
| `drift_p95_sec` | 越小越好 | +20% FAIL |
| `max_segment_drift_sec` | 越小越好 | +20% FAIL |
| `cost_usd_total` | 越小越好 | +20% FAIL |
| `wall_time_sec` | 越小越好 | +20% FAIL |
| `judge_score_mean` | 越大越好 | -5% FAIL（更严） |

> 注：cost 阈值 +20% 与 `incremental-evolution.mdc` §3 一致；
> judge score 阈值 -5% 沿用 `testing-and-rollout.mdc` §3 的 golden set 规则。

## 5. 运维 checklist（操作员每次必须跑完）

```
□ 确认 dev-win 已 push 到 main，docker compose pull 拉最新镜像
□ 三档全部用统一 .env（参考 .env.example），下列 flag 必须是默认值：
    SEGMENT_AGENT_ENABLED=true
    JUDGE_VETO_DRIFT_RETRY=true
    SEGMENT_AGENT_ALLOW_SPLIT=false   ← Phase 2 仍 marker
    ENSEMBLE_RETRANSLATE_ENABLED=true
    DUBBING_PLAN_ENABLED=true
□ L1 60s: 跑 1 个 job → diff vs baseline-pre-p0.json，无 FAIL
□ L2 10min: 跑 1 个 job → diff vs baseline-post-p0-10min-final.json，无 FAIL
□ L3 79min: 跑 1 个 job → diff vs opt402-79min-episode-139.json，无 FAIL
□ 三档 diff 输出保存到 docs/quality-mainline-q/results/YYYY-MM-DD-{60s,10min,79min}.json
□ 任一 FAIL: 在 PR 描述里写"恶化指标 + 解释 + 是否接受"
□ 任一 WARN（恶化但 < 阈值）: 在 PR 描述里写"已观察"
```

## 6. 回退路径

如果 L3 出现 ≥ 2 个 FAIL 指标且无解释，必须按 phase 顺序关 flag 回退：

1. `DUBBING_PLAN_ENABLED=false` → 再跑 L3 → 看是不是 OPT-204 影响
2. `ENSEMBLE_RETRANSLATE_ENABLED=false` → 再跑 L3 → 看是不是 OPT-202
3. `JUDGE_VETO_DRIFT_RETRY=false` → 再跑 L3 → 看是不是 OPT-002-fu-4
4. `SEGMENT_AGENT_ENABLED=false` → legacy 路径，必须能复现 baseline

每一步耗时 ~ 1 小时（79min episode）+ 10 分钟分析。
最坏情况 4 小时内可以定位"哪一档 flag 引入了回归"。

## 7. 三档完成后

- 把三份 diff 报告附在 PR-14 上
- 更新 `docs/roadmap/optimization-roadmap.md` 把 OPT-201/202/204 改 status=done
- CHANGELOG 加 `[v1.8.0] Quality mainline Q complete`
- 跨 phase 边界打 release tag `v1.8.0`（详见 plan §9）
