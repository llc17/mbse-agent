"""V7 — benchmark 框架 + LLM-as-Judge。

目录结构:
    experiments/
      cases.py          统一用例集（V4/V6 复用同一段 raw_input，保证 A/B 公平）
      syntax_check.py   统一 SysML 语法检查器（口径一致，不依赖 V4/V6 各自实现）
      runner.py         单版本 thin runner（subprocess 隔离 import 冲突）
      benchmark.py      单模式运行器（subprocess 调 runner，收集指标）
      judge.py          LLM-as-Judge（Kimi 盲评打分）
      run_ab.py         主编排（A/B 对比 + 汇总表）
      retrieval_eval.py 复合词检索精度提升（monkey-patch 独立验证，不改 week7）
      collect_root_causes.py 根因样本收集（可选，构造失败用例触发 root_cause）
"""
