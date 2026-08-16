# -*- coding: utf-8 -*-
"""
一键评测：python scripts/run_eval.py
  - regression  问答回归（断言式，退出码非 0 表示有用例失败）
  - retrieval   检索命中（hitRate/MRR）
  - all         两者都跑（默认）

这是"自我优化"的入口：每次改完代码先跑回归，失败用例即待修清单。
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from app.learn import eval_engine  # noqa: E402


def run_regression() -> int:
    report = eval_engine.run_regression()
    print(f"\n=== 问答回归: {report['passed']}/{report['total']} 通过 ===")
    for r in report["results"]:
        mark = "✓" if r["passed"] else "✗"
        detail = f"（{'；'.join(r['failed_checks'])}）" if r["failed_checks"] else ""
        print(f"  {mark} [{r['id']}] {r['query'][:30]} {r['seconds']}s {detail}")
    return 0 if report["failed"] == 0 else 1


def run_retrieval() -> None:
    from app.service.retrieval import current_params
    params = current_params()
    report = eval_engine.run_retrieval_set(top_k=params["top_k"], min_score=params["min_score"])
    print(f"\n=== 检索评测（top_k={params['top_k']} 阈值={params['min_score']}）: "
          f"hitRate={report['hit_rate']:.2%} MRR={report['mrr']} ===")
    for d in report["details"]:
        print(f"  {'✓' if d['hit'] else '✗'} {d['question'][:32]} rank={d['rank']}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    code = 0
    if mode in ("all", "regression"):
        code = run_regression()
    if mode in ("all", "retrieval"):
        run_retrieval()
    sys.exit(code)
