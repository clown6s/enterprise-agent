# -*- coding: utf-8 -*-
"""
管理后台 API 全量（rag-admin 前端复用，共用 MySQL 表）。
覆盖页面：仪表盘 / 审计看板 / 告警中心 / 评测中心 / 数字员工 / 技能管理 /
SOP 管理 / 答案审核 / 文档权限 / 知识库健康度 / 用量看板 / 账号管理。
"""
import json
import os
import threading
import uuid
from datetime import datetime

import pymysql
from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from .. import config, milvus_store, mysql_store
from ..learn import feedback, gap_discovery, usage
from ..service import agent

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


def _db():
    return pymysql.connect(
        host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
        password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
        charset="utf8mb4", connect_timeout=5,
    )


def _query(sql: str, args: tuple = ()) -> list:
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return cur.fetchall()
        conn.close()
    except pymysql.MySQLError:
        return []


def _exec(sql: str, args: tuple = ()) -> None:
    try:
        conn = _db()
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()
        conn.close()
    except pymysql.MySQLError:
        pass


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
    rows = _query("SELECT id, username, display_name, role, status FROM sys_user")
    return _ok([{"id": r[0], "username": r[1], "displayName": r[2], "role": r[3], "status": r[4]}
                for r in rows])


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
    trend = [{"day": r[0], "requests": r[1], "success": int(r[2] or 0),
              "avgMs": round(float(r[3] or 0))} for r in rows]
    top = _query(
        "SELECT query, COUNT(*) c FROM audit_event WHERE query IS NOT NULL AND query<>'' "
        "AND timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY query ORDER BY c DESC LIMIT 8",
        (days,))
    routes = _query(
        "SELECT category, COUNT(*) c FROM audit_event "
        "WHERE timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY) GROUP BY category ORDER BY c DESC",
        (days,))
    return _ok({
        "trend": trend,
        "topQueries": [{"name": r[0][:30], "count": r[1]} for r in top],
        "routes": [{"name": r[0] or "未知", "count": r[1]} for r in routes],
        "employees": [], "depts": [],
    })


@router.get("/api/rag/admin/audit/kb-health")
def kb_health(days: int = 7, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    total_rows = _query("SELECT COUNT(*) FROM audit_event "
                        "WHERE timestamp >= DATE_SUB(NOW(), INTERVAL %s DAY)", (days,))
    total = total_rows[0][0] if total_rows else 0
    gaps = gap_discovery.report()
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
    snap = usage.snapshot()
    by_model = [{"model": m, "purpose": m, "calls": v["calls"], "chars": v["chars"],
                 "estTokens": v["chars"] // 2, "totalMs": round(v["seconds"] * 1000)}
                for m, v in snap["by_model"].items()]
    return _ok([{
        "day": datetime.now().strftime("%Y-%m-%d"),
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
    if set_id == "retrieval-baseline":
        return json.load(open(os.path.join(EVAL_DIR, "retrieval_baseline.json"), encoding="utf-8"))["items"]
    p = os.path.join(EVAL_SETS_DIR, f"{set_id}.json")
    return json.load(open(p, encoding="utf-8"))["items"] if os.path.exists(p) else []


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
         "createdAt": datetime.now().isoformat(), "items": items}
    json.dump(d, open(os.path.join(EVAL_SETS_DIR, f"{set_id}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return _ok({"id": set_id, "name": d["name"], "itemCount": len(items),
                "createdAt": d["createdAt"]})


@router.delete("/api/rag/admin/eval/sets/{set_id}")
def eval_set_delete(set_id: str, authorization: str | None = Header(default=None)):
    if not _auth(authorization):
        return _unauthorized()
    p = os.path.join(EVAL_SETS_DIR, f"{set_id}.json")
    if os.path.exists(p):
        os.remove(p)
    return _ok(True)


def _run_eval_task(task_id: str, set_id: str) -> None:
    from ..learn import eval_engine
    from ..service.retrieval import current_params
    params = current_params()
    items = _load_set_items(set_id)
    results = [eval_engine.run_retrieval_case(i["question"], i["groundTruth"],
                                              top_k=params["top_k"], min_score=params["min_score"])
               for i in items]
    hits = sum(1 for r in results if r["hit"])
    mrr = sum(1.0 / r["rank"] for r in results if r["hit"]) / len(results) if results else 0.0
    with _eval_lock:
        _eval_tasks[task_id] = {
            "status": "SUCCESS",
            "result": {
                "setId": set_id, "total": len(results), "evaluated": len(results),
                "hitRate": round(hits / len(results), 4) if results else 0.0,
                "mrr": round(mrr, 4), "ragasTaskId": None,
                "items": [{"question": i["question"], "hit": r["hit"],
                           "mrr": 1.0 / r["rank"] if r["hit"] else 0.0, "topSource": ""}
                          for i, r in zip(items, results)],
            },
        }


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
    return _ok(t or {"status": "RUNNING", "result": None})


# ---------------- SOP ----------------

def _seed_sop() -> None:
    rows = _query("SELECT COUNT(*) FROM sop WHERE code='leave_action'")
    if rows and rows[0][0] == 0:
        _exec("INSERT INTO sop (code, name, keywords, steps_json, enabled, builtin) VALUES (%s,%s,%s,%s,%s,%s)",
              ("leave_action", "请假申请",
               "提交请假,帮我请假,帮我请,申请年假,请事假,请病假,请年假,请调休,请个假,请一天假,请两天假,请三天假,请半天假,请假一天,请假两天,请假三天",
               json.dumps([
                   {"type": "tool", "toolName": "oa_employee_list", "instruction": None},
                   {"type": "extract", "toolName": None, "instruction": "提取 userName/startTime/endTime/days/leaveType/reason"},
                   {"type": "validate", "toolName": None, "instruction": "leave_rules"},
                   {"type": "tool", "toolName": "oa_leave_submit", "instruction": None},
                   {"type": "end", "toolName": None, "instruction": None},
               ], ensure_ascii=False), 1, 1))


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
    _exec("INSERT INTO sop (code, name, keywords, steps_json, enabled, builtin) "
          "VALUES (%s,%s,%s,%s,%s,0) ON DUPLICATE KEY UPDATE name=VALUES(name), "
          "keywords=VALUES(keywords), steps_json=VALUES(steps_json), enabled=VALUES(enabled)",
          (body.get("code"), body.get("name"), body.get("keywords"),
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
    where = f"WHERE status='{status}'" if status else ""
    rows = _query(f"SELECT id, rule_code, level, title, detail, status, first_at, last_at, count, "
                  f"ack_by, ack_at FROM alert_event {where} ORDER BY id DESC LIMIT 100")
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
    where = f"WHERE status='{status}'" if status else ""
    rows = _query(f"SELECT id, query, answer, sources, employee_id, user, status, reviewer, "
                  f"created_at, reviewed_at FROM answer_review {where} ORDER BY id DESC LIMIT 100")
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
    docs = milvus_store.list_documents()
    return _ok({"collection": config.MILVUS_COLLECTION, "status": "OK",
                "totalDocuments": len(docs), "totalChunks": sum(d["chunks"] for d in docs),
                "vectorDimension": 1024, "indexType": "HNSW+BM25(RRF)"})


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
    return _ok({"docId": doc_id, "chunksRemoved": removed})


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
