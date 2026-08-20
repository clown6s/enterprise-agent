# -*- coding: utf-8 -*-
"""
Append-only 事件日志 + surface 投影（dsh 一-4）。

事件日志是唯一事实源：纯追加、seq 连续、深冻结。给模型的历史是 surface 投影
（按预算截断最近 N 轮），旧事件仍可审计和重建——审计完整性与上下文大小解耦。

第一版：双写旁路——history.save_turn 同时写事件日志（不切主读），
等验证稳定后再切 history.recent_turns 从事件日志投影读取。
"""
import json
import os
import threading
from datetime import datetime

from .. import config
from ..time_context import now as business_now

_LOCK = threading.Lock()
_SEQ: dict = {}   # conversation_id → seq 计数（内存，进程级；事件日志自身是事实源）
_EVENT_DIR = os.path.join(config.LOG_DIR, "events")


def _path(conversation_id: str) -> str:
    """每个会话一个事件日志文件（JSONL append-only）。"""
    safe = "".join(c for c in (conversation_id or "default") if c.isalnum() or c in "-_")[:64]
    os.makedirs(_EVENT_DIR, exist_ok=True)
    return os.path.join(_EVENT_DIR, f"{safe}.jsonl")


def append(conversation_id: str, kind: str, payload: dict) -> int:
    """追加一条事件，返回 seq。纯追加不改旧记录。

    kind: turn_user / turn_assistant / sop_step / pending_save / pending_confirm /
          status_change / adopt / ingest 等。
    payload: 事件内容（不可变快照，调用方传 dict 副本）。
    """
    with _LOCK:
        seq = _SEQ.get(conversation_id, 0) + 1
        _SEQ[conversation_id] = seq
    entry = {
        "seq": seq,
        "ts": business_now().isoformat(),
        "conversation_id": conversation_id,
        "kind": kind,
        "payload": payload,
    }
    try:
        with open(_path(conversation_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass   # 软依赖：日志写失败不阻断主链路
    return seq


def project_turns(conversation_id: str, max_turns: int = 10) -> list[dict]:
    """Surface 投影：从事件日志折叠最近 max_turns 轮对话（user+assistant 配对）。

    这是给模型的历史上下文——按预算截断，旧事件仍在日志中可审计。
    返回 [{role, content, seq}, ...] 格式，与 history.recent_turns 兼容。
    """
    events = _read_all(conversation_id)
    turns = []
    for e in events:
        if e["kind"] == "turn_user":
            turns.append({"role": "user", "content": e["payload"].get("query", ""),
                          "seq": e["seq"]})
        elif e["kind"] == "turn_assistant":
            turns.append({"role": "assistant", "content": e["payload"].get("answer", ""),
                          "seq": e["seq"]})
    # 截断最近 max_turns * 2 条（一轮 = user + assistant）
    limit = max_turns * 2
    return turns[-limit:]


def _read_all(conversation_id: str) -> list[dict]:
    """读全部事件（按 seq 顺序）。"""
    try:
        path = _path(conversation_id)
        if not os.path.exists(path):
            return []
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except (json.JSONDecodeError, ValueError):
                    continue
        events.sort(key=lambda e: e.get("seq", 0))
        return events
    except OSError:
        return []


def replay(conversation_id: str) -> list[dict]:
    """完整回放（审计用）：返回全部事件，不改不删。"""
    return _read_all(conversation_id)
