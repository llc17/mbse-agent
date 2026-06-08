"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  节点 3 — Modelica 生成 + 编译仿真 + 自修复                                   ║
║                                                                            ║
║  输入：StructuredRequirement + SysMLArtifact（来自节点1和2）                   ║
║  输出：ModelicaArtifact（.mo + CSV + PNG + success）                          ║
║                                                                            ║
║  流程（while 循环，最多重试2次）：                                             ║
║    ① LLM 生成 .mo 代码                                                       ║
║    ② OMC 编译 → 失败 → 错误回喂 → 重试（attempt+1, continue）                 ║
║    ③ OMC 仿真 → 失败 → 错误回喂 → 重试                                       ║
║    ④ 成功 → CSV 落盘 + matplotlib 画 PNG → return                             ║
║                                                                            ║
║  下游消费者：node4_summary（读 success/csv_path/plot_path）                    ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import os               # 标准库：路径处理
import re               # 标准库：正则表达式，提取模型名
import subprocess       # 标准库：调外部命令（omc 编译仿真）
from pathlib import Path  # 标准库：Path 文件路径

from src.llm_client import chat, user_msg  # 调 LLM
from src.schemas import StructuredRequirement, SysMLArtifact, ModelicaArtifact
#                       └────── 输入1 ──────┘  └─── 输入2 ───┘  └──── 输出 ────┘


def _load_prompt(name: str) -> str:        # 同 node1/2，读模板文件
    path = os.path.join(os.path.dirname(__file__), "..", "prompts", name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ===== 核心函数 =====
def generate_and_simulate(
    req: StructuredRequirement,            # 节点1产出 — 知道要仿什么
    sysml_artifact: SysMLArtifact,         # 节点2产出 — 知道系统架构
    modelica_dir: Path,                    # .mo 保存目录
    results_dir: Path,                     # CSV/PNG 保存目录
    max_retries: int = 2,                  # 最多重试2次
) -> ModelicaArtifact:                     # 返回节点3产出
    """生成 Modelica 代码，编译，仿真，失败自修复。"""

    # 第41-42行：格式化需求参数和约束，供 prompt 使用
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    artifact = ModelicaArtifact()          # 创建空的产出对象（所有字段用默认值）
    attempt = 1                            # 初始化计数器

    # 第47行：while 循环 — 自修复引擎
    while attempt <= max_retries:          # 条件：计数器 ≤ 2

        # === 构建 prompt：如果有上次错误，拼入修正提示 ===
        prev_error_section = ""            # 默认无错误提示
        if artifact.errors:                # 第2次才有内容（errors 非空列表）
            prev_error_section = (         # 拼错误回喂段落到 prompt
                f"\n## 上次编译/仿真的错误日志（请修正这些错误）\n"
                f"```\n{chr(10).join(artifact.errors)}\n```"
            )                              # chr(10) = "\n"

        prompt = (                         # 拼最终 prompt
            _load_prompt("node3_modelica.txt")   # 从磁盘读模板
            .replace("{component_type}", req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{constraints}", constraints_str)
            .replace("{sysml_code}", sysml_artifact.sysml_code[:3000])  # 截前3000字
            .replace("{prev_error_section}", prev_error_section)        # 填错误提示
        )

        print(f"[节点3] 第{attempt}次尝试生成 Modelica...")
        mo_code = chat(                    # 调 LLM 生成 .mo 代码
            [user_msg(prompt)],
            temperature=0.2,               # 低温度 → 代码要精确
            max_tokens=4096                # Modelica 代码可能很长
        ).strip()
        mo_code = _clean_code_block(mo_code, "modelica")  # 去 ```modelica ... ```

        model_name = _extract_model_name(mo_code) or "MyModel"  # 从代码提取模型名
        artifact.modelica_code = mo_code   # 填入产出对象
        artifact.attempts = attempt        # 记录第几次

        # === 保存 .mo 到 modelica/ 目录 ===
        mo_path = modelica_dir / "model.mo"
        mo_path.write_text(mo_code, encoding="utf-8")
        artifact.file_path = str(mo_path)

        # ============================================================
        #  步骤 A：编译（OMC 检查语法/类型/方程平衡）
        # ============================================================
        print(f"[节点3] 编译 {model_name}...")
        compile_ok, compile_err = _compile(str(mo_path), model_name)
        #                           └───── 返回 (True/False, 错误文本) ─────┘

        if not compile_ok:                 # 编译失败
            print(f"[节点3] 编译失败: {compile_err[:200]}")
            artifact.errors.append(        # 记录错误
                f"编译: {compile_err[:500]}"  # 截前500字
            )
            attempt += 1                   # 计数器+1
            continue                       # ← 跳回 while 开头，错误回喂给 LLM

        # ============================================================
        #  步骤 B：仿真（OMC 求解方程，生成 CSV）
        # ============================================================
        print(f"[节点3] 仿真 {model_name}...")
        sim_ok, sim_err = _simulate(str(mo_path), model_name, results_dir)
        #                  └───── 返回 (True/False, 错误文本) ─────┘

        if not sim_ok:                     # 仿真失败
            print(f"[节点3] 仿真失败: {sim_err[:200]}")
            artifact.errors.append(        # 记录错误
                f"仿真: {sim_err[:500]}"
            )
            attempt += 1                   # 计数器+1
            continue                       # ← 跳回 while 开头，错误回喂给 LLM

        # ============================================================
        #  步骤 C：成功！保存结果 + 画图
        # ============================================================
        csv_path = results_dir / "simulation.csv"   # CSV 路径
        plot_path = results_dir / "simulation.png"  # PNG 路径
        artifact.csv_path = str(csv_path)   # 记录 CSV 路径
        artifact.plot_path = str(plot_path) # 记录 PNG 路径
        artifact.success = True             # ← 标记成功！

        if csv_path.exists():               # CSV 确实生成了
            _plot_csv(                      # matplotlib 画图
                str(csv_path),              # 从 CSV 读数据
                str(plot_path),             # 保存到 PNG
                req.component_type          # 图标题
            )
        else:
            print(f"[节点3] 警告: CSV 文件未找到 {csv_path}")

        print(f"[节点3] 仿真成功！PNG: {plot_path}")
        break                               # ← 成功，跳出 while 循环
    else:
        # while...else: 循环正常结束（没用 break）时执行
        # 即 max_retries 次全部失败
        print(f"[节点3] {max_retries}次重试后仿真仍未成功。")

    return artifact                         # ← 返回到 main.py


# ============================================================
#  编译：先试 OMPython（Python 接口），失败则 subprocess 调 omc
# ============================================================
def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    """返回 (成功?, 错误信息)。"""

    # 方案1：用 OMPython（pip install OMPython）
    try:                                   # try = 尝试，可能抛异常
        from OMPython import ModelicaSystem  # 本地导入
        ModelicaSystem(mo_path, model_name)  # 创建模型系统 → 触发编译
        return True, ""                    # 没抛异常 = 编译通过
    except ImportError:                    # OMPython 没装
        pass                               # 跳过，用方案2
    except Exception as e:                 # 编译错误
        return False, str(e)              # 返回失败 + 错误文本

    # 方案2：subprocess 调命令行 omc
    try:
        r = subprocess.run(               # 调外部命令
            ["omc", "--modelica", mo_path],  # omc --modelica 编译 .mo
            capture_output=True,            # 捕获 stdout 和 stderr
            text=True,                      # 返回 str 而非 bytes
            timeout=60,                     # 60秒超时
        )
        if r.returncode == 0:              # 返回码 0 = 成功
            return True, ""
        return False, r.stderr or r.stdout # 返回失败 + 错误输出
    except FileNotFoundError:              # omc 命令没找到
        return False, "omc 命令未找到，请确认 OpenModelica 已安装并在 PATH 中"
    except Exception as e:
        return False, str(e)


# ============================================================
#  仿真：先试 OMPython，失败则 subprocess 调 omc
# ============================================================
def _simulate(
    mo_path: str,                          # .mo 文件路径
    model_name: str,                       # 模型类名
    results_dir: Path                      # 输出目录
) -> tuple[bool, str]:
    """返回 (成功?, 错误信息)。"""

    # 方案1：OMPython
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)  # 加载模型
        sim.setSimulationOptions(          # 设仿真参数
            "stopTime=10.0",               # 仿真10秒
            "numberOfIntervals=500"        # 500个采样点
        )
        sim.simulate()                     # 执行仿真 → 生成 CSV
        return True, ""
    except ImportError:
        pass
    except Exception as e:
        return False, str(e)

    # 方案2：subprocess 调 omc
    try:
        mos_script = results_dir / "_sim.mos"   # MOS 脚本路径
        mos_script.write_text(             # 写脚本内容
            f'loadFile("{mo_path}");\n'    # 加载 .mo 文件
            f"simulate({model_name}, stopTime=10.0, numberOfIntervals=500);\n"
        )
        r = subprocess.run(               # 调 omc 执行脚本
            ["omc", str(mos_script)],
            capture_output=True,
            text=True,
            timeout=120,                   # 仿真可能更慢，120秒
            cwd=str(results_dir),          # 工作目录=results/，CSV 生成在这里
        )
        if r.returncode == 0:
            return True, ""
        return False, r.stderr or r.stdout
    except FileNotFoundError:
        return False, "omc 命令未找到"
    except Exception as e:
        return False, str(e)


# ============================================================
#  工具函数
# ============================================================
def _clean_code_block(text: str, lang: str) -> str:   # 去 markdown 包裹
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _extract_model_name(code: str) -> str | None:     # 正则提取模型名
    """从 "model MyRCFilter" 中提取 "MyRCFilter"。"""
    m = re.search(r"model\s+(\w+)", code)  # \s+ = 空格, \w+ = 字母数字下划线
    return m.group(1) if m else None       # group(1) = 括号里捕获的内容


def _plot_csv(csv_path: str, plot_path: str, title: str):  # matplotlib 画图
    """从 CSV 读取仿真数据，画曲线图。"""
    import csv
    import matplotlib
    matplotlib.use("Agg")                  # 非交互后端 — 不需要弹窗
    import matplotlib.pyplot as plt

    rows = []
    with open(csv_path, "r") as f:         # 读 CSV
        reader = csv.reader(f)
        rows = list(reader)                # 全部行 → 列表

    if len(rows) < 2:                      # 只有表头没数据
        print("[节点3] CSV 数据不足，跳过画图。")
        return

    header = rows[0]                       # 第一行 = 列名（time, variable1, ...）
    data = {col: [] for col in header}     # {列名: [值列表]}
    for row in rows[1:]:                   # 从第2行开始（跳过表头）
        for i, col in enumerate(header):   # 遍历每列
            try:
                data[col].append(float(row[i]))  # 字符串 → 浮点数
            except (ValueError, IndexError):
                pass                       # 解析失败跳过

    time_col = header[0]                   # 第一列 = 时间轴
    plt.figure(figsize=(10, 5))            # 画布大小 10×5 英寸
    for col in header[1:]:                 # 从第2列开始画曲线
        if data[col]:
            plt.plot(
                data[time_col][:len(data[col])],  # X轴=时间
                data[col],                        # Y轴=变量值
                label=col                         # 图例=变量名
            )

    plt.xlabel(time_col)                   # X轴标签
    plt.ylabel("Value")                    # Y轴标签
    plt.title(f"仿真结果: {title}")        # 标题
    plt.legend()                           # 显示图例
    plt.grid(True)                         # 显示网格
    plt.tight_layout()                     # 自动调整边距
    plt.savefig(plot_path, dpi=150)        # 保存 PNG，150 DPI
    plt.close()                            # 关闭图形，释放内存


# ╔══════════════════════════════════════════════════════════════╗
# ║  数据流追踪：                                                  ║
# ║                                                              ║
# ║  main.py:                                                    ║
# ║    mo = generate_and_simulate(req, sysml, modelica_dir,       ║
# ║                                results_dir)                  ║
# ║         req = StructuredRequirement (R=1000, C=1e-6, ...)     ║
# ║         sysml = SysMLArtifact (sysml_code=...)               ║
# ║         modelica_dir = outputs/run_xxx/modelica/             ║
# ║         results_dir = outputs/run_xxx/results/               ║
# ║         │                                                    ║
# ║         ▼                                                    ║
# ║  node3: while attempt <= 2:                                  ║
# ║           第1次: LLM 生成 .mo → 编译 → 仿真 → 成功 → break     ║
# ║           (或) 编译失败 → errors=["变量R未声明"]               ║
# ║                attempt=2 → 错误回喂 LLM → 重新生成             ║
# ║         modelica_dir/model.mo 落盘                             ║
# ║         results_dir/simulation.csv 落盘                       ║
# ║         results_dir/simulation.png 落盘                       ║
# ║         return ModelicaArtifact(success=True, ...)            ║
# ║                                                              ║
# ║  main.py 继续:                                                ║
# ║    summary = generate_summary(req, sysml, mo, results_dir)    ║
# ║              ↑ 三个产出都传给节点4                              ║
# ╚══════════════════════════════════════════════════════════════╝
