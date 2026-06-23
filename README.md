# MBSE+AI 自动化闭环系统

LLM Agent 驱动的系统工程建模与物理仿真自反思闭环。

## 项目概述

- **目标**: 用 LLM Agent 打通 "自然语言需求 → SysML v2 系统建模 → Modelica 物理仿真 → 自修复闭环"
- **技术栈**: Python 3.13, LangGraph, DeepSeek-v4-pro, OpenModelica, SysML v2
- **导师**: 来自 OMG SysML v2 规范的 4 节点终极目标

## 版本迭代

| 版本 | 周次 | 架构 | 核心能力 |
|------|------|------|---------|
| V1 | Week 2 | Sequential Python | 4 节点端到端跑通：需求→SysML→Modelica仿真→总结 |
| V2 | Week 3 | LangGraph 状态图 | HITL 人机确认 + 打回机制 + 自修复子图(5次) + 实验框架 |

## 仓库结构

```
├── week2/                  # V1: 第一版最丑跑通
│   ├── src/                #   4节点 sequential 流水线
│   ├── prompts/            #   初期 prompt 模板
│   └── README.md
│
├── week3/                  # V2: LangGraph 架构升级
│   ├── src/                #   LangGraph 状态图 + HITL + 子图
│   ├── src_commented/      #   全量逐行中文注释版（学习用）
│   ├── prompts/            #   参考官方库的 prompt 模板
│   ├── experiments/        #   批量实验框架 (360次参数扫描)
│   ├── V2-优缺点.md         #   设计决策记录
│   └── 可用领域说明.txt     #   当前支持的物理域
```

## 快速开始

```bash
# 安装依赖
pip install langgraph langgraph-checkpoint requests pydantic matplotlib

# 配置 API Key
set DEEPSEEK_API_KEY=your_key_here

# 交互模式（带人工确认）
cd week3
python src/main.py

# 实验模式（自动确认，无人值守）
python src/main.py --mode experiment

# 批量实验
python experiments/run_experiment.py --small
```

