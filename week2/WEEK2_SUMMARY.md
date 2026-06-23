# Week 2 总结 — 第一版端到端最丑跑通

> **时间**：2026-05-26 ~ 2026-05-29  
> **目标**：4 节点 sequential Python 流水线跑通  
> **代码**：`src/`（运行版） + `src_annotated/`（逐行注释学习版）

---

## 一、做了什么（操作清单）

| 步骤 | 内容 |
|------|------|
| 1. 计划调整 | 节点 2 从"自动渲染出图"改为"只生成 .sysml 文本，用户手动 Eclipse 看图"（Pilot Implementation 无可用 CLI） |
| 2. 数据契约 | `schemas.py` — 4 个 Pydantic 模型（StructuredRequirement / SysMLArtifact / ModelicaArtifact / SummaryArtifact） |
| 3. LLM 封装 | `llm_client.py` — DeepSeek API 统一入口，模型 `deepseek-v4-pro`，temperature 分场景设值 |
| 4. 节点 1 | `node1_requirement.py` — 多轮对话精炼需求，for 循环 + 对话历史回传 LLM 判完整性 |
| 5. 节点 2 | `node2_sysml.py` — LLM 生成 .sysml，基本语法检查，max_retries=2 自修复 |
| 6. 节点 3 | `node3_modelica.py` — LLM 生成 .mo → OMC 编译 → 仿真 → 失败回喂 error_log → while 循环重试 → CSV + matplotlib PNG |
| 7. 节点 4 | `node4_summary.py` — LLM 拼全流程 summary.md |
| 8. 串联 | `main.py` — 顺序调用 4 节点，输出目录三分（sysml/ modelica/ results/） |
| 9. Prompt 模板 | 4 个 .txt 文件：node1_completeness + node1_clarify + node2_sysml + node3_modelica |
| 10. 修 bug | 节点1 循环反复问（历史未喂 LLM）、Pydantic 字段缺默认值、模型名错误 |
| 11. Code review | 发现 API Key 硬编码、`_load_prompt` 重复定义、node2 绑死 RC 参数等问题 |
| 12. Git + GitHub | 初始化仓库、配置代理、上传至 `github.com/llc17/mbse-agent` |

---

## 二、架构图

```
用户输入 "做个1kHz低通滤波器"
    │
    ▼
节点1: refine_requirement()  ──►  StructuredRequirement
    │   for 循环: LLM 判完整性 → 反问 → 用户答 → 追加历史
    │
    ▼
节点2: generate_sysml(req)   ──►  SysMLArtifact (.sysml)
    │   for 循环: LLM 生成 → 语法检查 → 错误回喂重试(max 2)
    │
    ▼
节点3: generate_and_simulate(req, sysml)  ──►  ModelicaArtifact (.mo + CSV + PNG)
    │   while 循环: LLM 生成 → OMC 编译 → 仿真 → 错误回喂重试(max 2)
    │
    ▼
节点4: generate_summary(req, sysml, mo)  ──►  SummaryArtifact (summary.md)
```

---

## 三、优点

1. **架构清晰**：每个节点独立封装，输入输出用 Pydantic 校验，改一个节点不影响其他节点
2. **类型安全**：Pydantic 在 runtime 自动校验数据类型，缺字段立即报错，不会污染下游
3. **自修复闭环**：节点2/3 的 `attempts`/`errors`/`success` 三字段构成完整的自修复记录链，论文数据直接导出
4. **LLM 调用统一**：7 个节点文件全部通过 `llm_client.chat()` 调 API，换模型只改一行配置
5. **输出目录三分**：sysml/ modelica/ results/ 分类清晰，每次运行独立时间戳目录
6. **源码 + 注释双版本**：`src/` 跑程序，`src_annotated/` 学习用，每行有注释 + 数据流追踪
7. **通用性**：数据契约不是写死给 RC 电路的——`component_type`/`parameters`/`topology` 对任何物理系统通用

---

## 四、缺点

1. **`_load_prompt` 重复定义**：node1、node2、node3 各写了一遍相同的函数，应抽到 `utils.py`
2. **`_clean_code_block` 重复**：node2、node3 各一份，同上
3. **node2 prompt 绑死 RC 电路**：`{parameters_R}`、`{parameters_C}` 占位符对热传导等非电系统无意义
4. **主入口无错误处理**：LLM API 断连时程序直接崩，缺 try/except 兜底
5. **stopTime 硬编码**：仿真时长写死 10 秒，不同系统需要不同时长
6. **无测试**：`tests/` 目录为空，没有单元测试
7. **`chat_structured` 未使用**：定义了 JSON Mode 但实际没用上，节点1 手动 parse JSON
8. **sequential 模式无并行**：4 个节点顺序执行，无法并行独立步骤

---

## 五、技术决策记录

| 决策 | 原因 |
|------|------|
| 不用 LangChain | 项目初期太重，一个 POST 请求不需要框架 |
| 节点2 不自动渲染 | Pilot Implementation 无可用 CLI，Jupyter nbconvert 太脆弱 |
| temperature 分场景 | 完整性判断 0.1（确定），反问生成 0.5（自然），代码生成 0.2（精确） |
| prompt 放 .txt 外部文件 | 代码和数据分离，调 prompt 不碰 Python 代码 |
| 对话历史全量传 LLM | 修了"反复问同一句"的 bug——每次都让 LLM 看到完整上下文 |
| 模型选 deepseek-v4-pro | 当前最新，1M 上下文 / 384K 输出；deepseek-chat 2026-07-24 退役 |

---

## 六、面试叙事（2 分钟版）

> 我做了一个基于 LLM 的系统工程自动化流水线。用户输入非结构化需求，系统通过多轮对话精炼为 Pydantic 结构化数据，然后自动生成 SysML v2 系统模型代码和 Modelica 物理仿真代码。仿真失败时，编译器错误日志会回喂给 LLM 自动修复，最多重试 2 次。四个节点用 Pydantic 做类型安全的数据契约，整个 pipeline 解耦——每个节点可以独立替换和测试。项目用 Git 管理，代码在 GitHub 开源。

---

## 七、下一步（Week 3）

1. LangGraph 重构：sequential → 状态图
2. interrupt 节点：HITL 人工确认/打回
3. 自修复加深：max_retries=5 + 论文级对比实验

---

*Generated: 2026-05-29 | [GitHub: llc17/mbse-agent](https://github.com/llc17/mbse-agent)*
