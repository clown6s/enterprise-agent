# -*- coding: utf-8 -*-
"""审批三件套扩展（T45，2026-08-19 去 OA 化）：
1. 建 4 张申请表（仿 oa_leave_application：申请号+申请人+业务字段+状态机+当前审批人）
2. sys_role 补 finance（财务）角色
3. sys_employee_role 绑定 赵敏→finance（demo 数据，后台可调）
4. oa_approval_flow 加 expense/travel/purchase/contract 四条审批链（存在即跳过，幂等）
状态机语义与 oa_leave_application 一致：0草稿 1待审批 2审批中 3已通过 4已拒绝 5已撤回。"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config  # noqa: E402
import pymysql  # noqa: E402

# 申请表：表名 → (注释, 业务列 DDL)
APP_TABLES = {
    "expense_application": (
        "报销申请表",
        "expense_type VARCHAR(64) COMMENT '报销类型',"
        "amount DECIMAL(12,2) COMMENT '金额(元)',"
        "expense_date DATE COMMENT '报销日期',"
        "reason VARCHAR(512) COMMENT '事由',"),
    "travel_application": (
        "出差申请表",
        "destination VARCHAR(128) COMMENT '目的地',"
        "start_date DATE COMMENT '出发日期',"
        "end_date DATE COMMENT '返回日期',"
        "reason VARCHAR(512) COMMENT '事由',"),
    "purchase_application": (
        "采购申请表",
        "item_name VARCHAR(128) COMMENT '采购物品',"
        "quantity DECIMAL(12,2) COMMENT '数量',"
        "amount DECIMAL(12,2) COMMENT '金额(元)',"
        "reason VARCHAR(512) COMMENT '事由',"),
    "contract_seal_application": (
        "合同用印申请表",
        "contract_name VARCHAR(128) COMMENT '合同名称',"
        "counterparty VARCHAR(128) COMMENT '对方单位',"
        "amount DECIMAL(12,2) COMMENT '合同金额(元)',"
        "reason VARCHAR(512) COMMENT '用印事由',"),
}

# 审批链：business_type → [(flow_name, level, approver_role_key, approver_dept_leader, order_num)]
APPROVAL_FLOWS = {
    "expense": [("报销审批", 1, "", 1, 1),
                ("报销审批", 2, "finance", 0, 2)],
    "travel": [("出差审批", 1, "", 1, 1)],
    "purchase": [("采购审批", 1, "", 1, 1),
                 ("采购审批", 2, "finance", 0, 2)],
    "contract": [("合同用印审批", 1, "", 1, 1),
                 ("合同用印审批", 2, "admin", 0, 2)],
}

conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                       password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                       charset="utf8mb4", connect_timeout=5)
with conn.cursor() as cur:
    # 1. 四张申请表
    for tname, (comment, biz_cols) in APP_TABLES.items():
        cur.execute(f"CREATE TABLE IF NOT EXISTS {tname} ("
                    "id INT AUTO_INCREMENT PRIMARY KEY,"
                    "apply_no VARCHAR(40) NOT NULL,"
                    "user_id INT COMMENT '申请人 employee.id',"
                    "user_name VARCHAR(64) COMMENT '申请人姓名',"
                    "dept_id INT COMMENT '部门 sys_dept.id',"
                    f"{biz_cols}"
                    "status TINYINT DEFAULT 1 COMMENT '0草稿 1待审批 2审批中 3已通过 4已拒绝 5已撤回',"
                    "current_approver_id INT COMMENT '当前审批人 employee.id',"
                    "create_time DATETIME DEFAULT CURRENT_TIMESTAMP,"
                    f"KEY idx_status (status)) COMMENT='{comment}'")
        print(f"表就绪: {tname}")

    # 2. finance 角色（存在跳过）
    cur.execute("SELECT id FROM sys_role WHERE role_key='finance'")
    if not cur.fetchone():
        cur.execute("INSERT INTO sys_role (role_name, role_key, status, create_time) "
                    "VALUES ('财务', 'finance', 1, NOW())")
        print("sys_role + finance")
    else:
        print("sys_role finance 已存在")

    # 3. 赵敏 → finance（幂等）
    cur.execute("SELECT r.id FROM sys_role r JOIN sys_employee_role er ON er.role_id=r.id "
                "JOIN employee e ON er.employee_id=e.id "
                "WHERE r.role_key='finance' AND e.name='赵敏'")
    if not cur.fetchone():
        cur.execute("SELECT id FROM employee WHERE name='赵敏'")
        emp_id = cur.fetchone()
        cur.execute("SELECT id FROM sys_role WHERE role_key='finance'")
        role_id = cur.fetchone()
        if emp_id and role_id:
            cur.execute("INSERT INTO sys_employee_role (employee_id, role_id) VALUES (%s,%s)",
                        (emp_id[0], role_id[0]))
            print("角色绑定: 赵敏 → finance")

    # 4. 审批链配置（存在即跳过——不删旧行，幂等）
    for btype, rows in APPROVAL_FLOWS.items():
        cur.execute("SELECT COUNT(*) FROM oa_approval_flow WHERE business_type=%s", (btype,))
        if cur.fetchone()[0] > 0:
            print(f"审批链已存在: {btype}")
            continue
        for flow_name, level, role_key, dept_leader, order_num in rows:
            cur.execute(
                "INSERT INTO oa_approval_flow (flow_name, business_type, level, "
                "approver_role_key, approver_dept_leader, order_num) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (flow_name, btype, level, role_key, dept_leader, order_num))
        print(f"审批链就绪: {btype} ({len(rows)} 级)")
    conn.commit()

    # 5. 输出现状
    cur.execute("SELECT business_type, level, approver_role_key, approver_dept_leader "
                "FROM oa_approval_flow ORDER BY business_type, level")
    print("\noa_approval_flow 现状:")
    for r in cur.fetchall():
        print("  ", r)
conn.close()
print("done")
