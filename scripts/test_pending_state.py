# -*- coding: utf-8 -*-
"""
State replace 强校验测试（dsh 一-5）。

验证：用户源只能动白名单字段，系统字段被拒绝，plan 先验证后提交。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.plan.pending_state import ChangePlan, validate_change, apply_plan  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


def test_user_mutable():
    """用户源：白名单内字段通过"""
    old = {"userName": "李娜", "reason": "家里有事", "startTime": "2026-08-20"}
    new = {"reason": "改个事由", "startTime": "2026-08-21"}
    plan = validate_change(old, new, source="user")
    check("用户改白名单字段 approved", plan.approved)
    check("rejected_keys 为空", not plan.rejected_keys)
    merged = apply_plan(plan)
    check("merged 正确", merged["reason"] == "改个事由" and merged["startTime"] == "2026-08-21"
          and merged["userName"] == "李娜")


def test_user_rejected():
    """用户源：系统字段被拒绝"""
    old = {"userName": "李娜", "sop_code": "leave_action"}
    new = {"sop_code": "expense_action", "reason": "改事由"}
    plan = validate_change(old, new, source="user")
    check("系统字段被拒绝", not plan.approved)
    check("rejected_keys 含 sop_code", "sop_code" in plan.rejected_keys)
    try:
        apply_plan(plan)
        check("apply_plan 拒绝时抛异常", False)
    except ValueError:
        check("apply_plan 拒绝时抛异常", True)


def test_system_source():
    """系统源：可改系统字段"""
    old = {"userName": "李娜", "status": 1}
    new = {"status": 2, "current_approver_id": 100}
    plan = validate_change(old, new, source="system")
    check("系统源改系统字段 approved", plan.approved)
    merged = apply_plan(plan)
    check("merged 正确", merged["status"] == 2 and merged["current_approver_id"] == 100)


def test_unknown_field():
    """未知字段（不在白名单也不在 old）：被拒绝"""
    old = {"userName": "李娜"}
    new = {"malicious_field": "hacked"}
    plan = validate_change(old, new, source="user")
    check("未知字段被拒绝", not plan.approved)
    check("rejected_keys 含 malicious_field", "malicious_field" in plan.rejected_keys)


def test_empty_change():
    """空变更：approved=True，merged=old"""
    old = {"userName": "李娜"}
    plan = validate_change(old, {}, source="user")
    check("空变更 approved", plan.approved)
    merged = apply_plan(plan)
    check("merged=old", merged == old)


if __name__ == "__main__":
    test_user_mutable()
    test_user_rejected()
    test_system_source()
    test_unknown_field()
    test_empty_change()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
