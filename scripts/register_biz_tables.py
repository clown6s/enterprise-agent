# -*- coding: utf-8 -*-
"""注册 biz_expense / biz_metric_daily 进 query_schema（NL2SQL 数据面）。
去 OA 化后报销/经营问答由 NL2SQL 接管，能力总线对应规则退役。
幂等 upsert；列描述是中文标签尾缀匹配的依据，改列前先跑回归。"""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

ROWS = [
    ("biz_expense", "员工报销记录（员工姓名/部门/报销类型/金额/报销日期/备注，金额单位元）",
     [{"name": "emp_name", "desc": "员工姓名", "sample": "赵敏"},
      {"name": "dept_name", "desc": "部门", "sample": "行政部"},
      {"name": "expense_type", "desc": "报销类型", "sample": "交通费"},
      {"name": "amount", "desc": "金额", "sample": "96.00"},
      {"name": "expense_date", "desc": "报销日期", "sample": "2026-08-16"},
      {"name": "remark", "desc": "备注", "sample": "行政采购打车"}]),
    ("biz_metric_daily", "每日经营数据（日期/营收/成本/利润，单位元）",
     [{"name": "metric_date", "desc": "日期", "sample": "2026-08-01"},
      {"name": "revenue", "desc": "营收", "sample": "120000"},
      {"name": "cost", "desc": "成本", "sample": "80000"},
      {"name": "profit", "desc": "利润", "sample": "40000"}]),
]

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    for table_name, description, columns in ROWS:
        cur.execute(
            "INSERT INTO query_schema (table_name, description, columns_json, grants) "
            "VALUES (%s, %s, %s, '') ON DUPLICATE KEY UPDATE "
            "description=VALUES(description), columns_json=VALUES(columns_json)",
            (table_name, description, json.dumps(columns, ensure_ascii=False)))
        print(f"registered: {table_name}")
    conn.commit()
conn.close()
print("done")
