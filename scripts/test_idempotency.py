# -*- coding: utf-8 -*-
"""
幂等性契约测试：验证 DB UNIQUE 约束 + Feishu 持久化事件去重。

真实 MySQL + 测试数据清理。不进 CI 门（需 DB）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.plan import approval  # noqa: E402
from app.plan import registry  # noqa: E402

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


def _conn():
    import pymysql
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                           user="root", password=config.MYSQL_PASSWORD,
                           database=config.MYSQL_DB, charset="utf8mb4",
                           connect_timeout=3)


def _cleanup(conn):
    try:
        with conn.cursor() as cur:
            for table in registry.TABLE_MAP.values():
                cur.execute(f"DELETE FROM {table} WHERE apply_no LIKE %s", ("TESTIDEMP%",))
            cur.execute("DELETE FROM oa_approval_record WHERE apply_no LIKE %s", ("TESTIDEMP%",))
            cur.execute("DELETE FROM feishu_event_seen WHERE message_id LIKE %s", ("TESTIDEMP%",))
        conn.commit()
    except Exception as e:
        print(f"[cleanup] {e!r}")


def _seed_leave(conn, emp_id, dept_id, apply_no, status=1):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oa_leave_application (apply_no, user_id, user_name, dept_id, "
            "leave_type, start_time, end_time, days, reason, status, current_approver_id, "
            "create_time) VALUES (%s,%s,%s,%s,%s,NOW(),NOW(),%s,%s,%s,%s,NOW())",
            (apply_no, emp_id, "测试员", dept_id, 1, 1.0, "测试事由", status, emp_id))
        conn.commit()
        return cur.lastrowid


def test_approval_idempotent():
    """重复审批同一单：第二次应返回"已处理"（应用层 + DB UNIQUE 双保险）"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, dept_id FROM employee WHERE status=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            check("approval 幂等 跳过（无员工）", True)
            return
        emp_id, dept_id = row[0], row[1]
        apply_no = "TESTIDEMP0001"
        aid = _seed_leave(conn, emp_id, dept_id, apply_no, status=1)

        # 第一次拒绝 → 成功
        r1 = approval.act_on_application("leave", aid, emp_id, approval.ACT_REJECT)
        check("approval 首次操作 ok", r1["ok"], r1.get("message"))

        # 第二次操作 → "已处理"（幂等）
        r2 = approval.act_on_application("leave", aid, emp_id, approval.ACT_APPROVE)
        check("approval 重复操作拒绝", not r2["ok"] and "已处理" in r2["message"],
              r2.get("message"))

        # DB 层：审批记录只有一条
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM oa_approval_record WHERE apply_no=%s", (apply_no,))
            count = cur.fetchone()[0]
        check("approval DB 唯一记录", count == 1, f"count={count}")
    finally:
        _cleanup(conn)
        conn.close()


def test_apply_no_unique():
    """DB UNIQUE 约束：同 apply_no 第二次 INSERT 应失败"""
    conn = _conn()
    try:
        apply_no = "TESTIDEMP0002"
        with conn.cursor() as cur:
            cur.execute("SELECT id, dept_id FROM employee WHERE status=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            check("apply_no UNIQUE 跳过", True)
            return
        emp_id, dept_id = row[0], row[1]
        _seed_leave(conn, emp_id, dept_id, apply_no)

        # 第二次同 apply_no INSERT → 应抛异常（UNIQUE 约束）
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO oa_leave_application (apply_no, user_id, user_name, dept_id, "
                    "leave_type, start_time, end_time, days, reason, status, "
                    "current_approver_id, create_time) VALUES (%s,%s,%s,%s,%s,NOW(),NOW(),%s,%s,%s,%s,NOW())",
                    (apply_no, emp_id, "测试员2", dept_id, 1, 1.0, "测试2", 1, emp_id))
                conn.commit()
            check("apply_no UNIQUE 约束生效", False, "第二次 INSERT 应失败但成功了")
        except Exception:
            check("apply_no UNIQUE 约束生效", True)
            conn.rollback()
    finally:
        _cleanup(conn)
        conn.close()


def test_feishu_event_dedup():
    """Feishu 事件持久化去重：第二次同 message_id 应返回 False"""
    from app.channels.feishu import channel
    # 测试持久化表去重
    conn = _conn()
    try:
        msg_id = "TESTIDEMP-feishu-001"
        # 清理残留
        with conn.cursor() as cur:
            cur.execute("DELETE FROM feishu_event_seen WHERE message_id=%s", (msg_id,))
        conn.commit()
    finally:
        conn.close()

    # 模拟首次
    r1 = channel._mark_seen(msg_id)
    check("feishu 首次事件", r1 is True)
    # 模拟重投
    r2 = channel._mark_seen(msg_id)
    check("feishu 重投去重", r2 is False)

    # 清理
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM feishu_event_seen WHERE message_id LIKE %s", ("TESTIDEMP%",))
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    test_approval_idempotent()
    test_apply_no_unique()
    test_feishu_event_dedup()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
