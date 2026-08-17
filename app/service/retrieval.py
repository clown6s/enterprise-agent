# -*- coding: utf-8 -*-
"""
检索管线：query → embedding → Milvus 召回 → rerank 精排 → 上下文拼装。

关键点：
- rerank 分数过滤（低于阈值的候选视为不相关，宁可无来源也不让噪音进上下文）
- 参数自适应：data/tuned_params.json 存在时覆盖默认 top_k/阈值
  （由 app/learn/tune_retrieval.py 网格搜索产生，只接受不劣化变更）
"""
import json
import os

from .. import config, embeddings, milvus_store

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TUNED_FILE = os.path.join(_BASE, "data", "tuned_params.json")

_tuned: dict = {}


def _load_tuned() -> dict:
    global _tuned
    if not _tuned:
        try:
            with open(_TUNED_FILE, encoding="utf-8") as f:
                _tuned = json.load(f)
        except (OSError, ValueError):
            _tuned = {}
    return _tuned


def current_params() -> dict:
    """当前生效的检索参数（调优结果优先，否则配置默认值）"""
    t = _load_tuned()
    return {
        "top_k": t.get("top_k", config.TOP_K),
        "min_score": t.get("min_score", config.RERANK_MIN_SCORE),
    }


def retrieve(query: str, *, top_k: int | None = None,
             min_score: float | None = None,
             rerank_top_n: int | None = None,
             user: str = "") -> list[dict]:
    """返回 [{text, document_id, title, source, score}]，按相关度降序。
    user：飞书 open_id/后台用户名——经 doc_acl 转成 Milvus expr 过滤（None=不过滤）。"""
    params = current_params()
    top_k = top_k if top_k is not None else params["top_k"]
    min_score = min_score if min_score is not None else params["min_score"]

    from . import acl
    expr = acl.allowed_expr(user)
    vec = embeddings.embed(query)
    hits = milvus_store.search(vec, query, top_k=top_k, expr=expr)
    if not hits:
        # 混合检索失败（如 BM25 字段异常）→ 纯稠密回退，主链路不崩
        hits = milvus_store.dense_search(vec, top_k, expr=expr)
    if not hits:
        return []
    # rerank 精排（软依赖：rerank 挂了回退原始顺序+分数过滤）
    reranked = embeddings.rerank(query, [h["text"] for h in hits],
                                 top_n=rerank_top_n or config.RERANK_TOP_N)
    if reranked:
        kept = []
        for idx, score in sorted(reranked, key=lambda x: -x[1]):
            if score < min_score:
                continue
            h = hits[idx]
            kept.append({**h, "score": score})
        return kept
    # 无 rerank：不按 min_score 粗筛（RRF 融合分是 0~0.03 量级，与阈值不可比），
    # 直接取前 RERANK_TOP_N 条
    return hits[: config.RERANK_TOP_N]


def build_context(hits: list[dict], max_chars: int = 6000) -> str:
    """上下文拼装（文档部分；图谱-only 上下文由 graph 模块提供）。
    带页码的来源注记（"第N页"）——溯源粒度到页。"""
    if not hits:
        return "暂无相关资料。"
    parts = []
    total = 0
    for h in hits:
        loc = f" 第{h['page']}页" if h.get("page") else ""
        entry = f"[来源: {h['source'] or '未知'}{loc}]\n{h['text']}\n\n"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)
    return "".join(parts)
