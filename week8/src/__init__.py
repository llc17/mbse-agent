"""V7 可复用库 — benchmark 框架的核心模块。

V8/V9/V10 可 import 这些模块复用评测能力:
  - cases:         统一用例集
  - syntax_check:  统一 SysML 语法检查器
  - runner:        单版本 thin runner（subprocess 隔离）
  - benchmark:     单模式运行器
  - judge:         LLM-as-Judge（Kimi 盲评）

入口脚本在 ../experiments/ 下。
"""
