# -*- coding: utf-8 -*-
"""
检索管线：query → embedding → Milvus 召回 → rerank 精排 → 上下文拼装。

Java 版对应：RetrievalPipeline。关键移植点：
- rerank 分数过滤（低于阈值的候选视为不相关，宁可无来源也不让噪音进上下文）
- 图谱上下文本阶段不接 Neo4j（路线图），先保证文档检索主链路
"""
from .. import config, embeddings, milvus_store


def retrieve(query: str) -> list[dict]:
    """返回 [{text, document_id, title, source, score}]，按相关度降序（最多 RERANK_TOP_N 条）"""
    vec = embeddings.embed(query)
    hits = milvus_store.search(vec)
    if not hits:
        return []
    # rerank 精排（软依赖：rerank 挂了回退原始顺序+分数过滤）
    reranked = embeddings.rerank(query, [h["text"] for h in hits])
    if reranked:
        kept = []
        for idx, score in sorted(reranked, key=lambda x: -x[1]):
            if score < config.RERANK_MIN_SCORE:
                continue
            h = hits[idx]
            kept.append({**h, "score": score})
        return kept
    # 无 rerank：用 Milvus 距离粗筛（COSINE 距离与相关性同向）
    return [h for h in hits if h["score"] >= config.RERANK_MIN_SCORE][: config.RERANK_TOP_N]


def build_context(hits: list[dict], max_chars: int = 6000) -> str:
    """上下文拼装（Java 版 buildContext 的文档部分；图谱-only 注入留待 Neo4j 接入）"""
    if not hits:
        return "暂无相关资料。"
    parts = []
    total = 0
    for h in hits:
        entry = f"[来源: {h['source'] or '未知'}]\n{h['text']}\n\n"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)
    return "".join(parts)
