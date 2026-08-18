# -*- coding: utf-8 -*-
"""审批动作探针：提交→通过(一级)→转交→通过(二级)→终态；拒绝路径；护栏。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.service import agent  # noqa: E402
from app.plan import approval  # noqa: E402
from app import config  # noqa: E402
import pymysql  # noqa: E402

# 1. 提交一笔报销（王强，审批链：部门负责人=王强→财务=赵敏）
cid = "probe_approval_flow"
r1 = agent.answer("帮我报销，王强办公用品 200 元，买键盘", conversation_id=cid, user="admin")
print("提交:", r1.get("answer", "")[:80])
r2 = agent.answer("确认", conversation_id=cid, user="admin")
print("确认:", r2.get("answer", "")[:120])

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    cur.execute("SELECT id, apply_no, status, current_approver_id FROM expense_application "
                "ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
apply_id, apply_no, status, current = row
print(f"\n落库: id={apply_id} {apply_no} status={status} 审批人={current}")

# 2. 越权护栏：李娜（id=2）不是当前审批人 → 拒
print("\n[越权] 李娜(2)尝试审批:", approval.act_on_application("expense", apply_id, 2, 1))

# 3. 一级通过（王强=3）→ 转交赵敏(4)
print("[一级通过] 王强(3):", approval.act_on_application("expense", apply_id, 3, 1))
with conn.cursor() as cur:
    cur.execute("SELECT status, current_approver_id FROM expense_application WHERE id=%s",
                (apply_id,))
    print("  单子状态:", cur.fetchone())

# 4. 二级通过（赵敏=4）→ 终态
print("[二级通过] 赵敏(4):", approval.act_on_application("expense", apply_id, 4, 1))
with conn.cursor() as cur:
    cur.execute("SELECT status, current_approver_id FROM expense_application WHERE id=%s",
                (apply_id,))
    print("  单子状态:", cur.fetchone())
    cur.execute("SELECT approval_level, action, approver_id FROM oa_approval_record "
                "WHERE apply_no=%s ORDER BY approval_level", (apply_no,))
    print("  审批记录:", cur.fetchall())

# 5. 重复操作护栏
print("[重复操作]:", approval.act_on_application("expense", apply_id, 4, 1))
conn.close()

# 6. 审批指令入口（zzp 绑 employee_id=2=李娜，李娜无待审批单 → 返回 None 漏回路由）
print("\n[指令-无待审批] try_approval_command('同意', 'ou_aeaaa6ce05b1727612c1e1e2f529b355'):",
      approval.try_approval_command("同意", "ou_aeaaa6ce05b1727612c1e1e2f529b355"))
