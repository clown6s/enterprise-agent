# -*- coding: utf-8 -*-
"""
后台登录（与 Java 版同路径同协议，rag-admin 前端零改动复用）：
POST /api/rag/admin/auth/login  {"username","password"} → {success, data:{username,token,role}}
POST /api/rag/admin/auth/logout （前端退出时调用）
"""
from fastapi import APIRouter, Header

from .. import mysql_store

router = APIRouter()


def _fail(msg: str = "用户名或密码错误"):
    return {"success": False, "message": msg, "data": None}


@router.post("/api/rag/admin/auth/login")
def login(body: dict):
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = mysql_store.verify_user(username, password)
    if not user:
        return _fail()
    token = mysql_store.issue_token(user)
    return {"success": True, "message": None,
            "data": {"username": user["username"], "token": token, "role": user["role"]}}


@router.post("/api/rag/admin/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        mysql_store.revoke_token(authorization[7:])
    return {"success": True, "message": None, "data": None}


@router.get("/api/rag/admin/auth/me")
def me(authorization: str | None = Header(default=None)):
    token = (authorization or "").removeprefix("Bearer ")
    user = mysql_store.resolve_token(token)
    return {"success": True, "message": None, "data": user}
