# -*- coding: utf-8 -*-
"""
幂等性 DB 约束补丁（dsh 一-5 思想 + 幂等性契约）：

为申请表 apply_no、审批记录 (apply_no, approval_level)、反馈采纳 query_hash
补 UNIQUE 索引。执行前先扫描重复数据；扫描/执行均幂等可重复运行。

用法：python scripts/patch_idempotent_constraints.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pymysql  # noqa: E402

from app import config  # noqa: E402

# 表 → 需要 UNIQUE 的列（补丁只补缺失的，已有索引跳过）
_PATCHES = {
    "expense_application": ("uk_apply_no", ["apply_no"]),
    "travel_application": ("uk_apply_no", ["apply_no"]),
    "purchase_application": ("uk_apply_no", ["apply_no"]),
    "contract_seal_application": ("uk_apply_no", ["apply_no"]),
    "oa_approval_record": ("uk_apply_level", ["apply_no", "approval_level"]),
}


def _conn():
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=5)


def _table_indexes(cur, table):
    cur.execute("SHOW INDEX FROM `%s`" % table)
    cols = {}
    for r in cur.fetchall():
        idx_name = r[2]
        cols.setdefault(idx_name, []).append(r[4])
    return {k: sorted(v) for k, v in cols.items()}


def _duplicates(cur, table, cols):
    col_sql = ", ".join(cols)
    cur.execute(
        "SELECT COUNT(*) FROM (SELECT %s FROM `%s` GROUP BY %s HAVING COUNT(*)>1) x"
        % (col_sql, table, col_sql))
    return cur.fetchone()[0]


def _apply(cur, conn):
    for table, (idx_name, cols) in _PATCHES.items():
        indexes = _table_indexes(cur, table)
        if idx_name in indexes:
            print(f"✓ {idx_name} 已存在，跳过")
            continue
        dup = _duplicates(cur, table, cols)
        if dup:
            print(f"✗ {table} 存在 {dup} 个重复（{cols}），先清理再补索引")
            continue
        col_sql = ", ".join("`%s`" % c for c in cols)
        cur.execute(
            "ALTER TABLE `%s` ADD UNIQUE KEY `%s` (%s)" % (table, idx_name, col_sql))
        conn.commit()
        print(f"✓ {table} 已加 {idx_name} ({cols})")


def main():
    c = _conn()
    try:
        with c.cursor() as cur:
            _apply(cur, c)
    finally:
        c.close()


if __name__ == "__main__":
    main()