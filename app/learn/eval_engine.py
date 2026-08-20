# -*- coding: utf-8 -*-
"""
评测引擎（自我优化的地基）：断言式回归跑分 + 检索命中跑分。

两种评测：
1. 问答回归用例（regression_cases.json）：对 agent.answer() 的结果做断言——
   路由必须是什么、来源必须含什么、答案必须/不得含什么、时限。
   每修一个 bug 就锁一条用例（棘轮：只增不减）。
2. 检索用例（retrieval_baseline.json）：groundTruth 短语是否出现在召回文本中，
   输出 hitRate / MRR，用于检索参数自适应（top_k / 阈值网格搜索）。
"""
import json
import os
import re
import time
from typing import Any

from ..service import agent, retrieval

_EVAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "eval")


# ---------------- 1. 问答回归 ----------------

def _verify_sql(ext: dict) -> tuple[bool, str]:
    """外部验证：只读 SELECT 查真实库，断言 expect_rows/expect_min_rows/
    expect_value(列,值)。SQL 来自 eval 用例文件（测试代码，非用户输入）。
    数据库不可用返回失败（外部验证失败必须红，不能静默跳过）"""
    sql = (ext.get("sql") or "").strip()
    if not sql or not sql.lower().startswith("select"):
        return False, "verify_sql 必须是以 SELECT 开头的只读查询"
    try:
        import pymysql
        from .. import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        conn.close()
    except Exception as e:
        return False, f"verify_sql 执行失败: {e!r}"
    if "expect_rows" in ext and len(rows) != ext["expect_rows"]:
        return False, f"行数 {len(rows)} != {ext['expect_rows']}"
    if "expect_min_rows" in ext and len(rows) < ext["expect_min_rows"]:
        return False, f"行数 {len(rows)} < {ext['expect_min_rows']}"
    if "expect_value" in ext:
        col, val = ext["expect_value"]
        if col not in cols:
            return False, f"列 {col} 不存在（现有 {cols}）"
        if not rows or str(rows[0][cols.index(col)]) != str(val):
            got = str(rows[0][cols.index(col)]) if rows else "（空）"
            return False, f"{col} 期望 {val!r} 实际 {got!r}"
    return True, f"{len(rows)} 行"


def run_answer_case(case: dict, replay=None, skip_external: bool = False) -> dict:
    """执行一条断言用例，返回 {id, query, passed, checks, seconds}。
    每用例独立会话（多轮历史/待确认单不串用例）；followup 字段支持二轮断言
    （如请假流程先回确认提示、再答"确认"后断言提交回执）。

    replay: LLMReplay 实例（可选）。非 None 时，每用例 begin_case/assert_consumed/save_case，
    mock llm.chat/chat_stream（record 录 fixture / replay 回放 fixture / off 不拦截）。
    """
    query = case["query"]
    expect = case.get("expect", {})
    case_id = case.get("id", "case")
    conversation_id = f"eval-{case_id}"
    # 每用例独立会话：先清空再按需预置历史。曾只在有 history 字段时清空——
    # 无历史用例的会话里残留早期版本跑分写下的轮次（旧代码不跳 eval 会话），
    # has_history 恒 True，追问路由改变后 guard-010 被残史带偏到
    # KNOWLEDGE_QA 的实况。清空无条件执行（历史字段=预置内容，非清空条件）
    from ..service import history as _history
    _history.clear_conversation(conversation_id)
    if case.get("history"):
        for t in case["history"]:
            _history.save_turn(conversation_id, t.get("q", ""), t.get("a", ""))
    # LLM replay：切换用例 fixture（replay 加载 / record 清空录制队列）
    if replay is not None:
        replay.begin_case(case_id)
    start = time.time()
    result = None
    replay_warning = ""
    try:
        result = agent.answer(query, conversation_id)
        followup = case.get("followup")
        if followup:
            result = agent.answer(followup, conversation_id)
    except RuntimeError as e:
        # replay 模式下 fixture 耗尽/不匹配——记录警告，不崩溃（答案断言是最终判定）
        result = {"answer": "", "route": "ERROR", "sources": []}
        replay_warning = str(e)
    finally:
        seconds = time.time() - start
        # replay 结束：断言 fixture 全消费（防假绿）；record 结束：保存 fixture
        if replay is not None:
            try:
                replay.assert_consumed()
            except RuntimeError as e:
                if not replay_warning:
                    replay_warning = str(e)
            replay.save_case()

    checks: list[tuple[str, bool, str]] = []
    # replay 模式下 fixture 警告（耗尽/未消费完）→ 整体失败，不放过（防假绿）
    if replay_warning:
        checks.append(("replay_fixture", False, replay_warning))
    answer = result.get("answer") or ""
    sources = result.get("sources") or []
    route = result.get("route") or ""

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    if "route" in expect:
        check("route", route == expect["route"], f"route={route}")
    if "sources_include" in expect:
        ok = any(any(exp in s for s in sources) for exp in expect["sources_include"])
        check("sources_include", ok, f"sources={sources}")
    if "answer_contain" in expect:
        for text in expect["answer_contain"]:
            check(f"contain:{text}", text in answer, "")
    if "answer_not_contain" in expect:
        for text in expect["answer_not_contain"]:
            check(f"not_contain:{text}", text not in answer, "")
    if "chart_present" in expect:
        has_chart = result.get("chart") is not None
        check("chart", has_chart == expect["chart_present"], f"chart={has_chart}")
    if "max_seconds" in expect:
        check("max_seconds", seconds <= expect["max_seconds"], f"{seconds:.1f}s")
    # 外部世界验证（2026-08-19 借鉴 dsh「验证外部世界，而非自我报告」）：
    # 断言实际数据库状态——关键词探测会让作弊 agent 通过，落库/真实行才是凭据。
    # replay CI 无业务 MySQL 时显式跳过；本地 replay 默认仍执行，不能把跳过伪装成通过。
    for ext in expect.get("verify_sql", []):
        if skip_external:
            check(f"verify_sql:skipped:{str(ext.get('sql', ''))[:30]}", True,
                  "replay CI 显式跳过外部库验证")
        else:
            ok, detail = _verify_sql(ext)
            check(f"verify_sql:{str(ext.get('sql', ''))[:40]}", ok, detail)

    failed = [c for c in checks if not c[1]]
    return {
        "id": case.get("id", query[:20]),
        "query": query,
        "passed": not failed,
        "seconds": round(seconds, 2),
        "failed_checks": [f"{name} {detail}".strip() for name, _, detail in failed],
    }


def run_regression(path: str | None = None, replay=None, skip_external: bool = False) -> dict:
    """跑全量回归，返回报告；用例文件默认 eval/regression_cases.json

    replay: LLMReplay 实例（可选）。非 None 时激活 mock 并在每用例切换 fixture。
    mode=record 录 fixture；replay 回放 fixture（零成本）；off 不拦截。
    """
    path = path or os.path.join(_EVAL_DIR, "regression_cases.json")
    with open(path, encoding="utf-8") as f:
        cases = json.load(f)
    # 激活 LLM mock（patch llm.chat/chat_stream）；try/finally 恢复
    if replay is not None:
        replay.activate()
    try:
        results = [run_answer_case(c, replay, skip_external) for c in cases]
    finally:
        if replay is not None:
            replay.deactivate()
    passed = sum(1 for r in results if r["passed"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


# ---------------- 2. 检索命中 ----------------

def _norm_phrase(s: str) -> str:
    """命中判定归一化：去标点/空白（"放假调休共7天" vs chunk"放假调休，共7天"是同一命中）"""
    return re.sub(r"[，。、；：（）()\s]+", "", s)


def run_retrieval_case(question: str, ground_truth: str, *,
                       top_k: int, min_score: float) -> dict:
    """单条检索用例：GT 短语出现在召回文本中即命中（标点归一化比较）；返回 hit + 名次"""
    hits = retrieval.retrieve(question, top_k=top_k, min_score=min_score, rerank_top_n=top_k)
    gt = _norm_phrase(ground_truth)
    for rank, h in enumerate(hits, start=1):
        if gt in _norm_phrase(h["text"]):
            return {"hit": True, "rank": rank}
    return {"hit": False, "rank": 0}


def run_retrieval_set(path: str | None = None, *,
                      top_k: int, min_score: float) -> dict:
    """全量检索评测：hitRate / MRR（用例文件默认 eval/retrieval_baseline.json）"""
    path = path or os.path.join(_EVAL_DIR, "retrieval_baseline.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    items = data["items"]
    results = [run_retrieval_case(i["question"], i["groundTruth"],
                                  top_k=top_k, min_score=min_score) for i in items]
    hits = sum(1 for r in results if r["hit"])
    mrr = sum(1.0 / r["rank"] for r in results if r["hit"]) / len(items) if items else 0.0
    return {
        "total": len(items),
        "hit": hits,
        "hit_rate": round(hits / len(items), 4) if items else 0.0,
        "mrr": round(mrr, 4),
        "details": [
            {"question": i["question"], "hit": r["hit"], "rank": r["rank"]}
            for i, r in zip(items, results)
        ],
    }
