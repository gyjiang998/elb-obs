# Deterministic Pattern Matching Layer

这是从研究期 `heterogeneous_kb` 中筛出的在线最小层，供 `tools/analysis/matching_engine.py` 使用。它包括算法签名、硬件映射、能力、成本、fallback、真实实现和热点规则，不包括约 74MB 的逐版本扫描、候选 JSONL 与人工审计中间产物。

正式页面展示与模型 RAG 使用 `../optimization/` 和 `../hardware/`；本目录只提供 AST 特征的确定性补充匹配，不能把候选规则提升为真实硬件或性能证据。

