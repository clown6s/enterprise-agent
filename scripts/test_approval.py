# -*- coding: utf-8 -*-
"""
审批动作层测试（approval.py）——补 22%→80% 覆盖率缺口。

测试对象：summary_text（纯函数）/ pending_by_approver / act_on_application /
try_approval_command / list_applications。

策略：真实 MySQL（测真实 SQL 行为比 mock 有价值）+ 测试数据用 TESTCOV 前缀
+ try/finally 清理（不污染库）。summary_text 全离线。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.plan import approval, registry  # noqa: E402
from app.service import acl as _acl  # noqa: E402

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


# ---------------- 纯函数：summary_text（离线，无 DB） ----------------

def test_summary_text():
    """5 个业务的摘要文案：列名转中文 + 精选列子集"""
    check("summary leave",
          "请假类型" in approval.summary_text("leave",
              {"leave_type": "事假", "days": 1, "reason": "家里有事", "user_name": "李娜"})
          and "user_name" not in approval.summary_text("leave",
              {"leave_type": "事假", "days": 1, "reason": "x", "user_name": "李娜"}),
          "应展示精选列中文、不含未精选的 user_name")
    check("summary expense",
          "类型" in approval.summary_text("expense",
              {"expense_type": "交通费", "amount": 200, "reason": "打车", "expense_date": "x"})
          and "expense_date" not in approval.summary_text("expense",
              {"expense_type": "x", "amount": 1, "reason": "x", "expense_date": "应被精选排除"}))
    check("summary contract",
          "合同" in approval.summary_text("contract",
              {"contract_name": "采购合同", "counterparty": "天启", "amount": 3, "reason": "用印"})
          and "amount" not in approval.summary_text("contract",
              {"contract_name": "x", "counterparty": "x", "amount": 999, "reason": "x"}))
    check("summary travel",
          "目的地" in approval.summary_text("travel",
              {"destination": "上海", "start_date": "2026-08-21", "reason": "拜访"}))
    check("summary purchase",
          "物品" in approval.summary_text("purchase",
              {"item_name": "A4纸", "amount": 500, "reason": "办公", "quantity": 999}))
    # 未知业务类型 → 空摘要不崩
    check("summary unknown biz 空",
          approval.summary_text("nonexistent_biz", {"a": 1}) == "")


# ---------------- DB 测试基础设施 ----------------

_TEST_PREFIX = "TESTCOV"
# 真实单号格式 = 前缀 + 8位日期 + 8位hex（审批指令正则要 8 位日期才匹配）
_DATE = "20260820"
def _ano(n):
    return f"{_TEST_PREFIX}{_DATE}{n:08d}"


def _conn():
    import pymysql
    from app import config
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=5)


def _cleanup(conn):
    """清理所有 TESTCOV 前缀的测试数据（申请表 + 审批记录）"""
    try:
        with conn.cursor() as cur:
            for table in registry.TABLE_MAP.values():
                cur.execute(f"DELETE FROM {table} WHERE apply_no LIKE %s", (_TEST_PREFIX + "%",))
            cur.execute("DELETE FROM oa_approval_record WHERE apply_no LIKE %s",
                        (_TEST_PREFIX + "%",))
        conn.commit()
    except Exception as e:
        print(f"[cleanup] {e!r}")


def _seed_leave(conn, emp_id, dept_id, apply_no, status=1):
    """插一条测试请假申请（current_approver_id=emp_id 自审）"""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO oa_leave_application (apply_no, user_id, user_name, dept_id, "
            "leave_type, start_time, end_time, days, reason, status, current_approver_id, "
            "create_time) VALUES (%s,%s,%s,%s,%s,NOW(),NOW(),%s,%s,%s,%s,NOW())",
            (apply_no, emp_id, "测试员", dept_id, 1, 1.0, "测试事由", status, emp_id))
        conn.commit()
        return cur.lastrowid


def _get_status(conn, table, apply_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {table} WHERE id=%s", (apply_id,))
        r = cur.fetchone()
        return r[0] if r else None


def _count_records(conn, apply_no):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM oa_approval_record WHERE apply_no=%s", (apply_no,))
        return cur.fetchone()[0]


def test_act_on_application():
    """审批动作：拒绝→status=4+记录；重复操作→拒绝；非当前审批人→拒绝"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, dept_id FROM employee WHERE status=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            check("act_on_application 跳过（无员工）", True, "员工表空")
            return
        emp_id, dept_id = row[0], row[1]

        apply_no = _ano(1)
        aid = _seed_leave(conn, emp_id, dept_id, apply_no)

        # 拒绝
        r = approval.act_on_application("leave", aid, emp_id, approval.ACT_REJECT, "测试拒绝")
        check("act reject ok", r["ok"], r.get("message"))
        check("act reject status=4", _get_status(conn, "oa_leave_application", aid) == 4)
        check("act reject record=1", _count_records(conn, apply_no) == 1)

        # 重复操作（status 已离开 1/2）
        r2 = approval.act_on_application("leave", aid, emp_id, approval.ACT_APPROVE)
        check("act 重复拒绝", not r2["ok"] and "已处理" in r2["message"], r2.get("message"))

        # 非当前审批人
        apply_no2 = _ano(2)
        aid2 = _seed_leave(conn, emp_id, dept_id, apply_no2)
        # 用一个不等于 current_approver_id 的 id（emp_id+99999 几乎必不匹配）
        r3 = approval.act_on_application("leave", aid2, emp_id + 99999, approval.ACT_REJECT)
        check("act 非审批人拒绝", not r3["ok"] and "当前审批人" in r3["message"], r3.get("message"))
        check("act 非审批人不改状态", _get_status(conn, "oa_leave_application", aid2) == 1)

        # 不存在的单据
        r4 = approval.act_on_application("leave", 999999999, emp_id, approval.ACT_APPROVE)
        check("act 不存在单据", not r4["ok"] and "不存在" in r4["message"], r4.get("message"))
    finally:
        _cleanup(conn)
        conn.close()


def test_pending_by_approver():
    """按审批人查待审批单：跨业务聚合 + _business_type 标记"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, dept_id FROM employee WHERE status=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            check("pending_by_approver 跳过", True, "员工表空")
            return
        emp_id, dept_id = row[0], row[1]
        apply_no = _ano(3)
        aid = _seed_leave(conn, emp_id, dept_id, apply_no, status=1)
        try:
            pend = approval.pending_by_approver(emp_id)
            hit = next((p for p in pend if p.get("apply_no") == apply_no), None)
            check("pending 命中", hit is not None)
            check("pending 带 business_type",
                  hit is not None and hit.get("_business_type") == "leave")
        finally:
            _cleanup(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def test_try_approval_command():
    """对话审批指令：「同意/拒绝 + 单号」解析 + 命中/未命中"""
    conn = _conn()
    orig_resolve = _acl.resolve_employee
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, dept_id FROM employee WHERE status=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            check("try_approval_command 跳过", True, "员工表空")
            return
        emp_id, dept_id = row[0], row[1]
        # mock：对话审批入口靠 acl.resolve_employee 把 user 解析成 employee_id
        _acl.resolve_employee = lambda u: emp_id

        apply_no = _ano(4)
        aid = _seed_leave(conn, emp_id, dept_id, apply_no, status=1)

        # 「拒绝 + 单号」
        r = approval.try_approval_command(f"拒绝 {apply_no}", "testuser")
        check("cmd 拒绝+单号 命中", r is not None and r.get("route") == "APPROVAL"
              and "拒绝" in r.get("answer", ""))
        check("cmd 拒绝后 status=4", _get_status(conn, "oa_leave_application", aid) == 4)

        # 单号不存在
        r2 = approval.try_approval_command(f"同意 {_ano(99)}", "testuser")
        check("cmd 未命中单号", r2 is not None and "未找到" in r2.get("answer", ""))

        # 非审批指令 → None（漏回正常路由）
        r3 = approval.try_approval_command("今天天气怎么样", "testuser")
        check("cmd 非指令漏回 None", r3 is None)

        # 无单的「同意」→ 无待审批 → None（哨兵 id 确保无 pending，避免环境残留数据干扰）
        _cleanup(conn)
        _acl.resolve_employee = lambda u: 99999999
        r4 = approval.try_approval_command("同意", "testuser")
        check("cmd 无单同意漏回 None", r4 is None)
    finally:
        _acl.resolve_employee = orig_resolve
        _cleanup(conn)
        conn.close()


def test_list_applications():
    """看板查询：返回列表 + 每行带业务码/名"""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, dept_id FROM employee WHERE status=1 LIMIT 1")
            row = cur.fetchone()
        if not row:
            check("list_applications 跳过", True, "员工表空")
            return
        emp_id, dept_id = row[0], row[1]
        apply_no = _ano(5)
        _seed_leave(conn, emp_id, dept_id, apply_no, status=1)
        try:
            rows = approval.list_applications("leave", status=1)
            hit = [r for r in rows if r.get("apply_no") == apply_no]
            check("list 命中", len(hit) == 1)
            check("list 带 business_name",
                  hit and hit[0].get("_business_name") == "请假申请")
            # 全业务看板
            all_rows = approval.list_applications()
            check("list 全业务返回列表", isinstance(all_rows, list))
        finally:
            _cleanup(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    test_summary_text()
    test_act_on_application()
    test_pending_by_approver()
    test_try_approval_command()
    test_list_applications()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
