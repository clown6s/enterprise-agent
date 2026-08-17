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
from datetime import datetime, timedelta

from .. import config

_FILE = os.path.join(config.LOG_DIR, "gaps.jsonl")

# 报告缓存：文件未变化且同窗口时直接复用（聚类 O(N·M)，首页轮询
# 不能每次都重算全量——曾每次都全量读文件+重聚类）
_report_cache: dict = {"key": None, "val": None}

# 停用词（问法里不携带语义的虚词）
_STOPWORDS = set("请问 是什么 什么 怎么 如何 哪些 那个 这个 帮我 一下 有没有 多少 几天 吗 呢 的 了 在 是 有 请".split())
_TOKEN = re.compile(r"[一-龥]{2,}|[A-Za-z]{2,}")


def record_gap(query: str) -> None:
    """兜底句式触发即记录（JSONL 为唯一数据源——曾内存+文件双源合并，
    每条缺口在报告里重复计数，重启后数字跳变）"""
    entry = {"ts": datetime.now().isoformat(), "query": query}
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        with open(_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _tokens(query: str) -> set[str]:
    # 中文无空格，先按疑问词/虚词切分，再提 2-6 字实词
    q = re.sub(r"(是多少|有哪些|是什么|是什么|多少|如何|怎么|什么|吗|呢|请问|帮我|一下|的|了|是|有)",
               " ", query)
    return {t for t in _TOKEN.findall(q) if t not in _STOPWORDS and len(t) <= 10}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _load_gaps(days: int) -> list[dict]:
    """读近 N 天缺口（JSONL 全量历史；按 ts 过滤窗口——
    曾 total_gaps 是全时段计数，与看板 N 天查询口径不一致）"""
    cutoff = datetime.now() - timedelta(days=days)
    gaps: list[dict] = []
    try:
        with open(_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    ts = datetime.fromisoformat(e.get("ts", ""))
                except (ValueError, TypeError):
                    continue
                if ts >= cutoff:
                    gaps.append(e)
    except OSError:
        pass
    return gaps


def report(days: int = 30) -> dict:
    """缺口报告：单遍聚类（同问法聚合）+ 补库建议。
    文件 mtime/size 未变时复用缓存（首页轮询零重算）。"""
    global _report_cache
    try:
        st = os.stat(_FILE)
        key = (st.st_mtime, st.st_size, days)
    except OSError:
        key = None
    if key and _report_cache.get("key") == key:
        return _report_cache["val"]

    gaps = _load_gaps(days)
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
    result = {"total_gaps": len(gaps), "clusters": items}
    if key:
        _report_cache = {"key": key, "val": result}
    return result


def daily_counts(days: int) -> dict[str, int]:
    """近 N 天缺口按天计数（看板 noAnswer 列——兜底句式的每日实况）"""
    out: dict[str, int] = {}
    for g in _load_gaps(days):
        day = str(g.get("ts", ""))[:10]
        if day:
            out[day] = out.get(day, 0) + 1
    return out
