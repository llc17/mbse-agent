"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  节点 4 — 总结生成                                                            ║
║                                                                            ║
║  输入：StructuredRequirement + SysMLArtifact + ModelicaArtifact（前3个节点）  ║
║  输出：SummaryArtifact（summary.md Markdown 文件）                            ║
║                                                                            ║
║  流程（最简单，单次 LLM 调用，无循环）：                                        ║
║    ① 把前3个产物的关键信息拼成 prompt                                         ║
║    ② LLM 生成 Markdown 总结                                                  ║
║    ③ 保存 summary.md → return                                                ║
║                                                                            ║
║  下游消费者：人类（直接打开 summary.md 看报告）                                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os  # 标准库：路径
from pathlib import Path  # 标准库：Path

from src.llm_client import chat, user_msg  # 调 LLM
from src.schemas import (                  # 分批导入，括号内换行
    StructuredRequirement,                 # 输入1 — 需求数据
    SysMLArtifact,                         # 输入2 — SysML 代码
    ModelicaArtifact,                      # 输入3 — 仿真结果
    SummaryArtifact,                       # 输出 — 总结
)


# ===== 核心函数 =====
def generate_summary(
    req: StructuredRequirement,            # 节点1产出
    sysml_artifact: SysMLArtifact,         # 节点2产出
    mo_artifact: ModelicaArtifact,         # 节点3产出
    results_dir: Path,                     # 保存目录（summary.md 放这里）
) -> SummaryArtifact:                      # 返回节点4产出
    """汇总全流程产物为 Markdown 总结。"""

    # 第28-52行：构造 prompt — 把三个产物的关键信息拼进去
    prompt = f"""你是一个系统工程报告撰写人。请根据以下全流程产出的数据，写一份简洁的系统设计总结报告。

## 需求
{req.model_dump_json(indent=2)}           # Pydantic 对象 → 格式化 JSON 字符串

## SysML v2 代码（节选前 1000 字符）
{sysml_artifact.sysml_code[:1000]}        # 截前1000字，太长浪费 token

## Modelica 代码（节选前 1000 字符）
{mo_artifact.modelica_code[:1000]}        # 同上

## 仿真结果
- 编译+仿真成功: {mo_artifact.success}    # True 或 False
- 尝试次数: {mo_artifact.attempts}        # 1 或 2
- 错误记录: {chr(10).join(f'  - {e[:100]}' for e in mo_artifact.errors) if mo_artifact.errors else '无'}

## 要求
写一份 Markdown 格式的总结，包含以下章节：
1. 项目概述（一句话）
2. 系统参数（表格）
3. 架构说明（基于 SysML v2 的关键部件和连接）
4. 仿真结果（成功/失败，关键曲线变量名）
5. 反思与下一步（哪些环节顺利，哪些需要改进）

总字数控制在 500 字以内。直接输出 Markdown。"""

    print("[节点4] 生成总结...")
    summary_text = chat(                   # 单次 LLM 调用，无循环
        [user_msg(prompt)],
        temperature=0.3,                   # 中等偏低 → 有结构但要可读
        max_tokens=2048                    # Markdown 500字，2048 足够
    )

    # 第57-58行：保存到磁盘
    file_path = results_dir / "summary.md"
    file_path.write_text(summary_text, encoding="utf-8")

    # 第60-67行：构造返回对象，记录所有文件路径引用
    return SummaryArtifact(
        summary_text=summary_text,                          # Markdown 全文
        file_path=str(file_path),                           # summary.md 路径
        requirement_path=str(results_dir / "requirement.json"),  # 需求 JSON 路径
        sysml_path=sysml_artifact.file_path,                # .sysml 路径（来自节点2）
        modelica_path=mo_artifact.file_path,                # .mo 路径（来自节点3）
        plot_path=mo_artifact.plot_path,                    # 仿真图路径（来自节点3）
    )


# ╔══════════════════════════════════════════════════════════════╗
# ║  数据流追踪（最后一步）：                                      ║
# ║                                                              ║
# ║  main.py:                                                    ║
# ║    summary = generate_summary(req, sysml, mo, results_dir)    ║
# ║              │                                               ║
# ║              ▼                                               ║
# ║  node4:  把 req + sysml_code + modelica_code + success        ║
# ║          拼成一段 prompt                                      ║
# ║              │                                               ║
# ║              ▼  chat()                                       ║
# ║          LLM 返回 Markdown 文本                               ║
# ║              │                                               ║
# ║              ▼                                               ║
# ║          保存 results_dir/summary.md                          ║
# ║          return SummaryArtifact（含所有文件路径引用）           ║
# ║                                                              ║
# ║  流水线结束。最终产出目录结构：                                   ║
# ║    outputs/run_xxx/                                          ║
# ║    ├── sysml/model.sysml                                     ║
# ║    ├── modelica/model.mo                                     ║
# ║    └── results/                                              ║
# ║        ├── requirement.json                                  ║
# ║        ├── simulation.csv                                    ║
# ║        ├── simulation.png                                    ║
# ║        └── summary.md                                        ║
# ╚══════════════════════════════════════════════════════════════╝
