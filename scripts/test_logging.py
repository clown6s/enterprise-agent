# -*- coding: utf-8 -*-
"""统一 JSON 日志测试（运行诊断日志，不触碰业务 JSONL）。"""
import datetime
import io
import json
import logging
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.logging_config import JsonFormatter, RequestIdFilter, safe_serialize  # noqa: E402
from app.request_context import reset_request_id, set_request_id  # noqa: E402

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


def record(msg="hello", **kwargs):
    r = logging.LogRecord("app.test", logging.INFO, __file__, 1, msg, (), None)
    for k, v in kwargs.items():
        setattr(r, k, v)
    return r


def test_basic():
    out = json.loads(JsonFormatter().format(record("hello")))
    check("JSON 基础可解析", out["message"] == "hello")
    check("固定字段完整", all(k in out for k in ("ts", "level", "logger", "request_id", "process", "thread", "source")))
    check("时间 UTC Z", out["ts"].endswith("Z"))

    tok = set_request_id("logging-test-rid")
    try:
        out = json.loads(JsonFormatter().format(record("ctx")))
        check("request_id 注入", out["request_id"] == "logging-test-rid")
    finally:
        reset_request_id(tok)
    check("无上下文 request_id", json.loads(JsonFormatter().format(record()))["request_id"] == "-")


def test_extra_and_safe_values():
    loop = []
    loop.append(loop)
    out = json.loads(JsonFormatter().format(record(
        "values", event="test_values", fields={
            "when": datetime.datetime(2026, 8, 21), "amount": Decimal("1.20"),
            "tags": {"a", "b"}, "raw": b"secret", "loop": loop})))
    check("extra event", out.get("event") == "test_values")
    check("字段 JSON 可解析", isinstance(out.get("fields"), dict))
    check("循环引用安全", out["fields"]["loop"] == ["<circular>"])
    check("Decimal 安全", out["fields"]["amount"] == "1.20")


def test_redaction():
    out = json.loads(JsonFormatter().format(record(
        "Bearer abc123 password=secret", fields={
            "api_key": "key-secret", "password": "pw-secret",
            "open_id": "ou_123", "query": "这是一个不应完整出现的查询"})))
    text = json.dumps(out, ensure_ascii=False)
    check("message Bearer 脱敏", "abc123" not in text)
    check("api_key 脱敏", "key-secret" not in text)
    check("password 脱敏", "pw-secret" not in text)
    check("open_id 不输出原值", "ou_123" not in text)
    check("query 不输出完整原文", "这是一个不应完整出现的查询" not in text)


def test_exception_and_access():
    try:
        raise ValueError("boom")
    except ValueError:
        r = logging.LogRecord("app.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    out = json.loads(JsonFormatter().format(r))
    check("异常结构存在", out["exception"]["type"] == "ValueError")
    check("异常栈存在", "boom" in out["exception"]["traceback"])

    access = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                               '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/healthz", "1.1", 200), None)
    out = json.loads(JsonFormatter(access=True).format(access))
    check("uvicorn access JSON", out.get("event") == "http_access")
    check("access status", out.get("fields", {}).get("status_code") == 200)


def test_filter_and_serialize():
    f = RequestIdFilter()
    r = record()
    check("filter 返回 True", f.filter(r) is True and hasattr(r, "request_id"))
    check("safe datetime", safe_serialize(datetime.date(2026, 1, 1)) == "2026-01-01")
    check("safe Decimal", safe_serialize(Decimal("2.3")) == "2.3")
    check("safe bytes", safe_serialize(b"123") == "<bytes:3>")


def test_app_no_print():
    import ast
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
    hits = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                    hits.append(path)
    check("app 无 print 调用", not hits, str(hits))


if __name__ == "__main__":
    test_basic()
    test_extra_and_safe_values()
    test_redaction()
    test_exception_and_access()
    test_filter_and_serialize()
    test_app_no_print()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
