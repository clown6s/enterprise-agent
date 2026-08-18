# -*- coding: utf-8 -*-
"""
能力总线（2026-08-18）：意图路由从"代码规则表"升级为"数据驱动注册表"。

capability 表：每行一个能力配置（code/pattern/priority/status/tool）。
与内置种子规则（intent_rules.INTENT_RULES）按 code 合并：
- 表里有同 code 行 → 覆盖 pattern/priority/status/tool（后台可调，30s TTL 生效）
- status=0 → 禁用该规则（与 skill 表开关叠加）
- 表空/不可用 → 全部用内置种子（软依赖，行为与从前一致）
管理后台可增改：调触发正则、调优先级、停用某条——不用改代码。
新 handler 类型仍需代码（全新能力类型写一个 handler 类，一次性类扩展）。
"""
import threading
import time

from .intent import rules as intent_rules

_TTL = 30

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "rows": None}


def _load_db_rows() -> list[dict]:
    try:
        import pymysql
        from . import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS capability ("
                "code VARCHAR(64) PRIMARY KEY,"
                "pattern VARCHAR(512) DEFAULT '',"
                "priority INT DEFAULT 100,"
                "status TINYINT DEFAULT 1,"
                "tool VARCHAR(64) DEFAULT '',"
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            cur.execute("SELECT code, pattern, priority, status, tool FROM capability")
            rows = [{"code": r[0], "pattern": r[1] or "", "priority": r[2],
                     "status": r[3], "tool": r[4] or ""} for r in cur.fetchall()]
        conn.commit()
        conn.close()
        return rows
    except Exception:
        return []


def _merged() -> list[intent_rules.IntentRule]:
    """内置种子 + DB 覆盖（按 code），TTL 30s"""
    now = time.time()
    with _lock:
        if _cache["rows"] is None or now - _cache["ts"] > _TTL:
            db = {r["code"]: r for r in _load_db_rows()}
            merged = []
            for rule in intent_rules.INTENT_RULES:
                row = db.get(rule.name)
                merged.append(intent_rules.IntentRule(
                    name=rule.name,
                    pattern=row["pattern"] if row else rule.pattern,
                    handler=rule.handler,
                    priority=row["priority"] if row else rule.priority,
                    tool=row["tool"] if row else rule.tool,
                ) if (not row or row["status"]) else None)
            merged = [r for r in merged if r is not None]
            _cache["rows"] = merged
            _cache["ts"] = now
        return _cache["rows"]


def dispatch(query: str):
    """按优先级遍历合并后的能力表；命中且 handler 有结果即返回，否则 None。
    与 intent_rules.match 同语义（handler 返回 None 继续下一规则）。
    正则异常按不命中处理（后台配了非法正则不该炸路由——安全扫描补的护栏）"""
    import re as _re
    from . import tools
    for rule in sorted(_merged(), key=lambda r: r.priority):
        if rule.tool and not tools.is_enabled(rule.tool):
            continue
        try:
            hit = rule.matches(query)
        except _re.error:
            hit = False
        if hit:
            result = rule.handler(query)
            if result is not None:
                return result
    return None


def list_capabilities() -> list[dict]:
    """管理后台展示：合并后的当前生效配置"""
    out = []
    for r in sorted(_merged(), key=lambda r: r.priority):
        out.append({"code": r.name, "pattern": r.pattern, "priority": r.priority,
                    "tool": r.tool})
    return out


def save_capability(code: str, pattern: str, priority: int, status: int,
                    tool: str = "") -> None:
    """后台保存一条能力配置（code 必须是内置种子规则名——handler 引用）。
    非法正则直接拒存（防 ReDoS/崩溃型 pattern 进注册表）"""
    import re as _re
    try:
        _re.compile(pattern)
    except _re.error:
        print(f"[capabilities] 非法正则拒存: {pattern[:60]!r}")
        return
    try:
        import pymysql
        from . import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS capability ("
                "code VARCHAR(64) PRIMARY KEY,"
                "pattern VARCHAR(512) DEFAULT '',"
                "priority INT DEFAULT 100,"
                "status TINYINT DEFAULT 1,"
                "tool VARCHAR(64) DEFAULT '',"
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            cur.execute(
                "INSERT INTO capability (code, pattern, priority, status, tool) "
                "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "pattern=VALUES(pattern), priority=VALUES(priority), "
                "status=VALUES(status), tool=VALUES(tool)",
                (code, pattern, priority, status, tool))
        conn.commit()
        conn.close()
        with _lock:
            _cache["ts"] = 0.0
    except Exception:
        pass


def delete_capability(code: str) -> None:
    """删除 DB 覆盖行 → 回退内置种子配置"""
    try:
        import pymysql
        from . import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM capability WHERE code=%s", (code,))
        conn.commit()
        conn.close()
        with _lock:
            _cache["ts"] = 0.0
    except Exception:
        pass
