# Chapterize Multi-Model Benchmark — episode 142

- **Episode**: "opt405-lec1-v2"
- **Languages**: en → zh-CN
- **Segments**: 175  (≈ 79.5min)
- **Generated**: 2026-05-11T04:21:59Z
- **Judge**: `kimi-k2-thinking`  (runs/candidate: 1)
- **Extract runs/candidate**: 3

## Leaderboard (by judge total)

| Rank | Model | Total | Boundary | Title | Topic | Avg Chapters | Mean Dur (min) | Valid % | Wall Time (s) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | `kimi-k2.5` | 4.76 | 4.60 | 5.00 | 4.67 | 6.0 | 13.3 | 100% | 37.5 |  |
| 2 | `qwen-max-latest` | 4.06 | 3.67 | 4.00 | 4.50 | 4.7 | 17.7 | 100% | 34.3 |  |
| 3 | `qwen3-235b-a22b-thinking-2507` | 3.44 | 3.00 | 4.33 | 3.00 | 4.3 | 19.4 | 100% | 106.2 |  |
| 4 | `kimi-k2-thinking` | 3.33 | 3.00 | 4.00 | 3.00 | 1.3 | 6.6 | 33% | 261.5 |  |
| 5 | `qwen-plus-latest` | 3.08 | 3.00 | 2.75 | 3.50 | 4.0 | 19.9 | 100% | 29.8 |  |
| 6 | `deepseek-v3` | 2.44 | 3.00 | 2.00 | 2.33 | 6.7 | 12.7 | 100% | 25.4 |  |

_Total = mean(boundary, title, topic), 0..5 scale. N/A means the judge failed for that candidate (see error)._

## Per-model details

### 1. `kimi-k2.5`

- STABLE: every run produced 6 chapters (counts: [6 6 6])
- Static: avg_chapters=6.0 mean_dur=13.3min valid_rate=100% avg_wall=37.5s
- Judge: total=4.76  boundary=4.60  title=5.00  topic=4.67
  - run 1: chapters=6 mean=13.3min merge=0 split=0 jitter=40662ms wall=40.3s (ok)
  - run 2: chapters=6 mean=13.3min merge=0 split=0 jitter=40662ms wall=35.6s (ok)
  - run 3: chapters=6 mean=13.3min merge=1 split=0 jitter=40561ms wall=36.6s (ok)

**Worst-scored boundaries (judge):**
- boundary 1  → score 3  — Student question about grades at segment 39 interrupts clean transition; topic shift is implied but muddled.
- boundary 0  → score 5  — Explicit transition with speaker saying 'Let me talk about course structure' after finishing introduction.
- boundary 2  → score 5  — Explicit transition with 'another big topic that comes up a lot is fault tolerance.'

**Per-chapter judge breakdown:**
- ch1  title=5 topic=5  — Title accurately summarizes coherent introduction; covers one theme from start to end.
- ch2  title=5 topic=5  — Title accurately summarizes logistics; covers one coherent theme with clean boundaries.
- ch3  title=5 topic=3  — Title accurate but segment 39 contains unrelated student question about grades breaking theme coherence.
- ch4  title=5 topic=5  — Title accurately summarizes fault tolerance discussion; covers one coherent theme throughout.
- ch5  title=5 topic=5  — Title accurately summarizes consistency models; covers one coherent theme throughout.
- ch6  title=5 topic=5  — Title accurately summarizes MapReduce deep dive; covers one coherent theme throughout.

Full chapter list (for spot checks): `chapters-kimi-k2_5.txt`

### 2. `qwen-max-latest`

- VARIABLE: chapter counts ranged 4..6 across 3 runs (counts: [4 6 4])
- Static: avg_chapters=4.7 mean_dur=17.7min valid_rate=100% avg_wall=34.3s
- Judge: total=4.06  boundary=3.67  title=4.00  topic=4.50
  - run 1: chapters=4 mean=19.9min merge=0 split=0 jitter=41101ms wall=32.1s (ok)
  - run 2: chapters=6 mean=13.3min merge=1 split=0 jitter=45255ms wall=44.3s (ok)
  - run 3: chapters=4 mean=19.9min merge=0 split=0 jitter=27341ms wall=26.3s (ok)

**Worst-scored boundaries (judge):**
- boundary 0  → score 1  — The cut separates logistics from distributed systems overview but leaves a logistics question in chapter 2, breaking theme continuity.
- boundary 1  → score 5  — Clear explicit pivot from sharding discussion to consistency topic.
- boundary 2  → score 5  — Explicit wrap-up of consistency and transition to MapReduce case study.

**Per-chapter judge breakdown:**
- ch1  title=5 topic=5  — Title accurately describes course intro and logistics; chapter covers only that theme.
- ch2  title=5 topic=3  — Title matches the distributed systems overview content, but chapter includes a brief unrelated grading question.
- ch3  title=1 topic=5  — Title mentions fault tolerance which is not present; chapter is purely about consistency.
- ch4  title=5 topic=5  — Title accurately reflects MapReduce case study; chapter stays on topic throughout.

Full chapter list (for spot checks): `chapters-qwen-max-latest.txt`

### 3. `qwen3-235b-a22b-thinking-2507`

- VARIABLE: chapter counts ranged 3..5 across 3 runs (counts: [3 5 5])
- Static: avg_chapters=4.3 mean_dur=19.4min valid_rate=100% avg_wall=106.2s
- Judge: total=3.44  boundary=3.00  title=4.33  topic=3.00
  - run 1: chapters=3 mean=26.5min merge=0 split=0 jitter=52632ms wall=226.8s (ok)
  - run 2: chapters=5 mean=15.9min merge=1 split=0 jitter=28045ms wall=50.0s (ok)
  - run 3: chapters=5 mean=15.9min merge=0 split=0 jitter=49842ms wall=41.7s (ok)

**Worst-scored boundaries (judge):**
- boundary 0  → score 1  — Cut separates question from answer about grading mechanics, breaking conversational flow.
- boundary 1  → score 5  — Speaker explicitly concludes previous section and announces transition to MapReduce case study.

**Per-chapter judge breakdown:**
- ch1  title=5 topic=1  — Title accurately reflects content but chapter cuts off course mechanics Q&A partway through.
- ch2  title=3 topic=3  — Generic title for specific challenges; chapter mixes brief grading Q&A with core challenges.
- ch3  title=5 topic=5  — Title precisely describes MapReduce case study; chapter maintains coherent theme throughout.

Full chapter list (for spot checks): `chapters-qwen3-235b-a22b-thinking-2507.txt`

### 4. `kimi-k2-thinking`

- VARIABLE: chapter counts ranged 0..4 across 3 runs (counts: [0 4 0])
- Static: avg_chapters=1.3 mean_dur=6.6min valid_rate=33% avg_wall=261.5s
- Judge: total=3.33  boundary=3.00  title=4.00  topic=3.00
  - run 1: chapters=0 mean=0.0min merge=0 split=0 jitter=0ms wall=364.2s (ERROR: glossary tool call: llm.chat.completions: Post "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions": context deadline exceeded (Client.Timeout exceeded while awaiting headers))
  - run 2: chapters=4 mean=19.9min merge=1 split=0 jitter=63448ms wall=56.4s (ok)
  - run 3: chapters=0 mean=0.0min merge=0 split=0 jitter=0ms wall=364.0s (ERROR: glossary tool call: llm.chat.completions: Post "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions": context deadline exceeded (Client.Timeout exceeded while awaiting headers))

**Worst-scored boundaries (judge):**
- boundary 1  → score 1  — Cut occurs mid-logistics discussion; technical content doesn't start until segment 43, breaking theme continuity.
- boundary 0  → score 3  — Transition from distributed systems importance to course logistics is reasonable but not explicitly signaled.
- boundary 2  → score 5  — Speaker explicitly signals pivot: 'Any questions about this before I start talking about MapReduce?'

**Per-chapter judge breakdown:**
- ch1  title=5 topic=5  — Title accurately summarizes coherent introduction to distributed systems; no theme mixing or leakage.
- ch2  title=5 topic=1  — Title fits content but chapter cuts off logistics theme prematurely; conclusion appears in next chapter.
- ch3  title=1 topic=1  — Title mismatches opening logistics content (seg 38-42) and mixes two distinct themes.
- ch4  title=5 topic=5  — Title accurately captures MapReduce case study; chapter presents single coherent theme throughout.

Full chapter list (for spot checks): `chapters-kimi-k2-thinking.txt`

### 5. `qwen-plus-latest`

- STABLE: every run produced 4 chapters (counts: [4 4 4])
- Static: avg_chapters=4.0 mean_dur=19.9min valid_rate=100% avg_wall=29.8s
- Judge: total=3.08  boundary=3.00  title=2.75  topic=3.50
  - run 1: chapters=4 mean=19.9min merge=0 split=0 jitter=36465ms wall=28.7s (ok)
  - run 2: chapters=4 mean=19.9min merge=0 split=0 jitter=54386ms wall=30.0s (ok)
  - run 3: chapters=4 mean=19.9min merge=1 split=0 jitter=32303ms wall=30.6s (ok)

**Worst-scored boundaries (judge):**
- boundary 1  → score 1  — Cut occurs mid-concept during key-value service consistency example, breaking the explanation.
- boundary 0  → score 3  — Cut splits Q&A flow between course logistics and infrastructure discussion, reasonable but slightly abrupt.
- boundary 2  → score 5  — Speaker explicitly concludes consistency discussion and transitions to MapReduce case study.

**Per-chapter judge breakdown:**
- ch1  title=5 topic=3  — Title accurately reflects content; chapter mixes course intro with some motivation material.
- ch2  title=0 topic=1  — Title is wrong for infrastructure/implementation content; chapter mixes multiple distinct topics.
- ch3  title=1 topic=5  — Title mentions three themes but only covers consistency; chapter is coherent deep-dive on consistency.
- ch4  title=5 topic=5  — Title accurately describes MapReduce case study; chapter is fully coherent and complete.

Full chapter list (for spot checks): `chapters-qwen-plus-latest.txt`

### 6. `deepseek-v3`

- VARIABLE: chapter counts ranged 5..9 across 3 runs (counts: [6 5 9])
- Static: avg_chapters=6.7 mean_dur=12.7min valid_rate=100% avg_wall=25.4s
- Judge: total=2.44  boundary=3.00  title=2.00  topic=2.33
  - run 1: chapters=6 mean=13.3min merge=0 split=0 jitter=34575ms wall=24.6s (ok)
  - run 2: chapters=5 mean=15.9min merge=0 split=0 jitter=39841ms wall=22.5s (ok)
  - run 3: chapters=9 mean=8.8min merge=1 split=0 jitter=43602ms wall=29.1s (ok)

**Worst-scored boundaries (judge):**
- boundary 0  → score 1  — Cut breaks flow; segment 17 continues research thread from 16 and reasons were already introduced in chapter 0.
- boundary 1  → score 1  — Cut is in middle of logistics Q&A; technical content doesn't start until segment 41, making boundary artificial.
- boundary 2  → score 4  — Clear topic shift from scalability to fault tolerance, though lacks explicit verbal transition.

**Per-chapter judge breakdown:**
- ch1  title=3 topic=1  — Title is accurate but too broad; chapter mixes introduction with reasons and challenges that belong elsewhere.
- ch2  title=0 topic=1  — Title is completely wrong (claims reasons but covers course logistics); content is coherent but mismatched.
- ch3  title=1 topic=1  — Title misleading (says challenges but covers infrastructure, implementation, performance); mixes leftover logistics with multiple technical topics.
- ch4  title=0 topic=2  — Title is completely wrong (says logistics but is about fault tolerance); fault tolerance content is coherent but segments 39-40 don't belong here.
- ch5  title=3 topic=4  — Title is accurate but overly generic for a chapter focused specifically on consistency; maintains coherent theme throughout.
- ch6  title=5 topic=5  — Perfectly specific title and coherent MapReduce case study from introduction to implementation details.

Full chapter list (for spot checks): `chapters-deepseek-v3.txt`


## Recommendation

Top candidate: **`kimi-k2.5`** with judge total 4.76 (boundary 4.60 / title 5.00 / topic 4.67).
It produced an average of 6.0 chapters per run (mean dur 13.3min) with 100% validation pass rate and 37.5s avg wall time.
Margin over runner-up `qwen-max-latest`: 0.70 points. This is a CLEAR win — recommend switching production GLOSSARY_MODEL to the top candidate.
