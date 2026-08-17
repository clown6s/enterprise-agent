# -*- coding: utf-8 -*-
"""
SOP 状态机引擎（执行准确的核心）。

请假 SOP 四步：查员工 → LLM 提取参数 → 确定性校验 → 提交 OA → 回执。
关键设计（血泪教训）：
1. 校验失败是确定性的——失败信息直接输出，禁止让模型包装（模型曾把失败改写成"已提交"）
2. 每一步落执行审计（logs/execution.jsonl），入参出参可回放
3. 时间锚点注入提取提示词（模型不知道"今天"是哪天）
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import config, llm
from ..oa_client import oa

_AUDIT_FILE = os.path.join(config.LOG_DIR, "execution.jsonl")

# ---------------- 执行审计 ----------------

def _audit(sop: str, step: str, input_: dict, output: str, seconds: float) -> None:
    entry = {"ts": datetime.now().isoformat(), "sop": sop, "step": step,
             "input": input_, "output": output, "seconds": round(seconds, 2)}
    try:
        os.makedirs(os.path.dirname(_AUDIT_FILE), exist_ok=True)
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------- SOP 定义 ----------------

@dataclass
class SopDefinition:
    code: str
    name: str
    keywords: list[str]        # 触发词（含任一即进入 SOP）

    def matches(self, query: str) -> bool:
        return any(k in query for k in self.keywords)


# 请假触发词
_LEAVE_KEYWORDS = ["提交请假", "帮我请假", "帮我请", "申请年假", "请事假", "请病假", "请年假",
                   "请调休", "请个假", "请一天假", "请两天假", "请三天假", "请半天假",
                   "请假一天", "请假两天", "请假三天"]

LEAVE_SOP = SopDefinition(code="leave_action", name="请假申请", keywords=_LEAVE_KEYWORDS)

SOP_REGISTRY = [LEAVE_SOP]


def match(query: str) -> SopDefinition | None:
    for sop in SOP_REGISTRY:
        if sop.matches(query):
            return sop
    return None


# ---------------- 请假 SOP 步骤 ----------------

_EXTRACT_PROMPT = """你是请假信息提取器。今天是 {today}（星期{week}）。
从用户输入中提取请假参数，输出严格 JSON（不要任何其他内容）：
{{"userName": "员工姓名", "startTime": "yyyy-MM-dd", "endTime": "yyyy-MM-dd",
  "days": 数字, "leaveType": 数字, "reason": "事由"}}
规则：
1. 员工姓名必须从这份名单中选（一字不差）：{names}；名单外的人名也要原样输出，由校验层拦截
2. "明天"= {tomorrow}，"后天"= {after_tomorrow}，"今天"= {today}；"下午/上午/半天"记 days=0.5
3. days 必须与 startTime/endTime 一致（同一天=1，两天=2）
4. leaveType：1事假 2病假 3年假；没说类型默认 1
5. 提取不到 reason 就留空字符串，不要编造

用户输入：
{query}
"""


def _parse_json(s: str) -> dict | None:
    """容错 JSON 提取（模型可能包一层说明文字）"""
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


def _validate_leave(params: dict, employees: list[dict]) -> str | None:
    """
    确定性校验规则（校验不通过返回错误文案，通过返回 None）。
    这些规则是公司制度的确定性表达——不需要模型参与判断。
    """
    name = (params.get("userName") or "").strip()
    if not name:
        return "校验未通过：缺少请假人姓名。"
    if not any(e.get("realName") == name for e in employees):
        names = "、".join(e.get("realName", "") for e in employees)
        return f"校验未通过：员工「{name}」不在员工名册中（现有：{names}）。"
    start = params.get("startTime") or ""
    end = params.get("endTime") or ""
    if not start or not end:
        return "校验未通过：缺少请假起止日期，请说明请哪天到哪天。"
    try:
        d0, d1 = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return "校验未通过：日期格式无法识别，请用「明天」「8月18日」这类表述。"
    if d1 < d0:
        return "校验未通过：结束日期早于开始日期。"
    days = params.get("days")
    try:
        days = float(days) if days not in (None, "") else (d1 - d0).days + 1
    except (TypeError, ValueError):
        return "校验未通过：请假天数无法识别。"
    if days < 0.5 or days > 30:
        return f"校验未通过：请假天数 {days:g} 天超出范围（0.5~30 天）。"
    if not (params.get("reason") or "").strip():
        return "校验未通过：缺少请假事由。"
    return None


# ---------------- 提交前确认（人机协同闸门） ----------------
# 校验通过后先落"待确认单"，用户回复"确认"才提交 OA——高危写操作不再无人把关。
# MySQL 不可用时降级为直接提交（功能不因存储抖动而断）。

_CONFIRM_WORDS = {"确认", "确定", "好的", "可以", "提交", "是的", "确认提交", "确定提交"}
_CANCEL_WORDS = {"取消", "算了", "不用了", "不请了"}


def _pending_conn():
    import pymysql
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=3)


def save_pending(chat_id: str, params: dict, emp: dict) -> bool:
    """保存待确认请假单；失败返回 False（调用方降级直接提交）"""
    try:
        conn = _pending_conn()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sop_pending ("
                "chat_id VARCHAR(64) PRIMARY KEY, params TEXT NOT NULL, emp TEXT NOT NULL,"
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
            cur.execute(
                "REPLACE INTO sop_pending (chat_id, params, emp, created_at) VALUES (%s,%s,%s,NOW())",
                (chat_id, json.dumps(params, ensure_ascii=False), json.dumps(emp, ensure_ascii=False)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"[sop] 待确认单保存失败（降级直接提交）: {e!r}")
        return False


def load_pending(chat_id: str) -> dict | None:
    try:
        conn = _pending_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT params, emp, created_at FROM sop_pending WHERE chat_id=%s",
                        (chat_id,))
            row = cur.fetchone()
        conn.close()
        if not row:
            return None
        # 过期清理：24 小时未确认的待确认单自动作废（曾无过期，僵尸单堆积）
        if row[2] and (datetime.now() - row[2]).total_seconds() > 86400:
            clear_pending(chat_id)
            return None
        return {"params": json.loads(row[0]), "emp": json.loads(row[1])}
    except Exception:
        return None


def clear_pending(chat_id: str) -> None:
    try:
        conn = _pending_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sop_pending WHERE chat_id=%s", (chat_id,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def try_confirm_or_cancel(chat_id: str, query: str) -> dict | None:
    """待确认单的确认/取消入口：命中返回 {answer, route}，否则 None。
    "确认"这类词只在有待确认单时生效，无单时落回闲聊等正常路由。"""
    q = (query or "").strip()
    if not chat_id or q not in _CONFIRM_WORDS | _CANCEL_WORDS:
        return None
    pending = load_pending(chat_id)
    if not pending:
        return None
    clear_pending(chat_id)
    if q in _CONFIRM_WORDS:
        _audit("leave_action", "user_confirmed", pending["params"], pending["emp"].get("realName", ""), 0.0)
        return _submit_leave(pending["params"], pending["emp"])
    return {"answer": "已取消，未提交请假申请。", "route": "SOP"}


def _execute_leave(query: str, chat_id: str = "") -> dict:
    """请假 SOP 主流程：查名册 → 提取 → 校验 →（提交前确认）→ 提交 → 回执"""
    start_ts = time.time()
    # 步骤1：查员工名册
    employees = oa.employees() or []
    if not employees:
        return {"answer": "请假服务暂不可用（OA 系统未响应），请稍后再试。", "route": "SOP"}
    _audit("leave_action", "oa_employee_list", {}, f"{len(employees)}人", time.time() - start_ts)

    # 步骤2：LLM 提取参数（时间锚点注入）
    now = datetime.now()
    names = "、".join(e.get("realName", "") for e in employees)
    t0 = time.time()
    raw = llm.chat(
        "", _EXTRACT_PROMPT.format(
            today=now.strftime("%Y-%m-%d"), week="一二三四五六日"[now.weekday()],
            tomorrow=(now + timedelta(days=1)).strftime("%Y-%m-%d"),
            after_tomorrow=(now + timedelta(days=2)).strftime("%Y-%m-%d"),
            names=names, query=query),
        model=config.VERIFY_MODEL, base=config.DASHSCOPE_BASE, key=config.DASHSCOPE_KEY,
        temperature=0.1,
    )
    params = _parse_json(raw) or {}
    _audit("leave_action", "extract", {"query": query}, json.dumps(params, ensure_ascii=False),
           time.time() - t0)

    # 步骤3：确定性校验（失败即终止，不做 LLM 包装）
    t0 = time.time()
    error = _validate_leave(params, employees)
    _audit("leave_action", "validate", params, error or "通过", time.time() - t0)
    if error:
        # 防"编造成功"：校验失败信息确定性直出，模型没有机会改写成"已提交"
        return {"answer": error, "route": "SOP"}

    emp = next(e for e in employees if e.get("realName") == params["userName"].strip())

    # 提交前确认（人机协同闸门）：落待确认单，等用户"确认"；
    # 存储失败降级为直接提交——功能不因 MySQL 抖动而断
    if chat_id and save_pending(chat_id, params, emp):
        _audit("leave_action", "pending_confirm", params, emp.get("realName", ""), 0.0)
        return {"answer": (f"请确认请假申请：{emp.get('realName')}，"
                           f"{params['startTime']} 至 {params['endTime']}，"
                           f"事由：{params['reason'].strip()}。\n"
                           f"回复「确认」提交，回复「取消」放弃。"), "route": "SOP"}
    return _submit_leave(params, emp)


def _submit_leave(params: dict, emp: dict) -> dict:
    """步骤4-5：提交 OA + 回执（参数已通过确定性校验；确认闸门之后或降级直提）"""
    start_date = datetime.fromisoformat(params["startTime"])
    end_date = datetime.fromisoformat(params["endTime"])
    payload = {
        "userId": emp.get("id"),
        "userName": emp.get("realName"),
        "deptId": emp.get("deptId"),
        "leaveType": int(params.get("leaveType") or 1),
        "startTime": start_date.strftime("%Y-%m-%dT00:00:00"),
        "endTime": end_date.strftime("%Y-%m-%dT23:59:59"),
        "reason": params["reason"].strip(),
    }
    t0 = time.time()
    resp = oa.leave_submit(payload)
    _audit("leave_action", "oa_leave_submit", payload,
           json.dumps(resp, ensure_ascii=False)[:200], time.time() - t0)
    if not resp or resp.get("code") != 200:
        return {"answer": "请假提交失败：OA 系统未受理，请稍后再试。", "route": "SOP"}
    apply_no = resp.get("data") or ""

    # 回执（数据全来自 OA 返回，模型只做措辞包装）
    receipt_prompt = (
        "用户已成功提交请假申请。请用一句话回复用户，必须包含以下信息：\n"
        f"申请单号：{apply_no}；姓名：{emp.get('realName')}；"
        f"时间：{params['startTime']} 至 {params['endTime']}；"
        f"事由：{params['reason'].strip()}。\n"
        "只输出这句话，不要编造其他信息（如审批人、预计通过时间）。")
    receipt = llm.chat("", receipt_prompt, model=config.MAIN_MODEL,
                       base=config.MAIN_BASE, key=config.MAIN_KEY)
    return {"answer": receipt.strip(), "route": "SOP"}


def execute(sop: SopDefinition, query: str, chat_id: str = "") -> dict:
    if sop.code == "leave_action":
        return _execute_leave(query, chat_id)
    return {"answer": "该流程暂未实现。", "route": "SOP"}
