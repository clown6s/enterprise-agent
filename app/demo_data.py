# -*- coding: utf-8 -*-
"""
员工目录演示数据（Java 版 DemoMcpData 的移植）。
测试环境没有真实人事系统时，员工目录问答（"公司有多少人""王强什么时候入职的"）
走这份确定性数据——不依赖模型调工具，答案是算出来的，不会漂。
"""

EMPLOYEES = [
    {"name": "张伟", "dept": "研发部", "position": "高级工程师", "entry": "2021-03-15", "employee_no": "E001"},
    {"name": "李娜", "dept": "人力资源部", "position": "HRBP", "entry": "2022-07-01", "employee_no": "E002"},
    {"name": "王强", "dept": "市场部", "position": "市场经理", "entry": "2020-05-20", "employee_no": "E003"},
    {"name": "赵敏", "dept": "财务部", "position": "会计主管", "entry": "2021-11-08", "employee_no": "E004"},
    {"name": "陈杰", "dept": "研发部", "position": "架构师", "entry": "2019-04-12", "employee_no": "E005"},
    {"name": "刘洋", "dept": "运营部", "position": "运营专员", "entry": "2023-02-27", "employee_no": "E006"},
    {"name": "孙悦", "dept": "研发部", "position": "前端工程师", "entry": "2022-09-05", "employee_no": "E007"},
    {"name": "周涛", "dept": "销售部", "position": "大客户销售", "entry": "2021-06-30", "employee_no": "E008"},
]

DEPTS = sorted({e["dept"] for e in EMPLOYEES})


def directory(dept: str | None = None) -> str:
    """员工名单文本（dept=None 全员），每行一人：姓名（部门 职位）入职时间"""
    rows = [e for e in EMPLOYEES if dept is None or e["dept"] == dept]
    lines = [f"- {e['name']}（{e['dept']} {e['position']}）{e['entry']} 入职" for e in rows]
    return "\n".join(lines)


def lookup(name: str) -> str | None:
    """单人信息文本，如 "王强（市场部 市场经理），2020-05-20 入职，工号 E003" """
    for e in EMPLOYEES:
        if e["name"] == name:
            return f"{name}（{e['dept']} {e['position']}），{e['entry']} 入职，工号 {e['employee_no']}"
    return None


def names() -> list[str]:
    return [e["name"] for e in EMPLOYEES]
