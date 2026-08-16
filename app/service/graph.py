# -*- coding: utf-8 -*-
"""
Neo4j 知识图谱访问（图谱-only 兜底：文档检索空手时，用图谱关系作答）。

Java 版对应：GraphRetriever + GraphStore。移植要点全部保留：
- 实体候选从问句切窗口提取，直接 MATCH n.name IN 候选（35k 节点，DB 里匹配才可靠）
- 关系链按（源实体 → 关系链）分组自然语言化——三元组格式会被模型模仿粘贴（血泪教训）
- 上限 20 条防灌爆上下文
"""
import re

from dotenv import dotenv_values
from neo4j import GraphDatabase

from .. import config

_driver = None


def _db():
    global _driver
    if _driver is None:
        # .env 在项目根目录（config.py 在 app/ 下，往上两级）
        env_path = str(__import__("pathlib").Path(__file__).resolve().parent.parent.parent / ".env")
        pwd = dotenv_values(env_path).get("NEO4J_PASSWORD", "")
        _driver = GraphDatabase.driver(
            f"bolt://{config._env('NEO4J_HOST', '10.211.55.5')}:7687",
            auth=("neo4j", pwd))
    return _driver


_STOP = set("请问 什么 怎么 如何 为什么 是否 这个 那个 一下 帮我 哪些 多少 是什么 有哪些 的 了 是 在 有 吗 呢".split())
_TOKEN = re.compile(r"[一-龥]{2,}|[A-Za-z]{2,}")


def _candidates(query: str) -> list[str]:
    """实体候选：切掉虚词/疑问词后的 2-8 字片段 + 英文词"""
    q = re.sub(r"(是多少|有哪些|是什么|多少|如何|怎么|什么|吗|呢|请问|帮我|一下|的|了|是|有|在)",
               " ", query)
    out = []
    for m in _TOKEN.finditer(q):
        t = m.group()
        if t not in _STOP and len(t) <= 8:
            out.append(t)
    return out[:8]


def expand(query: str, limit: int = 20) -> str:
    """图谱遍历 → 自然语言上下文；无可关联实体/图不可用返回空串（软依赖）"""
    candidates = _candidates(query)
    if not candidates:
        return ""
    try:
        with _db().session() as s:
            rows = s.run(
                # 无向遍历：实体常是叶子节点（如"奔马律"只有入边）
                "MATCH (a:Entity) WHERE a.name IN $cands "
                "MATCH p=(a)-[r:RELATES*1..2]-(b:Entity) "
                "RETURN a.name AS src, b.name AS tgt, "
                "[x IN relationships(p) | x.type] AS chain LIMIT 200",
                cands=candidates,
            ).data()
    except Exception:
        return ""
    if not rows:
        return ""
    # 按（源，关系链）分组（Java GraphStore.format 的自然语言版）
    grouped: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        chain = " → ".join(r["chain"])
        grouped.setdefault((r["src"], chain), set()).add(r["tgt"])
    lines = []
    for (src, chain), tgts in grouped.items():
        lines.append(f"- {src}（{chain}）：{'、'.join(sorted(tgts)[:8])}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)
