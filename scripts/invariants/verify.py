# -*- coding: utf-8 -*-
"""二、verify 契约（校验链短路语义）——搬自原 test_invariants.py 第二段。

4 条 check：空答案/空上下文/兜底短路、有据数字留存率。
"""
from app import guards
from app.service import verify


def run(check) -> None:
    # 12. 短路：空答案/空上下文/全量兜底原样返回（不调 LLM）
    check("verify 空答案短路", verify.verify("", "ctx") == "")
    check("verify 空上下文短路", verify.verify("答案", "") == "答案")
    check("verify 兜底短路", verify.verify(guards.NO_ANSWER, "ctx") == guards.NO_ANSWER)

    # 13. 有据数字留存率：有据 90 mmHg 保留 → 1.0
    check("有据数字留存率",
          verify._grounded_number_retention(
              "收缩压 ≥ 140 和/或 舒张压 ≥ 90 mmHg",
              "收缩压 ≥ 140 和/或 舒张压 ≥ 90 mmHg",
              "高血压定义为诊室血压 ≥ 140/90 mmHg。") >= 1.0)
