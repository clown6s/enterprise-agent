# -*- coding: utf-8 -*-
"""统一 JSON Lines 诊断日志配置。

业务审计 JSONL（execution/ingestion/usage/events 等）保持独立 schema；本模块只负责
stdout/uvicorn 运行诊断日志。每条记录固定含时间、级别、logger、request_id、进程/线程，
动态参数从 extra.fields 展开。Formatter 永不因不可序列化值或第三方 LogRecord 崩溃。
"""
import datetime as _dt
import hashlib
import json
import logging
import math
import re
import sys
import traceback
from decimal import Decimal
from typing import Any

from .request_context import get_request_id

_DATEFMT = "%Y-%m-%d %H:%M:%S"
_NOISY = ("pymilvus", "grpc", "urllib3", "httpx", "httpcore", "asyncio",
          "neo4j", "pypdf", "pdfplumber", "lark_oapi", "pymysql")
_SECRET_KEY = re.compile(
    r"authorization|bearer|api[_-]?key|password|passwd|secret|token|cookie|webhook",
    re.I)
_SENSITIVE_VALUE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def _short_hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def safe_serialize(value: Any, depth: int = 0, seen: set[int] | None = None) -> Any:
    """把常见/未知对象安全转 JSON；深度、数量、字符串均有上限，永不抛异常。"""
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str):
            return value[:1000]
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if depth >= 4:
        return f"<{type(value).__name__}>"
    ident = id(value)
    if ident in seen:
        return "<circular>"
    seen.add(ident)
    try:
        if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, bytes):
            return f"<bytes:{len(value)}>"
        if isinstance(value, (list, tuple, set, frozenset)):
            return [safe_serialize(v, depth + 1, seen) for v in list(value)[:50]]
        if isinstance(value, dict):
            return {str(k)[:100]: safe_serialize(v, depth + 1, seen)
                    for k, v in list(value.items())[:50]}
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return safe_serialize(model_dump(), depth + 1, seen)
        return repr(value)[:1000]
    except Exception:
        return f"<{type(value).__name__}>"
    finally:
        seen.discard(ident)


def redact_value(key: str, value: Any) -> Any:
    """按字段名脱敏；身份/查询类字段只保留长度+短 hash，避免日志泄露原文。"""
    key_lower = key.lower()
    if _SECRET_KEY.search(key_lower):
        return "[REDACTED]"
    if key_lower in {"open_id", "message_id", "conversation_id", "chat_id"}:
        return {"len": len(str(value)), "hash": _short_hash(value)}
    if key_lower in {"query", "answer", "sql", "final_sql", "body", "filename"}:
        text = str(value)
        return {"len": len(text), "hash": _short_hash(text)}
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("[REDACTED]", value[:1000])
    return safe_serialize(value)


class RequestIdFilter(logging.Filter):
    """为所有日志记录注入 request_id；无上下文时用 '-'。"""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id("-")
        return True


class JsonFormatter(logging.Formatter):
    """稳定 JSON Lines formatter，兼容普通 logging 和 uvicorn access/error 记录。"""
    _RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "processName", "process", "request_id",
        "message", "color_message",
    }

    def __init__(self, *, access: bool = False):
        super().__init__()
        self.access = access

    def format(self, record: logging.LogRecord) -> str:
        try:
            payload = {
                "ts": _dt.datetime.fromtimestamp(record.created, _dt.timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "level": record.levelname,
                "logger": record.name,
                "request_id": getattr(record, "request_id", get_request_id("-")),
                "process": record.process,
                "thread": record.threadName,
                "source": f"{record.filename}:{record.lineno}",
                "message": self._message(record),
            }
            if self.access:
                payload["event"] = "http_access"
                self._access_fields(record, payload)
            event = getattr(record, "event", None)
            if event:
                payload["event"] = str(event)[:100]
            fields = getattr(record, "fields", None)
            if isinstance(fields, dict):
                payload["fields"] = {str(k): redact_value(str(k), v)
                                      for k, v in list(fields.items())[:50]}
            if record.exc_info:
                payload["exception"] = {
                    "type": record.exc_info[0].__name__ if record.exc_info[0] else "Exception",
                    "message": str(record.exc_info[1])[:1000],
                    "traceback": self.formatException(record.exc_info)[:5000],
                }
            if record.stack_info:
                payload["stack"] = record.stack_info[:3000]
            return json.dumps(payload, ensure_ascii=False, allow_nan=False, default=safe_serialize)
        except Exception as e:
            # 日志不能把业务打崩；最后兜底仍输出合法 JSON。
            return json.dumps({"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                               "level": "ERROR", "logger": record.name,
                               "request_id": get_request_id("-"),
                               "message": f"日志格式化失败: {e!r}"}, ensure_ascii=False)

    def _message(self, record: logging.LogRecord) -> str:
        try:
            return _SENSITIVE_VALUE.sub("[REDACTED]", record.getMessage()[:2000])
        except Exception:
            return "<unformattable log message>"

    def _access_fields(self, record: logging.LogRecord, payload: dict) -> None:
        """解析 uvicorn access 的五元 args，不把 tuple 直接塞进 JSON。"""
        args = record.args if isinstance(record.args, tuple) else ()
        if len(args) >= 5:
            payload["fields"] = {
                "client": redact_value("client", args[0]),
                "method": redact_value("method", args[1]),
                "request_line": redact_value("request_line", args[2]),
                "http_version": redact_value("http_version", args[3]),
                "status_code": safe_serialize(args[4]),
            }


def _install_filter(root: logging.Logger) -> None:
    for handler in root.handlers:
        if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
            handler.addFilter(RequestIdFilter())


def setup(level: str = "INFO") -> None:
    """应用启动调用：JSON formatter + request filter + 第三方噪声压制。幂等。"""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setFormatter(JsonFormatter())
    _install_filter(root)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
