# -*- coding: utf-8 -*-
"""
向量与重排（硅基流动 SiliconFlow，OpenAI 兼容接口）：
- embed()   文本 → Qwen3-Embedding-8B 4096 维向量（检索时用）
- rerank()  问题 + 候选文档 → 相关性分数（召回后精排）

直接 HTTP 调用，不需要独立 sidecar 进程——少一层部署。
"""
import time

import httpx

from . import config


def embed(text: str, retries: int = 2) -> list[float]:
    """文本 → 向量；瞬时网络抖动自动重试（检索路径曾无重试——SiliconFlow
    一次 ReadTimeout 整轮检索空手走兜底，有资料却答"暂无法回答"的实况）。"""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(
                f"{config.SILICONFLOW_BASE}/embeddings",
                headers={"Authorization": f"Bearer {config.SILICONFLOW_KEY}"},
                json={"model": config.EMBED_MODEL, "input": [text],
                      "dimensions": config.EMBED_DIM},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            return data[0]["embedding"]
        except Exception as e:
            last = e
            if attempt < retries:
                time.sleep(0.5 * (attempt + 1))
    raise last


def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """批量嵌入：一次请求多条（上传管线用——曾逐条串行，200 chunk 的文档
    要 200 次 HTTP 调用）。返回与输入等长的向量列表，顺序对应。"""
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = httpx.post(
            f"{config.SILICONFLOW_BASE}/embeddings",
            headers={"Authorization": f"Bearer {config.SILICONFLOW_KEY}"},
            json={"model": config.EMBED_MODEL, "input": batch,
                  "dimensions": config.EMBED_DIM},
            timeout=120,
        )
        resp.raise_for_status()
        out.extend(d["embedding"] for d in resp.json()["data"])
    return out


def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[tuple[int, float]]:
    """
    query 与 documents 的重排分数。
    返回 [(原始下标, 相关性分数)]，按分数降序；失败返回空列表（调用方回退原始顺序）。
    top_n=None 时用配置的 RERANK_TOP_N（参数调优时会传全量 top_k 做网格分析）。
    """
    try:
        resp = httpx.post(
            f"{config.SILICONFLOW_BASE}/rerank",
            headers={"Authorization": f"Bearer {config.SILICONFLOW_KEY}"},
            json={
                "model": config.RERANK_MODEL,
                "query": query,
                "documents": documents,
                "top_n": min(top_n or config.RERANK_TOP_N, len(documents)),
            },
            timeout=60,
        )
        resp.raise_for_status()
        return [(r["index"], r["relevance_score"]) for r in resp.json()["results"]]
    except Exception:
        # 软依赖：HTTP 错/JSON 解析错/结构不符都回退原始顺序
        # （曾只捕 HTTPError+KeyError，JSON 解析失败会炸穿到 500）
        return []
