# -*- coding: utf-8 -*-
"""
意图路由规则表（P2 核心）：模式 → 处理器，按顺序匹配，命中即确定性作答。

设计目标：新增一类确定性意图 = 加一行 IntentRule，不改任何代码逻辑。
处理器返回 dict（{"answer": str, "chart": dict|None}）或 None（不命中→继续下一规则）。
流程制度问法（"报销流程"）在数据类处理器内自行排除——数据工具不碰制度问题。
"""
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from .. import demo_data, employee_store, tools
from ..oa_client import oa

# 结果形态：answer 必填；chart 供渠道端渲染图表卡（无图表为 None）
Result = dict | None
Handler = Callable[[str], Result]


@dataclass
class IntentRule:
    name: str
    pattern: str          # 触发正则
    handler: Handler
    priority: int = 100   # 数值越小越先匹配
    tool: str = ""        # 关联工具码（skill 表开关管控，空=不受管控）

    def matches(self, query: str) -> bool:
        return re.search(self.pattern, query) is not None


# ---------------- 处理器 ----------------

_DAYS = re.compile(r"(\d{1,3})\s*天")
_PROCESS_WORDS = re.compile(
    r"流程|制度|规定|标准|政策|怎么|如何|条件|材料|发票|格式|模板|须知|方法|注意事项"
)


def _days_of(query: str) -> int:
    m = _DAYS.search(query)
    return min(90, max(1, int(m.group(1)) if m else 7))


def _fmt(v) -> str:
    f = float(v or 0)
    return str(int(f)) if f == int(f) else str(f)


def _fnum(v) -> float:
    """安全转浮点（金额/数值可能带千分位等格式——转失败按 0，不让图表卡炸 500）"""
    try:
        return float(str(v or 0).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


_UNAVAILABLE = {"answer": "员工目录暂不可用，请稍后再试。", "chart": None}


def _directory_text(dept: str | None = None) -> str:
    """员工目录文本（从 employee 表拼，格式与种子时代 demo_data 一致）"""
    rows = employee_store.all_active()
    if rows is None:
        return ""
    if dept:
        rows = [r for r in rows if r["dept"] == dept]
    return "\n".join(f"- {r['name']}（{r['dept']} {r['position']}）{r['entry']} 入职"
                     for r in rows)


def _info_text(r: dict) -> str:
    return f"{r['name']}（{r['dept']} {r['position']}），{r['entry']} 入职，工号 {r['employee_no']}"


def headcount(query: str, user: str = "") -> Result:
    """人数统计（含部门级："研发部有几个人"）——employee 表实时数据"""
    rows = employee_store.all_active()
    if rows is None:
        return _UNAVAILABLE
    dept = next((d for d in {r["dept"] for r in rows} if d and d in query), None)
    if dept:
        count = sum(1 for r in rows if r["dept"] == dept)
        return {"answer": f"{dept}共有 {count} 人：\n" + _directory_text(dept), "chart": None}
    return {"answer": f"根据员工目录，公司共有 {len(rows)} 位员工：\n"
                      + _directory_text(), "chart": None}


def employee_info(query: str, user: str = "") -> Result:
    """个人信息问法（入职/工号/部门/职位…），姓名必须在问句中出现"""
    names = employee_store.names()
    if names is None:
        return _UNAVAILABLE
    hits = [employee_store.by_name(n) for n in names if n in query]
    hits = [h for h in hits if h]
    if not hits:
        return None
    return {"answer": (_info_text(hits[0]) + "。" if len(hits) == 1
                      else "\n".join(_info_text(h) for h in hits)), "chart": None}


def employee_contact(query: str, user: str = "") -> Result:
    """联系方式问法（电话/手机/号码…）：姓名必须在问句中出现——
    不报全员通讯录（隐私）；未录入明确说未录入（不编造）。
    名单外姓名也要确定性接管（答"未在目录"）——曾漏到图谱兜底，
    "张三的电话"被图谱"电话"节点接住答出无关内容的实况。
    字段级权限：employee.mobile 有限制时未授权用户视同未录入。"""
    from ..service import acl
    names = employee_store.names()
    if names is None:
        return _UNAVAILABLE
    name = next((n for n in names if n in query), None)
    if name:
        r = employee_store.by_name(name)
        if r and r.get("mobile") and acl.field_visible("employee", "mobile", user):
            return {"answer": f"{name}的电话是 {r['mobile']}。", "chart": None}
        return {"answer": f"{name}的电话尚未录入员工目录，可联系管理员在后台维护。", "chart": None}
    # 名单没命中但有姓名形态片段（2-4 字）→ 明确答复，不漏给图谱/KB
    m = re.search(r"[一-龥]{2,4}",
                  re.sub(r"(电话|手机|联系方式|号码|多少|是多少|请问|的)", " ", query))
    if m:
        return {"answer": f"未在员工目录中找到「{m.group()}」的登记信息，无法查询联系方式。",
                "chart": None}
    return None


def oa_expenses(query: str, user: str = "") -> Result:
    """报销数据：带姓名查该员工，否则全员；流程制度问法不碰数据"""
    if _PROCESS_WORDS.search(query):
        return None
    days = _days_of(query)
    # 名册口径与员工目录统一（employee 表；目录不可用时回退种子名单仅做姓名识别）
    name = next((n for n in (employee_store.names() or demo_data.names()) if n in query), None)
    data = oa.expenses(days, name)
    d = (data or {}).get("data") or {}
    if not isinstance(d, dict) or not d.get("items"):
        return None
    who = name or "全员"
    head = f"{who}最近 {days} 天共报销 {_fmt(d.get('total', 0))} 元，共 {d.get('count', 0)} 笔，明细如下：\n"
    lines, dept, same_dept = [], None, True
    for it in d["items"]:
        dn = it.get("deptName", "")
        if dept is None:
            dept = dn
        elif dept != dn:
            same_dept = False
        remark = it.get("remark") or ""
        line = (f"- {it.get('d')}：{it.get('expenseType')}"
                + (f"（{remark}）" if remark else "")
                + f"，金额 **{_fmt(it.get('amount'))} 元**")
        if name is None and it.get("empName"):
            line += f"（{it['empName']}）"
        lines.append(line)
    text = head + "\n".join(lines)
    if name and same_dept and dept:
        text += f"\n全部报销均归属{dept}。"
    table = [["日期", "类型", "金额", "备注"]]
    values = []
    for it in d["items"]:
        table.append([it.get("d"), it.get("expenseType"), str(it.get("amount")), it.get("remark", "")])
        values.append({"date": it.get("d"), "series": it.get("expenseType"), "value": _fnum(it.get("amount"))})
    chart = {
        "kind": "expenses",
        "chartType": "bar",
        "values": values,
        "title": f"{(name or '全员')}近{d['count']}笔报销（元）",
        "table": table,
        "summary": f"合计 {d.get('total')} 元，共 {d.get('count')} 笔",
    }
    return {"answer": text, "chart": chart}


def oa_metrics(query: str, user: str = "") -> Result:
    """经营数据：营收/成本/利润 + 图表卡"""
    if _PROCESS_WORDS.search(query):
        return None
    days = _days_of(query)
    data = oa.metrics(days)
    d = (data or {}).get("data") or {}
    if not isinstance(d, dict) or not d.get("dates"):
        return None
    dates = d["dates"]
    s = d.get("summary", {})
    text = (f"近 {days} 天（{dates[0]} 至 {dates[-1]}）经营数据，"
            f"营收合计 {_fmt(s.get('revenue'))} 元，成本 {_fmt(s.get('cost'))} 元，"
            f"利润 {_fmt(s.get('profit'))} 元。每日明细见下表。")
    table = [["日期", "营收", "成本", "利润"]]
    values = []
    # 按最短数组对齐（OA 返回的 dates 与数值数组长度不一致时曾 IndexError 炸 500）
    n = min(len(dates), len(d.get("revenue") or []), len(d.get("cost") or []),
            len(d.get("profit") or []))
    for i in range(n):
        date = dates[i]
        table.append([date, str(d["revenue"][i]), str(d["cost"][i]), str(d["profit"][i])])
        for name, key in (("营收", "revenue"), ("成本", "cost"), ("利润", "profit")):
            values.append({"date": date, "series": name, "value": _fnum(d[key][i])})
    chart = {
        "kind": "metrics",
        "chartType": "line",
        "values": values,
        "title": f"近{len(dates)}天经营数据（元）",
        "table": table,
        "summary": f"合计：营收 {s.get('revenue')}，成本 {s.get('cost')}，利润 {s.get('profit')}",
    }
    return {"answer": text, "chart": chart}


# ---------------- 规则表 ----------------

INTENT_RULES: list[IntentRule] = [
    IntentRule("员工人数", r"多少人|有几个人|有几名|几名员工|员工人数|员工总数|人数是多少",
               headcount, priority=10, tool="employee_directory"),
    IntentRule("员工信息", r"入职|工号|部门|职位|岗位|什么时候|何时|哪一年|薪资|工资",
               employee_info, priority=20, tool="employee_directory"),
    IntentRule("员工联系方式", r"电话|手机|联系方式|号码",
               employee_contact, priority=21, tool="employee_directory"),
    IntentRule("OA报销", r"报销|花了多少钱|费用明细|开支", oa_expenses, priority=30,
               tool="oa_expenses"),
    IntentRule("经营数据", r"经营数据|营收|成本|利润|业绩|销售额|经营情况|生意",
               oa_metrics, priority=40, tool="oa_metrics"),
]


def match(query: str) -> Result:
    """按优先级顺序匹配规则表；全部不命中返回 None（走常规 QA/闲聊路径）。
    被管理后台禁用（skill 表 status=0）的工具规则直接跳过。"""
    for rule in sorted(INTENT_RULES, key=lambda r: r.priority):
        if rule.tool and not tools.is_enabled(rule.tool):
            continue
        if rule.matches(query):
            result = rule.handler(query)
            if result is not None:
                return result
    return None
