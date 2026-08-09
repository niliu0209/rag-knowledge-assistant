"""QaService：检索参数（top-5）、引用构造、诚实回答规则、问答记录写入
（owner 合同见 architecture.md 模块表；不得绕过 ProviderService 直连 LLM）。

链路二（architecture.md）：问题非空校验 → Chroma 检索（user_id 过滤、top-5）
→ 无结果诚实回答（不调 LLM）→ prompt 构造（编号片段、不得编造）→
ProviderService 调 LLM（超时重试/429 退避在 provider 层）→ 引用构造 →
qa_records 写入（失败不阻塞回答，告警日志——评估证据链路需可见）。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from app.data import chroma_store, document_store
from app.data.db import get_connection
from app.services.provider import ProviderError, ProviderService

logger = logging.getLogger(__name__)

# 检索参数（owner：services/qa.py，architecture.md 链路二 top-5）
TOP_K = 5

# 检索相关阈值（S1-1 实测校准，2026-08-09）：
# Chroma 集合为默认 l2 空间；真实文档（bge-m3 中文）实测距离分布——
# 命中问题 top-1 距离 ≤0.925，无关问题 top-1 距离 ≥1.031，取 0.98 完全分隔
# （命中侧余量 0.055，无关侧余量 0.051）。distance > 阈值的片段视为无关，
# 不进入引用——检索层判定替代"依赖 LLM 判断片段无关"（阶段 0 remaining_risk）。
RELEVANCE_DISTANCE_THRESHOLD: float = 0.98

# 诚实回答：检索无结果时明确说明，不编造（F0-3 边界）
HONEST_NO_RESULT = "知识库中没有相关内容"


class QaError(Exception):
    """问答链路错误基类（api 层映射统一错误体）。"""


class EmptyQuestionError(QaError):
    """问题为空（api 映射 422 empty_question）。"""


class ProviderNotConfiguredError(QaError):
    """未配置提供商 Key（api 映射 503 provider_not_configured）。"""


class EmbeddingFailedError(QaError):
    """检索向量化失败（api 映射 502 embedding_failed）。"""


class LlmFailedError(QaError):
    """LLM 调用失败/超时重试后仍失败（api 映射 503 llm_failed）。"""


class QaService:
    def __init__(self, data_dir: Path, provider: ProviderService) -> None:
        self.data_dir = data_dir
        self.provider = provider

    def ask(self, question: str, user_id: str = "default") -> dict:
        """提问 → 带引用回答；成功返回 {answer, citations, provider}。"""
        if not question or not question.strip():
            raise EmptyQuestionError("问题不能为空")

        cfg = self.provider.get_full_config(user_id)
        if not cfg.get("api_key"):
            raise ProviderNotConfiguredError(
                "尚未配置提供商 Key，请到「提供商配置」页设置后重试"
            )

        start = time.perf_counter()
        try:
            query_vector = self.provider.embed(user_id, [question.strip()])[0]
        except ProviderError as exc:
            raise EmbeddingFailedError(
                f"向量化失败（提供商不可用或额度受限）：{exc}"
            ) from exc

        collection = chroma_store.get_collection(self.data_dir)
        hits = chroma_store.query_chunks(collection, query_vector, user_id, TOP_K)
        # S1-1：低相关片段按相似度阈值过滤（检索层判定无关，见常量注释）
        relevant_hits = [h for h in hits if h["distance"] <= RELEVANCE_DISTANCE_THRESHOLD]

        provider_label = f"{cfg['provider']} · {cfg['model']}"
        citations, snippets = self._build_references(user_id, relevant_hits)
        if citations:
            prompt = self._build_prompt(question.strip(), snippets)
            try:
                answer = self.provider.chat(user_id, prompt, max_tokens=1024)
            except ProviderError as exc:
                raise LlmFailedError(f"大模型生成失败：{exc}") from exc
        else:
            # 检索无结果 / 全部片段低于相关阈值 / 命中文档均已删除：诚实回答，不调 LLM
            answer = HONEST_NO_RESULT

        self._record(
            user_id=user_id,
            question=question.strip(),
            snippets=self._record_snapshot(user_id, hits, relevant_hits),
            answer=answer,
            provider=provider_label,
            started=start,
        )
        return {"answer": answer, "citations": citations, "provider": provider_label}

    # ---------- 引用构造（citations 合同：文档名 + 原文片段 + 页码） ----------

    def _build_references(
        self, user_id: str, hits: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """构造 citations 与记录用 snippets；page 缺失（docx 无页码）时省略键。

        hits 里文档已删除（同步删除保证一致性，此处防御）→ 跳过该引用。
        """
        names = self._document_names(user_id, [h["doc_id"] for h in hits])
        citations: list[dict] = []
        snippets: list[dict] = []
        for hit in hits:
            name = names.get(hit["doc_id"])
            if name is None:
                logger.warning("检索命中已删除文档切片（doc_id=%s），跳过引用", hit["doc_id"])
                continue
            citation: dict = {
                "document_id": hit["doc_id"],
                "document_name": name,
                "snippet": hit["snippet"],
                "chunk_index": hit["chunk_index"],
            }
            if hit["page"] is not None:
                citation["page"] = hit["page"]
            citations.append(citation)
            snippet = dict(citation)
            snippet["doc_id"] = hit["doc_id"]
            snippets.append(snippet)
        return citations, snippets

    def _document_names(self, user_id: str, doc_ids: list[str]) -> dict[str, str]:
        with get_connection(self.data_dir) as conn:
            return document_store.get_documents_by_ids(conn, user_id, doc_ids)

    def _record_snapshot(
        self, user_id: str, hits: list[dict], relevant_hits: list[dict]
    ) -> list[dict]:
        """记录快照：全部检索结果（含被过滤片段）+ distance + relevant 标记。

        供检索质量评估（F0-5）：阈值过滤是否误伤真实命中、无关片段分布可回溯。
        page 缺失（docx）时省略键（与引用构造一致）；hits 元素按值比较判相关。
        """
        names = self._document_names(user_id, [h["doc_id"] for h in hits])
        rows: list[dict] = []
        for h in hits:
            row: dict = {
                "doc_id": h["doc_id"],
                "document_name": names.get(h["doc_id"]),
                "chunk_index": h["chunk_index"],
                "snippet": h["snippet"],
                "distance": h["distance"],
                "relevant": h in relevant_hits,
            }
            if h["page"] is not None:
                row["page"] = h["page"]
            rows.append(row)
        return rows

    # ---------- prompt 构造（编号片段 + 诚实回答指令） ----------

    @staticmethod
    def _build_prompt(question: str, snippets: list[dict]) -> str:
        parts = ["你是基于知识库回答问题的助手。以下是检索到的相关内容片段：", ""]
        for i, s in enumerate(snippets, start=1):
            source = f"来源：{s['document_name']}"
            if s.get("page") is not None:
                source += f"，第 {s['page']} 页"
            parts.append(f"[{i}]（{source}）\n{s['snippet']}")
            parts.append("")
        parts += [
            f"请只基于以上片段回答问题：{question}",
            "要求：",
            "1. 片段中没有答案时，直接回答“知识库中没有相关内容”。",
            "2. 不得编造片段之外的信息；引用片段中的事实，不确定就明确说明。",
            "3. 区分价格与规格：片段中的数量、张数、规格（如“500 张/包”）不是价格；"
            "回答价格须引用明确的金额数字，不确定就如实说明。",
        ]
        return "\n".join(parts)

    # ---------- qa_records 写入（F0-5；失败不阻塞回答） ----------

    def _record(
        self,
        *,
        user_id: str,
        question: str,
        snippets: list[dict],
        answer: str,
        provider: str,
        started: float,
    ) -> None:
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        try:
            with get_connection(self.data_dir) as conn:
                document_store.insert_qa_record(
                    conn,
                    user_id=user_id,
                    question=question,
                    retrieved_chunks=json.dumps(snippets, ensure_ascii=False),
                    answer=answer,
                    provider=provider,
                    latency_ms=latency_ms,
                )
        except Exception:  # noqa: BLE001——记录失败不阻塞回答（F0-5 验收），告警需可见
            logger.exception(
                "问答记录写入失败（question=%r）——回答不受影响，请检查磁盘空间", question
            )
