# -*- coding: utf-8 -*-
"""
SOP 状态机引擎（执行准确的核心）——组件化：步骤描述符 + 确定性 handler，
新增 SOP = 定义 steps 顺序 + 每步 handler + 注册表加一行（借鉴 ragflow agent
component 的结构；曾 SOP 逻辑硬编码在 execute 的 if 分支里，加流程要写整条）。

请假 SOP：查员工 → LLM 提取参数 → 确定性校验 → 提交前确认 → 提交 OA → 回执。
2026-08-19 去 OA 化：名册/提交从 OA 接口改直连本库（业务库即 rag_service）——
oa_leave_application 落单、oa_approval_flow 解析审批人、employee+sys_dept 是身份主数据。
关键设计（血泪教训）：
1. 校验失败是确定性的——失败信息直接输出，禁止让模型包装（模型曾把失败改写成"已提交"）
2. 每一步落执行审计（logs/execution.jsonl），入参出参可回放
3. 时间锚点注入提取提示词（模型不知道"今天"是哪天）
"""
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..time_context import now as business_now
from .. import config, mysql_store, llm
import logging
log = logging.getLogger(__name__)

_AUDIT_FILE = os.path.join(config.LOG_DIR, "execution.jsonl")

# ---------------- 执行审计 ----------------

def _audit(sop: str, step: str, input_: dict, output: str, seconds: float) -> None:
    entry = {"ts": business_now().isoformat(), "sop": sop, "step": step,
             "input": input_, "output": output, "seconds": round(seconds, 2)}
    try:
        os.makedirs(os.path.dirname(_AUDIT_FILE), exist_ok=True)
        with open(_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------- SOP 定义（组件化：步骤描述符 + 确定性 handler） ----------------
# 借鉴 ragflow agent component：新增 SOP = 定义步骤顺序 + 每个步骤的 handler，
# 注册表加一行即可——不用再写整条状态机代码（曾 SOP 逻辑硬编码在 execute 分支里）。

# 制度问法排除（宽词触发的代价）："报销流程是什么/采购需要什么材料"不进 SOP——
# 流程制度问题走知识问答。2026-08-19 变体矩阵抓出触发词覆盖不足后加宽词，
# 宽词必须配这个排除（曾"请假流程"无关键词不触发纯属运气）
_PROCESS_WORDS_SOP = re.compile(
    r"流程|制度|规定|标准|政策|条件|材料|模板|须知|注意事项|怎么|如何|多少|多少钱"
    r"|谁|哪个|最多|最少|比较|对比")


@dataclass
class SopDefinition:
    code: str
    name: str
    keywords: list[str]        # 触发词（含任一即进入 SOP）
    steps: list[str]           # 步骤名序列（执行顺序）
    handlers: dict             # 步骤名 → handler(state) → (终止?, 结果)
    finish: Callable | None = None   # 确认后的提交收尾 params,emp → result（待确认单分发用）
    confirm_detail: Callable | None = None   # params → 确认单详情文案（请假=起止日期等）
    # dsh 一-2 五段式 hook 槽（默认空=无 hook=行为不变）。
    # post_hooks: 结果产生后同步执行（如 fix_output 格式守卫）；每个独立 try/except。
    # observe_hooks: 提交成功后异步执行（如 graph-build/通知）；daemon 线程，失败不影响主结果。
    post_hooks: list = None    # list[Callable[[dict], dict]] → 改写后的 result
    observe_hooks: list = None    # list[Callable[[dict], None]] → 旁路副作用

    def matches(self, query: str) -> bool:
        if _PROCESS_WORDS_SOP.search(query):
            return False
        return any(k in query for k in self.keywords)


# ---------------- 管理后台开关接线 ----------------
# sop 表 enabled 开关（TTL 60s 缓存；曾只存储不消费——后台禁用请假 SOP，
# 运行时照常触发）。keywords 运行时仍用代码内触发词（改动是危险操作，
# 后台 keywords 字段为展示用途）。

_enabled_cache: dict = {"ts": 0.0, "val": {}}
_ENABLED_TTL = 60


def is_enabled(code: str = "") -> bool:
    """sop 表 enabled 开关（无记录=启用，表不可用=启用——软依赖）。
    2026-08-19 泛化：按 code 查（曾写死 leave_action，新 SOP 开关失灵）"""
    now = time.time()
    if now - _enabled_cache["ts"] > _ENABLED_TTL:
        _enabled_cache["val"] = {}
        _enabled_cache["ts"] = now
    if code in _enabled_cache["val"]:
        return _enabled_cache["val"][code]
    try:
        import pymysql
        conn = mysql_store.connection()
        with conn.cursor() as cur:
            cur.execute("SELECT enabled FROM sop WHERE code=%s", (code,))
            row = cur.fetchone()

        val = row is None or bool(row[0])
    except Exception:
        val = True
    _enabled_cache["val"][code] = val
    return val


def match(query: str) -> SopDefinition | None:
    for sop in SOP_REGISTRY:
        if sop.matches(query):
            return sop
    return None


# ---------------- 请假 SOP 步骤 ----------------


def _parse_json(s: str) -> dict | None:
    """容错 JSON 提取（模型可能包一层说明文字）"""
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except json.JSONDecodeError:
        return None


# ---------------- 提交前确认（人机协同闸门） ----------------
# 校验通过后先落"待确认单"，用户回复"确认"才提交 OA——高危写操作不再无人把关。
# MySQL 不可用时降级为直接提交（功能不因存储抖动而断）。

_CONFIRM_WORDS = {"确认", "确定", "好的", "可以", "提交", "是的", "确认提交", "确定提交"}
_CANCEL_WORDS = {"取消", "算了", "不用了", "不请了"}


def _pending_conn():
    import pymysql
    return mysql_store.connection()


def save_pending(chat_id: str, params: dict, emp: dict, sop_code: str = "leave_action") -> bool:
    """保存待确认单（业务码区分哪条 SOP；2026-08-19 泛化——曾只存请假，
    确认分发硬编码请假提交）；失败返回 False（调用方降级直接提交）。

    保存前过 pending_state.validate_change——用户源只允许改白名单字段；
    系统字段（sop_code/status 等）被剔除（dsh 一-5）。剔除而不是拒绝：
    save_pending 返回 False 会触发调用方降级直提，比重启确认闸门更糟。"""
    from . import pending_state as _ps
    plan = _ps.validate_change({}, params, source="user")
    if not plan.approved:
        _audit(sop_code, "pending_rejected", params,
               f"非法字段剔除: {plan.rejected_keys}", 0.0)
        params = {k: v for k, v in params.items() if k not in plan.rejected_keys}
        params = _ps.apply_plan(_ps.validate_change({}, params, source="user"))
    else:
        params = _ps.apply_plan(plan)
    try:
        conn = _pending_conn()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sop_pending ("
                "chat_id VARCHAR(64) PRIMARY KEY, params TEXT NOT NULL, emp TEXT NOT NULL,"
                "sop_code VARCHAR(32) DEFAULT 'leave_action',"
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)")
            # 老表补列（CREATE IF NOT EXISTS 不动已存在表）
            cur.execute("SELECT COUNT(*) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
                        "AND TABLE_NAME='sop_pending' AND COLUMN_NAME='sop_code'",
                        (config.MYSQL_DB,))
            if cur.fetchone()[0] == 0:
                cur.execute("ALTER TABLE sop_pending ADD COLUMN sop_code VARCHAR(32) "
                            "DEFAULT 'leave_action'")
            cur.execute(
                "REPLACE INTO sop_pending (chat_id, params, emp, sop_code, created_at) "
                "VALUES (%s,%s,%s,%s,NOW())",
                (chat_id, json.dumps(params, ensure_ascii=False),
                 json.dumps(emp, ensure_ascii=False), sop_code))
        conn.commit()

        return True
    except Exception as e:
        log.warning(f"待确认单保存失败（降级直接提交）: {e!r}")
        return False


def load_pending(chat_id: str) -> dict | None:
    try:
        conn = _pending_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT params, emp, sop_code, created_at FROM sop_pending "
                        "WHERE chat_id=%s", (chat_id,))
            row = cur.fetchone()

        if not row:
            return None
        # 过期清理：24 小时未确认的待确认单自动作废（曾无过期，僵尸单堆积）
        if row[3] and (business_now() - row[3]).total_seconds() > 86400:
            clear_pending(chat_id)
            return None
        return {"params": json.loads(row[0]), "emp": json.loads(row[1]),
                "sop_code": row[2] or "leave_action"}
    except Exception:
        return None


def clear_pending(chat_id: str) -> None:
    try:
        conn = _pending_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sop_pending WHERE chat_id=%s", (chat_id,))
        conn.commit()

    except Exception:
        pass


def try_confirm_or_cancel(chat_id: str, query: str) -> dict | None:
    """待确认单的确认/取消入口：命中返回 {answer, route}，否则 None。
    "确认"这类词只在有待确认单时生效，无单时落回闲聊等正常路由。
    2026-08-19 泛化：按 sop_code 在注册表找该 SOP 的 finish 提交——
    曾写死请假 _finish_submit，报销等新 SOP 确认会误提成请假单。"""
    q = (query or "").strip()
    if not chat_id or q not in _CONFIRM_WORDS | _CANCEL_WORDS:
        return None
    pending = load_pending(chat_id)
    if not pending:
        return None
    clear_pending(chat_id)
    if q in _CONFIRM_WORDS:
        sop_code = pending.get("sop_code") or "leave_action"
        finish = next((s.finish for s in SOP_REGISTRY if s.code == sop_code and s.finish), None)
        _audit(sop_code, "user_confirmed", pending["params"],
               pending["emp"].get("realName", ""), 0.0)
        if not finish:
            return {"answer": "该流程的提交通道未配置，请稍后再试。", "route": "SOP"}
        return finish(pending["params"], pending["emp"])
    return {"answer": "已取消，未提交申请。", "route": "SOP"}


# ---------------- 请假 SOP 步骤 handler（每步返回 (终止?, 结果)） ----------------

def _load_employees_db() -> list[dict]:
    """员工名册（employee 表，2026-08-19 去 OA 化：oa.employees() 退役）。
    返回与旧 OA 接口同构 [{id, realName, deptId}]——下游校验/提交零改动。
    dept_id 经迁移回填（migrate_identity 脚本）；库不可用返回 []。"""
    try:
        import pymysql
        conn = mysql_store.connection()
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, dept_id FROM employee WHERE status=1 ORDER BY id")
            rows = [{"id": r[0], "realName": r[1], "deptId": r[2]} for r in cur.fetchall()]

        return rows
    except Exception:
        return []


def _gen_apply_no(prefix: str) -> str:
    """申请单号：前缀+日期+8位随机hex（与历史 LEAVE20260818XXXXXXXX 同格式）"""
    return f"{prefix}{business_now().strftime('%Y%m%d')}{secrets.token_hex(4).upper()}"


def _resolve_approver(business_type: str, emp: dict) -> int | None:
    """审批人解析（确定性，去 OA 化后审批链在本库）：
    oa_approval_flow 按 level 取第一级——
    approver_dept_leader=1 → 申请人部门的负责人（sys_dept.leader 姓名 → employee.id）；
    否则 approver_role_key → sys_role → sys_employee_role → employee。
    无配置/无绑定返回 None（单子 status=1 停着，等审批入口接人）"""
    try:
        import pymysql
        conn = mysql_store.connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approver_dept_leader, approver_role_key FROM oa_approval_flow "
                "WHERE business_type=%s ORDER BY level LIMIT 1", (business_type,))
            row = cur.fetchone()
            if not row:
                return None
            if row[0]:  # 部门负责人
                cur.execute("SELECT leader FROM sys_dept WHERE id=%s",
                            (emp.get("deptId"),))
                leader = cur.fetchone()
                if leader and leader[0]:
                    cur.execute("SELECT id FROM employee WHERE name=%s AND status=1",
                                (leader[0],))
                    r2 = cur.fetchone()
                    if r2:
                        return r2[0]
                return None
            if row[1]:  # 角色 Key
                cur.execute(
                    "SELECT e.id FROM sys_employee_role er "
                    "JOIN sys_role r ON er.role_id = r.id "
                    "JOIN employee e ON er.employee_id = e.id "
                    "WHERE r.role_key=%s AND e.status=1 LIMIT 1", (row[1],))
                r2 = cur.fetchone()
                if r2:
                    return r2[0]

    except Exception:
        pass
    return None


def _h_load_employees(state: dict) -> tuple[bool, dict | None]:
    """步骤1：查员工名册（本库 employee 表；不可用即终止——确定性错误文案）"""
    start_ts = time.time()
    employees = _load_employees_db()
    _audit(state["sop"], "employee_list", {}, f"{len(employees)}人", time.time() - start_ts)
    if not employees:
        return True, {"answer": "服务暂不可用（员工目录未响应），请稍后再试。", "route": "SOP"}
    state["employees"] = employees
    return False, None


def _h_confirm(state: dict) -> tuple[bool, dict | None]:
    """提交前确认（人机协同闸门）：落待确认单等用户"确认"（带业务码）；
    存储失败降级为直接提交——功能不因 MySQL 抖动而断。
    2026-08-19 泛化：确认文案按 SOP 自身的 name/confirm_detail 拼——
    曾写死请假（startTime/endTime/"请假申请"），新 SOP 复用直接 KeyError。"""
    params, emp = state["params"], state["emp"]
    if state["chat_id"] and save_pending(state["chat_id"], params, emp, state["sop"]):
        _audit(state["sop"], "pending_confirm", params, emp.get("realName", ""), 0.0)
        defn = next((s for s in SOP_REGISTRY if s.code == state["sop"]), None)
        detail = ""
        if defn and defn.confirm_detail:
            try:
                detail = defn.confirm_detail(params)
            except Exception:
                detail = ""
        reason = (params.get("reason") or "").strip()
        text = f"请确认{defn.name if defn else '申请'}：{emp.get('realName')}"
        if detail:
            text += f"，{detail}"
        if reason:
            text += f"，事由：{reason}"
        text += "。\n回复「确认」提交，回复「取消」放弃。"
        return True, {"answer": text, "route": "SOP"}
    return False, None


SOP_REGISTRY: list[SopDefinition] = []


def register(definition: SopDefinition) -> None:
    """注册一条新 SOP（审批类 SOP 在 app/plan/approvals.py 定义并注册）。
    曾 SOP_REGISTRY 写死单列表——新流程只能改这里，注册函数化后各业务自行挂载。"""
    SOP_REGISTRY.append(definition)


def _apply_post_hooks(sop: SopDefinition, result: dict) -> dict:
    """执行 post_hooks：结果改写（如 fix_output 格式守卫）。每个独立容错。

    dsh 一-2 post-execute 切面：accept/replace/block，可注入附加上下文。
    空列表=无 hook=行为不变（向后兼容）。"""
    if not sop.post_hooks:
        return result
    for hook in sop.post_hooks:
        try:
            out = hook(result)
            if isinstance(out, dict):
                result = out   # replace
        except Exception as e:
            _audit(sop.code, "post_hook_error", {}, f"{e!r}", 0.0)
    return result


def _fire_observe_hooks(sop: SopDefinition, ctx: dict) -> None:
    """执行 observe_hooks：异步旁路（如 graph-build/通知），每个独立容错。

    dsh 一-2 observe 切面：逐监听器容错，旁路失败不影响主结果。
    同步执行（调用方如需异步可包 daemon thread）；空列表=无 hook=行为不变。"""
    if not sop.observe_hooks:
        return
    for hook in sop.observe_hooks:
        try:
            hook(ctx)
        except Exception as e:
            _audit(sop.code, "observe_hook_error", {}, f"{e!r}", 0.0)


def execute(sop: SopDefinition, query: str, chat_id: str = "") -> dict:
    """通用 SOP 执行器：按步骤描述符顺序执行，步骤返回 (终止?, 结果)。
    新增 SOP = 定义 steps 顺序 + 每步确定性 handler + 注册表加一行。
    state["sop"] 供 confirm 步骤落待确认单时区分业务码。
    结果产生后过 post_hooks（dsh 一-2 post 切面）。"""
    state = {"query": query, "chat_id": chat_id, "sop": sop.code}
    result = None
    for step_name in sop.steps:
        stop, result = sop.handlers[step_name](state)
        if stop:
            break
    if result is None:
        result = {"answer": "该流程未完成。", "route": "SOP"}
    result = _apply_post_hooks(sop, result)
    return result
