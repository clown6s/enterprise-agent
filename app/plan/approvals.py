# -*- coding: utf-8 -*-
"""审批类 SOP 定义（2026-08-19 去 OA 化，T47）：报销 / 出差 / 采购 / 合同用印。
每类 = ApprovalDef（提取 schema/规则 + 确定性校验 + 落库表列映射 + 回执模板），
_build_sop 组装成 SopDefinition 挂进 sop.SOP_REGISTRY——
加新审批类型 = 加一个 ApprovalDef 并注册，不写状态机代码（组件化的类扩展，
与请假共用查名册/确认闸门/审批人解析/审计链路）。
状态机语义统一：status 1=待审批（提交时按审批流解析第一级审批人）。"""
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from .. import llm
from . import sop as _sop

# ---------------- 提取提示词模板（与请假同款时间锚点注入） ----------------

_EXTRACT_TMPL = """你是{name}信息提取器。今天是 {today}（星期{week}）。
从用户输入中提取{name}参数，输出严格 JSON（不要任何其他内容）：
{schema}
规则：
1. 员工姓名必须从这份名单中选（一字不差）：{names}；名单外的人名也要原样输出，由校验层拦截
2. "明天"= {tomorrow}，"后天"= {after_tomorrow}，"今天"= {today}
{extra_rules}
3. 提取不到 reason 就留空字符串，不要编造

用户输入：
{query}
"""


@dataclass
class ApprovalDef:
    code: str
    name: str
    keywords: list[str]        # 触发词（动作词收窄——"报销流程"这类制度问法不进 SOP）
    prefix: str                # 申请单号前缀
    table: str                 # 申请表名（代码内常量，非用户输入）
    business_type: str         # oa_approval_flow.business_type（审批链配置）
    fields: dict               # 提取字段 → (表列, 值转换)
    extract_schema: str        # 提取提示词 JSON 示例
    extra_rules: str           # 提取规则（业务特有）
    validate: Callable         # (params, employees) → 错误文案 | None
    receipt_detail: Callable   # (params) → 回执详情文本（给回执提示词拼）
    col_labels: dict = None    # 表列名 → 中文名（摘要文案用；None=用英文列名兜底）
    summary_cols: tuple = None # 摘要精选 DB 列子集（None=不展示摘要列）

    def prompt(self, query: str, names: str) -> str:
        now = datetime.now()
        return _EXTRACT_TMPL.format(
            name=self.name, today=now.strftime("%Y-%m-%d"),
            week="一二三四五六日"[now.weekday()],
            tomorrow=(now + timedelta(days=1)).strftime("%Y-%m-%d"),
            after_tomorrow=(now + timedelta(days=2)).strftime("%Y-%m-%d"),
            schema=self.extract_schema, extra_rules=self.extra_rules,
            names=names, query=query)


# ---------------- 确定性校验（通用 + 业务） ----------------

def _check_name(params: dict, employees: list[dict]) -> str | None:
    name = (params.get("userName") or "").strip()
    if not name:
        return "校验未通过：缺少申请人姓名。"
    if not any(e.get("realName") == name for e in employees):
        names = "、".join(e.get("realName", "") for e in employees)
        return f"校验未通过：员工「{name}」不在员工名册中（现有：{names}）。"
    return None


def _check_reason(params: dict) -> str | None:
    if not (params.get("reason") or "").strip():
        return "校验未通过：缺少事由。"
    return None


def _check_date(s: str) -> bool:
    try:
        datetime.fromisoformat((s or "").strip())
        return True
    except ValueError:
        return False


def _v_expense(params: dict, employees: list[dict]) -> str | None:
    if e := _check_name(params, employees):
        return e
    if not (params.get("expenseType") or "").strip():
        return "校验未通过：缺少报销类型（如交通费/办公用品）。"
    try:
        if float(params.get("amount") or 0) <= 0:
            return "校验未通过：报销金额必须大于 0。"
    except (TypeError, ValueError):
        return "校验未通过：报销金额无法识别。"
    if not _check_date(params.get("expenseDate") or ""):
        return "校验未通过：报销日期格式无法识别。"
    return _check_reason(params)


def _v_travel(params: dict, employees: list[dict]) -> str | None:
    if e := _check_name(params, employees):
        return e
    if not (params.get("destination") or "").strip():
        return "校验未通过：缺少出差目的地。"
    s, e2 = params.get("startDate") or "", params.get("endDate") or ""
    if not _check_date(s) or not _check_date(e2):
        return "校验未通过：出差日期格式无法识别，请用「明天」「8月20日」这类表述。"
    if datetime.fromisoformat(s.strip()) > datetime.fromisoformat(e2.strip()):
        return "校验未通过：返回日期早于出发日期。"
    return _check_reason(params)


def _v_purchase(params: dict, employees: list[dict]) -> str | None:
    if e := _check_name(params, employees):
        return e
    if not (params.get("itemName") or "").strip():
        return "校验未通过：缺少采购物品名称。"
    try:
        if float(params.get("quantity") or 0) <= 0:
            return "校验未通过：采购数量必须大于 0。"
        if float(params.get("amount") or 0) <= 0:
            return "校验未通过：采购金额必须大于 0。"
    except (TypeError, ValueError):
        return "校验未通过：采购数量/金额无法识别。"
    return _check_reason(params)


def _v_contract(params: dict, employees: list[dict]) -> str | None:
    if e := _check_name(params, employees):
        return e
    if not (params.get("contractName") or "").strip():
        return "校验未通过：缺少合同名称。"
    if not (params.get("counterparty") or "").strip():
        return "校验未通过：缺少对方单位。"
    try:
        if float(params.get("amount") or 0) <= 0:
            return "校验未通过：合同金额必须大于 0。"
    except (TypeError, ValueError):
        return "校验未通过：合同金额无法识别。"
    return _check_reason(params)


# ---------------- 四个 ApprovalDef ----------------

def _fnum(v):
    try:
        return float(str(v or 0).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _fstr(v) -> str:
    return "" if v is None else str(v).strip()


# 注册顺序 = 匹配优先级（"孙悦申请合同用印，天启科技采购合同30万"同时含
# "用印"和"采购"——曾采购排前面抢成采购申请单）。特化词（用印/盖章）
# 的 contract 排最前，动作语义优先于宽词
APPROVAL_DEFS: list[ApprovalDef] = [
    ApprovalDef(
        code="contract_action", name="合同用印申请", prefix="CONTRACT",
        table="contract_seal_application", business_type="contract",
        keywords=["用印", "盖章"],
        fields={"contractName": ("contract_name", _fstr),
                "counterparty": ("counterparty", _fstr),
                "amount": ("amount", _fnum), "reason": ("reason", _fstr)},
        extract_schema=('{{"userName": "员工姓名", "contractName": "合同名称", '
                        '"counterparty": "对方单位", "amount": 数字, "reason": "用印事由"}}'),
        extra_rules=("amount 是合同金额；counterparty 是签合同的对方单位"
                     "（\"与XX签的采购合同\"中 XX 是 counterparty，"
                     "合同名称通常含\"合同/协议\"字样）"),
        validate=_v_contract,
        receipt_detail=lambda p: (f"合同：{p.get('contractName', '')}，"
                                   f"对方：{p.get('counterparty', '')}"),
        col_labels={"contract_name": "合同", "counterparty": "对方",
                    "amount": "金额", "reason": "事由"},
        summary_cols=("contract_name", "counterparty", "reason")),
    ApprovalDef(
        code="expense_action", name="报销申请", prefix="EXPENSE",
        table="expense_application", business_type="expense",
        keywords=["报销", "报账"],
        fields={"expenseType": ("expense_type", _fstr), "amount": ("amount", _fnum),
                "expenseDate": ("expense_date", _fstr), "reason": ("reason", _fstr)},
        extract_schema=('{{"userName": "员工姓名", "expenseType": "报销类型", '
                        '"amount": 数字, "expenseDate": "yyyy-MM-dd", "reason": "事由"}}'),
        extra_rules="报销日期没说就默认今天；金额是报销总金额",
        validate=_v_expense,
        receipt_detail=lambda p: f"报销类型：{p.get('expenseType', '')}，金额：{p.get('amount', 0)} 元",
        col_labels={"expense_type": "类型", "amount": "金额",
                    "expense_date": "日期", "reason": "事由"},
        summary_cols=("expense_type", "amount", "reason")),
    ApprovalDef(
        code="travel_action", name="出差申请", prefix="TRAVEL",
        table="travel_application", business_type="travel",
        keywords=["出差"],
        fields={"destination": ("destination", _fstr), "startDate": ("start_date", _fstr),
                "endDate": ("end_date", _fstr), "reason": ("reason", _fstr)},
        extract_schema=('{{"userName": "员工姓名", "destination": "目的地", '
                        '"startDate": "yyyy-MM-dd", "endDate": "yyyy-MM-dd", "reason": "事由"}}'),
        extra_rules="出差当天往返时 endDate 与 startDate 相同",
        validate=_v_travel,
        receipt_detail=lambda p: (f"目的地：{p.get('destination', '')}，"
                                   f"时间：{p.get('startDate', '')} 至 {p.get('endDate', '')}"),
        col_labels={"destination": "目的地", "start_date": "出发日期",
                    "end_date": "返回日期", "reason": "事由"},
        summary_cols=("destination", "start_date", "reason")),
    ApprovalDef(
        code="purchase_action", name="采购申请", prefix="PURCHASE",
        table="purchase_application", business_type="purchase",
        keywords=["采购", "采买", "买办公", "买设备"],
        fields={"itemName": ("item_name", _fstr), "quantity": ("quantity", _fnum),
                "amount": ("amount", _fnum), "reason": ("reason", _fstr)},
        extract_schema=('{{"userName": "员工姓名", "itemName": "采购物品", '
                        '"quantity": 数字, "amount": 数字, "reason": "事由"}}'),
        extra_rules="quantity 是数量，amount 是总金额",
        validate=_v_purchase,
        receipt_detail=lambda p: (f"物品：{p.get('itemName', '')}，数量：{p.get('quantity', 0)}，"
                                   f"金额：{p.get('amount', 0)} 元"),
        col_labels={"item_name": "物品", "quantity": "数量",
                    "amount": "金额", "reason": "事由"},
        summary_cols=("item_name", "amount", "reason")),
]


# ---------------- 组装成 SopDefinition ----------------

def _do_insert(d: ApprovalDef, params: dict, emp: dict) -> dict:
    """写库提交（共享逻辑，确认闸门降级直提与确认后收尾复用）：
    INSERT 到 def.table，审批人按审批流第一级解析，status=1 待审批"""
    apply_no = _sop._gen_apply_no(d.prefix)
    columns = ["apply_no", "user_id", "user_name", "dept_id"]
    values = [apply_no, emp.get("id"), emp.get("realName"), emp.get("deptId")]
    for key, (col, conv) in d.fields.items():
        columns.append(col)
        values.append(conv(params.get(key)))
    approver = _sop._resolve_approver(d.business_type, emp)
    t0 = time.time()
    try:
        import pymysql
        from .. import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            sql = (f"INSERT INTO {d.table} ({', '.join(columns)}, status, "
                   "current_approver_id, create_time) VALUES ("
                   + ",".join(["%s"] * len(values)) + ",1,%s,NOW())")
            cur.execute(sql, [str(v) for v in values] + [approver])
        conn.commit()
        conn.close()
    except Exception as e:
        _sop._audit(d.code, "insert", params, f"DB 失败: {e!r}", time.time() - t0)
        return {"answer": f"{d.name}提交失败：服务未受理，请稍后再试。", "route": "SOP"}
    _sop._audit(d.code, "insert", params, apply_no, time.time() - t0)
    # 飞书通知审批人（软依赖：无绑定静默跳过，审批人可主动查待审批单）
    if approver:
        from . import approval as _approval
        _approval.notify_approver(approver, d.business_type,
                                  {"apply_no": apply_no, "user_name": emp.get("realName"),
                                   **{col: values[i + 4] for i, (key, (col, _)) in
                                      enumerate(d.fields.items())}})
    return {"apply_no": apply_no}


def _finish(d: ApprovalDef, params: dict, emp: dict) -> dict:
    """确认后收尾：提交 → 回执（try_confirm_or_cancel 按 sop_code 分发）"""
    out = _do_insert(d, params, emp)
    if "apply_no" not in out:
        return out
    receipt_prompt = (
        f"用户已成功提交{d.name}。请用一句话回复用户，必须包含以下信息：\n"
        f"申请单号：{out['apply_no']}；姓名：{emp.get('realName')}；"
        f"{d.receipt_detail(params)}。\n"
        "只输出这句话，不要编造其他信息（如审批人、预计通过时间）。")
    receipt = llm.chat_chain("", receipt_prompt)
    return {"answer": receipt.strip(), "route": "SOP"}


def _build_sop(d: ApprovalDef) -> _sop.SopDefinition:
    def h_extract(state):
        names = "、".join(e.get("realName", "") for e in state["employees"])
        t0 = time.time()
        raw = llm.verify_chat("", d.prompt(state["query"], names), temperature=0.1)
        state["params"] = _sop._parse_json(raw) or {}
        _sop._audit(d.code, "extract", {"query": state["query"]},
                    json.dumps(state["params"], ensure_ascii=False), time.time() - t0)
        return False, None

    def h_validate(state):
        t0 = time.time()
        error = d.validate(state["params"], state["employees"])
        _sop._audit(d.code, "validate", state["params"], error or "通过", time.time() - t0)
        if error:
            return True, {"answer": error, "route": "SOP"}
        state["emp"] = next(e for e in state["employees"]
                            if e.get("realName") == state["params"]["userName"].strip())
        return False, None

    def h_submit(state):
        out = _do_insert(d, state["params"], state["emp"])
        if "apply_no" in out:
            state["apply_no"] = out["apply_no"]
            return False, None
        return True, out

    def h_receipt(state):
        emp = state["emp"]
        receipt_prompt = (
            f"用户已成功提交{d.name}。请用一句话回复用户，必须包含以下信息：\n"
            f"申请单号：{state['apply_no']}；姓名：{emp.get('realName')}；"
            f"{d.receipt_detail(state['params'])}。\n"
            "只输出这句话，不要编造其他信息（如审批人、预计通过时间）。")
        receipt = llm.chat_chain("", receipt_prompt)
        return True, {"answer": receipt.strip(), "route": "SOP"}

    return _sop.SopDefinition(
        code=d.code, name=d.name, keywords=d.keywords,
        steps=["load_employees", "extract", "validate", "confirm", "submit", "receipt"],
        handlers={
            "load_employees": _sop._h_load_employees,
            "extract": h_extract,
            "validate": h_validate,
            "confirm": _sop._h_confirm,   # 共享：落待确认单（带业务码）
            "submit": h_submit,
            "receipt": h_receipt,
        },
        finish=lambda params, emp: _finish(d, params, emp),
        confirm_detail=d.receipt_detail)


for _def in APPROVAL_DEFS:
    _sop.register(_build_sop(_def))
