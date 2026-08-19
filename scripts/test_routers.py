# -*- coding: utf-8 -*-
"""
路由层测试（routers/）——补 0% 覆盖率缺口。

测试对象：admin / chat / health / feedback / auth / ingestion 路由。
策略：FastAPI TestClient + 注入测试 token 到 mysql_store._sessions（走真实
resolve_token 路径，不 monkeypatch）+ 跳过飞书 lifespan（避免 WS 挂起）。
聚焦：鉴权闸门（401）+ 只读端点契约（200 + success 形状），不碰真实 LLM。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import mysql_store  # noqa: E402
from app.channels import feishu as feishu_mod  # noqa: E402
from app.main import app  # noqa: E402

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("缺少 fastapi 测试依赖（httpx）；pip install httpx 后重跑")
    sys.exit(2)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


_TEST_TOK = "cov-test-token-xyz"
_FAKE_USER = {"id": 1, "username": "covtester", "role_key": "admin"}
_AUTH = f"Bearer {_TEST_TOK}"

# 整个测试会话跳过飞书 lifespan（避免 WS 重连噪声 + 通讯录同步副作用）。
# enabled() 在 lifespan 进入时读，全程置 False 即跳过。
_ORIG_ENABLED = feishu_mod.channel.enabled
feishu_mod.channel.enabled = lambda: False


def _setup_auth():
    """注入测试 token（真实 resolve_token 路径，不 mock）"""
    mysql_store._sessions[_TEST_TOK] = _FAKE_USER


def _teardown_auth():
    mysql_store._sessions.pop(_TEST_TOK, None)


def _client():
    """TestClient（飞书已全局跳过）"""
    return TestClient(app)


# ---------------- health（无鉴权） ----------------

def test_health():
    c = _client()
    with c:
        r = c.get("/healthz")
    check("health 200", r.status_code == 200)
    check("health UP", r.json().get("status") == "UP")


# ---------------- 鉴权闸门（无 token → 401） ----------------

def test_auth_gating():
    """受保护端点无 token 一律 401——覆盖每个 router 的 _auth 检查分支"""
    c = _client()
    with c:
        no_token = [
            ("GET", "/api/rag/admin/sops"),
            ("GET", "/api/rag/admin/approvals"),
            ("GET", "/api/rag/admin/auth/users"),
            ("GET", "/api/rag/admin/employees/directory"),
            ("GET", "/api/rag/admin/audit/dashboard"),
            ("GET", "/api/rag/admin/usage"),
            ("POST", "/api/rag/agent/chat"),     # chat 路由
        ]
        for method, path in no_token:
            resp = c.request(method, path, json={} if method == "POST" else None)
            check(f"401 无token {method} {path}", resp.status_code == 401,
                  f"got {resp.status_code}")


# ---------------- 带有效 token 的只读端点 ----------------

def test_read_endpoints():
    _setup_auth()
    try:
        c = _client()
        with c:
            # SOP 列表（触发 _seed_sop 幂等种子 + MySQL 读）
            r = c.get("/api/rag/admin/sops", headers={"Authorization": _AUTH})
            check("sops 200", r.status_code == 200)
            data = r.json()
            check("sops success", data.get("success") is True)
            codes = [s.get("code") for s in (data.get("data") or [])]
            check("sops 含 leave_action", "leave_action" in codes, str(codes))

            # 审批看板
            r = c.get("/api/rag/admin/approvals", headers={"Authorization": _AUTH})
            check("approvals 200", r.status_code == 200)
            check("approvals success", r.json().get("success") is True)

            # 用户列表
            r = c.get("/api/rag/admin/auth/users", headers={"Authorization": _AUTH})
            check("users 200", r.status_code == 200)

            # 员工目录
            r = c.get("/api/rag/admin/employees/directory",
                      headers={"Authorization": _AUTH})
            check("directory 200", r.status_code == 200)

            # 审计 dashboard
            r = c.get("/api/rag/admin/audit/dashboard",
                      headers={"Authorization": _AUTH})
            check("dashboard 200", r.status_code == 200)

            # usage
            r = c.get("/api/rag/admin/usage", headers={"Authorization": _AUTH})
            check("usage 200", r.status_code == 200)

            # query-schema
            r = c.get("/api/rag/admin/query-schema",
                      headers={"Authorization": _AUTH})
            check("query-schema 200", r.status_code == 200)

            # 无效 token → 401
            r = c.get("/api/rag/admin/sops",
                      headers={"Authorization": "Bearer wrong-token"})
            check("401 无效token", r.status_code == 401)
    finally:
        _teardown_auth()


def test_chat_contract():
    """chat 端点契约：鉴权 + 缺 query 的错误形状（不触发真实 LLM）"""
    _setup_auth()
    try:
        c = _client()
        with c:
            # 缺 query 字段 → 应返回错误形状（不崩）
            r = c.post("/api/rag/agent/chat",
                       json={}, headers={"Authorization": _AUTH})
            check("chat 无query 不500", r.status_code < 500,
                  f"got {r.status_code}")
    finally:
        _teardown_auth()


if __name__ == "__main__":
    test_health()
    test_auth_gating()
    test_read_endpoints()
    test_chat_contract()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
