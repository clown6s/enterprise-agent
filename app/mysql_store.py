# -*- coding: utf-8 -*-
"""
MySQL 访问（pymysql）：后台统一身份 + 审计落库。

沿用既有 rag_service.sys_user 表——密码算法是 SHA-256(salt + password)
的十六进制，一行 hashlib 就能复现，现有 admin 账号（admin / Admin@2026）
直接可用，管理前端无需改账号。
"""
import hashlib
import uuid

import pymysql

from . import config


def _conn() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user="root",
        password=config.MYSQL_PASSWORD,
        database=config.MYSQL_DB,
        charset="utf8mb4",
        connect_timeout=5,
    )


def _hash(password: str, salt: str) -> str:
    """SHA-256(salt + password) 十六进制（沿用既有账号体系）"""
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def verify_user(username: str, password: str) -> dict | None:
    """登录校验：命中返回 {username, role, display_name}，否则 None"""
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash, salt, role, display_name, status "
                "FROM sys_user WHERE username=%s",
                (username,),
            )
            row = cur.fetchone()
        conn.close()
        if not row:
            return None
        pw_hash, salt, role, display_name, status = row
        if status != 1 or _hash(password, salt) != pw_hash:
            return None
        return {"username": username, "role": role, "display_name": display_name or username}
    except pymysql.MySQLError:
        return None


_sessions: dict[str, dict] = {}


def issue_token(user: dict) -> str:
    token = uuid.uuid4().hex
    _sessions[token] = user
    return token


def resolve_token(token: str) -> dict | None:
    return _sessions.get(token)


def revoke_token(token: str) -> None:
    _sessions.pop(token, None)
