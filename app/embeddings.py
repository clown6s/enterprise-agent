# -*- coding: utf-8 -*-
"""
向量与重排（硅基流动 SiliconFlow，OpenAI 兼容接口）：
- embed()   文本 → BGE-M3 1024 维向量（检索时用）
- rerank()  问题 + 候选文档 → 相关性分数（召回后精排）

Java 版对应：ai-sidecar 的 /rerank + Java 侧 embedding 调用。
Python 里直接 HTTP 调用，不需要独立 sidecar 进程——少一层部署。
"""
import httpx

from . import config


def embed(text: str) -> list[float]:
    resp = httpx.post(
        f"{config.SILICONFLOW_BASE}/embeddings",
        headers={"Authorization": f"Bearer {config.SILICONFLOW_KEY}"},
        json={"model": config.EMBED_MODEL, "input": [text]},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return data[0]["embedding"]


def rerank(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """
    query 与 documents 的重排分数。
    返回 [(原始下标, 相关性分数)]，按分数降序；失败返回空列表（调用方回退原始顺序）。
    """
    try:
        resp = httpx.post(
            f"{config.SILICONFLOW_BASE}/rerank",
            headers={"Authorization": f"Bearer {config.SILICONFLOW_KEY}"},
            json={
                "model": config.RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": min(config.RERANK_TOP_N, len(documents)),
            },
            timeout=60,
        )
        resp.raise_for_status()
        return [(r["index"], r["relevance_score"]) for r in resp.json()["results"]]
    except (httpx.HTTPError, KeyError):
        return []
