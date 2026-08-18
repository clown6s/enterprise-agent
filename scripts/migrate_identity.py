# -*- coding: utf-8 -*-
"""身份体系统一（2026-08-19 去 OA 化，T44）：
1. employee 加 dept_id 列（指向 sys_dept.id）——部门主数据从文本转外键
2. sys_dept 扩充 employee 的 6 个部门（研发/人力资源/市场/财务/运营/销售），leader 填姓名
3. employee 按部门文本回填 dept_id
4. sys_employee_role 语义重写：employee_id 改指向 employee 表（原指向 sys_employee，
   张三李四王五那套 OA 演示账号体系退役），绑定 demo 角色
幂等（重复执行不炸）。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

DEPT_ROWS = [
    # (dept_name, leader 姓名)
    ("研发部", "张伟"),
    ("人力资源部", "李娜"),
    ("市场部", "王强"),
    ("财务部", "赵敏"),
    ("运营部", "刘洋"),
    ("销售部", "周涛"),
]

# employee.name → 新部门名（用于回填 dept_id）
EMP_DEPT = {
    "张伟": "研发部", "李娜": "人力资源部", "王强": "市场部", "赵敏": "财务部",
    "陈杰": "研发部", "刘洋": "运营部", "孙悦": "研发部", "周涛": "销售部",
}

# employee.name → role_key（demo 角色分配；admin 角色留给后台账号体系）
EMP_ROLE = {
    "王强": "dept_manager",   # 市场经理
    "李娜": "hr_manager",     # HRBP → 人事经理
}

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    # 1. employee.dept_id 列
    cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                "AND TABLE_NAME='employee' AND COLUMN_NAME='dept_id'",
                (config.MYSQL_DB,))
    if cur.fetchone()[0] == 0:
        cur.execute("ALTER TABLE employee ADD COLUMN dept_id INT DEFAULT NULL")
        print("employee.dept_id 列已加")
    else:
        print("employee.dept_id 列已存在")

    # 2. sys_dept 扩充（重名跳过）
    dept_ids = {}
    for dname, leader in DEPT_ROWS:
        cur.execute("SELECT id FROM sys_dept WHERE dept_name=%s", (dname,))
        row = cur.fetchone()
        if row:
            dept_ids[dname] = row[0]
        else:
            cur.execute("INSERT INTO sys_dept (parent_id, dept_name, order_num, leader, phone, status, create_time) "
                        "VALUES (1, %s, 9, %s, '', 1, NOW())", (dname, leader))
            dept_ids[dname] = cur.lastrowid
            print(f"sys_dept +{dname} (id={dept_ids[dname]})")

    # 3. employee 回填 dept_id
    for emp_name, dname in EMP_DEPT.items():
        cur.execute("UPDATE employee SET dept_id=%s WHERE name=%s AND dept_id IS NULL",
                    (dept_ids[dname], emp_name))
    print("employee.dept_id 回填完成")

    # 4. sys_employee_role 重写（指向 employee 表 + 角色）
    cur.execute("DELETE FROM sys_employee_role")
    for emp_name, role_key in EMP_ROLE.items():
        cur.execute("SELECT id FROM employee WHERE name=%s", (emp_name,))
        er = cur.fetchone()
        cur.execute("SELECT id FROM sys_role WHERE role_key=%s", (role_key,))
        rr = cur.fetchone()
        if er and rr:
            cur.execute("INSERT INTO sys_employee_role (employee_id, role_id) VALUES (%s,%s)",
                        (er[0], rr[0]))
            print(f"角色绑定: {emp_name}(employee#{er[0]}) → {role_key}")
    conn.commit()

    # 5. 校验输出
    cur.execute("SELECT id, name, dept_id FROM employee ORDER BY id")
    print("\nemployee 现状:")
    for r in cur.fetchall():
        print("  ", r)
    cur.execute("SELECT er.employee_id, e.name, r.role_key FROM sys_employee_role er "
                "JOIN employee e ON er.employee_id=e.id JOIN sys_role r ON er.role_id=r.id")
    print("角色绑定现状:")
    for r in cur.fetchall():
        print("  ", r)
conn.close()
print("done")
