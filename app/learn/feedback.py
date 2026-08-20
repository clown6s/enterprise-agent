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
from ..time_context import now as business_now
import logging
log = logging.getLogger(__name__)

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


def list_pending() -> list[dict]:
    """待审队列列表（管理后台）"""
    if not os.path.exists(_PENDING_FILE):
        return []
    out = []
    with open(_PENDING_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def clear_pending() -> int:
    """清空待审队列，返回清除条数"""
    n = len(list_pending())
    try:
        os.remove(_PENDING_FILE)
    except OSError:
        pass
    return n


def promote_pending() -> dict:
    """
    待审用例一键转正（👎 带纠正的队列）→ 正式回归用例：
    纠正文本作为 answer_contain 断言，无纠正的锁"不得再兜底"。
    同问法已有用例跳过；转正后清空待审文件。返回 {promoted, remaining}。
    """
    if not os.path.exists(_PENDING_FILE):
        return {"promoted": 0, "remaining": 0}
    entries = []
    with open(_PENDING_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    with open(_REGRESSION_FILE, encoding="utf-8") as f:
        cases = json.load(f)
    promoted = 0
    for e in entries:
        q = (e.get("query") or "").strip()
        if not q or any(c.get("query") == q for c in cases):
            continue
        correction = (e.get("correction") or "").strip()
        expect = {"max_seconds": 60}
        if correction:
            expect["answer_contain"] = [correction[:60]]
        else:
            expect["answer_not_contain"] = ["暂无法回答"]
        cases.append({"id": f"pending-{_hash_query(q)[:10]}", "query": q, "expect": expect})
        promoted += 1
    if promoted:
        with open(_REGRESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(cases, f, ensure_ascii=False, indent=2)
        os.remove(_PENDING_FILE)
    return {"promoted": promoted, "remaining": len(entries) - promoted}


def enqueue_review(query: str, answer: str, sources: str = "") -> bool:
    """
    审核入队：低把握答案（兜底句式触发 / 图谱-only 兜底）自动进人工审核队列，
    管理后台"答案审核"页审批。软依赖——入队失败只记日志不影响回答。
    同问法已有 PENDING 记录则跳过（曾重复入队——同一问法每次被拒插一条，
    队列被同一问题刷爆）。"""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM answer_review WHERE query=%s AND status='PENDING' LIMIT 1",
                (query[:500],))
            if cur.fetchone():
                conn.close()
                return False
            cur.execute(
                "INSERT INTO answer_review (query, answer, sources, employee_id, user, status) "
                "VALUES (%s, %s, %s, 0, 'system', 'PENDING')",
                (query[:500], answer[:2000], sources[:1000]))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        log.warning(f"审核入队失败: {e!r}")
        return False


# ---------------- 1. 采纳 → 知识库回流 ----------------

def adopt(query: str, answer: str, source: str = "", user: str = "admin") -> dict:
    """采纳问答：MySQL 留痕 + Milvus 入库（立即可被检索到）。
    质量门（E 确定性启发式）：客观错误确定性拦截（兜底/过短/注入/
    格式残骸清洗）——主观质量不做硬评分（用户点👍是表态，系统不替
    用户做主）。"""
    from .. import guards
    qh = _hash_query(query)
    # 质量门：无信息量内容无采纳价值（确定性，先于一切落库）
    if not query or len(query.strip()) < 2:
        return {"query": query, "error": "问题过短，无采纳价值"}
    if guards.NO_ANSWER in (answer or ""):
        return {"query": query, "error": "兜底回答无采纳价值"}
    if len((answer or "").strip()) < 10:
        return {"query": query, "error": "回答过短，无采纳价值"}
    # 回流投毒防线：采纳内容进知识库前必须过注入检查（曾无检查——
    # 先发一条含注入的问答再点👍，注入内容入库毒化所有检索）
    if guards.contains_injection(f"{query}\n{answer}"):
        return {"adoptId": qh, "query": query, "error": "内容含异常指令，已拒绝采纳"}
    # 格式残骸不带进库（fix_output 确定性重建后入库）
    answer = guards.fix_output(answer or "")
    if not answer.strip():
        return {"query": query, "error": "回答内容为空，已拒绝采纳"}
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback_adopt (query_hash, query, answer, source, adopted_by, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (qh, query, answer, source or "", user, business_now()),
            )
        conn.commit()
        conn.close()
    except pymysql.MySQLError:
        pass   # 软依赖：MySQL 挂了只影响留痕，不影响入库

    # Milvus 入库：Q&A 拼成一个 chunk（标题用问句本身，可读溯源——
    # 曾用哈希标题"问答采纳-0cae5983"，机器名无法溯源，治理实况）。
    # 锚定句是展示层，剥掉再入库——否则新答案引用它再被锚定 → 双重锚定（实况）
    doc_id = f"adopt-{qh[:16]}"
    # 重复采纳去重：同一问法点两次👍会生成同 doc_id 两套 chunk，污染检索
    if milvus_store.has_document(doc_id):
        return {"adoptId": qh, "query": query, "duplicate": True}
    text = f"问题：{query}\n回答：{guards.strip_leading_anchor(answer)}"
    vec = embeddings.embed(text)
    label = f"问答采纳·{query[:16]}"
    milvus_store.insert_document(
        text=text,
        vector=vec,
        document_id=doc_id,
        title=label,
        source=label,
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
    - 采纳直答层的负向闭环：👎 命中已采纳问答 → 失败计数 +1，满 2 停用回慢路径
    - 无纠正文本 → 自动锁一条回归用例：该问法不得再回兜底句式（当前答案确实是兜底才有效力）
    - 有纠正文本 → 进待审队列（人工把纠正提炼成断言后转正式用例）
    """
    from . import adopted as adopted_qa
    adopted_qa.penalize(query)
    if correction and correction.strip():
        with open(_PENDING_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": business_now().isoformat(),
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
