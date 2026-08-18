# -*- coding: utf-8 -*-
"""
采纳升级直答层（智能路由架构 4/5）：👍 采纳过的问答自动成为确定性知识——
相同/近似问法直接返回已采纳答案（秒回、零 LLM、零漂移）。

- lookup：问法归一化（去空白/标点/语气词/大小写）精确匹配——只精确不模糊，
  防"你好"误命中"你好吗"；近似语义留给 KB 慢路径
- penalize：👎 回调时失败计数 +1，满 2 次停用（回慢路径重学）——
  与词汇对齐的同款负向闭环
- 采纳写入由 feedback.adopt 统一完成（质量门之后），本层只读+惩罚
"""
import threading
import time

from .. import config

_TTL = 60

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "rows": None}


def _norm(q: str) -> str:
    """问法归一化：去空白/标点/语气词，小写——比对口径与入库口径一致
    （连字符放字符类末尾，避免转义告警与范围歧义）"""
    import re
    return re.sub(r"[\s，。！？?、；;：:""''「」『』（）()～~.—]+", "",
                  (q or "").lower().rstrip("？?吗呢嘛"))


def _ensure_columns() -> None:
    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM feedback_adopt LIKE 'query_norm'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE feedback_adopt ADD COLUMN query_norm "
                            "VARCHAR(128) DEFAULT ''")
            cur.execute("SHOW COLUMNS FROM feedback_adopt LIKE 'penalties'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE feedback_adopt ADD COLUMN penalties INT DEFAULT 0")
            cur.execute("SHOW COLUMNS FROM feedback_adopt LIKE 'status'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE feedback_adopt ADD COLUMN status TINYINT DEFAULT 1")
            # 历史采纳行回填归一化问法（新列首次迁移时）
            cur.execute("SELECT id, query FROM feedback_adopt WHERE query_norm=''")
            for rid, q in cur.fetchall():
                cur.execute("UPDATE feedback_adopt SET query_norm=%s WHERE id=%s",
                            (_norm(q), rid))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _load() -> dict[str, dict]:
    """query_norm → {answer, source}（status=1 才生效）"""
    try:
        _ensure_columns()
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SELECT query_norm, answer, source FROM feedback_adopt "
                        "WHERE status=1 AND query_norm<>''")
            rows = {r[0]: {"answer": r[1], "source": r[2] or ""} for r in cur.fetchall()}
        conn.close()
        return rows
    except Exception:
        return {}


def lookup(query: str) -> dict | None:
    """已采纳问答直答命中；未命中返回 None（走慢路径）"""
    norm = _norm(query)
    if not norm:
        return None
    now = time.time()
    with _lock:
        if _cache["rows"] is None or now - _cache["ts"] > _TTL:
            _cache["rows"] = _load()
            _cache["ts"] = now
        hit = _cache["rows"].get(norm)
        return dict(hit) if hit else None


def penalize(query: str) -> None:
    """👎 回调：失败计数 +1，满 2 停用（回答被用户否定两次 → 回慢路径重学）"""
    norm = _norm(query)
    if not norm:
        return
    try:
        import pymysql
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("UPDATE feedback_adopt SET penalties=penalties+1, "
                        "status=IF(penalties+1>=2, 0, 1) WHERE query_norm=%s AND status=1",
                        (norm,))
        conn.commit()
        conn.close()
        with _lock:
            _cache["ts"] = 0.0
    except Exception:
        pass


def clear_cache() -> None:
    with _lock:
        _cache["ts"] = 0.0
