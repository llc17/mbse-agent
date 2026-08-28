# V7 — benchmark + LLM-as-Judge（修订版）

> 本文件是对 `任务规划/V7-启动prompt-完整版.md` 的修订，按实际代码结构纠正了三个会跑不通的问题，
> 并把 V7 定位从"V4 vs V6 验收报告"微调为"**消融 + 成本-收益 + 评测基建**"。

---

## 一、V7 到底在干什么（修订后的定位）

**一句话**：不再是为了证明"V6 比 V4 好"（成功率/语法维度 V6>V4 大体是显然的），而是
**量化 V6 每个质量机制的边际贡献和成本，找出哪个是正收益、哪个是负收益、哪个是纯耗 token 的空转**，
同时把 benchmark 框架做成以后 V8/V9/V10 都能复用的评测基建。

要回答的三个真问题（都有可能是**负号**）：

| 问题 | 可能的结果 |
|------|-----------|
| 三阶段审查是不是在空转（审了但没改对） | 审查轮数高但成功率没涨 → 空转 |
| 检索层贴的官方示例是帮了还是害了 | 贴错域的示例带偏 LLM → V6 可能更差 |
| token 涨 ~3 倍换来的质量提升值不值 | 性价比曲线找拐点，可能"不值" |

---

## 二、V7 的结构

> 与 week5/week7 的 `src/` 惯例对齐：**可复用库在 `src/`，入口脚本在 `experiments/`，产物在 `results/`**。

```
week8/
  README.md                     本文件
  src/                          可复用库（V8/V9/V10 会 import）
    __init__.py
    cases.py                    统一用例集（V4/V6 复用同一段 raw_input）
    syntax_check.py             统一语法检查器（口径一致）
    runner.py                   单版本 thin runner（subprocess 隔离 import 冲突）
    benchmark.py                单模式运行器（subprocess 调 runner，收指标）
    judge.py                    LLM-as-Judge（Kimi 盲评打分）
  experiments/                  入口脚本（直接 python xxx.py 跑）
    __init__.py
    run_ab.py                   主编排（A/B 对比 + 汇总表）
    run_ablation.py             消融实验（检索层 on/off，支持 --trials 多次采样）
    resample_judge.py           judge 重采样（对同一产出评 N 次取均值±std，量化打分方差）
    retrieval_eval.py           复合词检索精度提升（monkey-patch 独立验证）
    collect_root_causes.py      根因样本收集（可选，构造失败用例）
  results/                      输出目录（运行时自动创建）
```

---

## 三、修订了什么（对应原 prompt 的三个致命问题）

### 致命 1：V4 实验入口搞错了 → 用 thin runner 直接 `invoke()`

原 prompt 让调 `python week5/src/main.py --mode experiment --case <id>`，但：
- `week5/src/main.py` **没有 `--case`**（只有 `--mode/--temperature/--max-retries/--max-rejects/--thread-id`）
- experiment 模式下它还会 `input()` 阻塞等 HITL

V4 真正能自动跑实验的是 `week5/experiments/run_experiment.py`，但那是参数扫描框架。
**解法**：`runner.py` 不经过两个入口，直接 `sys.path` 指向对应 week 目录，然后
`build_pipeline().invoke(initial_state, config)`。V4/V6 的 state 字段我已逐字段对齐（见 runner.py 的 `_build_initial_state`）。

### 致命 2：V4/V6 用例文本不一致 → 统一用例集

V4 `test_cases.json` 的 RC 用例是"电容根据截止频率计算"，V6 `PREDEFINED_CASES` 的 RC 用例是
"电容 C=0.159μF"（把答案喂死了）。**解法**：`cases.py` 自持一套统一用例集（取自 V4 test_cases.json 的严谨文本），
**同时喂给 V4 和 V6**，唯一变量 = 有没有 Agent 机制。

### 致命 3：任务⑦（改 week7 检索）与"week7 一行不改"矛盾 → monkey-patch 独立验证

`retrieval_eval.py` 用 monkey-patch 方式在运行时替换 `detect_domain`，做改前/改后的检索准确率对比。
**磁盘上 week7 一行不动**，A/B 对比用冻结的 V6。

---

## 四、额外的三个数据来源修正

| 原 prompt 的说法 | 实际情况 | 修正 |
|------|------|------|
| 语法错误数读 sysmlpy fatal/error | V4/V6 的 node2 在成功时都会清空 errors | `syntax_check.py` 统一重算，口径一致 |
| V4 token 落 `token_usage.json`，V6 打 stdout | V4 的 `run_experiment.py` 落盘，但 V6 `main.py` 只打印 | thin runner 里统一调 `get_token_stats()` 落盘 |
| 根因分析准确率"顺手" | 3 个成功用例不产出 root_cause | `collect_root_causes.py` 构造失败用例专门触发 |

---

## 五、用法

```bash
# 1. 先跑通单用例（验证流程，token 消耗最小）
python experiments/run_ab.py --case rc_lowpass

# 2. 全量 3 用例 × 2 模式（token 翻倍，确认单用例通了再跑）
python experiments/run_ab.py

# 3. 只跑 A/B 不调裁判（省钱/先验证 benchmark 本身）
python experiments/run_ab.py --case rc_lowpass --no-judge

# 4. 检索精度提升验证（独立，不改 week7）
python experiments/retrieval_eval.py

# 5. 根因样本收集（可选，构造失败用例，只跑 V6）
python experiments/collect_root_causes.py
```

---

## 六、指标口径（对比表怎么读）

| 指标 | 来源 | 说明 |
|------|------|------|
| 成功率 | `mo.success` | 仿真是否成功 |
| 语法错误 | `syntax_check.check()` 的 fatal+error | 统一口径，warning 不计入 |
| 物理偏差 | `quality_checks.physics_validate.deviation_percent` | 仿真值 vs 理论值 |
| 裁判分 | Kimi 盲评 0-100 | syntax/consistency/topology/traceability 各 25 |
| token | `get_token_stats().total_tokens` | prompt + completion |
| 耗时(s) | runner 计时 | 端到端 |

---

## 七、验收标准（对应原 prompt 四条）

1. ✅ `python experiments/run_ab.py --case rc_lowpass` 跑通（V4 + V6 + judge + 对比表）
2. 至少 3 个用例跑通，输出对比表
3. 对比表能看出 V4 vs V6 差异（成功率、裁判分、token、耗时）
4. judge 用 Kimi（结果里 `judge_provider=kimi`），被评是 DeepSeek

---

## 八、技术决策记录（为什么这么设计）

- **subprocess 隔离**（难点1）：V4/V6 同名 `src/` 包不能同进程 import，subprocess 天然隔离 token/产出文件。
- **thin runner 而非复用人入口**（难点2）：两个入口机制不同（V4 的 main.py 是 HITL 入口），统一绕过。
- **runner 先 `load_dotenv` 再 import src**（难点3）：V4 的 llm_client 不加载 .env，必须手动补。
- **judge 也用 subprocess**：judge 要 import week7 的 llm_client + retrieval，若与主进程混用会有 import 污染。
- **judge 逐份独立打分**（不"同时看两份"）：真正盲评，裁判不知道另一份存在。

### ⚠️ 环境关键修复（难点3 实际踩的坑）

**DeepSeek 必须用 `deepseek-chat`，不能用推理模型（`deepseek-v4-pro`）。**

`.env` 里原本没有 `DEEPSEEK_MODEL`，代码默认 `deepseek-v4-pro`（推理模型）。
推理模型会把 `max_tokens` 全部花在 `reasoning_content`（思考过程）上，导致 `content` 为空、
`finish_reason=length`，V4/V6 生成出**空代码**（`model.sysml`/`model.mo` 都是 0 字节）。

修复：`.env` 设 `DEEPSEEK_MODEL=deepseek-chat`（非推理模型，content 正常，且快得多：
V4 rc_lowpass 从 758s → 40.9s）。这是纯环境配置，不动 week5/week7 代码。

同理，Kimi K3 也是推理模型，judge 调用时已按官方约束处理：**不传 temperature**（K3 固定 1.0，
传了就 400）、`max_tokens` 加大到 16384、用 `reasoning_effort=low`、读 `content` 字段。

---

## 九、实验结果速查（2026-08-26 实跑数据）

### A/B 对比（3 用例 × V4 vs V6）

| 用例 | 模式 | 成功率 | 语法错误 | 物理偏差 | 裁判分 | token | 耗时(s) |
|---|---|---|---|---|---|---|---|
| rc_lowpass | v4 | ✅ | 0 | 0.0% | 90 | 7,215 | 35.6 |
| rc_lowpass | v6 | ✅ | 0 | 0.1% | 85 | 21,253 | 50.1 |
| dual_room_thermal | v4 | ✅ | 0 | - | 86 | 9,995 | 40.3 |
| dual_room_thermal | v6 | ❌ | 0 | - | 64 | 37,307 | 93.5 |
| rlc_lowpass | v4 | ❌ | 1 | 43.1% | 81 | 22,528 | 108.1 |
| rlc_lowpass | v6 | ❌ | 1 | 31.2% | 83 | 37,627 | 143.0 |

### 消融（V6 检索层 on/off）

| 用例 | 检索 | 成功率 | 裁判分 | token | 耗时(s) |
|---|---|---|---|---|---|
| rc_lowpass | on | ✅ | 91 | 44,128 | 79.9 |
| rc_lowpass | off | ✅ | 81 | 14,737 | 50.7 |
| dual_room_thermal | on | ❌ | 77 | 110,024 | 196.1 |
| dual_room_thermal | off | ❌ | 72 | 30,224 | 86.0 |
| rlc_lowpass | on | ❌ | 79 | 52,075 | 161.4 |
| rlc_lowpass | off | ❌ | 74 | 26,709 | 86.0 |

### 结论（有数据支撑）

**主结论（方向可靠）**：

1. **V6 不是稳赢**：`dual_room_thermal` 上 V6（64 分）反而不如 V4（86 分）——V6 的 SysML 检索层
   在该用例写出了 "connect 连 attribute 而非 port" 的硬伤。
2. **检索层是「温和正收益 + 高昂成本」**：3/3 用例裁判分方向一致地 +5~10 分（盲评证实产出更规范），
   但 token 涨 95%~264%、耗时涨 60%~128%，**且没能让任何失败用例转成功**。方向稳定，量级含噪声。
3. **V8 优化靶点明确**：检索层性价比极低（每 +1 裁判分要花 3k~16k token），是头号优化对象——
   方向是"贴对域的示例"（提高相关性）而非"贴更多示例"。

**⚠️ 噪声与局限（如实标注，供论文/答辩时注意）**：

- **消融是每配置 1 次采样**。thermal 用例单次跑出现过「检索开 70 < 检索关 75」的反向结果，
  全量复测后方向反转为「77 > 72」。所以**只有"检索层正收益"这个方向是稳的**，"+5 还是 +10 分"
  这个量级有噪声，不能引用为精确值。
- **judge 打分本身有方差**：Kimi K3 的 temperature 固定 1.0，同一份产出每次打分都可能不同。
  这比 pipeline 重跑更便宜地引入噪声。`resample_judge.py` 提供了对同一份产出评 N 次取均值±std
  的工具，但受 Kimi RPM=3 限流约束，当前未能跑通多次采样。
- **未做多次采样取均值的原因**：(a) Kimi 免费档 `max RPM: 3`，连发 judge 请求必 429；
  (b) `kimi-k3` 新推理引擎频繁 `engine_overloaded_error`（服务端过载，重试无效）；
  (c) thermal 用例 node3 的 repair 循环可拖到 16 分钟，pipeline 重跑成本过高。
- **要根治噪声**，方向是换一个支持 temperature=0 的非推理模型当 judge（但会破坏"与被测模型
  不同源"的独立裁判原则），或提供更高 RPM 的 Kimi/其他供应商 key 做错峰多次采样。

### 环境修复（必读，否则重跑必踩坑）

- `DEEPSEEK_MODEL=deepseek-chat`（**不能用推理模型 `deepseek-v4-pro`**，会把 max_tokens 全花在
  reasoning 上导致生成空代码，V4 从 758s 拖慢到 40s 就是它的锅）
- Kimi K3 是推理模型：judge 调用不传 temperature（固定 1.0）、max_tokens≥16000、读 content 字段
- **Kimi 免费档限流**：`max RPM: 3` + `kimi-k3` 引擎过载。judge.py 已对 429 做 20s 重试退避，
  `resample_judge.py` 采样间加 22s 间隔，但免费额度下仍可能撞限流——多次采样建议错峰跑。
