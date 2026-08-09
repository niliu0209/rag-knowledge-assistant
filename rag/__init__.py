"""rag：LlamaIndex 数据层适配（选型定案组合，architecture.md owner map）。

S2 只承载解析与切片原语；检索/评估在 S4 扩展。embedding 不在此层——
向量化统一走 ProviderService（OpenAI 兼容抽象，S1 已定）。
"""
