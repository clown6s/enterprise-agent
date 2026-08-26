# -*- coding: utf-8 -*-
"""
审批状态事件日志（dsh 一-12：状态即日志折叠）。

原则：每次状态变更写 append-only 事件；当前状态由事件折叠得到（last-wins）；
旧状态不伪造历史事件；事件表是审计事实源，申请表 status 是兼容投影。

第一版：旁路双写——approval.act_on_application 在 UPDATE status 时同时
写一条状态事件。事件表查询提供回放/时间线能力，不改现有 status 读取。
"""
import json
import logging
import os
from datetime import datetime

from .. import config, mysql_store

log = logging.getLogger(__name__)

# 合法状态转移边（非法转移拒绝并记录原因）
# 1=待审批 2=审批中 3=已通过 4=已拒绝 5=已撤回
_TRANSITIONS = {
    (1, 2): "forwarded",       # 转交下一级
    (1, 3): "approved",         # 直接通过（无下一级）
    (1, 4): "rejected",         # 拒绝
    (2, 3): "approved",         # 多级通过
    (2, 4): "rejected",         # 多级拒绝
    (1, 5): "withdrawn",        # 撤回
    (2, 5): "withdrawn",        # 审批中撤回
}


def _conn():
    return mysql_store.connection()


def _ensure_table() -> None:
    """幂等建事件表（不改现有表结构）"""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS approval_status_event ("
                    "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                    "apply_no VARCHAR(32) NOT NULL,"
                    "from_status TINYINT,"
                    "to_status TINYINT NOT NULL,"
                    "action VARCHAR(32) NOT NULL,"
                    "actor_id BIGINT,"
                    "comment TEXT,"
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                    "KEY idx_apply (apply_no, id))")
            conn.commit()
    except Exception as e:
        log.warning(f"状态事件表建表失败: {e!r}")


def record(apply_no: str, from_status: int, to_status: int,
           action: str, actor_id: int = 0, comment: str = "") -> bool:
    """记录一条状态变更事件（旁路软依赖，失败不阻断主流程）。

    非法转移记录但标记 action='invalid_transition'，不阻断——
    审计层发现后人工处理。
    """
    transition = (from_status, to_status)
    if transition not in _TRANSITIONS:
        action = "invalid_transition"
        log.warning(f"非法状态转移 {from_status}→{to_status}（apply_no={apply_no}）")
    try:
        _ensure_table()
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO approval_status_event "
                    "(apply_no, from_status, to_status, action, actor_id, comment) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (apply_no, from_status, to_status, action, actor_id, comment))
            conn.commit()
        return True
    except Exception as e:
        log.warning(f"状态事件记录失败: {e!r}")
        return False


def replay(apply_no: str) -> list[dict]:
    """完整回放：返回该申请单的全部状态事件（时间线），按时间正序。"""
    try:
        _ensure_table()
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT from_status, to_status, action, actor_id, comment, created_at "
                    "FROM approval_status_event WHERE apply_no=%s ORDER BY id", (apply_no,))
                rows = cur.fetchall()
        return [{"from": r[0], "to": r[1], "action": r[2],
                 "actor_id": r[3], "comment": r[4], "ts": str(r[5])}
                for r in rows]
    except Exception:
        return []


def current(apply_no: str) -> int | None:
    """从事件折叠当前状态（last-wins）。无事件返回 None。"""
    events = replay(apply_no)
    if not events:
        return None
    return events[-1]["to"]


def is_valid_transition(from_status: int, to_status: int) -> bool:
    """检查状态转移是否合法"""
    return (from_status, to_status) in _TRANSITIONS
