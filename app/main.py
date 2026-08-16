# -*- coding: utf-8 -*-
"""
enterprise-agent 入口：FastAPI 应用装配。

项目结构（学习导览）：
    app/config.py          配置（.env → 常量）
    app/llm.py             LLM 客户端（OpenAI 兼容，手写 httpx）
    app/embeddings.py      向量/重排（硅基流动）
    app/milvus_store.py    向量库检索
    app/mysql_store.py     后台账号（复用 Java 版 sys_user 表）
    app/guards.py          确定性守卫（防编造/格式修复，Java 版血泪移植）
    app/demo_data.py       员工目录演示数据
    app/oa_client.py       OA 业务系统客户端
    app/service/retrieval.py  检索管线
    app/service/agent.py      编排核心（路由/守卫/校验/流式）
    app/routers/*           HTTP 端点（与 rag-admin 前端协议兼容）
"""
from fastapi import FastAPI

from .routers import auth, chat, feedback, health

app = FastAPI(title="enterprise-agent", version="0.1.0")

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(feedback.router)
