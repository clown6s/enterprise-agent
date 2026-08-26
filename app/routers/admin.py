# -*- coding: utf-8 -*-
"""
管理后台 API 全量（rag-admin 前端复用，共用 MySQL 表）。
覆盖页面：仪表盘 / 审计看板 / 告警中心 / 评测中心 / 数字员工 / 技能管理 /
SOP 管理 / 答案审核 / 文档权限 / 知识库健康度 / 用量看板 / 账号管理。
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime

from ..time_context import now as business_now
import pymysql
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from .. import config, milvus_store, mysql_store
from ..learn import feedback, gap_discovery, usage
from ..service import agent, graph
import logging
log = logging.getLogger(__name__)

router = APIRouter()

EVAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "eval")
EVAL_SETS_DIR = os.path.join(EVAL_DIR, "sets")


# ---------------- 基础设施 ----------------

def _auth(authorization: str | None) -> dict | None:
    token = (authorization or "").removeprefix("Bearer ")
    return mysql_store.resolve_token(token)


def _unauthorized():
    return JSONResponse(
        {"success": False, "message": "未登录或会话已过期，请重新登录", "data": None},
        status_code=401,
    )


def _ok(data):
    return {"success": True, "message": None, "data": data}


def _query(sql: str, args: tuple = ()) -> list:
    try:
        with mysql_store.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
                return cur.fetchall()
    except pymysql.MySQLError:
        return []


def _exec(sql: str, args: tuple = ()) -> None:
    try:
        with mysql_store.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, args)
            conn.commit()
    except pymysql.MySQLError as e:
        # 管理操作失败必须可见（曾静默吞错——前端看到成功但实际没执行）
        log.error(f"写操作失败: {e!r} | {sql[:60]}")


# ---------------- 账号 ----------------

@router.post("/api/rag/admin/auth/password")
def change_password(body: dict, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    import hashlib
    rows = _query("SELECT salt, password_hash FROM sys_user WHERE username=%s", (user["username"],))
    if not rows or hashlib.sha256((rows[0][0] + body.get("oldPassword", "")).encode()).hexdigest() != rows[0][1]:
        return {"success": False, "message": "当前密码错误", "data": None}
    new_pwd = body.get("newPassword") or ""
    if len(new_pwd) < 8:
        return {"success": False, "message": "新密码至少 8 位", "data": None}
    _exec("UPDATE sys_user SET password_hash=%s WHERE username=%s",
          (hashlib.sha256((rows[0][0] + new_pwd).encode()).hexdigest(), user["username"]))
    return _ok(None)


@router.get("/api/rag/admin/auth/users")
def list_users(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    # 身份列由 acl 懒迁移保证存在（与 rag-admin 共用表，加列加性变更）
    from ..service import acl
    acl._ensure_columns()
    rows = _query("SELECT id, username, display_name, role, status, "
                  "employee_id, mobile FROM sys_user")
    return _ok([{"id": r[0], "username": r[1], "displayName": r[2], "role": r[3],
                 "status": r[4], "employeeId": r[5], "mobile": r[6] or ""}
                for r in rows])


# ---------------- 员工目录（employee 表，问答/请假/身份的数据源） ----------------

@router.get("/api/rag/admin/employees/directory")
def list_directory(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    from .. import employee_store
    employee_store._ensure_table()
    rows = _query("SELECT id, name, dept, position, entry, employee_no, mobile, status "
                  "FROM employee ORDER BY id")
    return _ok([{"id": r[0], "name": r[1], "dept": r[2] or "", "position": r[3] or "",
                 "entry": r[4] or "", "employeeNo": r[5] or "", "mobile": r[6] or "",
                 "status": r[7]}
                for r in rows])


@router.post("/api/rag/admin/employees/directory/save")
def save_directory(body: dict, authorization: str | None = Header(default=None)):
    """新增/更新员工（name 必填；id 非空则更新）。写后清缓存——问答下次
    查询即新数据（30s TTL 缓存机制）。"""
    if not _auth(authorization):
        return _unauthorized()
    from .. import employee_store
    employee_store._ensure_table()
    name = (body.get("name") or "").strip()
    if not name:
        return {"success": False, "message": "姓名必填", "data": None}
    uid = body.get("id")
    if uid:
        _exec("UPDATE employee SET name=%s, dept=%s, position=%s, entry=%s, "
              "employee_no=%s, mobile=%s, status=%s WHERE id=%s",
              (name, body.get("dept") or "", body.get("position") or "",
               body.get("entry") or "", body.get("employeeNo") or "",
               body.get("mobile") or "", 1 if body.get("status", 1) else 0, uid))
    else:
        _exec("INSERT INTO employee (name, dept, position, entry, employee_no, mobile) "
              "VALUES (%s, %s, %s, %s, %s, %s)",
              (name, body.get("dept") or "", body.get("position") or "",
               body.get("entry") or "", body.get("employeeNo") or "",
               body.get("mobile") or ""))
    employee_store.clear_cache()
    return _ok(None)


@router.post("/api/rag/admin/employees/directory/delete")
def delete_directory(body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    from .. import employee_store
    uid = body.get("id")
    if not uid:
        return {"success": False, "message": "缺少员工 id", "data": None}
    _exec("DELETE FROM employee WHERE id=%s", (uid,))
    employee_store.clear_cache()
    return _ok(None)


@router.post("/api/rag/admin/auth/users/identity")
def save_user_identity(body: dict, authorization: str | None = Header(default=None)):
    """绑定账号身份：employeeId（数字员工 id，doc_acl 受众口径）+
    mobile（飞书手机号匹配落点）。employeeId 空/0 = 解绑；必须存在于
    数字员工表（防拼错产生悬空绑定）。"""
    if not _auth(authorization):
        return _unauthorized()
    from ..service import acl
    acl._ensure_columns()
    uid = body.get("id")
    emp = body.get("employeeId")
    mobile = (body.get("mobile") or "").strip()
    if not uid:
        return {"success": False, "message": "缺少用户 id", "data": None}
    emp_val = None
    if emp not in (None, "", 0):
        if not str(emp).isdigit():
            return {"success": False, "message": "employeeId 必须为数字", "data": None}
        rows = _query("SELECT id FROM digital_employee WHERE id=%s", (int(emp),))
        if not rows:
            return {"success": False, "message": "数字员工不存在", "data": None}
        emp_val = int(emp)
    _exec("UPDATE sys_user SET employee_id=%s, mobile=%s WHERE id=%s",
          (emp_val, mobile or None, uid))
    # 身份变了清缓存（否则最长 60s 旧映射生效）
    with acl._lock:
        acl._emp_cache.clear()
    return _ok(None)


# ---------------- NL2SQL 表注册（query_schema，表级权限） ----------------
# 能力总线 2026-08-19 退役：/capabilities 三端点随机制删除（员工/报销/经营
# 规则并入 NL2SQL 数据面，query-schema 注册即用，路由数据驱动由本端点承担）


# ---------------- 审批看板（去 OA 化后自建审批闭环） ----------------

@router.get("/api/rag/admin/approvals")
def list_approvals(business_type: str = "", status: int | None = None,
                   authorization: str | None = Header(default=None)):
    """申请单看板：business_type 空=全部业务；status 空=全部状态"""
    if not _auth(authorization):
        return _unauthorized()
    from ..plan import approval as approval_flow
    return _ok(approval_flow.list_applications(business_type or "", status))


@router.post("/api/rag/admin/approvals/act")
def act_approval(body: dict, authorization: str | None = Header(default=None)):
    """审批动作：{business_type, apply_id, action(1通过/2拒绝), comment}。
    操作人身份取登录账号绑定的 employee_id（sys_user.employee_id 列）——
    未绑定时拒绝（审批人身份必须可追溯，不允许后台账号无身份代审）"""
    session = _auth(authorization)
    if not session:
        return _unauthorized()
    from ..plan import approval as approval_flow
    biz = (body.get("business_type") or "").strip()
    try:
        apply_id = int(body.get("apply_id") or 0)
        action = int(body.get("action") or 0)
    except (TypeError, ValueError):
        return {"success": False, "message": "apply_id/action 非法", "data": None}
    if not biz or not apply_id or action not in (1, 2):
        return {"success": False, "message": "参数不完整", "data": None}
    # 登录账号 → employee_id（_auth 会话里无此列，直查 sys_user）
    emp_id = None
    for row in _query("SELECT employee_id FROM sys_user WHERE username=%s",
                      (session.get("username"),)):
        emp_id = row[0]
    if not emp_id:
        return {"success": False, "message": "当前账号未绑定员工身份，无法审批（后台-账号身份绑定）",
                "data": None}
    result = approval_flow.act_on_application(biz, apply_id, emp_id, action,
                                              (body.get("comment") or "").strip())
    return {"success": result["ok"], "message": result["message"], "data": None}


_SAFE_TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


@router.get("/api/rag/admin/query-schema/tables")
def list_db_tables(authorization: str | None = Header(default=None)):
    """列出 MySQL 全部表（注册页候选；表名白名单校验防注入——SHOW 不支持
    参数化表名，正则白名单是唯一防线）"""
    if not _auth(authorization):
        return _unauthorized()
    rows = _query("SHOW TABLES")
    return _ok([r[0] for r in rows])


@router.get("/api/rag/admin/query-schema/introspect")
def introspect_table(table: str = "", authorization: str | None = Header(default=None)):
    """读取表结构（SHOW FULL COLUMNS）→ [{name, type, comment}]，注册页
    自动填充列定义用。表名必须过白名单正则（防注入/防跨库）"""
    if not _auth(authorization):
        return _unauthorized()
    if not _SAFE_TABLE_NAME.match(table or ""):
        return {"success": False, "message": "非法表名", "data": None}
    rows = _query(f"SHOW FULL COLUMNS FROM `{table}`")
    return _ok([{"name": r[0], "type": r[1], "comment": r[8] or ""} for r in rows])


@router.get("/api/rag/admin/query-schema")

@router.get("/api/rag/admin/query-schema")
def list_query_schema(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    import json as _json
    rows = _query("SELECT id, table_name, description, columns_json, grants, status "
                  "FROM query_schema")
    out = []
    for r in rows:
        try:
            cols = _json.loads(r[3] or "[]")
        except ValueError:
            cols = []
        out.append({"id": r[0], "table": r[1], "description": r[2] or "",
                    "columns": cols, "grants": r[4] or "", "status": r[5]})
    return _ok(out)


@router.post("/api/rag/admin/query-schema/save")
def save_query_schema(body: dict, authorization: str | None = Header(default=None)):
    """登记/更新可查询表：columns=[{name, desc, sample}]；grants 同 doc_acl
    口径（空=全员可见，非空=employee:ID 名单）；status 0=停用。"""
    if not _auth(authorization):
        return _unauthorized()
    from ..service import nlsql
    table = (body.get("table") or "").strip()
    if not table or not isinstance(body.get("columns"), list):
        return {"success": False, "message": "table/columns 必填", "data": None}
    nlsql.register_table(table, body.get("description") or "",
                         body["columns"], body.get("grants") or "")
    if not body.get("status", 1):
        _exec("UPDATE query_schema SET status=0 WHERE table_name=%s", (table,))
    return _ok(None)


# ---------------- 字段级权限（field_acl，PII 列管控） ----------------

@router.get("/api/rag/admin/field-acl")
def list_field_acl(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    rows = _query("SELECT id, table_name, column_name, grants, status FROM field_acl")
    return _ok([{"id": r[0], "table": r[1], "column": r[2], "grants": r[3] or "",
                 "status": r[4]} for r in rows])


@router.post("/api/rag/admin/field-acl/save")
def save_field_acl(body: dict, authorization: str | None = Header(default=None)):
    """字段级权限：如 employee.mobile 设 grants=employee:6,7 → 仅管理层可查电话。
    grants 空 = 全员可见。NL2SQL 列过滤与意图 handler 双处生效。"""
    if not _auth(authorization):
        return _unauthorized()
    from ..service import acl
    table = (body.get("table") or "").strip()
    column = (body.get("column") or "").strip()
    if not table or not column:
        return {"success": False, "message": "table/column 必填", "data": None}
    acl.save_field_acl(table, column, body.get("grants") or "",
                       int(body.get("status", 1)))
    return _ok(None)


# ---------------- 仪表盘 / 审计 ----------------

@router.get("/api/rag/admin/audit/dashboard")
def dashboard(days: int = 7, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    rows = _query(
        "SELECT DATE_FORMAT(timestamp, '%%Y-%%m-%%d') d, COUNT(*) c, "
        "SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) s, AVG(total_ms) a "
        "FROM audit_event WHERE timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) "
        "GROUP BY d ORDER BY d", (days,))
    trend = {r[0]: {"requests": r[1], "success": int(r[2] or 0),
                    "avgMs": round(float(r[3] or 0))} for r in rows}
    # noAnswer：gaps.jsonl 兜底句式按天计数（曾该列恒 0——前端字段无数据源）
    for day, c in gap_discovery.daily_counts(days).items():
        trend.setdefault(day, {"requests": 0, "success": 0, "avgMs": 0})["noAnswer"] = c
    # feishu：审计 endpoint 区分渠道（曾所有渠道都写 agent/chat，无法区分）
    feishu_rows = _query(
        "SELECT DATE_FORMAT(timestamp, '%%Y-%%m-%%d') d, COUNT(*) c FROM audit_event "
        "WHERE endpoint='feishu/chat' AND timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) "
        "GROUP BY d", (days,))
    for r in feishu_rows:
        trend.setdefault(r[0], {"requests": 0, "success": 0, "avgMs": 0})["feishu"] = r[1]
    # adoptions：采纳回流按天
    adopt_rows = _query(
        "SELECT DATE_FORMAT(created_at, '%%Y-%%m-%%d') d, COUNT(*) c FROM feedback_adopt "
        "WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY d", (days,))
    for r in adopt_rows:
        trend.setdefault(r[0], {"requests": 0, "success": 0, "avgMs": 0})["adoptions"] = r[1]
    top = _query(
        "SELECT query, COUNT(*) c FROM audit_event WHERE query IS NOT NULL AND query<>'' "
        "AND timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY query ORDER BY c DESC LIMIT 8",
        (days,))
    routes = _query(
        "SELECT category, COUNT(*) c FROM audit_event "
        "WHERE timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY category ORDER BY c DESC",
        (days,))
    employees = _query(
        "SELECT user, COUNT(*) c, AVG(total_ms) a FROM audit_event "
        "WHERE user IS NOT NULL AND user<>'' AND user<>'admin' "
        "AND timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY user ORDER BY c DESC LIMIT 8",
        (days,))
    depts = _query(
        "SELECT dept, COUNT(*) c FROM digital_employee WHERE dept IS NOT NULL AND dept<>'' "
        "GROUP BY dept ORDER BY c DESC")
    return _ok({
        "trend": [{"day": d, "requests": v["requests"], "success": v["success"],
                   "noAnswer": v.get("noAnswer", 0), "feishu": v.get("feishu", 0),
                   "avgMs": v["avgMs"], "adoptions": v.get("adoptions", 0)}
                  for d, v in sorted(trend.items())],
        "topQueries": [{"name": r[0][:30], "count": r[1]} for r in top],
        "routes": [{"name": r[0] or "未知", "count": r[1]} for r in routes],
        "employees": [{"name": r[0][:20], "count": r[1], "avgMs": round(float(r[2] or 0))}
                      for r in employees],
        "depts": [{"name": r[0], "count": r[1], "avgMs": 0} for r in depts],
    })


@router.get("/api/rag/admin/audit/kb-health")
def kb_health(days: int = 7, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    total_rows = _query("SELECT COUNT(*) FROM audit_event "
                        "WHERE timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)", (days,))
    total = total_rows[0][0] if total_rows else 0
    gaps = gap_discovery.report(days)
    return _ok({
        "totalQueries": total,
        "unanswered": gaps["total_gaps"],
        "unansweredRate": round(gaps["total_gaps"] / total, 4) if total else 0.0,
        "topics": gaps["clusters"],
    })


@router.get("/api/rag/admin/audit/stats")
def audit_stats(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    today = _query(
        "SELECT COUNT(*), SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END), AVG(total_ms), "
        "SUM(CASE WHEN status<>'SUCCESS' THEN 1 ELSE 0 END) "
        "FROM audit_event WHERE timestamp >= CURDATE()")
    t = today[0] if today else (0, 0, 0, 0)
    hourly = _query(
        "SELECT DATE_FORMAT(timestamp, '%%H'), COUNT(*), "
        "SUM(CASE WHEN status<>'SUCCESS' THEN 1 ELSE 0 END), AVG(total_ms) "
        "FROM audit_event WHERE timestamp >= CURDATE() GROUP BY 1 ORDER BY 1")
    return _ok({
        "todayRequests": t[0] or 0,
        "successRate": round((t[1] or 0) / t[0], 4) if t[0] else 1.0,
        "avgTotalMs": round(float(t[2] or 0)),
        "errorCount": t[3] or 0,
        "hourly": [{"hour": r[0], "count": r[1], "errorCount": r[2] or 0,
                    "avgTotalMs": round(float(r[3] or 0))} for r in hourly],
    })


@router.get("/api/rag/admin/audit/events")
def audit_events(page: int = 1, size: int = 20, q: str = "", status: str = "",
                 authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    where, args = "WHERE 1=1", []
    if q:
        where += " AND query LIKE %s"
        args.append(f"%{q}%")
    if status:
        where += " AND status=%s"
        args.append(status)
    total_rows = _query(f"SELECT COUNT(*) FROM audit_event {where}", tuple(args))
    total = total_rows[0][0] if total_rows else 0
    rows = _query(
        f"SELECT trace_id, timestamp, user, ip, endpoint, category, query, history_count, sources, "
        f"retrieval_ms, rerank_degraded, graph_context_chars, answer_preview, answer_length, "
        f"total_ms, status, error FROM audit_event {where} ORDER BY id DESC LIMIT %s OFFSET %s",
        tuple(args) + (size, (page - 1) * size))
    items = []
    for r in rows:
        try:
            sources = json.loads(r[8]) if r[8] else []
        except (ValueError, TypeError):
            sources = []
        items.append({
            "traceId": r[0], "timestamp": str(r[1]), "user": r[2] or "", "ip": r[3] or "",
            "endpoint": r[4] or "", "category": r[5] or "", "query": r[6] or "",
            "historyCount": r[7] or 0, "sources": sources, "retrievalMs": r[9] or 0,
            "rerankDegraded": bool(r[10]), "graphContextChars": r[11] or 0,
            "answerPreview": r[12] or "", "answerLength": r[13] or 0,
            "totalMs": r[14] or 0, "status": r[15] or "", "error": r[16] or "",
        })
    return _ok({"total": total, "page": page, "size": size, "items": items})


# ---------------- 用量 ----------------

@router.get("/api/rag/admin/usage")
def usage_view(days: int = 7, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    # JSONL 历史按天聚合（重启不归零；曾只返回进程内快照单日——重启即清零）
    hist = usage.history(days)
    if hist:
        return _ok(hist)
    # JSONL 空（极早期/写入异常）→ 进程内快照兜底单日
    snap = usage.snapshot()
    by_model = [{"model": m, "purpose": m, "calls": v["calls"], "chars": v["chars"],
                 "estTokens": v["chars"] // 2, "totalMs": round(v["seconds"] * 1000)}
                for m, v in snap["by_model"].items()]
    return _ok([{
        "day": business_now().strftime("%Y-%m-%d"),
        "calls": snap["calls"], "estTokens": (snap["prompt_chars"] + snap["completion_chars"]) // 2,
        "totalMs": round(snap["total_seconds"] * 1000), "byModel": by_model,
    }])


# ---------------- 评测中心 ----------------

_eval_tasks: dict = {}
_eval_lock = threading.Lock()


def _load_sets() -> list:
    os.makedirs(EVAL_SETS_DIR, exist_ok=True)
    builtin = os.path.join(EVAL_DIR, "retrieval_baseline.json")
    sets = []
    if os.path.exists(builtin):
        d = json.load(open(builtin, encoding="utf-8"))
        sets.append({"id": "retrieval-baseline", "name": d.get("name", "检索基线"),
                     "itemCount": len(d.get("items", [])), "createdAt": d.get("createdAt", "")})
    for f in sorted(os.listdir(EVAL_SETS_DIR)):
        if f.endswith(".json"):
            d = json.load(open(os.path.join(EVAL_SETS_DIR, f), encoding="utf-8"))
            sets.append({"id": d.get("id", f[:-5]), "name": d.get("name", f[:-5]),
                         "itemCount": len(d.get("items", [])), "createdAt": d.get("createdAt", "")})
    return sets


def _load_set_items(set_id: str) -> list:
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", set_id):
        return []
    if set_id == "retrieval-baseline":
        return json.load(open(os.path.join(EVAL_DIR, "retrieval_baseline.json"), encoding="utf-8"))["items"]
    p = os.path.join(EVAL_SETS_DIR, f"{set_id}.json")
    # 路径穿越防护：校验 realpath 必须在 EVAL_SETS_DIR 内
    real = os.path.realpath(p)
    if not real.startswith(os.path.realpath(EVAL_SETS_DIR) + os.sep):
        return []
    return json.load(open(real, encoding="utf-8"))["items"] if os.path.exists(real) else []


@router.get("/api/rag/admin/eval/sets")
def eval_sets(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    return _ok(_load_sets())


@router.post("/api/rag/admin/eval/sets")
def eval_set_create(body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    os.makedirs(EVAL_SETS_DIR, exist_ok=True)
    set_id = uuid.uuid4().hex[:8]
    items = []
    for line in (body.get("itemsText") or "").splitlines():
        if "|" in line:
            q, gt = line.split("|", 1)
            items.append({"question": q.strip(), "groundTruth": gt.strip()})
    d = {"id": set_id, "name": body.get("name") or "未命名评测集",
         "createdAt": business_now().isoformat(), "items": items}
    json.dump(d, open(os.path.join(EVAL_SETS_DIR, f"{set_id}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return _ok({"id": set_id, "name": d["name"], "itemCount": len(items),
                "createdAt": d["createdAt"]})


@router.delete("/api/rag/admin/eval/sets/{set_id}")
def eval_set_delete(set_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", set_id):
        return _ok(False)
    p = os.path.join(EVAL_SETS_DIR, f"{set_id}.json")
    real = os.path.realpath(p)
    if not real.startswith(os.path.realpath(EVAL_SETS_DIR) + os.sep) or not os.path.exists(real):
        return _ok(False)
    os.remove(real)
    return _ok(True)


def _run_eval_task(task_id: str, set_id: str) -> None:
    from ..learn import eval_engine
    from ..service.retrieval import current_params
    try:
        params = current_params()
        items = _load_set_items(set_id)
        results = [eval_engine.run_retrieval_case(i["question"], i["groundTruth"],
                                                  top_k=params["top_k"], min_score=params["min_score"])
                   for i in items]
        hits = sum(1 for r in results if r["hit"])
        mrr = sum(1.0 / r["rank"] for r in results if r["hit"]) / len(results) if results else 0.0
        status, payload = "SUCCESS", {
            "setId": set_id, "total": len(results), "evaluated": len(results),
            "hitRate": round(hits / len(results), 4) if results else 0.0,
            "mrr": round(mrr, 4), "ragasTaskId": None,
            "items": [{"question": i["question"], "hit": r["hit"],
                       "mrr": 1.0 / r["rank"] if r["hit"] else 0.0, "topSource": ""}
                      for i, r in zip(items, results)],
        }
    except Exception as e:
        # 评测任务异常必须可见可查（曾线程内裸崩——task 永远 RUNNING，前端死等）
        log.error(f"评测任务失败: {e!r}")
        status, payload = "FAILED", {"setId": set_id, "error": str(e)[:200]}
    with _eval_lock:
        _eval_tasks[task_id] = {"status": status, "result": payload}


@router.post("/api/rag/admin/eval/sets/{set_id}/run")
def eval_set_run(set_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    task_id = uuid.uuid4().hex[:8]
    with _eval_lock:
        _eval_tasks[task_id] = {"status": "RUNNING", "result": None}
    threading.Thread(target=_run_eval_task, args=(task_id, set_id), daemon=True).start()
    return _ok({"taskId": task_id})


@router.get("/api/rag/admin/eval/runs/{task_id}")
def eval_run_status(task_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    with _eval_lock:
        t = _eval_tasks.get(task_id)
    # 未知 task 明确 UNKNOWN（曾返回 RUNNING——前端永远轮询不结束）
    return _ok(t or {"status": "UNKNOWN", "result": None})


# ---------------- SOP ----------------

def _seed_sop() -> None:
    """从注册表自动种子所有 SOP 到 sop 表（幂等：已存在则跳过）"""
    from ..plan.registry import ALL_DEFS
    for d in ALL_DEFS:
        rows = _query("SELECT COUNT(*) FROM sop WHERE code=%s", (d.code,))
        if rows and rows[0][0] == 0:
            _exec(
                "INSERT INTO sop (code, name, keywords, steps_json, enabled, builtin) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (d.code, d.name, ",".join(d.keywords),
                 json.dumps(["load_employees", "extract", "validate", "confirm", "submit", "receipt"],
                            ensure_ascii=False),
                 1, 1))


@router.get("/api/rag/admin/sops")
def list_sops(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _seed_sop()
    rows = _query("SELECT code, name, keywords, steps_json, enabled, builtin, updated_at FROM sop")
    return _ok([{
        "code": r[0], "name": r[1], "keywords": (r[2] or "").split(","),
        "steps": json.loads(r[3] or "[]"), "enabled": bool(r[4]), "builtin": bool(r[5]),
        "updatedAt": str(r[6]),
    } for r in rows])


@router.post("/api/rag/admin/sops")
def save_sop(body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    keywords = body.get("keywords")
    if isinstance(keywords, list):
        keywords = ",".join(str(k).strip() for k in keywords if str(k).strip())
    _exec("INSERT INTO sop (code, name, keywords, steps_json, enabled, builtin) "
          "VALUES (%s,%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE name=VALUES(name), "
          "keywords=VALUES(keywords), steps_json=VALUES(steps_json), enabled=VALUES(enabled)",
          (body.get("code"), body.get("name"), keywords or "",
           json.dumps(body.get("steps") or [], ensure_ascii=False),
           1 if body.get("enabled") else 0))
    return _ok(True)


@router.post("/api/rag/admin/sops/{code}/toggle")
def toggle_sop(code: str, body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _exec("UPDATE sop SET enabled=%s WHERE code=%s", (1 if body.get("enabled") else 0, code))
    return _ok(True)


@router.delete("/api/rag/admin/sops/{code}")
def delete_sop(code: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _exec("DELETE FROM sop WHERE code=%s", (code,))
    return _ok(True)


# ---------------- 数字员工 / 技能 / 权限 / 告警 / 审核 ----------------

@router.get("/api/rag/admin/employees")
def list_employees(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    rows = _query("SELECT id, name, avatar, description, system_prompt, knowledge_scope, status, "
                  "lifecycle, dept, created_at, updated_at FROM digital_employee ORDER BY id")
    return _ok([{"id": r[0], "name": r[1], "avatar": r[2] or "", "description": r[3] or "",
                 "systemPrompt": r[4] or "", "knowledgeScope": r[5] or "all", "status": r[6],
                 "lifecycle": r[7] or "ONLINE", "dept": r[8], "createdAt": str(r[9]),
                 "updatedAt": str(r[10])} for r in rows])


@router.post("/api/rag/admin/employees")
def save_employee(body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    status = 1 if body.get("status") else 0
    if body.get("id"):
        _exec("UPDATE digital_employee SET name=%s, avatar=%s, description=%s, system_prompt=%s, "
              "knowledge_scope=%s, status=%s, dept=%s WHERE id=%s",
              (body.get("name"), body.get("avatar", ""), body.get("description", ""),
               body.get("systemPrompt", ""), body.get("knowledgeScope", "all"), status,
               body.get("dept"), body["id"]))
        return _ok(True)
    _exec("INSERT INTO digital_employee (name, avatar, description, system_prompt, knowledge_scope, status, dept) "
          "VALUES (%s,%s,%s,%s,%s,%s,%s)",
          (body.get("name"), body.get("avatar", ""), body.get("description", ""),
           body.get("systemPrompt", ""), body.get("knowledgeScope", "all"), status, body.get("dept")))
    return _ok(True)


@router.post("/api/rag/admin/employees/{emp_id}/lifecycle")
def employee_lifecycle(emp_id: int, body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _exec("UPDATE digital_employee SET lifecycle=%s WHERE id=%s", (body.get("lifecycle"), emp_id))
    return _ok(True)


@router.delete("/api/rag/admin/employees/{emp_id}")
def delete_employee(emp_id: int, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _exec("DELETE FROM digital_employee WHERE id=%s", (emp_id,))
    return _ok(True)


@router.get("/api/rag/admin/skills")
def list_skills(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    rows = _query("SELECT id, code, name, description, skill_md, tool_type, mcp_config, tool_names, "
                  "status, created_at, updated_at FROM skill ORDER BY id")
    return _ok([{"id": r[0], "code": r[1], "name": r[2], "description": r[3] or "",
                 "skillMd": r[4] or "", "toolType": r[5] or "", "mcpConfig": r[6] or "",
                 "toolNames": r[7] or "", "status": r[8], "employeeCount": 0,
                 "createdAt": str(r[9]), "updatedAt": str(r[10])} for r in rows])


@router.post("/api/rag/admin/skills")
def save_skill(body: dict, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    status = 1 if body.get("status") else 0
    if body.get("id"):
        _exec("UPDATE skill SET code=%s, name=%s, description=%s, skill_md=%s, tool_type=%s, "
              "mcp_config=%s, tool_names=%s, status=%s WHERE id=%s",
              (body.get("code"), body.get("name"), body.get("description", ""), body.get("skillMd", ""),
               body.get("toolType", ""), body.get("mcpConfig", ""), body.get("toolNames", ""),
               status, body["id"]))
        return _ok(True)
    _exec("INSERT INTO skill (code, name, description, skill_md, tool_type, mcp_config, tool_names, status) "
          "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
          (body.get("code"), body.get("name"), body.get("description", ""), body.get("skillMd", ""),
           body.get("toolType", ""), body.get("mcpConfig", ""), body.get("toolNames", ""), status))
    return _ok(True)


@router.delete("/api/rag/admin/skills/{skill_id}")
def delete_skill(skill_id: int, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _exec("DELETE FROM skill WHERE id=%s", (skill_id,))
    return _ok(True)


@router.get("/api/rag/admin/acl")
def list_acls(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    rows = _query("SELECT doc_id, grants, updated_by, updated_at FROM doc_acl")
    return _ok([{"docId": r[0], "grants": json.loads(r[1] or "[]"), "updatedBy": r[2] or "",
                 "updatedAt": str(r[3])} for r in rows])


@router.post("/api/rag/admin/acl")
def set_acl(body: dict, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    _exec("INSERT INTO doc_acl (doc_id, grants, updated_by) VALUES (%s,%s,%s) "
          "ON DUPLICATE KEY UPDATE grants=VALUES(grants), updated_by=VALUES(updated_by)",
          (body.get("docId"), json.dumps(body.get("grants") or [], ensure_ascii=False),
           user["username"]))
    return _ok(True)


@router.delete("/api/rag/admin/acl/{doc_id}")
def remove_acl(doc_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    _exec("DELETE FROM doc_acl WHERE doc_id=%s", (doc_id,))
    return _ok(True)


@router.get("/api/rag/admin/alerts")
def list_alerts(status: str = "", authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    where = "WHERE status=%s" if status else ""
    args = (status,) if status else ()
    rows = _query(f"SELECT id, rule_code, level, title, detail, status, first_at, last_at, count, "
                  f"ack_by, ack_at FROM alert_event {where} ORDER BY id DESC LIMIT 100", args)
    return _ok([{"id": r[0], "ruleCode": r[1], "level": r[2], "title": r[3], "detail": r[4],
                 "status": r[5], "firstAt": str(r[6]), "lastAt": str(r[7]), "count": r[8],
                 "ackBy": r[9], "ackAt": str(r[10])} for r in rows])


@router.get("/api/rag/admin/alerts/stats")
def alert_stats(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    rows = _query("SELECT status, COUNT(*) FROM alert_event GROUP BY status")
    d = dict(rows)
    return _ok({"unresolved": d.get("UNRESOLVED", 0), "acknowledged": d.get("ACKED", 0)})


@router.post("/api/rag/admin/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    _exec("UPDATE alert_event SET status='ACKED', ack_by=%s, ack_at=NOW() WHERE id=%s",
          (user["username"], alert_id))
    return _ok(True)


@router.get("/api/rag/admin/review")
def list_reviews(status: str = "", authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    where = "WHERE status=%s" if status else ""
    args = (status,) if status else ()
    rows = _query(f"SELECT id, query, answer, sources, employee_id, user, status, reviewer, "
                  f"created_at, reviewed_at FROM answer_review {where} ORDER BY id DESC LIMIT 100", args)
    return _ok([{"id": r[0], "query": r[1], "answer": r[2], "sources": r[3] or "",
                 "employeeId": r[4], "user": r[5] or "", "status": r[6], "reviewer": r[7] or "",
                 "createdAt": str(r[8]), "reviewedAt": str(r[9])} for r in rows])


@router.post("/api/rag/admin/review/{review_id}/approve")
def approve_review(review_id: int, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    _exec("UPDATE answer_review SET status='APPROVED', reviewer=%s, reviewed_at=NOW() WHERE id=%s",
          (user["username"], review_id))
    return _ok(True)


@router.post("/api/rag/admin/review/{review_id}/reject")
def reject_review(review_id: int, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    _exec("UPDATE answer_review SET status='REJECTED', reviewer=%s, reviewed_at=NOW() WHERE id=%s",
          (user["username"], review_id))
    return _ok(True)


@router.get("/api/rag/admin/tasks/{task_id}")
def task_status(task_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    with _eval_lock:
        t = _eval_tasks.get(task_id)
    if t:
        return _ok({"id": task_id, "type": "EVAL", "status": t["status"],
                    "result": json.dumps(t["result"], ensure_ascii=False) if t["result"] else None})
    return _ok({"id": task_id, "type": "UNKNOWN", "status": "COMPLETED", "result": None})


@router.get("/api/rag/admin/stats")
def kb_stats(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    # 真实探活：Milvus 不可用必须反映出来（曾状态恒 OK、维度写死 1024——
    # 向量库挂了看板还显示正常，维度升级 4096 后页面还是旧数）
    docs: list = []
    try:
        if milvus_store._get_client().has_collection(config.MILVUS_COLLECTION):
            docs = milvus_store.list_documents()
            status = "OK"
        else:
            status = "DISCONNECTED"
    except Exception:
        status = "DISCONNECTED"
    return _ok({"collection": config.MILVUS_COLLECTION, "status": status,
                "totalDocuments": len(docs), "totalChunks": sum(d["chunks"] for d in docs),
                "vectorDimension": config.EMBED_DIM, "indexType": "HNSW+BM25(RRF)"})


@router.get("/api/rag/admin/health/verify")
def verify_health(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    return _ok(agent.verify_breaker_state())


@router.post("/api/rag/admin/pending/promote")
def promote_pending_cases(authorization: str | None = Header(default=None)):
    """待审用例一键转正：👎带纠正的队列 → 正式回归用例（棘轮只增不减）"""
    if not _auth(authorization):
        return _unauthorized()
    return _ok(feedback.promote_pending())


@router.get("/api/rag/admin/pending")
def pending_list(authorization: str | None = Header(default=None)):
    """待审队列列表"""
    if not _auth(authorization):
        return _unauthorized()
    return _ok(feedback.list_pending())


@router.delete("/api/rag/admin/pending")
def pending_clear(authorization: str | None = Header(default=None)):
    """清空待审队列"""
    if not _auth(authorization):
        return _unauthorized()
    return _ok({"cleared": feedback.clear_pending()})


@router.get("/api/rag/admin/documents")
def documents_view(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    return _ok(milvus_store.list_documents())


@router.get("/api/rag/admin/documents/details")
def documents_details(authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    docs = milvus_store.list_documents()
    return _ok([{"docId": d["docId"], "title": d["title"], "chunks": d["chunks"],
                 "status": "STORED"} for d in docs])


@router.delete("/api/rag/admin/documents/{doc_id}")
def delete_document(doc_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    removed = milvus_store.delete_by_document_id(doc_id)
    graph_result = graph.delete_by_document_id(doc_id)
    return _ok({"docId": doc_id, "chunksRemoved": removed,
                 "graph": graph_result})


@router.post("/api/rag/admin/documents/{doc_id}/rename")
def rename_document(doc_id: str, body: dict, authorization: str | None = Header(default=None)):
    """知识库文档改名（治理用）：机器标题（图片识别_xxx/问答采纳-xxx）改回真实名称"""
    if not _auth(authorization):
        return _unauthorized()
    title = (body.get("title") or "").strip()
    source = (body.get("source") or "").strip() or title
    if not title:
        return _ok({"docId": doc_id, "updated": 0, "error": "title 不能为空"})
    updated = milvus_store.update_document_meta(doc_id, title=title, source=source)
    return _ok({"docId": doc_id, "updated": updated})
