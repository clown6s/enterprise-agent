# -*- coding: utf-8 -*-
"""
文档访问控制（doc_acl → Milvus 检索 expr 过滤，2026-08-17 接线）。

与 rag-admin 前端约定：doc_acl.grants 空数组 = 全员可见；非空 = 仅限
"employee:ID" 名单（ID 为数字员工表 digital_employee 的 id）。

身份映射（2026-08-18 落地，此前恒 None）：
- Web 渠道：user = 后台用户名 → sys_user.employee_id（后台用户管理页维护）
- 飞书渠道：user = open_id（ou_ 前缀）→ feishu_employee.employee_id 直查
  （rag-admin 维护的飞书员工表，open_id 主数据；曾设计通讯录 API
  手机号匹配，发现表已存在后废弃——零 API 依赖更稳）
- 映射结果缓存 60s（负结果也缓存，防每条消息查库）

安全语义：
- doc_acl 无任何行 → 不过滤（expr=None，行为与从前一致）
- 有行且身份未映射 → 仅可见全员文档（安全默认：未知身份不放行受限文档）
- 软依赖：ACL 读取/构造失败 → 不过滤，检索不因权限系统出错而断
"""
import json
import threading
import time
import logging
log = logging.getLogger(__name__)

_TTL = 60

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "state": None}
# 身份映射缓存：user → (ts, employee_id)（None 也是有效值=未知身份）
_emp_cache: dict[str, tuple[float, int | None]] = {}


def _ensure_columns() -> None:
    """身份列懒迁移（与 rag-admin 共用表——加列是加性变更，前端不感知；
    列已存在/表不可用都跳过，软依赖）：
    - sys_user.employee_id / mobile：Web 账号 → 数字员工绑定
    - feishu_employee.employee_id：飞书 open_id → 数字员工绑定"""
    try:
        import pymysql
        from .. import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            for table, cols in (("sys_user", ["employee_id", "mobile"]),
                                ("feishu_employee", ["employee_id"])):
                for col in cols:
                    cur.execute(f"SHOW COLUMNS FROM {table} LIKE '{col}'")
                    if not cur.fetchone():
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} "
                                    f"{'VARCHAR(32)' if col == 'mobile' else 'INT'} NULL")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _emp_by_column(table: str, column: str, value: str) -> int | None:
    """{table}.{column}=value → employee_id（NULL 视为未绑定）"""
    import pymysql
    from .. import config
    conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                           user="root", password=config.MYSQL_PASSWORD,
                           database=config.MYSQL_DB, charset="utf8mb4",
                           connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT employee_id FROM {table} "
                        f"WHERE {column}=%s AND employee_id IS NOT NULL LIMIT 1", (value,))
            row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def resolve_employee(user: str) -> int | None:
    """user → 数字员工 id（doc_acl 的 employee:ID 口径）。
    飞书 open_id 以 ou_ 开头 → feishu_employee 表直查（2026-08-18 定案：
    曾设计通讯录 API 手机号匹配——发现 rag-admin 已有 feishu_employee 表
    持 open_id 主数据，直绑零 API 依赖，手机号方案废弃）；其余按后台
    用户名查 sys_user。映射结果缓存 60s（含负结果）；失败返回 None
    （未知身份，安全默认只看全员文档）。"""
    user = (user or "").strip()
    if not user:
        return None
    now = time.time()
    hit = _emp_cache.get(user)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    _ensure_columns()
    emp = None
    try:
        if user.startswith("ou_"):
            emp = _emp_by_column("feishu_employee", "open_id", user)
        else:
            emp = _emp_by_column("sys_user", "username", user)
    except Exception:
        emp = None
    _emp_cache[user] = (now, emp)
    return emp


def _parse_grants(grants: str) -> set[int]:
    """doc_acl.grants → 数字员工 id 集合。兼容两种存储形态：管理接口写入的
    JSON 数组（["employee:3"]）与裸逗号串（employee:3,employee:4）——
    曾只按逗号串解析，JSON 形态的引号把前缀匹配全带崩，受限文档从未
    生效过（doc_acl 长期为空所以无实况，解析修好后按 JSON 优先）。"""
    ids: set[int] = set()
    items: list[str] = []
    text = grants or ""
    try:
        val = json.loads(text)
        if isinstance(val, list):
            items = [str(v) for v in val]
        elif isinstance(val, str):
            items = val.replace("；", ",").split(",")
    except ValueError:
        items = text.replace("；", ",").split(",")
    for g in items:
        g = g.strip()
        if g.startswith("employee:") and g[9:].isdigit():
            ids.add(int(g[9:]))
        elif g.isdigit():
            ids.add(int(g))
    return ids


def _load_state() -> dict:
    """{restricted: {doc_id: {emp_id...}}, all_docs: [doc_id...]}"""
    import pymysql
    from .. import config, milvus_store
    conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT, user="root",
                           password=config.MYSQL_PASSWORD, database=config.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=3)
    restricted: dict[str, set] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id, grants FROM doc_acl")
        for doc_id, grants in cur.fetchall():
            ids = _parse_grants(grants)
            if ids:
                restricted[doc_id] = ids
    conn.close()
    docs = [d["docId"] for d in milvus_store.list_documents()]
    return {"restricted": restricted, "all_docs": docs}


def field_visible(table: str, column: str, user: str) -> bool:
    """字段级权限（PII 列如员工电话）：field_acl 有限制行且用户不在
    grants 名单 → False；无限制行/表不可用 → True（默认可见——表级
    grants 是外层防线）。安全默认方向与 doc_acl 一致。"""
    try:
        import pymysql
        from .. import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS field_acl ("
                "id INT AUTO_INCREMENT PRIMARY KEY,"
                "table_name VARCHAR(64) NOT NULL,"
                "column_name VARCHAR(64) NOT NULL,"
                "grants TEXT,"
                "status TINYINT DEFAULT 1,"
                "UNIQUE KEY uq_tc (table_name, column_name))")
            cur.execute("SELECT grants FROM field_acl "
                        "WHERE table_name=%s AND column_name=%s AND status=1",
                        (table, column))
            row = cur.fetchone()
        conn.close()
        if not row:
            return True
        emp = resolve_employee(user)
        ids = _parse_grants(row[0] or "")
        return not ids or emp in ids
    except Exception:
        # 字段级权限查询异常 fail-closed——权限故障时宁可隐藏 PII 也不放行
        log.warning(f"字段级权限查询异常（fail-closed）: {table}.{column}")
        return False


def save_field_acl(table: str, column: str, grants: str, status: int = 1) -> None:
    """后台维护字段级权限（grants 同 doc_acl 口径）"""
    try:
        import pymysql
        from .. import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS field_acl ("
                "id INT AUTO_INCREMENT PRIMARY KEY,"
                "table_name VARCHAR(64) NOT NULL,"
                "column_name VARCHAR(64) NOT NULL,"
                "grants TEXT,"
                "status TINYINT DEFAULT 1,"
                "UNIQUE KEY uq_tc (table_name, column_name))")
            cur.execute(
                "INSERT INTO field_acl (table_name, column_name, grants, status) "
                "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "grants=VALUES(grants), status=VALUES(status)",
                (table, column, grants or "", status))
        conn.commit()
        conn.close()
    except Exception:
        pass


def allowed_expr(user: str) -> str | None:
    """用户 → Milvus 过滤 expr；None=无 ACL 配置时不过滤。

    ACL 查询/解析异常必须 fail-closed（恒假 expr），不能把权限故障变成全量放行。"""
    try:
        with _lock:
            now = time.time()
            if _cache["state"] is None or now - _cache["ts"] > _TTL:
                _cache["state"] = _load_state()
                _cache["ts"] = now
            state = _cache["state"]
        if not state["restricted"]:
            return None
        emp = resolve_employee(user)
        visible = [d for d in state["all_docs"]
                   if d not in state["restricted"]
                   or (emp is not None and emp in state["restricted"][d])]
        if len(visible) == len(state["all_docs"]):
            return None
        if not visible:
            return 'document_id == "__none__"'   # 全部不可见：恒假表达式
        ids = '", "'.join(visible)
        return f'document_id in ["{ids}"]'
    except Exception as e:
        log.warning(f"权限表达式构造失败（fail-closed）: {e!r}")
        return 'document_id == "__none__"'
