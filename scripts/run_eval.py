# -*- coding: utf-8 -*-
"""
一键评测：python scripts/run_eval.py
  - regression  问答回归（断言式，退出码非 0 表示有用例失败）
  - retrieval   检索命中（hitRate/MRR）
  - all         两者都跑（默认）

LLM replay 模式（dsh 三-2/三-3）：
  --record   调真 LLM 录 fixture（升级模型/改提示词后手动跑一次）
  --replay   从 fixture 读，不调真 LLM（CI push 门跑——零成本、确定性）
  不传则调真 LLM（当前默认行为）

这是"自我优化"的入口：每次改完代码先跑回归，失败用例即待修清单。
"""
import io
import os
import sys
from unittest.mock import patch, MagicMock

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.learn import eval_engine  # noqa: E402


def _setup_offline_mode():
    """离线模式：mock 所有外部依赖（Milvus/SiliconFlow/MySQL/LLM）"""
    # Mock MySQL 连接（避免初始连接超时）
    def mock_pymysql_connect(*args, **kwargs):
        raise pymysql.OperationalError(2003, "离线模式：跳过外部数据库连接")

    # Mock SiliconFlow API（避免 Bearer header 错误）
    def mock_httpx_post(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": [{"embedding": [0.0] * 4096}]}
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    # Patch 外部依赖
    import pymysql
    import httpx

    patcher_mysql = patch("pymysql.connect", side_effect=mock_pymysql_connect)
    patcher_httpx = patch("httpx.post", side_effect=mock_httpx_post)

    patcher_mysql.start()
    patcher_httpx.start()

    return [patcher_mysql, patcher_httpx]


def _make_replay():
    """从 CLI 参数解析 replay 模式；返回 LLMReplay 或 None。"""
    if "--replay" in sys.argv:
        from app.llm_replay import LLMReplay
        return LLMReplay("replay", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "eval", "fixtures"))
    if "--record" in sys.argv:
        from app.llm_replay import LLMReplay
        return LLMReplay("record", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "eval", "fixtures"))
    return None


def run_regression() -> int:
    # 离线模式：跳过 MySQL 验证，默认 True
    skip_external = "--skip-external" in sys.argv or True
    replay = _make_replay()

    patchers = []
    if replay is not None:  # replay 模式下启用离线 mock
        patchers = _setup_offline_mode()

    try:
        report = eval_engine.run_regression(replay=replay, skip_external=skip_external)
        print(f"\n=== 问答回归: {report['passed']}/{report['total']} 通过 ===")
        for r in report["results"]:
            mark = "✓" if r["passed"] else "✗"
            detail = f"（{'；'.join(r['failed_checks'])}）" if r["failed_checks"] else ""
            print(f"  {mark} [{r['id']}] {r['query'][:30]} {r['seconds']}s {detail}")
        return 0 if report["failed"] == 0 else 1
    finally:
        for p in patchers:
            p.stop()


def run_retrieval() -> None:
    from app.service.retrieval import current_params
    params = current_params()

    # 检索评测需要外部服务，replay 模式下 mock
    patchers = []
    if "--replay" in sys.argv or "--offline" in sys.argv:
        patchers = _setup_offline_mode()

    try:
        report = eval_engine.run_retrieval_set(top_k=params["top_k"], min_score=params["min_score"])
        print(f"\n=== 检索评测（top_k={params['top_k']} 阈值={params['min_score']}）: "
              f"hitRate={report['hit_rate']:.2%} MRR={report['mrr']} ===")
        for d in report["details"]:
            print(f"  {'✓' if d['hit'] else '✗'} {d['question'][:32]} rank={d['rank']}")
    finally:
        for p in patchers:
            p.stop()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    code = 0
    if mode in ("all", "regression"):
        code = run_regression()
    if mode in ("all", "retrieval"):
        run_retrieval()
    sys.exit(code)
