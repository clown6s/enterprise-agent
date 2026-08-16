# -*- coding: utf-8 -*-
"""
缺口发现（闭环 C）："暂无法回答"的每一次发生都是知识库缺口信号。

- record_gap()  兜底句式触发即记录（JSONL + 进程内聚合）
- report()      单遍聚类：问法按共享实词聚类（Jaccard ≥ 阈值），
                输出缺口主题 + 频次 + 样例 + 补库建议
- 建议形态："知识库缺少关于「XX」的资料，建议补充相关文档"——
                这就是知识库健康度报告，回答"下一步该往库里放什么"。
"""
import json
import os
import re
import threading
from datetime import datetime

from .. import config

_FILE = os.path.join(config.LOG_DIR, "gaps.jsonl")
_lock = threading.Lock()
_gaps: list[dict] = []

# 停用词（问法里不携带语义的虚词）
_STOPWORDS = set("请问 是什么 什么 怎么 如何 哪些 那个 这个 帮我 一下 有没有 多少 几天 吗 呢 的 了 在 是 有 请".split())
_TOKEN = re.compile(r"[一-龥]{2,}|[A-Za-z]{2,}")


def record_gap(query: str) -> None:
    """兜底句式触发即记录"""
    entry = {"ts": datetime.now().isoformat(), "query": query}
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        with open(_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
    with _lock:
        _gaps.append(entry)


def _tokens(query: str) -> set[str]:
    # 中文无空格，先按疑问词/虚词切分，再提 2-6 字实词
    q = re.sub(r"(是多少|有哪些|是什么|是什么|多少|如何|怎么|什么|吗|呢|请问|帮我|一下|的|了|是|有)",
               " ", query)
    return {t for t in _TOKEN.findall(q) if t not in _STOPWORDS and len(t) <= 10}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def report() -> dict:
    """缺口报告：单遍聚类（同问法聚合）+ 补库建议。
    进程内聚合 + JSONL 历史合并（服务重启后历史缺口不丢）。"""
    with _lock:
        gaps = list(_gaps)
    try:
        with open(_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    gaps.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    clusters: list[dict] = []
    for g in gaps:
        toks = _tokens(g["query"])
        placed = False
        for c in clusters:
            if _jaccard(toks, c["tokens"]) >= 0.4:
                c["count"] += 1
                c["samples"].append(g["query"])
                c["tokens"] |= toks
                placed = True
                break
        if not placed:
            clusters.append({"tokens": set(toks), "count": 1, "samples": [g["query"]]})
    items = []
    for c in sorted(clusters, key=lambda x: -x["count"]):
        topic = "、".join(sorted(c["tokens"])[:5]) or c["samples"][0]
        items.append({
            "topic": topic,
            "count": c["count"],
            "samples": c["samples"][:3],
            "suggestion": f"知识库缺少关于「{topic}」的资料，建议补充相关文档",
        })
    return {"total_gaps": len(gaps), "clusters": items}
