# -*- coding: utf-8 -*-
"""
员工目录数据层（2026-08-18 统一数据源：demo_data 硬编码 → MySQL employee 表）。

- 懒建表 + 空表种子（demo_data 8 人灌入一次，幂等：COUNT=0 才灌）
- 30s TTL 列表缓存（目录查询高频；管理后台写操作清缓存立即生效）
- 软依赖语义：表不可用返回 None → 调用方答"员工目录暂不可用"，
  不漏到 KB 编造（数据查询钉死原则）
- 请假 SOP 名册仍走 OA（userId/deptId 是 OA 口径，提交 payload 依赖），
  对齐路径：employee 表将来加 oa_id 字段桥接
"""
import threading
import time

from . import config, demo_data

_TTL = 30

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "rows": None}


def _conn():
    import pymysql
    return pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=3)


def _ensure_table() -> None:
    """建表 + 空表种子（demo_data 8 人；mobile 留空由后台维护录入）"""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS employee ("
                "id INT AUTO_INCREMENT PRIMARY KEY,"
                "name VARCHAR(32) NOT NULL UNIQUE,"
                "dept VARCHAR(32) DEFAULT '',"
                "position VARCHAR(32) DEFAULT '',"
                "entry VARCHAR(16) DEFAULT '',"
                "employee_no VARCHAR(16) DEFAULT '',"
                "mobile VARCHAR(32) DEFAULT '',"
                "status TINYINT DEFAULT 1,"
                "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)")
            cur.execute("SELECT COUNT(*) FROM employee")
            if cur.fetchone()[0] == 0:
                cur.executemany(
                    "INSERT INTO employee (name, dept, position, entry, employee_no) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(e["name"], e["dept"], e["position"], e["entry"], e["employee_no"])
                     for e in demo_data.EMPLOYEES])
        conn.commit()
        conn.close()
    except Exception:
        pass


def all_active() -> list[dict] | None:
    """全量在职员工（30s TTL）；表不可用返回 None"""
    now = time.time()
    with _lock:
        if _cache["rows"] is None or now - _cache["ts"] > _TTL:
            try:
                _ensure_table()
                conn = _conn()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name, dept, position, entry, employee_no, mobile "
                        "FROM employee WHERE status=1 ORDER BY id")
                    rows = cur.fetchall()
                conn.close()
                _cache["rows"] = [{"name": r[0], "dept": r[1], "position": r[2],
                                   "entry": r[3], "employee_no": r[4], "mobile": r[5] or ""}
                                  for r in rows]
                _cache["ts"] = now
            except Exception:
                _cache["rows"] = None
                _cache["ts"] = now
        return _cache["rows"]


def by_name(name: str) -> dict | None:
    """单人在职记录；表不可用/不存在返回 None"""
    rows = all_active()
    if rows is None:
        return None
    for r in rows:
        if r["name"] == name:
            return r
    return None


def names() -> list[str] | None:
    rows = all_active()
    return [r["name"] for r in rows] if rows is not None else None


def clear_cache() -> None:
    """管理后台写操作后调用——下一次查询即新数据"""
    with _lock:
        _cache["ts"] = 0.0
