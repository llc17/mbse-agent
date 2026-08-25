# V6 学习路径：从读懂到能写

> 你是 V4 才入门，V5 跳过了，V6 直接面对完整架构。
> 目标不是抄代码，是后续版本自己能写一部分。

---

## 先认清一个事实

V6 代码量 ~3300 行（src/ 12 个文件）。

不是所有代码都应该现在学。把它们分成 4 层：

```
第 0 层（不用写）: schemas.py, utils.py — 纯数据定义和工具函数，看一遍就行
第 1 层（先读懂）: llm_client.py, retrieval.py, modelica_templates.py — 基础设施，必须理解
第 2 层（模仿着写）: agent_loop.py, pipeline.py — 架构骨架，照着写成你自己的版本
第 3 层（目标是能写）: node1/2/3/4/quality — 业务逻辑，终极目标
```

---

## 第 0 层：不用写，看一眼就行

### `schemas.py`（153 行）— 数据契约

这是什么：5 个 Pydantic 类，定义了节点之间传递的数据格式。

```python
class StructuredRequirement(BaseModel):
    component_type: str       # "RC低通滤波器"
    parameters: dict          # {"R": 1000, "C": 1.59e-7}
    topology: str             # "串联RC"
```

**你不需要写它**。Pydantic 就是 Python 类型注解 + JSON 校验，会读就行。你要新增一个字段的时候加一行，10 秒。

### `utils.py`（126 行）— 工具箱

这是什么：`load_prompt()`（读文件）、`clean_code_block()`（剥 markdown）、`make_run_dir()`（建目录）。

**你不需要写它**。每个函数都是独立的，用到了再查。

**学这一层的标准**：打开文件能指出每个类是干什么的 → 过了。

---

## 第 1 层：先读懂，理解"为什么这样设计"

这一层是 V6 的根基。代码不多，但设计思想值得深究。

### ① `llm_client.py`（489 行）

```
学什么:
  - 为什么用抽象基类而不是 if-else？（策略模式：新增提供商不改旧代码）
  - 为什么 _request() 里要指数退避重试？（LLM API 不稳定，不能一次失败就崩）
  - V6 新增的 chat_with() 怎么实现 per-agent 切换？（优先级链 + 降级）
  - 中转站 URL /v1/v1 问题怎么发现的、怎么修的？（生产环境真实踩坑）

先读懂的部分:
  - LLMProvider.__init__()  → 看懂 self._chat_path 的动态判断
  - _request() 的重试循环  → 看懂 for attempt + exponential backoff
  - chat_with() 的降级逻辑 → 看懂 try/except + fallback

可以模仿着写的:
  - 新增一个 LLM 提供商（比如 OpenAI），只需要写一个新类继承 LLMProvider
    然后改 __init__ 读环境变量。总共 15 行。
```

### ② `retrieval.py`（367 行）

```
学什么:
  - 为什么用 Python 写死域映射而不是让 LLM 猜？（确定性——每次结果一样）
  - 什么时候让 LLM 参与？（域冲突时——热敏电阻同时命中 electrical+thermal）
  - 为什么 stage 参数控制输出格式？（generate 给完整示例 / review 给审查清单）
  - code_fences 参数是干什么的？（修 DeepSeek 多代码块空响应 quirk）

先读懂的部分:
  - DOMAIN_MAP 字典      → 域 → 目录的映射（42 个官方示例的组织方式）
  - get_references()     → 整个检索流程：域解析 → 目录映射 → 文件收集 → 限制数量 → 拼接
  - get_references_llm_select() → LLM 消歧的逻辑：什么时候调 LLM、怎么 fallback

可以模仿着写的:
  - 新增一个域（如 "fluidic" 流体），在 DOMAIN_MAP 和 DOMAIN_KEYWORDS 加条目
  - 改 max_files_per_domain 看看效果差异
```

### ③ `modelica_templates.py`（389 行）

```
学什么:
  - 为什么模板要 Python 维护？（OMC 编译验证过 → 保证语法正确）
  - 为什么选择权交给 LLM？（模板覆盖不了所有系统类型 → LLM 判断用哪个）
  - _build_param_map() 的参数名多变形映射（V/V_in/Vin_step/voltage → param_V）
    这是实战中自然产生的——node1 输出的参数名不稳定，必须兼容

先读懂的部分:
  - TEMPLATE_RC_FILTER 模板代码     → 看懂 Modelica 的 import + component + equation 结构
  - inject_template()               → 看懂 {param_R} → "1000.0" 的替换过程
  - get_candidate_templates()       → 看懂关键词打分 + 排序 + 截断

可以模仿着写的:
  - 新增一个模板（如 "机械弹簧阻尼系统"），在 TEMPLATE_REGISTRY 注册
  - 用 OMPython 验证你写的模板能编译通过
```

**学这一层的标准**：关掉代码，能说出每个文件解决了什么问题、为什么不能用更简单的方法 → 过了。

---

## 第 2 层：模仿着写，照着写成你自己的版本

这一层是架构核心。不建议直接读我的代码——**你应该先自己试着写，然后对比**。

### ④ `agent_loop.py`（439 行）— 三阶段循环

```
你应该这样学:

  第 1 步: 读数据结构（不用写）
    ReviewIssue / ReviewResult / AgentLoopResult 三个 dataclass
    → 理解"审查结果"长什么样：{ok, score, issues[]}

  第 2 步: 读 parse_review_json()——先读懂 4 级 fallback（不用写，但值得背下来）
    策略 1: 从 ```json ... ``` 块提取
    策略 2: 贪婪正则 { ... } 匹配
    策略 3: 手工括号计数（找到第一个 { 和对应的 }）
    策略 4: 全部失败 → 返回 ok=True（放行，不阻塞流水线）
    → 这是全文最重要的容错设计——LLM 真的有 20-30% 不按格式输出

  第 3 步: 关掉代码，自己写 run_review_loop()
    给你要求:
      - 接收 3 个回调函数：generate_fn, review_fn, revise_fn
      - 循环最多 max_rounds 轮
      - review_fn 返回 ok=True → 结束循环
      - 达到 max_rounds → 断路器触发，返回当前结果
      - 记录每轮的 review 结果到 review_history

    写完对比我的实现，看差异在哪。

  第 4 步: 自己写 build_review_prompt() 和 build_revise_prompt()
    → 这两个函数就是字符串拼接，但包含了 V6 prompt 写作规范
```

**为什么这个文件值得你手写一遍**：它是整个 V6 的"心脏"。理解了这个文件，其他 4 个 Agent 都一样。

### ⑤ `pipeline.py`（391 行）— 流水线编排

```
这个文件分两半:
  - 上半（你的 V4 代码）: StateGraph 构建 + HITL 节点 + 路由函数
  - 下半（V6 改动）: _route_after_node3() 改用 LLM 根因分析

你应该这样学:

  第 1 步: 画图（不用写代码）
    在一张纸上画出 StateGraph 的拓扑:
      START → node1 → node1_hitl → node2 → Q_cross → node2_hitl → node3 → node3_hitl → Q_physics → node4 → END
    标出每条 conditional_edge 的条件（什么时候打回、什么时候继续）
    → 这就是"状态机设计"——先想清楚再写代码

  第 2 步: 读路由函数（你 V4 写的，复习一遍）
    _route_after_hitl1(): 确认→继续 / 打回未超限→重做 / 超限→强制继续
    _route_after_cross_validate(): 通过→HITL / 失败→打回 node2
    → 模式都一样: 读 node_status → 读 reject_count → 判断

  第 3 步: 读 V6 改动的 _route_after_node3()
    对比 V4（关键词匹配）和 V6（读 root_cause 字段）
    → 理解了"关注点分离"：node3 负责分析，pipeline 负责路由决策
```

**为什么这个文件你应该能自己写**：你在 V4 已经写过了。V6 只是改了路由来源。

---

## 第 3 层：终极目标——能写出一个完整的 Agent

这一层是业务逻辑。4 个 Agent 文件结构完全相同：

```
nodeX.py 结构:
  def nodeX(state) → dict          # LangGraph 节点入口（从 state 读、往 state 写）
  def _generate(...) → str         # 第 1 步：生成
  def _review(...) → ReviewResult  # 第 2 步：审查（调用 agent_loop 的回调）
  def _revise(...) → str           # 第 3 步：修正（调用 agent_loop 的回调）
  主函数调用 run_review_loop(generate, review, revise)  → 运行
```

**每个 Agent 的差异只在 prompt 内容**，结构完全一样。

### 学习顺序（从简单到复杂）

```
第 1 个: node1_requirement.py（285 行）
  → 最简单——输入自然语言，输出 JSON。只有 2 轮审查。
  → 学习方法: 读 generate() → 读 review() → 读 revise() → 读 run_review_loop() 调用

第 2 个: node4_summary.py（261 行）
  → node1 的变体——输入各种产物，输出 Markdown 报告。
  → 学习方法: 对比 node1 的结构，找两个文件哪里一样、哪里不同

第 3 个: node2_sysml.py（332 行）
  → 第一个"真正有价值"的 Agent——检索官方示例 + 审查对照 + 修正。
  → 学习方法: 分开学——先学 generate（含 retrieval 调用），再学 review（含 references 注入），最后学 revise

第 4 个: node_quality.py（640 行）
  → 这个文件你可以跳过不用写——物理验证器是 V4 的 Python 计算，
    跨步检查是 prompt 模板 + parse_review_json。新东西不多。
```

---

## 你的 4 周学习计划

### 第 1 周：读懂基础设施

```
目标: 能说出每个基础设施文件解决了什么问题
任务:
  1. 读 llm_client.py → 画出 LLMProvider 的继承关系图
  2. 读 retrieval.py → 在纸上写出 DOMAIN_MAP 的每个域和对应目录
  3. 读 modelica_templates.py → 用 OMPython 验证你能运行 inject_template()
  4. 读 agent_loop.py → 把 4 级 JSON fallback 的逻辑写在纸上

检验: 关掉代码 → 在白板上画 retrieval 的流程图 → 能画出来就过了
```

### 第 2 周：手写 agent_loop

```
目标: 独立写出一个 run_review_loop()
任务:
  1. 读我的 agent_loop.py 3 遍
  2. 关掉所有代码
  3. 自己建一个 test_agent_loop.py
  4. 写 run_review_loop()（不看任何参考）
  5. 写 parse_review_json() 的至少 2 级 fallback
  6. 写完后对比我的版本，标出不同之处

检验: 你的版本能跑通 node1 的测试（输入"做一个1kHz RC滤波器"）
```

### 第 3 周：写一个完整的 Agent

```
目标: 新增 Agent ⑥——"单元测试生成 Agent"
任务:
  1. 自己设计这个 Agent 的 generate/review/revise 三个 prompt
  2. 写 node5_unittest.py（仿照 node1 的结构）
  3. 在 pipeline.py 中注册这个节点
  4. 跑通一个简单用例

为什么选"单元测试生成"？
  - 需求明确（输入模型代码，输出单元测试）
  - prompt 简单（不需要 42 个官方示例）
  - 可以专注在"写 Agent 结构"而不是"写 prompt 内容"

检验: 你的新 Agent 能生成可运行的 Python 单元测试
```

### 第 4 周：改造现有 Agent

```
目标: 修改 node2 让它支持新的 SysML 官方示例
任务:
  1. 在 DOMAIN_MAP 中新增一个域（如 "fluidic"）
  2. 在 DOMAIN_KEYWORDS 中新增对应关键词
  3. 在全流程中跑一个该域的用例（如"液压系统"）
  4. 分析检索出的官方示例是否正确

检验: 跑通一个新域的用例，且检索出的示例是相关的
```

---

## 怎么判断自己学到了

每周末问自己 3 个问题：

1. **关掉代码，能画出 pipeline 流程图吗？**（节点 + 连线 + 路由条件）
2. **能说出 4 级 JSON fallback 分别是什么吗？**（代码块 → 正则 → 括号计数 → 放行）
3. **能新增一个 Agent 吗？**（不抄代码，新建文件，写 generate/review/revise 三个函数）

3 个都 Yes → 你可以自己写 V7 了。
