"""Unified command-line entry point for published V9 query tools."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable


_COMMAND_MODULES = {
    "evidence": "week10.src.graph_evidence_cli",
    "trace": "week10.src.trace_cli",
    "dependency": "week10.src.dependency_cli",
    "ask": "week10.src.graphrag_v2_cli",
}

_HELP = """V9 公开证据查询统一入口

用法:
  python -B -m week10.src.v9_cli <command> [该入口的原始参数...]

命令:
  evidence    渲染确定性的图证据；默认输出 Markdown，API 调用为 0
  trace       需求与结构追踪；支持 requirements/trace/impact/reverse/compare/graph
  dependency  查询显式记录的算术依赖；支持 graph/impact
  ask         默认只做离线检索；只有显式传入 --answer 才请求模型回答

使用 "<command> --help" 查看对应入口参数。模型回答只执行证据引用覆盖检查，
不构成语义正确性验证，也不证明物理因果关系或最小重跑范围。
"""


def _print_help() -> None:
    print(_HELP, end="")


def main(argv: list[str] | None = None) -> int:
    """Forward untouched command arguments and preserve the child exit code."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        _print_help()
        return 0
    command = args[0]
    module_name = _COMMAND_MODULES.get(command)
    if module_name is None:
        print(f"未知命令: {command}\n", file=sys.stderr, end="")
        _print_help()
        return 2
    child_main: Callable[[list[str]], int] = importlib.import_module(module_name).main
    return child_main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())

