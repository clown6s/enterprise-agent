# -*- coding: utf-8 -*-
"""
文档访问控制（doc_acl → Milvus 检索 expr 过滤，2026-08-17 接线）。

与 rag-admin 前端约定：doc_acl.grants 空数组 = 全员可见；非空 = 仅限
"employee:ID" 名单（ID 为数字员工表 id）。用户维度（open_id → employee id）
映射待身份层落地（遗留②）——当前 resolve_employee 恒 None（未知身份）。

安全语义：
- doc_acl 无任何行 → 不过滤（expr=None，行为与从前一致）
- 有行且身份未映射 → 仅可见全员文档（安全默认：未知身份不放行受限文档）
- 软依赖：ACL 读取/构造失败 → 不过滤，检索不因权限系统出错而断
"""
import threading
import time

_TTL = 60

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "state": None}


def resolve_employee(user: str) -> int | None:
    """user（飞书 open_id / 后台用户名）→ 数字员工 id。
    身份层（通讯录/用户映射）未落地，恒 None——落地后在此实现映射。"""
    return None


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
            ids = set()
            for g in (grants or "").replace("；", ",").split(","):
                g = g.strip()
                if g.startswith("employee:") and g[9:].isdigit():
                    ids.add(int(g[9:]))
                elif g.isdigit():
                    ids.add(int(g))
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
