"""
节点 3 — Modelica 生成 + 仿真 + 自修复。

从 StructuredRequirement + SysMLArtifact 出发：
  1. LLM 生成 .mo 代码
  2. OMC 编译 + 仿真
  3. 失败 → error_log 回喂 LLM → 重试 (max_retries=2)
  4. 成功 → 存 CSV + matplotlib 画 PNG

用法：
    from src.node3_modelica import generate_and_simulate
    artifact = generate_and_simulate(req, sysml_artifact, work_dir)
"""

import os
import re
import subprocess
from pathlib import Path

from src.llm_client import chat, user_msg
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact


def _load_prompt(name: str) -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def generate_and_simulate(
    req: StructuredRequirement,
    sysml_artifact: SysMLArtifact,
    modelica_dir: Path,
    results_dir: Path,
    max_retries: int = 2,
) -> ModelicaArtifact:
    """生成 Modelica 代码，编译，仿真，失败自修复。

    .mo 文件存到 modelica_dir，CSV/PNG 存到 results_dir。
    """
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = ModelicaArtifact()
    attempt = 1

    while attempt <= max_retries:
        # 构建 prompt
        prev_error_section = ""
        if artifact.errors:
            prev_error_section = (
                f"\n## 上次编译/仿真的错误日志（请修正这些错误）\n"
                f"```\n{chr(10).join(artifact.errors)}\n```"
            )

        prompt = (
            _load_prompt("node3_modelica.txt")
            .replace("{component_type}", req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{sysml_code}", sysml_artifact.sysml_code[:3000])
            .replace("{prev_error_section}", prev_error_section)
        )

        print(f"[节点3] 第{attempt}次尝试生成 Modelica...")
        mo_code = chat([user_msg(prompt)], temperature=0.2, max_tokens=4096).strip()
        mo_code = _clean_code_block(mo_code, "modelica")

        # 提取模型名
        model_name = _extract_model_name(mo_code) or "MyModel"
        artifact.modelica_code = mo_code
        artifact.attempts = attempt

        # 保存 .mo 文件到 modelica/
        mo_path = modelica_dir / "model.mo"
        mo_path.write_text(mo_code, encoding="utf-8")
        artifact.file_path = str(mo_path)

        # 编译 + 仿真（结果输出到 results/）
        print(f"[节点3] 编译 {model_name}...")
        compile_ok, compile_err = _compile(str(mo_path), model_name)

        if not compile_ok:
            print(f"[节点3] 编译失败: {compile_err[:200]}")
            artifact.errors.append(f"编译: {compile_err[:500]}")
            attempt += 1
            continue

        print(f"[节点3] 仿真 {model_name}...")
        sim_ok, sim_err = _simulate(str(mo_path), model_name, results_dir)

        if not sim_ok:
            print(f"[节点3] 仿真失败: {sim_err[:200]}")
            artifact.errors.append(f"仿真: {sim_err[:500]}")
            attempt += 1
            continue

        # 成功！CSV 和 PNG 存到 results/
        csv_path = results_dir / "simulation.csv"
        plot_path = results_dir / "simulation.png"
        artifact.csv_path = str(csv_path)
        artifact.plot_path = str(plot_path)
        artifact.success = True

        if csv_path.exists():
            _plot_csv(str(csv_path), str(plot_path), req.component_type)
        else:
            print(f"[节点3] 警告: CSV 文件未找到 {csv_path}")

        print(f"[节点3] 仿真成功！PNG: {plot_path}")
        break
    else:
        print(f"[节点3] {max_retries}次重试后仿真仍未成功。")

    return artifact


# ============================================================
# 编译与仿真（复用 Week 1 已验证的模式）
# ============================================================

def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    """用 OMC 编译 .mo 文件。先尝试 OMPython，失败则 subprocess 调 omc。"""
    try:
        from OMPython import ModelicaSystem
        ModelicaSystem(mo_path, model_name)
        return True, ""
    except ImportError:
        pass
    except Exception as e:
        return False, str(e)

    # fallback: subprocess 调 omc
    try:
        r = subprocess.run(
            ["omc", "--modelica", mo_path],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return True, ""
        return False, r.stderr or r.stdout
    except FileNotFoundError:
        return False, "omc 命令未找到，请确认 OpenModelica 已安装并在 PATH 中"
    except Exception as e:
        return False, str(e)


def _simulate(
    mo_path: str, model_name: str, results_dir: Path
) -> tuple[bool, str]:
    """仿真模型，产出 CSV。"""
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        sim.setSimulationOptions("stopTime=10.0", "numberOfIntervals=500")
        sim.simulate()
        return True, ""
    except ImportError:
        pass
    except Exception as e:
        return False, str(e)

    # fallback: subprocess 调 omc
    try:
        mos_script = results_dir / "_sim.mos"
        mos_script.write_text(
            f'loadFile("{mo_path}");\n'
            f"simulate({model_name}, stopTime=10.0, numberOfIntervals=500);\n"
        )
        r = subprocess.run(
            ["omc", str(mos_script)],
            capture_output=True, text=True, timeout=120,
            cwd=str(results_dir),
        )
        if r.returncode == 0:
            return True, ""
        return False, r.stderr or r.stdout
    except FileNotFoundError:
        return False, "omc 命令未找到"
    except Exception as e:
        return False, str(e)


# ============================================================
# 工具函数
# ============================================================

def _clean_code_block(text: str, lang: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _extract_model_name(code: str) -> str | None:
    """从 Modelica 代码中提取模型类名。"""
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def _plot_csv(csv_path: str, plot_path: str, title: str):
    """从 CSV 画仿真曲线。"""
    import csv
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rows = []
    with open(csv_path, "r") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if len(rows) < 2:
        print("[节点3] CSV 数据不足，跳过画图。")
        return

    # 第一列是时间
    header = rows[0]
    data = {col: [] for col in header}
    for row in rows[1:]:
        for i, col in enumerate(header):
            try:
                data[col].append(float(row[i]))
            except (ValueError, IndexError):
                pass

    time_col = header[0]
    plt.figure(figsize=(10, 5))
    for col in header[1:]:
        if data[col]:
            plt.plot(data[time_col][:len(data[col])], data[col], label=col)

    plt.xlabel(time_col)
    plt.ylabel("Value")
    plt.title(f"仿真结果: {title}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()
