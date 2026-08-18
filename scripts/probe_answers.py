# -*- coding: utf-8 -*-
"""退役后实测：NL2SQL 对员工/报销/经营问法的实际答案（进程内调 agent.answer）。"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.service import agent  # noqa: E402

QUERIES = [
    "公司有多少人",
    "王强是什么时候入职的",
    "赵敏最近7天报销了多少",
    "最近7天的经营数据",
    "帮我请假，李四明天请一天事假，家里有事",
]

for q in QUERIES:
    t0 = time.time()
    try:
        r = agent.answer(q, conversation_id="", user="admin")
    except Exception as e:
        r = {"answer": f"EXC: {e!r}", "route": "EXC", "chart": None}
    print(f"\n===== {q} | {time.time()-t0:.1f}s | route={r.get('route')} =====")
    print(r.get("answer", "")[:400])
    if r.get("chart"):
        print("[chart]", r["chart"].get("kind"), r["chart"].get("title"))
