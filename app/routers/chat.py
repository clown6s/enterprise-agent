# -*- coding: utf-8 -*-
"""
对话端点（rag-admin 前端协议）：
- POST /api/rag/agent/chat        非流式 JSON
- POST /api/rag/agent/chat/stream SSE 流式（rag-admin 前端直接可用）

SSE 线格式：
    event: token
    data: <内容，可能含换行 → 天然拆成多个 data: 行>
    （空行分隔事件）

鉴权：/api/rag/agent/** 与 /api/rag/admin/** 统一走 Bearer token。
"""
import json
from typing import Iterator

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse

from .. import guards, mysql_store
from ..service import agent, history

router = APIRouter()


def _auth(authorization: str | None) -> dict | None:
    token = (authorization or "").removeprefix("Bearer ")
    return mysql_store.resolve_token(token)


def _unauthorized():
    return JSONResponse(
        {"success": False, "message": "未登录或会话已过期，请重新登录", "data": None},
        status_code=401,
    )


@router.post("/api/rag/agent/chat")
def chat(body: dict, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    result = agent.answer(body.get("query") or "", body.get("conversationId") or "",
                          user=(user or {}).get("username", ""))
    return {"success": True, "message": None,
            "data": {"answer": result["answer"], "sources": result["sources"],
                     "route": result["route"], "conversationId": body.get("conversationId") or ""}}


def _sse(events: Iterator[tuple[str, object]]) -> Iterator[str]:
    """(event, data) 流 → SSE 文本流。

    多行数据必须拆成多个 "data: " 行（SSE 规范）——
    否则数据里的空行会打断事件边界，前端只能收到第一行。
    """
    for event, data in events:
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False)
        yield f"event: {event}\n"
        for line in str(data).split("\n"):
            yield f"data: {line}\n"
        yield "\n"


@router.post("/api/rag/agent/chat/stream")
def chat_stream(body: dict, authorization: str | None = Header(default=None)):
    user = _auth(authorization)
    if not user:
        return _unauthorized()
    query = body.get("query") or ""
    conversation_id = body.get("conversationId") or ""

    def _events_with_history():
        """流式事件包装：收齐 token 后落多轮历史（与 answer() 中央落库对齐；
        兜底句式/GUARD 拦截不落——无信息量/注入文本不可回流）"""
        route, parts = "", []
        for event, data in agent.answer_stream(query, conversation_id,
                                               user=(user or {}).get("username", "")):
            if event == "route":
                route = str(data)
            elif event == "token":
                parts.append(str(data))
            yield event, data
        answer = "".join(parts)
        if (route != "GUARD" and answer and answer != guards.NO_ANSWER
                and not history.is_test_conversation(conversation_id)):
            history.save_turn(conversation_id, query, guards.strip_leading_anchor(answer))

    return StreamingResponse(
        _sse(_events_with_history()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
