# -*- coding: utf-8 -*-
"""
状态事件日志测试（dsh 一-12）。

验证：合法转移记录、非法转移标记、replay 时间线、current 折叠 last-wins。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.plan import status_events  # noqa: E402

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


def test_valid_transition():
    """合法转移 1→4（拒绝）记录成功"""
    apply_no = "TESTSTAT001"
    # 清理残留
    try:
        conn = status_events._conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_status_event WHERE apply_no=%s", (apply_no,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    ok = status_events.record(apply_no, 1, 4, "rejected", actor_id=1, comment="测试拒绝")
    check("合法转移记录成功", ok)
    events = status_events.replay(apply_no)
    check("replay 1 条", len(events) >= 1)
    check("事件 to=4", events[-1]["to"] == 4)
    check("current 折叠=4", status_events.current(apply_no) == 4)
    # 清理
    try:
        conn = status_events._conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_status_event WHERE apply_no=%s", (apply_no,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def test_invalid_transition():
    """非法转移 3→2（已通过→审批中）标记 invalid 但不阻断"""
    apply_no = "TESTSTAT002"
    try:
        conn = status_events._conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_status_event WHERE apply_no=%s", (apply_no,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    ok = status_events.record(apply_no, 3, 2, "approved", actor_id=1)
    check("非法转移仍记录", ok)
    events = status_events.replay(apply_no)
    check("非法转移 action=invalid", events and events[-1]["action"] == "invalid_transition")
    # 清理
    try:
        conn = status_events._conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_status_event WHERE apply_no=%s", (apply_no,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def test_is_valid():
    """状态转移合法性检查"""
    check("1→4 合法", status_events.is_valid_transition(1, 4))
    check("1→3 合法", status_events.is_valid_transition(1, 3))
    check("3→2 非法", not status_events.is_valid_transition(3, 2))
    check("4→1 非法", not status_events.is_valid_transition(4, 1))


def test_replay_order():
    """多事件 replay 按时间正序"""
    apply_no = "TESTSTAT003"
    try:
        conn = status_events._conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_status_event WHERE apply_no=%s", (apply_no,))
        conn.commit()
        conn.close()
    except Exception:
        pass
    status_events.record(apply_no, 1, 2, "forwarded", actor_id=1)
    status_events.record(apply_no, 2, 3, "approved", actor_id=2)
    events = status_events.replay(apply_no)
    check("2 事件", len(events) == 2)
    check("顺序 1→2→3", events[0]["from"] == 1 and events[0]["to"] == 2
          and events[1]["from"] == 2 and events[1]["to"] == 3)
    check("current=3", status_events.current(apply_no) == 3)
    # 清理
    try:
        conn = status_events._conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM approval_status_event WHERE apply_no=%s", (apply_no,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def test_empty_replay():
    """无事件 replay 返回空"""
    events = status_events.replay("NONEXISTENT-99999")
    check("无事件 replay 返回空", events == [])
    check("无事件 current 返回 None", status_events.current("NONEXISTENT-99999") is None)


if __name__ == "__main__":
    test_valid_transition()
    test_invalid_transition()
    test_is_valid()
    test_replay_order()
    test_empty_replay()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
