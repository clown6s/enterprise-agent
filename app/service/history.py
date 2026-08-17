# -*- coding: utf-8 -*-
"""
多轮对话历史（跨轮上下文的地基，2026-08-17 新增）：

- save_turn()      每轮问答落 chat_history 表（软依赖：MySQL 不可用只记日志）
- recent_turns()   最近 N 轮（旧→新，总量压缩）供提示词注入
- history_block()  转提示词片段

用途：理解指代（"那他的工号呢"）与延续话题；回答依据仍以【资料】为准。
兜底句式/GUARD 拦截的轮次不落库（无信息量且注入文本不可回流）。
"""
from .. import config

_MAX_TURNS = 4           # 注入最近 4 轮
_MAX_TURN_CHARS = 300    # 单轮单侧截断
_TOTAL_CAP = 1200        # 注入总量上限

_ensured = False


def _conn():
    import pymysql
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=3)


def _ensure_table() -> None:
    global _ensured
    if _ensured:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS chat_history ("
                "id BIGINT AUTO_INCREMENT PRIMARY KEY,"
                "conversation_id VARCHAR(64) NOT NULL,"
                "query TEXT NOT NULL, answer TEXT NOT NULL,"
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "KEY idx_conv (conversation_id, id))")
        conn.commit()
        conn.close()
        _ensured = True
    except Exception as e:
        print(f"[history] 建表失败: {e!r}")


def save_turn(conversation_id: str, query: str, answer: str) -> None:
    """落一轮问答（软依赖；空会话/空答案跳过）"""
    if not conversation_id or not query.strip() or not answer.strip():
        return
    try:
        _ensure_table()
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_history (conversation_id, query, answer) VALUES (%s,%s,%s)",
                (conversation_id, query.strip()[:500], answer.strip()[:2000]))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[history] 历史落库失败: {e!r}")


def recent_turns(conversation_id: str, max_turns: int = _MAX_TURNS) -> list[dict]:
    """最近 N 轮 [{q, a}]（旧→新，总量压缩）；失败/无会话返回空列表（软依赖）"""
    if not conversation_id:
        return []
    try:
        _ensure_table()
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT query, answer FROM chat_history WHERE conversation_id=%s "
                "ORDER BY id DESC LIMIT %s", (conversation_id, max_turns))
            rows = cur.fetchall()
        conn.close()
        turns = [{"q": r[0], "a": r[1]} for r in reversed(rows)]
        budget = _TOTAL_CAP
        out = []
        for t in turns:
            q, a = t["q"][:_MAX_TURN_CHARS], t["a"][:_MAX_TURN_CHARS]
            budget -= len(q) + len(a)
            if budget < 0:
                break
            out.append({"q": q, "a": a})
        return out
    except Exception:
        return []


def history_block(turns: list[dict]) -> str:
    """转提示词片段；空历史返回空串"""
    if not turns:
        return ""
    lines = ["【对话历史（仅供理解指代与延续话题，回答依据仍以【资料】为准）】"]
    for t in turns:
        lines.append(f"问：{t['q']}")
        lines.append(f"答：{t['a']}")
    return "\n".join(lines)
