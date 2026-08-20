# -*- coding: utf-8 -*-
"""
enterprise-agent 入口：FastAPI 应用装配。

项目结构（学习导览）：
    app/config.py          配置（.env → 常量）
    app/llm.py             LLM 客户端（OpenAI 兼容，手写 httpx）
    app/prompts.py         提示词集中管理（系统/校验/图谱收紧规则）
    app/embeddings.py      向量/重排（硅基流动）
    app/milvus_store.py    向量库检索
    app/mysql_store.py     后台账号（sys_user 表）
    app/guards.py          确定性守卫（防编造/格式修复/术语核对/CJK 清理）
    app/demo_data.py       员工目录演示数据
    app/employee_store.py  员工目录数据层（MySQL employee 表）
    app/plan/sop.py        SOP 状态机（审批申请直连本库，去 OA 化）
    app/service/retrieval.py  检索管线
    app/service/verify.py     答案校验链（小模型核查 + 熔断 + 守卫顺序）
    app/service/agent.py      编排核心（路由/双路径/审计）
    app/routers/*           HTTP 端点（与 rag-admin 前端协议兼容）
"""
from contextlib import asynccontextmanager
import logging
import time
import uuid

from fastapi import FastAPI, Request

from . import config
from .channels.feishu import channel as feishu_channel
from .request_context import reset_request_id, set_request_id
from .routers import admin, auth, chat, feedback, health, ingestion

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动配置体检：只报告缺失键，不阻断健康检查和管理端点启动。
    # 真正的外部调用仍由各自软依赖/降级链处理。
    missing = config.validate_config()
    if missing:
        log.warning("启动配置缺失：%s（相关能力首次调用可能失败）", ", ".join(missing))
    else:
        log.info("启动配置体检通过")
    # 飞书长连接（软依赖：未配置 app_id 或 SDK 缺失时静默跳过，主链路不受影响）
    if feishu_channel.enabled():
        feishu_channel.start()
    else:
        log.info("飞书通道未启用")
    yield


app = FastAPI(title="enterprise-agent", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """为每个 HTTP 请求生成/继承 request_id 并注入日志上下文。
    支持 X-Request-ID 头溯源（飞书/网关转发时保留原 ID）。"""
    rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
    token = set_request_id(rid)
    t0 = time.time()
    log.info("request_start", extra={"event": "request_start", "fields": {
        "method": request.method, "path": request.url.path, "request_id": rid}})
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        log.info("request_end", extra={"event": "http_request", "fields": {
            "method": request.method, "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": int((time.time() - t0) * 1000)}})
        return response
    except Exception:
        log.exception("request_error", extra={"event": "request_error", "fields": {
            "method": request.method, "path": request.url.path,
            "duration_ms": int((time.time() - t0) * 1000)}})
        raise
    finally:
        reset_request_id(token)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(feedback.router)
app.include_router(admin.router)
app.include_router(ingestion.router)
