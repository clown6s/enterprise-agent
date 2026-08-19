# -*- coding: utf-8 -*-
"""审批动作层（2026-08-19 去 OA 化后自建审批闭环，T48）：
通过/拒绝 → 写 oa_approval_record + 推进申请表状态机。
状态机：1待审批 2审批中 3已通过 4已拒绝 5已撤回（与 oa_leave_application 同口径）。
多级审批按 oa_approval_flow 配置逐级推进：通过且还有下一级 → status=2 +
current_approver_id=下一级；无下一级 → status=3 终态。
申请人/审批人体系：employee.id（身份统一后 sys_employee 体系已退役）。"""
import json
import time
from datetime import datetime

from .. import config
from . import registry

STATUS_PENDING, STATUS_IN_FLOW, STATUS_APPROVED, STATUS_REJECTED = 1, 2, 3, 4
ACT_APPROVE, ACT_REJECT = 1, 2


def _conn():
    import pymysql
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=3)


def _load_apply(business_type: str, apply_id: int) -> dict | None:
    table = registry.TABLE_MAP.get(business_type)
    if not table:
        return None
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} WHERE id=%s", (apply_id,))
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        conn.close()
        return dict(zip(cols, row)) if row else None
    except Exception:
        return None


def _resolve_level_approver(business_type: str, level: int, dept_id) -> int | None:
    """第 level 级审批人（确定性）：oa_approval_flow 该级配置——
    approver_dept_leader=1 → 部门负责人（sys_dept.leader → employee.id）；
    否则 approver_role_key → sys_role → sys_employee_role → employee。
    无该级配置返回 None（=流程走完）；配置在但没人绑定返回 -1 哨兵（缺审批人）。"""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approver_dept_leader, approver_role_key FROM oa_approval_flow "
                "WHERE business_type=%s AND level=%s", (business_type, level))
            row = cur.fetchone()
            if not row:
                return None
            if row[0]:
                cur.execute("SELECT leader FROM sys_dept WHERE id=%s", (dept_id,))
                leader = cur.fetchone()
                if leader and leader[0]:
                    cur.execute("SELECT id FROM employee WHERE name=%s AND status=1",
                                (leader[0],))
                    r2 = cur.fetchone()
                    if r2:
                        return r2[0]
                return -1
            if row[1]:
                cur.execute(
                    "SELECT e.id FROM sys_employee_role er "
                    "JOIN sys_role r ON er.role_id = r.id "
                    "JOIN employee e ON er.employee_id = e.id "
                    "WHERE r.role_key=%s AND e.status=1 LIMIT 1", (row[1],))
                r2 = cur.fetchone()
                if r2:
                    return r2[0]
                return -1
        conn.close()
    except Exception:
        pass
    return -1


def summary_text(business_type: str, row: dict) -> str:
    """申请摘要（通知/审批卡片用），列值全来自真实数据行。
    列名→中文走 registry.COL_LABELS（从 ApprovalDef.col_labels 派生，新增业务零代码）"""
    parts = []
    for col in registry.SUMMARY_MAP.get(business_type, ()):
        v = row.get(col)
        if v not in (None, ""):
            parts.append(f"{registry.COL_LABELS.get(col, col)}：{v}")
    return "，".join(parts)


def pending_by_approver(approver_id: int) -> list[dict]:
    """某人待审批的申请单列表（跨业务，供飞书对话审批/后台页）"""
    out = []
    for business_type, table in registry.TABLE_MAP.items():
        try:
            conn = _conn()
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT * FROM {table} WHERE current_approver_id=%s "
                    "AND status IN (1,2) ORDER BY id DESC LIMIT 20", (approver_id,))
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
            conn.close()
            for row in rows:
                d = dict(zip(cols, row))
                d["_business_type"] = business_type
                out.append(d)
        except Exception:
            continue
    out.sort(key=lambda d: d.get("id") or 0, reverse=True)
    return out


def act_on_application(business_type: str, apply_id: int, approver_id: int,
                       action: int, comment: str = "") -> dict:
    """审批动作：action 1=通过 2=拒绝。返回 {ok, message, next_level}。
    幂等护栏：单据已离开审批态（status 不在 1/2）→ 拒绝重复操作。"""
    row = _load_apply(business_type, apply_id)
    if not row:
        return {"ok": False, "message": "申请单不存在。"}
    if row.get("status") not in (STATUS_PENDING, STATUS_IN_FLOW):
        return {"ok": False, "message": f"该申请单已处理（当前状态 {row.get('status')}），无需重复操作。"}
    current = row.get("current_approver_id")
    if current is None:
        return {"ok": False, "message": "该申请单未解析出审批人，请在后台配置审批链/角色绑定后重试。"}
    if current != approver_id:
        return {"ok": False, "message": "您不是该申请单的当前审批人。"}
    # 当前级 = 已产生审批记录数 + 1
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM oa_approval_record WHERE apply_no=%s",
                        (row.get("apply_no"),))
            done_levels = cur.fetchone()[0]
            level = done_levels + 1
            approver_name = None
            cur.execute("SELECT name FROM employee WHERE id=%s", (approver_id,))
            r = cur.fetchone()
            if r:
                approver_name = r[0]
            cur.execute(
                "INSERT INTO oa_approval_record (apply_id, apply_no, approver_id, "
                "approver_name, approval_level, action, comment, approval_time) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW())",
                (apply_id, row.get("apply_no"), approver_id, approver_name, level,
                 action, comment))
            table = registry.TABLE_MAP[business_type]
            if action == ACT_REJECT:
                cur.execute(f"UPDATE {table} SET status=4 WHERE id=%s", (apply_id,))
                message = "已拒绝该申请。"
                next_level = None
            else:
                next_approver = _resolve_level_approver(business_type, level + 1,
                                                        row.get("dept_id"))
                if next_approver is None:
                    cur.execute(f"UPDATE {table} SET status=3 WHERE id=%s", (apply_id,))
                    message = "已通过，流程走完。"
                    next_level = None
                elif next_approver == -1:
                    # 下一级配置存在但没绑定人——单子留在审批中，等后台配人后
                    # 由「催办/重解析」接续（v1 先留在 status=2 不动）
                    message = "已通过本级，但下一级审批人未配置，请联系管理员。"
                    next_level = -1
                else:
                    cur.execute(
                        f"UPDATE {table} SET status=2, current_approver_id=%s WHERE id=%s",
                        (next_approver, apply_id))
                    cur.execute("SELECT name FROM employee WHERE id=%s", (next_approver,))
                    r2 = cur.fetchone()
                    message = f"已通过，转交下一级审批（{(r2[0] if r2 else '待定')}）。"
                    next_level = next_approver
        conn.commit()
        conn.close()
    except Exception as e:
        return {"ok": False, "message": f"审批动作失败：{e!r}"}
    return {"ok": True, "message": message, "next_level": next_level}


def notify_approver(approver_id: int, business_type: str, row: dict) -> bool:
    """飞书推卡给审批人（软依赖：无 open_id 绑定/渠道未启用时静默 False——
    审批人仍可通过「查我的待审批单」主动操作）"""
    try:
        from ..channels.feishu import channel as feishu_channel
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("SELECT open_id FROM feishu_employee WHERE employee_id=%s LIMIT 1",
                        (approver_id,))
            r = cur.fetchone()
        conn.close()
        if not r or not r[0]:
            return False
        text = (f"您有一笔待审批申请：\n"
                f"申请人：{row.get('user_name')}（{row.get('apply_no')}）\n"
                f"{summary_text(business_type, row)}\n"
                f"回复「同意 {row.get('apply_no')}」或「拒绝 {row.get('apply_no')}」即可处理。")
        return feishu_channel.send_to_user(r[0], text)
    except Exception:
        return False


def list_applications(business_type: str = "", status=None) -> list[dict]:
    """申请单看板（后台页）：全部申请单（可按业务/状态过滤），每行带业务码/名"""
    tables = (registry.TABLE_MAP if not business_type
              else ({business_type: registry.TABLE_MAP[business_type]}
                    if business_type in registry.TABLE_MAP else {}))
    out = []
    for bt, table in tables.items():
        try:
            conn = _conn()
            with conn.cursor() as cur:
                if status is not None:
                    cur.execute(f"SELECT * FROM {table} WHERE status=%s "
                                "ORDER BY id DESC LIMIT 100", (status,))
                else:
                    cur.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 100")
                rows = cur.fetchall()
                cols = [d[0] for d in cur.description]
            conn.close()
            for row in rows:
                d = dict(zip(cols, row))
                d["_business_type"] = bt
                d["_business_name"] = registry.BIZ_NAME_MAP.get(bt, bt)
                out.append(d)
        except Exception:
            continue
    out.sort(key=lambda d: d.get("id") or 0, reverse=True)
    return out


# ---------------- 审批指令入口（飞书/Web 对话审批） ----------------
# 审批人回复「同意/拒绝 [单号]」，或仅一笔待审批时回复「同意/拒绝」。
# 位置在待确认单之后、SOP 之前——"同意/拒绝"不在待确认词表，两者不冲突。

import re  # noqa: E402

_COMMAND = re.compile(r"(同意|通过|拒绝|驳回)\s*([A-Z]+\d{8,})")
_PREFIX_BIZ = registry.PREFIX_MAP


def try_approval_command(query: str, user: str) -> dict | None:
    """审批指令入口：命中返回 {answer, route}，否则 None（漏回正常路由）。
    身份经 acl.resolve_employee（飞书 open_id / 后台账号 → employee.id），
    与申请单 current_approver_id 比对——不是当前审批人直接拒绝操作。"""
    from ..service import acl
    emp_id = acl.resolve_employee(user)
    if not emp_id:
        return None
    q = (query or "").strip()
    m = _COMMAND.search(q)
    if m:
        action = ACT_APPROVE if m.group(1) in ("同意", "通过") else ACT_REJECT
        apply_no = m.group(2)
        pend = pending_by_approver(emp_id)
        hit = next((p for p in pend if p.get("apply_no") == apply_no), None)
        if not hit:
            return {"answer": f"未找到您名下的待审批单「{apply_no}」。", "route": "APPROVAL"}
    elif q in ("同意", "通过", "拒绝", "驳回"):
        action = ACT_APPROVE if q in ("同意", "通过") else ACT_REJECT
        pend = pending_by_approver(emp_id)
        if not pend:
            return None   # 没有待审批单 → 落回闲聊等正常路由
        if len(pend) > 1:
            return {"answer": "您有多笔待审批单，请回复「同意/拒绝 + 单号」指定要处理的申请。",
                    "route": "APPROVAL"}
        hit = pend[0]
    else:
        return None
    result = act_on_application(hit["_business_type"], hit["id"], emp_id, action)
    return {"answer": result["message"], "route": "APPROVAL"}
