# MBSE+AI 自动化闭环系统 — V4 质量深化版

V3 能发现"跑得起来但是错的"，V4 开始用标准解析器验证 SysML 语法正确性。

## V4 相比 V3 多了什么

| V3 | V4 新增 |
|----|---------|
| 3 条正则语法检查 | sysmlpy 标准解析器（打分制质量门） |
| 2 个测试用例 | 5 个（+RLC / 双房间热 / 运放） |
| 物理验证硬编码 | expected_physics 配置文件驱动 |
| 仿真结果无人确认 | 节点3 HITL（展示曲线 → 用户确认/打回） |
| 无官方对比 | LLM vs 官方 SysML 示例对比报告 |
| 无 token 追踪 | API 调用累计 token 统计 |

## V4 架构

```
START → node1(需求解析+矛盾自检) → HITL1
     → node2(SysML v2生成) → Q_cross_validate(交叉校验) → HITL2
     → node3_subgraph(Modelica生成+编译+仿真+自修复) → node3_hitl(🆕仿真确认)
     → Q_physics_validate(物理验证,🆕配置驱动) → node4(总结报告) → END
```

## 项目结构

```
week5/
├── src/                    # 工作代码
│   ├── pipeline.py         # LangGraph 主图编排
│   ├── schemas.py          # 数据契约
│   ├── node_quality.py     # 质量检查（交叉校验+物理验证）
│   ├── node1_requirement.py   # 需求解析
│   ├── node2_sysml.py      # SysML v2 生成（🆕 sysmlpy 语法检查）
│   ├── node3_modelica.py   # Modelica 仿真
│   ├── node4_summary.py    # 总结
│   ├── llm_client.py       # DeepSeek API（🆕 token 追踪）
│   ├── utils.py            # 工具函数
│   └── main.py             # 入口
│
├── prompts/                # Prompt 模板（🆕 对齐官方 SysML 写法）
├── experiments/            # 批量实验框架
│   └── test_cases.json     # 🆕 5 用例 + expected_physics
├── outputs/                # 运行产出
└── README.md
```

## 快速开始

```bash
# 1. 安装依赖
pip install langgraph langgraph-checkpoint requests pydantic matplotlib sysmlpy

# 2. 配置 API Key
set DEEPSEEK_API_KEY=your_key_here

# 3. 验收测试（V4 全用例冒烟）
python verify_v4.py

# 4. 单次调试
python experiments/run_experiment.py --single

# 5. 交互模式（含 V4 新增 node3 HITL）
python src/main.py --mode interactive
```

## V4 任务清单

| # | 任务 | 说明 |
|---|------|------|
| H3 | prompt 升级 | node2_sysml.txt 对齐官方 SysML 写法 |
| — | 探针实验 | 新 prompt × RC × 10 次，测 sysmlpy 通过率 |
| H1 | sysmlpy 集成 | 打分制语法检查替换正则，含降级策略 |
| D+E | 多用例 + 物理验证配置化 | 5 用例 + expected_physics 配置驱动 |
| F | 节点3 HITL | 仿真曲线确认/打回 |
| H2 | LLM vs 官方对比 | 10 项 checklist 对比报告 |
| G | 验收 + 实验 + 月报 | verify_v4.py + token 追踪 + M1 月报 |
