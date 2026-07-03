# -*- coding: utf-8 -*-
# src_commented/__init__.py
# V3 中文注释版源码包。
#
# 这个目录包含 week4/src/ 下所有 .py 文件的逐行中文注释版本。
# 代码逻辑与 week4/src/ 完全一致，只是增加了详细的设计说明。
#
# 阅读顺序建议（由浅入深）：
#   1. schemas.py         — 先理解数据结构
#   2. llm_client.py      — 再看怎么调 LLM
#   3. utils.py           — 共享工具函数
#   4. main.py            — 程序入口，看整体流程
#   5. pipeline.py        — 核心编排，理解状态图
#   6. node1_requirement.py — 第一个节点
#   7. node2_sysml.py     — 第二个节点（V3 不变）
#   8. node3_modelica.py  — 最复杂的节点（子图 + 修复 + MAT→CSV）
#   9. node_quality.py    — V3 新增的质量检查节点
#   10. node4_summary.py  — 总结生成
