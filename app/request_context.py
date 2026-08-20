# -*- coding: utf-8 -*-
"""请求链路上下文：单次 HTTP/飞书消息的 request_id。

用 ContextVar 而不是全局变量：并发请求/流式生成互不串值；线程入口（飞书 daemon）
显式 set。业务函数不新增 request_id 参数，保持 answer/answer_stream/Feishu API 兼容。
"""
from contextvars import ContextVar, Token

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id(default: str = "") -> str:
    """取当前请求 ID；无请求上下文时返回 default。"""
    return _REQUEST_ID.get() or default


def set_request_id(value: str) -> Token:
    """设置当前上下文 ID，返回 token 供调用方 reset。"""
    return _REQUEST_ID.set(value or "")


def reset_request_id(token: Token) -> None:
    """恢复调用前上下文，避免 worker/测试污染后续请求。"""
    _REQUEST_ID.reset(token)
