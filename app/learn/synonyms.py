# -*- coding: utf-8 -*-
"""
检索同义词自学习（词汇对齐的自我补全，闭环）：
- expand_queries(query)  静态词表（agent._SYNONYM_GROUPS）+ 已学词对 → 扩展短语
- learn(trigger, expand) 拒绝路径恢复成功后沉淀词对（触发词→扩展短语）
                         落盘 data/synonyms.json——曾每发现不对齐人工加一行，
                         现在系统第一次遇到就自愈并记住
"""
import json
import os
import threading

from .. import config

_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "synonyms.json")

_lock = threading.Lock()
_learned: list[dict] = []


def _load() -> list[dict]:
    global _learned
    if _learned:
        return _learned
    try:
        with open(_FILE, encoding="utf-8") as f:
            _learned = json.load(f).get("pairs", [])
    except (OSError, ValueError):
        _learned = []
    return _learned


def expand_queries(query: str) -> list[str]:
    """已学词对的扩展短语：查询命中触发词即返回其扩展（先静态表后学到的）"""
    out = []
    for p in _load():
        trigger, expand = p.get("trigger", ""), p.get("expand", "")
        if trigger and expand and trigger in query:
            out.append(expand)
    return out[:2]


def _write(pairs: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        with open(_FILE, "w", encoding="utf-8") as f:
            json.dump({"pairs": pairs}, f, ensure_ascii=False, indent=2)
        _learned[:] = pairs
    except OSError:
        pass


def learn(trigger: str, expand: str) -> None:
    """沉淀词对（触发词→扩展短语）；去重、上限 200 条防膨胀"""
    trigger, expand = (trigger or "").strip(), (expand or "").strip()
    if not trigger or not expand or len(trigger) > 12 or len(expand) > 60:
        return
    with _lock:
        pairs = _load()
        if any(p.get("trigger") == trigger and p.get("expand") == expand
               for p in pairs):
            return
        pairs.append({"trigger": trigger, "expand": expand})
        _write(pairs[-200:])


def penalize(trigger: str, expand: str) -> None:
    """负反馈惩罚：恢复答案被点👎时，其使用的词对失败计数 +1，
    满 2 次移除（曾词对只增不减——坏词对永久固化，恢复答案被👎
    无从纠正；移错了恢复链路会重新学回来，自愈）"""
    trigger, expand = (trigger or "").strip(), (expand or "").strip()
    if not trigger or not expand:
        return
    with _lock:
        pairs = _load()
        kept, changed = [], False
        for p in pairs:
            if p.get("trigger") == trigger and p.get("expand") == expand:
                p = dict(p)
                p["fail"] = p.get("fail", 0) + 1
                changed = True
                if p["fail"] < 2:
                    kept.append(p)
            else:
                kept.append(p)
        if changed:
            _write(kept)
