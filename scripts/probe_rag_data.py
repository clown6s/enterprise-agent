# -*- coding: utf-8 -*-
"""rag_service 关键表数据样例探针（只读）：审批流配置/角色/员工/部门/SOP/请假单/报销。
凭据从 app/config 读（.env），绝不内联。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)

TABLES = [
    ("oa_approval_flow", "SELECT * FROM oa_approval_flow ORDER BY order_num"),
    ("sys_role", "SELECT * FROM sys_role"),
    ("sys_employee", "SELECT id, username, real_name, dept_id, status FROM sys_employee"),
    ("sys_employee_role", "SELECT * FROM sys_employee_role"),
    ("sys_dept", "SELECT * FROM sys_dept"),
    ("sop", "SELECT id, code, name, keywords, steps_json, enabled, builtin FROM sop"),
    ("oa_leave_application", "SELECT * FROM oa_leave_application ORDER BY id DESC LIMIT 6"),
    ("oa_approval_record", "SELECT * FROM oa_approval_record ORDER BY id DESC LIMIT 6"),
    ("biz_expense", "SELECT * FROM biz_expense ORDER BY id DESC LIMIT 6"),
    ("employee", "SELECT id, name, dept, position, employee_no, status FROM employee"),
    ("sys_user", "SELECT id, username, display_name, role, employee_id, mobile FROM sys_user"),
    ("query_schema", "SELECT id, table_name, description, grants, status FROM query_schema"),
    ("capability", "SELECT code, pattern, priority, status, tool FROM capability"),
]

with conn.cursor() as cur:
    for name, sql in TABLES:
        cur.execute(sql)
        rows = cur.fetchall()
        print(f"\n===== {name} ({len(rows)} 行) =====")
        if cur.description:
            print("  | " + " | ".join(d[0] for d in cur.description))
        for r in rows:
            print("  | " + " | ".join("" if v is None else str(v) for v in r))
conn.close()
