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
import time
from typing import Any

from ..service import agent, retrieval

_EVAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "eval")


# ---------------- 1. 问答回归 ----------------

def run_answer_case(case: dict) -> dict:
    """执行一条断言用例，返回 {id, query, passed, checks, seconds}"""
    query = case["query"]
    expect = case.get("expect", {})
    start = time.time()
    result = agent.answer(query, "eval")
    seconds = time.time() - start

    checks: list[tuple[str, bool, str]] = []
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

    failed = [c for c in checks if not c[1]]
    return {
        "id": case.get("id", query[:20]),
        "query": query,
        "passed": not failed,
        "seconds": round(seconds, 2),
        "failed_checks": [f"{name} {detail}".strip() for name, _, detail in failed],
    }


def run_regression(path: str | None = None) -> dict:
    """跑全量回归，返回报告；用例文件默认 eval/regression_cases.json"""
    path = path or os.path.join(_EVAL_DIR, "regression_cases.json")
    with open(path, encoding="utf-8") as f:
        cases = json.load(f)
    results = [run_answer_case(c) for c in cases]
    passed = sum(1 for r in results if r["passed"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }


# ---------------- 2. 检索命中 ----------------

def run_retrieval_case(question: str, ground_truth: str, *,
                       top_k: int, min_score: float) -> dict:
    """单条检索用例：GT 短语出现在召回文本中即命中；返回 hit + 首个命中名次（MRR 用）"""
    hits = retrieval.retrieve(question, top_k=top_k, min_score=min_score, rerank_top_n=top_k)
    for rank, h in enumerate(hits, start=1):
        if ground_truth in h["text"]:
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
