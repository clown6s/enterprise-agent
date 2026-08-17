# -*- coding: utf-8 -*-
"""
反馈环（闭环 A）：👍采纳 → 知识库回流；👎/纠正 → 回归用例沉淀。

- adopt()            采纳问答 → feedback_adopt 表 + Milvus 入库
                      （embed → insert，立即可被检索）
- record_unhelpful() 负反馈 → 自动生成回归用例（该问法不得再出兜底句式），
                      带纠正文本的进待审队列（eval/pending_cases.jsonl，人工定断言）
- enqueue_review()   低把握答案（兜底句式/图谱-only）自动进 answer_review 审核队列
                      （此前该表只有列表/批准/驳回接口、无入队逻辑，队列空转）
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime

import pymysql

from .. import config, embeddings, milvus_store

# 本地计算 eval 目录（勿从 eval_engine 导入——agent→feedback→eval_engine→agent 循环导入）
_EVAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "eval")

_PENDING_FILE = os.path.join(_EVAL_DIR, "pending_cases.jsonl")
_REGRESSION_FILE = os.path.join(_EVAL_DIR, "regression_cases.json")


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
        password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
        charset="utf8mb4", connect_timeout=5,
    )


def _hash_query(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:64]


def enqueue_review(query: str, answer: str, sources: str = "") -> bool:
    """
    审核入队：低把握答案（兜底句式触发 / 图谱-only 兜底）自动进人工审核队列，
    管理后台"答案审核"页审批。软依赖——入队失败只记日志不影响回答。
    """
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO answer_review (query, answer, sources, employee_id, user, status) "
                "VALUES (%s, %s, %s, 0, 'system', 'PENDING')",
                (query[:500], answer[:2000], sources[:1000]))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[review] 审核入队失败: {e!r}")
        return False


# ---------------- 1. 采纳 → 知识库回流 ----------------

def adopt(query: str, answer: str, source: str = "", user: str = "admin") -> dict:
    """采纳问答：MySQL 留痕 + Milvus 入库（立即可被检索到）"""
    qh = _hash_query(query)
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback_adopt (query_hash, query, answer, source, adopted_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (qh, query, answer, source or "", user, datetime.now()),
            )
        conn.commit()
        conn.close()
    except pymysql.MySQLError:
        pass   # 软依赖：MySQL 挂了只影响留痕，不影响入库

    # Milvus 入库：Q&A 拼成一个 chunk（标题带"问答采纳"便于溯源）。
    # 锚定句是展示层，剥掉再入库——否则新答案引用它再被锚定 → 双重锚定（实况）
    from .. import guards
    text = f"问题：{query}\n回答：{guards.strip_leading_anchor(answer)}"
    vec = embeddings.embed(text)
    milvus_store.insert_document(
        text=text,
        vector=vec,
        document_id=f"adopt-{qh[:16]}",
        title=f"问答采纳-{qh[:8]}",
        source="问答采纳" + (f"·{source}" if source else ""),
    )
    return {"adoptId": qh, "query": query}


# ---------------- 2. 负反馈 → 用例沉淀 ----------------

def _case_exists(query: str) -> bool:
    try:
        with open(_REGRESSION_FILE, encoding="utf-8") as f:
            cases = json.load(f)
        return any(c.get("query") == query for c in cases)
    except (OSError, ValueError):
        return False


def record_unhelpful(query: str, answer: str, correction: str = "", user: str = "admin") -> dict:
    """
    👎 反馈处理：
    - 无纠正文本 → 自动锁一条回归用例：该问法不得再回兜底句式（当前答案确实是兜底才有效力）
    - 有纠正文本 → 进待审队列（人工把纠正提炼成断言后转正式用例）
    """
    if correction and correction.strip():
        with open(_PENDING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(),
                "query": query,
                "answer": answer,
                "correction": correction.strip(),
                "user": user,
            }, ensure_ascii=False) + "\n")
        return {"status": "pending_review", "query": query}

    if _case_exists(query):
        return {"status": "case_exists", "query": query}

    case = {
        "id": f"auto-unhelpful-{_hash_query(query)[:10]}",
        "query": query,
        "expect": {
            "answer_not_contain": ["暂无法回答"],
            "max_seconds": 60,
        },
    }
    try:
        with open(_REGRESSION_FILE, encoding="utf-8") as f:
            cases = json.load(f)
    except (OSError, ValueError):
        cases = []
    cases.append(case)
    with open(_REGRESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=2)
    return {"status": "case_created", "case_id": case["id"]}
