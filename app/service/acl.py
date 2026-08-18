# -*- coding: utf-8 -*-
"""
文档访问控制（doc_acl → Milvus 检索 expr 过滤，2026-08-17 接线）。

与 rag-admin 前端约定：doc_acl.grants 空数组 = 全员可见；非空 = 仅限
"employee:ID" 名单（ID 为数字员工表 digital_employee 的 id）。

身份映射（2026-08-18 落地，此前恒 None）：
- Web 渠道：user = 后台用户名 → sys_user.employee_id（后台用户管理页维护）
- 飞书渠道：user = open_id（ou_ 前缀）→ 通讯录 API 手机号 →
  sys_user.mobile 匹配同一账号 → employee_id
- 映射结果缓存 60s（负结果也缓存，防每条消息打飞书 API）

安全语义：
- doc_acl 无任何行 → 不过滤（expr=None，行为与从前一致）
- 有行且身份未映射 → 仅可见全员文档（安全默认：未知身份不放行受限文档）
- 软依赖：ACL 读取/构造失败 → 不过滤，检索不因权限系统出错而断
"""
import json
import threading
import time

import httpx

_TTL = 60

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "state": None}
# 身份映射缓存：user → (ts, employee_id)（None 也是有效值=未知身份）
_emp_cache: dict[str, tuple[float, int | None]] = {}
# 飞书 tenant_access_token（通讯录 API 用，与 app token 是不同端点）
_tenant_token: dict = {"token": "", "expire": 0.0}


def _ensure_columns() -> None:
    """sys_user 补 employee_id/mobile 列（与 rag-admin 共用表——加列是加性
    变更，前端不感知；列已存在/表不可用都跳过，软依赖）"""
    try:
        import pymysql
        from .. import config
        conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                               user="root", password=config.MYSQL_PASSWORD,
                               database=config.MYSQL_DB, charset="utf8mb4",
                               connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM sys_user LIKE 'employee_id'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE sys_user ADD COLUMN employee_id INT NULL")
            cur.execute("SHOW COLUMNS FROM sys_user LIKE 'mobile'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE sys_user ADD COLUMN mobile VARCHAR(32) NULL")
        conn.commit()
        conn.close()
    except Exception:
        pass


def _tenant_access_token() -> str:
    """飞书 tenant_access_token（通讯录 API 鉴权；与 channels 的 app token
    是不同端点——acl 不 import channels 防循环依赖）"""
    if _tenant_token["token"] and time.time() < _tenant_token["expire"] - 60:
        return _tenant_token["token"]
    from .. import config
    try:
        resp = httpx.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": config._env("FEISHU_APP_ID"),
                  "app_secret": config._env("FEISHU_APP_SECRET")},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            _tenant_token["token"] = data["tenant_access_token"]
            _tenant_token["expire"] = time.time() + data.get("expire", 7200)
    except (httpx.HTTPError, ValueError):
        pass
    return _tenant_token["token"]


def _feishu_mobile(open_id: str) -> str:
    """open_id → 手机号（通讯录 scope：contact:user.base:readonly，已开通）。
    失败/无手机号返回空串（调用方按未知身份处理）"""
    token = _tenant_access_token()
    if not token:
        return ""
    try:
        resp = httpx.get(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}",
            params={"user_id_type": "open_id"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            return ""
        user = (data.get("data") or {}).get("user") or {}
        return user.get("mobile") or ""
    except (httpx.HTTPError, ValueError):
        return ""


def _emp_by_column(column: str, value: str) -> int | None:
    """sys_user.{column}=value → employee_id（NULL 视为未绑定）"""
    import pymysql
    from .. import config
    conn = pymysql.connect(host=config.MYSQL_HOST, port=config.MYSQL_PORT,
                           user="root", password=config.MYSQL_PASSWORD,
                           database=config.MYSQL_DB, charset="utf8mb4",
                           connect_timeout=3)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT employee_id FROM sys_user "
                        f"WHERE {column}=%s AND employee_id IS NOT NULL LIMIT 1", (value,))
            row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def resolve_employee(user: str) -> int | None:
    """user → 数字员工 id（doc_acl 的 employee:ID 口径）。
    飞书 open_id 以 ou_ 开头 → 通讯录手机号匹配；其余按后台用户名匹配。
    映射结果缓存 60s（含负结果）；任何失败返回 None（未知身份）。"""
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
            mobile = _feishu_mobile(user)
            if mobile:
                emp = _emp_by_column("mobile", mobile)
        else:
            emp = _emp_by_column("username", user)
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


def allowed_expr(user: str) -> str | None:
    """用户 → Milvus 过滤 expr；None=不过滤。软依赖：任何失败返回 None。"""
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
        print(f"[acl] 权限表达式构造失败（不过滤）: {e!r}")
        return None
