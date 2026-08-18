# -*- coding: utf-8 -*-
"""
检索管线：query → embedding → Milvus 召回 → rerank 精排 → 上下文拼装。

关键点：
- rerank 分数过滤（低于阈值的候选视为不相关，宁可无来源也不让噪音进上下文）
- 名词查找类豁免（无问句意图词的查询：裸名词"高血压/宫崎骏"——rerank
  列表内归一打分，候选越杂分数越塌，裸名词全灭的实况；与相关性闸门
  豁免同款设计：名词查找按混合检索序直接取，不过滤）
- 参数自适应：data/tuned_params.json 存在时覆盖默认 top_k/阈值
  （由 app/learn/tune_retrieval.py 网格搜索产生，只接受不劣化变更）
"""
import json
import os
import re

from .. import config, embeddings, milvus_store

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TUNED_FILE = os.path.join(_BASE, "data", "tuned_params.json")

_tuned: dict = {}

# 问句意图词：含任一 → 有明确诉求，rerank 阈值过滤生效；不含 → 名词查找类豁免
_QUESTION_INTENT = re.compile(
    r"什么|怎么|如何|为什么|为何|哪些|几个|多少|哪年|哪一|吗|呢|注意|建议|推荐"
    r"|区别|流程|方法|方式|能否|可以|会不会|能不能|是不是|原因|时候|时间|多久")


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
             extra_queries: list[str] | None = None,
             user: str = "") -> list[dict]:
    """返回 [{text, document_id, title, source, score}]，按相关度降序。
    user：飞书 open_id/后台用户名——经 doc_acl 转成 Milvus expr 过滤（None=不过滤）。
    extra_queries：扩展检索短语（同义词桥接）——各自独立检索后与主查询
    结果并集再重排（曾拼进主查询——高频扩展词稀释 BM25 的实况）。"""
    params = current_params()
    top_k = top_k if top_k is not None else params["top_k"]
    min_score = min_score if min_score is not None else params["min_score"]

    from . import acl
    expr = acl.allowed_expr(user)
    try:
        vec = embeddings.embed(query)
    except Exception as e:
        # 软依赖：嵌入服务抖动时检索整体降级为空（问答走兜底），不炸 500
        print(f"[retrieval] 嵌入失败: {e!r}")
        return []
    hits = milvus_store.search(vec, query, top_k=top_k, expr=expr)
    if not hits:
        # 混合检索失败（如 BM25 字段异常）→ 纯稠密回退，主链路不崩
        hits = milvus_store.dense_search(vec, top_k, expr=expr)
    if not hits:
        return []
    # 扩展短语并联检索：并集去重（按 text），主查询结果保留原序优先
    for eq in (extra_queries or []):
        if not eq.strip():
            continue
        try:
            eh = milvus_store.search(embeddings.embed(eq), eq, top_k=top_k, expr=expr)
        except Exception:
            continue
        seen = {h["text"] for h in hits}
        for h in eh:
            if h["text"] not in seen:
                seen.add(h["text"])
                h["_expanded"] = True   # 扩展命中标记（维度闸门只认主查询命中）
                hits.append(h)
        if len(hits) >= 20:
            break   # rerank 候选上限，防扩展灌爆
    hits = hits[:20]
    # rerank 精排（软依赖：rerank 挂了回退原始顺序+分数过滤）。
    # 重排词 = 主查询 + 扩展短语（词汇对齐层：曾用干净主查询重排并集候选，
    # 饮食表格 chunk 全灭——"饮食"与"食物/膳食"不对齐在重排层同样存在）
    rerank_q = query
    if extra_queries:
        rerank_q = f"{query} {' '.join(e for e in extra_queries if e.strip())}"
    reranked = embeddings.rerank(rerank_q, [h["text"] for h in hits],
                                 top_n=rerank_top_n or config.RERANK_TOP_N)
    if reranked:
        kept = []
        for idx, score in sorted(reranked, key=lambda x: -x[1]):
            if score < min_score:
                continue
            h = hits[idx]
            kept.append({**h, "score": score})
        if kept:
            return kept
        # 名词查找豁免：重排全灭但问句无意图词（裸名词查询——rerank 列表内
        # 归一打分在候选杂时分数塌陷，裸名词全灭的实况）→ 按混合检索序取
        if not _QUESTION_INTENT.search(query):
            return hits[: config.RERANK_TOP_N]
        return []
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
