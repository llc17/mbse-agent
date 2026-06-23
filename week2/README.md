# MBSE+AI 自动化闭环系统

基于 LLM Agent 的工业软件工作流，通过 "AI 生成 + 物理仿真驱动" 的自反思闭环，解决传统系统工程建模门槛高、验证脱节的问题。

## 4 节点流水线

| 节点 | 名称 | 输入 | 产出 |
|------|------|------|------|
| 1 | 需求解析 | 自然语言 | `StructuredRequirement` JSON |
| 2 | SysML v2 生成 | StructuredRequirement | `.sysml` 文件 |
| 3 | Modelica 仿真 | req + sysml | `.mo` + CSV + 仿真曲线 PNG |
| 4 | 总结 | 全部产物 | `summary.md` |

## 快速开始

```bash
# 1. 设置 API Key
export DEEPSEEK_API_KEY=sk-xxx

# 2. 安装依赖
pip install -r requirements.txt

# 3. 运行
python -m src.main
```

## 项目结构

```
src/         — 所有源码
prompts/     — LLM 提示词模板
outputs/     — 每次运行的完整产出
experiments/ — 失败实验归档
docs/        — 文档
```

## 工具链

- **LLM**: DeepSeek Chat API
- **建模**: SysML v2（OMG Pilot Implementation，手动看图）
- **仿真**: OpenModelica + OMPython
- **数据**: Pydantic 类型校验
