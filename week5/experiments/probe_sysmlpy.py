"""
V4 探针实验：测试新 prompt 的 sysmlpy 语法通过率。

用法:
    cd D:/mbse/week5
    python experiments/probe_sysmlpy.py              # 默认: RC 用例 x 10 次
    python experiments/probe_sysmlpy.py --case rc_lowpass --trials 20

目的: 在正式集成 sysmlpy 到流水线（H1）之前，先验证 H3 的 prompt 升级
      是否能让 LLM 生成的 SysML 代码被标准解析器接受。

决策规则:
    - sysmlpy 通过率 >= 50% → H1 继续（打分制集成）
    - sysmlpy 通过率 < 50%  → 启动 Plan B（后处理自动修复 + 加强 few-shot）
"""

import sys
from pathlib import Path

_project_dir = Path(__file__).resolve().parent.parent  # D:\mbse\week5
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

import argparse
import json
import logging
import time
from collections import Counter
from datetime import datetime

# Windows UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(_project_dir))

from src.llm_client import chat, user_msg
from src.utils import load_prompt, clean_code_block
from src.node2_sysml import _build_parameter_replacements

logger = logging.getLogger("probe_sysmlpy")

# ── 检查 sysmlpy ──
try:
    import sysmlpy
    SYSPY_AVAILABLE = True
except ImportError:
    SYSPY_AVAILABLE = False
    print("❌ sysmlpy 未安装，探针实验无法进行。请先 pip install sysmlpy")
    sys.exit(1)


def generate_sysml_once(req: dict, temperature: float = 0.3) -> str:
    """调用 LLM 生成一次 SysML 代码。绕过 node2 流水线，直接测 prompt 质量。"""
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.get("parameters", {}).items())
    constraints_str = "\n".join(f"  - {c}" for c in req.get("constraints", []))

    prompt = load_prompt("node2_sysml.txt")
    prompt = prompt.replace("{component_type}", req.get("component_type", ""))
    prompt = prompt.replace("{component_name}", req.get("component_name", req.get("component_type", "")))
    prompt = prompt.replace("{parameters}", params_str)
    prompt = prompt.replace("{topology}", req.get("topology", ""))
    prompt = prompt.replace("{constraints}", constraints_str)
    prompt = prompt.replace("{prev_error_section}", "")

    # 参数占位符
    param_replacements = _build_parameter_replacements(req.get("parameters", {}))
    for ph, val in param_replacements.items():
        prompt = prompt.replace(ph, val)
    # 显式 R/C 兼容
    prompt = prompt.replace("{parameters_R}", str(req.get("parameters", {}).get("R", 1000)))
    prompt = prompt.replace("{parameters_C}", str(req.get("parameters", {}).get("C", 1e-6)))

    sysml_code = chat([user_msg(prompt)], temperature=temperature, max_tokens=4096).strip()
    return clean_code_block(sysml_code, "sysml")


def check_sysmlpy(code: str) -> dict:
    """用 sysmlpy.loads() 解析，返回 {passed, errors, error_categories}。"""
    try:
        sysmlpy.loads(code)
        return {"passed": True, "errors": [], "error_categories": []}
    except Exception as e:
        err_str = str(e)
        categories = _categorize_error(err_str)
        return {
            "passed": False,
            "errors": [err_str[:300]],
            "error_categories": categories,
        }


def _categorize_error(err_str: str) -> list[str]:
    """将 sysmlpy 错误归类，用于统计分析。"""
    cats = []
    if "import" in err_str.lower():
        cats.append("import")
    if "attribute" in err_str.lower() or "'>' " in err_str or "Real" in err_str:
        cats.append("attribute")
    if "port" in err_str.lower():
        cats.append("port")
    if "part def" in err_str.lower() or "part " in err_str.lower():
        cats.append("part_def")
    if "package" in err_str.lower():
        cats.append("package")
    if "connect" in err_str.lower():
        cats.append("connect")
    if "{" in err_str or "}" in err_str or "brace" in err_str.lower():
        cats.append("bracket")
    if not cats:
        cats.append("other")
    return cats


def run_probe(test_case: dict, trials: int, temperature: float) -> dict:
    """主探针逻辑。"""
    req = {
        "component_type": test_case.get("expected", {}).get("component_type", test_case.get("domain", "")),
        "component_name": test_case.get("id", ""),
        "parameters": test_case.get("expected", {}).get("parameters", {}),
        "topology": test_case.get("raw_input", ""),
        "constraints": [],
    }

    results = []
    error_categories_all = Counter()

    print(f"\n{'='*60}")
    print(f"  探针实验: {test_case['id']} × {trials} 次")
    print(f"  温度: {temperature}")
    print(f"{'='*60}\n")

    for i in range(1, trials + 1):
        print(f"[{i}/{trials}] 生成中...", end=" ", flush=True)
        t0 = time.time()

        try:
            code = generate_sysml_once(req, temperature)
            check = check_sysmlpy(code)
        except Exception as e:
            check = {"passed": False, "errors": [f"LLM/网络异常: {str(e)[:200]}"], "error_categories": ["llm_error"]}

        elapsed = time.time() - t0
        status = "✅ PASS" if check["passed"] else "❌ FAIL"
        print(f"{status} ({elapsed:.1f}s)")

        if not check["passed"]:
            for cat in check.get("error_categories", []):
                error_categories_all[cat] += 1
            for err in check.get("errors", []):
                print(f"     → {err[:200]}")

        results.append({
            "trial": i,
            "passed": check["passed"],
            "errors": check.get("errors", []),
            "categories": check.get("error_categories", []),
            "duration": round(elapsed, 1),
        })

        # API rate limiting
        if i < trials:
            time.sleep(0.5)

    # 统计
    passed_count = sum(1 for r in results if r["passed"])
    pass_rate = passed_count / trials * 100 if trials > 0 else 0

    summary = {
        "test_case": test_case["id"],
        "trials": trials,
        "temperature": temperature,
        "passed": passed_count,
        "failed": trials - passed_count,
        "pass_rate_pct": round(pass_rate, 1),
        "error_categories": dict(error_categories_all.most_common()),
        "results": results,
    }

    return summary


def print_report(summary: dict):
    """打印探针报告。"""
    print(f"\n{'='*60}")
    print(f"  探针实验结果")
    print(f"{'='*60}")
    print(f"  用例: {summary['test_case']}")
    print(f"  次数: {summary['trials']}")
    print(f"  通过: {summary['passed']}/{summary['trials']} ({summary['pass_rate_pct']}%)")
    print(f"  失败: {summary['failed']}/{summary['trials']}")

    if summary["error_categories"]:
        print(f"\n  错误分类:")
        for cat, count in sorted(summary["error_categories"].items(), key=lambda x: -x[1]):
            print(f"    {cat}: {count} 次")

    # 决策建议
    rate = summary["pass_rate_pct"]
    print(f"\n  {'─'*50}")
    if rate >= 80:
        print(f"  ✅ 通过率 {rate}% >= 80% — prompt 升级效果优异，H1 直接集成 sysmlpy")
    elif rate >= 50:
        print(f"  ✅ 通过率 {rate}% >= 50% — 达到预期，H1 按计划集成（打分制）")
    elif rate >= 30:
        print(f"  ⚠️  通过率 {rate}% >= 30% — 部分有效，H1 集成 + 加强 few-shot + 后处理修复")
    else:
        print(f"  🔴 通过率 {rate}% < 30% — 启动 Plan B: 后处理自动修复为主 + 极限 few-shot")
    print(f"  {'─'*50}")


def main():
    parser = argparse.ArgumentParser(description="V4 探针实验 — 测 sysmlpy 语法通过率")
    parser.add_argument("--case", type=str, default="rc_lowpass", help="测试用例 ID")
    parser.add_argument("--trials", type=int, default=10, help="试验次数 (default: 10)")
    parser.add_argument("--temperature", type=float, default=0.3, help="LLM 温度 (default: 0.3)")
    parser.add_argument("--output", type=str, default=None, help="输出 JSON 路径")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    if not SYSPY_AVAILABLE:
        print("❌ sysmlpy 未安装，探针实验无法进行。")
        print("   pip install sysmlpy")
        sys.exit(1)

    # 加载测试用例
    test_cases_path = Path(__file__).parent / "test_cases.json"
    with open(test_cases_path, "r", encoding="utf-8") as f:
        test_cases = json.load(f)["test_cases"]

    test_case = next((tc for tc in test_cases if tc["id"] == args.case), None)
    if not test_case:
        print(f"❌ 未找到用例: {args.case}")
        print(f"   可用用例: {[tc['id'] for tc in test_cases]}")
        sys.exit(1)

    # 检查 API Key
    import os
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("❌ 缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)

    # 运行探针
    summary = run_probe(test_case, args.trials, args.temperature)

    # 保存结果
    output_dir = Path(__file__).parent / "results"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = args.output or str(output_dir / f"probe_{args.case}_{timestamp}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, ensure_ascii=False, indent=2, default=str)
    print(f"\n详细结果: {output_path}")

    print_report(summary)


if __name__ == "__main__":
    main()
