"""知识图谱（P1-M5）。

三层抽取（规则候选 / LLM 抽取 / 别名对齐）+ 三道闸门（证据命中 / 置信度 /
关系枚举），存储落在 PostgreSQL 的 `entities` / `relations` / `entity_mentions`
三张表里，不用 Neo4j —— 理由见 `app/models.py` 顶部注释。

这里**不做 re-export**：`builder` 依赖本包内的 `candidates` / `aligner` /
`extractor`，在 `__init__` 里 import 它们会绕成循环。调用方直接写
`from app.services.graph.builder import build_graph`。
"""
