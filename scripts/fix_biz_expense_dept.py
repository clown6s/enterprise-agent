# -*- coding: utf-8 -*-
"""修复 biz_expense.dept_name 与 employee.dept 对齐（跨表 JOIN 前置）。
赵敏: 行政部→财务部，张伟: 销售部→研发部"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import config
import pymysql

def main():
    conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                           user="root", password=config.MYSQL_PASSWORD,
                           database=config.MYSQL_DB, charset="utf8mb4",
                           connect_timeout=5)
    with conn.cursor() as cur:
        # 查当前不匹配的记录
        cur.execute("""
            SELECT b.emp_name, b.dept_name AS biz_dept, e.dept AS emp_dept
            FROM biz_expense b
            JOIN employee e ON b.emp_name = e.name
            WHERE b.dept_name != e.dept
        """)
        mismatches = cur.fetchall()
        if not mismatches:
            print("所有 biz_expense 记录的 dept_name 已与 employee.dept 对齐")
            conn.close()
            return
        print(f"发现 {len(mismatches)} 条不匹配记录：")
        for row in mismatches:
            print(f"  {row[0]}: biz_expense.dept_name={row[1]} ≠ employee.dept={row[2]}")

        # 修正
        cur.execute("""
            UPDATE biz_expense b
            JOIN employee e ON b.emp_name = e.name
            SET b.dept_name = e.dept
            WHERE b.dept_name != e.dept
        """)
        fixed = cur.rowcount
        conn.commit()
        print(f"已修正 {fixed} 条记录")

        # 验证
        cur.execute("""
            SELECT b.emp_name, b.dept_name, e.dept
            FROM biz_expense b
            JOIN employee e ON b.emp_name = e.name
            WHERE b.dept_name != e.dept
        """)
        remaining = cur.fetchall()
        if remaining:
            print(f"仍有 {len(remaining)} 条不匹配！")
            for row in remaining:
                print(f"  {row[0]}: {row[1]} ≠ {row[2]}")
        else:
            print("验证通过：所有 biz_expense.dept_name 已与 employee.dept 对齐")
    conn.close()

if __name__ == "__main__":
    main()
