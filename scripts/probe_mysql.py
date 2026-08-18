# -*- coding: utf-8 -*-
"""MySQL 全实例表扫描（只读）：列出所有库/表/列/估算行数，用于「哪些表用得着」盘点。
凭据从 app/config 读（.env），绝不内联。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

SKIP_DBS = {"information_schema", "performance_schema", "mysql", "sys"}

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    cur.execute("SHOW DATABASES")
    dbs = [r[0] for r in cur.fetchall() if r[0] not in SKIP_DBS]
    print("实例:", config.MYSQL_HOST, "| 业务库:", dbs)
    for db in dbs:
        cur.execute(
            "SELECT TABLE_NAME, TABLE_ROWS, TABLE_COMMENT FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME", (db,))
        tabs = cur.fetchall()
        print(f"\n[{db}] {len(tabs)} 张表:")
        for tname, rows, comment in tabs:
            cur.execute(
                "SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION", (db, tname))
            cols = cur.fetchall()
            colstr = ", ".join(f"{c[0]}[{c[2] or '-'}]" for c in cols)
            print(f"  {tname} | 行数≈{rows} | {comment or ''}\n    列: {colstr}")
conn.close()
