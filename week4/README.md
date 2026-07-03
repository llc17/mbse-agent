# MBSE+AI 自动化闭环系统 — V3 质量自检版

V2 能发现"跑不起来的"，V3 开始拦住"跑得起来但是错的"。

## V3 架构

```
START → node1(需求解析+矛盾自检) → HITL1
     → node2(SysML v2生成) → Q_cross_validate(交叉校验) → HITL2
     → node3_subgraph(Modelica生成+编译+仿真+自修复) → Q_physics_validate(物理验证)
     → node4(总结报告) → END
```

## V3 相比 V2 多了什么

| V2 能发现的错误 | V3 新增能发现的错误 |
|----------------|-------------------|
| 编译失败 | SysML 里的 R=10，需求写的 R=1000（交叉校验拦） |
| 仿真跑崩 | 仿真通过了但截止频率是 100Hz，需求要 1kHz（物理验证拦） |
| | 每次 repair LLM 到底改了什么（修复日志记录） |
| | 电气仿真跑了 1000s、热仿真跑了 0.01s（stopTime 修复） |

## 三项自检

| 自检 | 位置 | 方式 | 失败打回 |
|------|------|------|---------|
| A1 矛盾检测 | node1 prompt 内嵌 | LLM 生成时自审参数一致性 | 不拦截，仅提示 |
| A2 交叉校验 | node2→HITL2 间，新节点 | LLM 对比 req JSON vs SysML 代码参数值 | node2 重生成 |
| A3 物理验证 | node3→node4 间，新节点 | 从 CSV 算截止频率/稳态温度，对比理论值 | node3 repair |

## 项目结构

```
week4/
├── src/                    # 工作代码 (11 个 .py)
│   ├── pipeline.py         # LangGraph 主图编排（+2节点 +3路由）
│   ├── schemas.py          # 数据契约（+QualityCheckResult +RepairLogEntry）
│   ├── node_quality.py     # 🆕 质量检查节点（交叉校验+物理验证）
│   ├── node1_requirement.py   # 需求解析（prompt 含矛盾自检）
│   ├── node2_sysml.py      # SysML v2 生成（V3 不变）
│   ├── node3_modelica.py   # Modelica 仿真（stopTime适配+MAT→CSV+修复日志）
│   ├── node4_summary.py    # 总结（含质量检查结果）
│   ├── llm_client.py       # DeepSeek API 封装
│   ├── utils.py            # 工具函数（+get_stop_time_for_domain）
│   └── main.py             # 入口（V3 banner + 新 State 字段）
│
├── src_commented/          # 全量逐行中文注释版（教学用）
├── prompts/                # Prompt 模板（+q_cross_validate.txt）
├── experiments/            # 批量实验框架（含 V3 质量指标收集）
├── V3-优缺点.md            # 设计决策记录
└── README.md
```

## 快速开始

```bash
# 1. 安装依赖
pip install langgraph langgraph-checkpoint requests pydantic matplotlib

# 2. 配置 API Key
set DEEPSEEK_API_KEY=your_key_here

# 3. 单次调试（最快验证流程）★ 推荐
python experiments/run_experiment.py --single

# 4. 小规模实验（RC滤波器 × 4种retries × 3种温度 × 5次 = 60次）
python experiments/run_experiment.py --small

# 5. 交互模式（带 HITL 人工确认）
python src/main.py --mode interactive
```

## 实验数据说明

实验框架输出 `results.json` 含 V3 新增质量指标：

| 指标 | 说明 |
|------|------|
| `success` | 编译+仿真是否成功 |
| `cross_validate_passed` | 需求↔SysML 参数是否一致 |
| `physics_validate_passed` | 仿真实测值↔理论值偏差是否在阈值内 |
| `physics_deviation_pct` | 物理量偏差百分比（如 f_c 偏差 0.0%） |
| `repair_count` | node3 修复次数 |
