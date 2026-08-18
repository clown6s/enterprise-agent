# -*- coding: utf-8 -*-
"""demo 数据清理（T49 收尾，用户已拍板）：
1. 李四的测试请假单（sys_employee 退役体系遗留）+ 回归产生的「家里有事」测试单
2. 王强 mobile 假数据清 NULL（我曾测字段权限时录的 13800001111）
3. 四张申请表的探针测试数据 + 审批记录（demo 环境归零）
biz_expense/biz_metric_daily demo 数据保留（数据面演示，用户拍板）。
谨慎：只删明确测试特征的行（李四/"家里有事"/探针单号前缀）。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    # 1. 请假测试单：李四体系 + 「家里有事」测试特征
    cur.execute("SELECT COUNT(*) FROM oa_leave_application "
                "WHERE user_name='李四' OR reason='家里有事'")
    n_leave = cur.fetchone()[0]
    cur.execute("DELETE FROM oa_leave_application WHERE user_name='李四' OR reason='家里有事'")
    print(f"请假测试单清理: {n_leave} 行")

    # 2. 王强假手机号
    cur.execute("UPDATE employee SET mobile=NULL WHERE name='王强'")
    print("王强 mobile 已清 NULL")

    # 3. 四张申请表测试数据 + 审批记录
    for t in ("expense_application", "travel_application",
              "purchase_application", "contract_seal_application"):
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        n = cur.fetchone()[0]
        cur.execute(f"DELETE FROM {t}")
        print(f"{t} 清理: {n} 行")
    cur.execute("SELECT COUNT(*) FROM oa_approval_record")
    n_rec = cur.fetchone()[0]
    cur.execute("DELETE FROM oa_approval_record")
    print(f"oa_approval_record 清理: {n_rec} 行")

    # 4. sop 表 steps_json 的 toolName 去 OA 口径（展示字段，运行时用代码）
    cur.execute("SELECT steps_json FROM sop WHERE code='leave_action'")
    row = cur.fetchone()
    if row and row[0]:
        new_json = row[0].replace("oa_employee_list", "employee_list").replace(
            "oa_leave_submit", "leave_insert")
        cur.execute("UPDATE sop SET steps_json=%s WHERE code='leave_action'", (new_json,))
        print("sop.steps_json 工具名去 OA 口径")
    conn.commit()
conn.close()
print("done")
