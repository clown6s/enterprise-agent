# -*- coding: utf-8 -*-
"""注册表描述补 JOIN 关联提示（跨表问法需要）：
employee.dept ↔ biz_expense.dept_name 同含义可关联——模型据此生成 JOIN。
幂等 upsert（register_biz_tables.py 同形态）。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    cur.execute(
        "UPDATE query_schema SET description=%s WHERE table_name='employee'",
        ("员工目录（姓名/部门/职位/入职/工号/电话）；dept 与 biz_expense.dept_name "
         "含义相同，跨表问法（如某部门的报销）可 JOIN 关联",))
    cur.execute(
        "UPDATE query_schema SET description=%s WHERE table_name='biz_expense'",
        ("员工报销记录（员工姓名/部门/报销类型/金额/报销日期/备注，金额单位元）；"
         "dept_name 与 employee.dept 含义相同，跨表问法可 JOIN 关联",))
    conn.commit()
conn.close()
print("done")
