# -*- coding: utf-8 -*-
"""
请求关联 ID 测试（主体运行可观测性）：
- ContextVar 设置/恢复
- logging Filter 注入 request_id
- HTTP middleware 生成/继承 X-Request-Id
- Feishu 线程入口恢复上下文（通过轻量 stub，不连 SDK/外部服务）
"""
import io
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.request_context import get_request_id, reset_request_id, set_request_id  # noqa: E402
from app.logging_config import RequestIdFilter  # noqa: E402

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


# 1. ContextVar 基础与 token 恢复
check("无上下文返回空", get_request_id() == "")
tok = set_request_id("rid-test-001")
check("设置后可读取", get_request_id() == "rid-test-001")
reset_request_id(tok)
check("reset 恢复为空", get_request_id() == "")

# 2. logging Filter：有上下文和无上下文都不崩
flt = RequestIdFilter()
record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
tok = set_request_id("rid-log-002")
try:
    check("日志 filter 注入 request_id", flt.filter(record) and record.request_id == "rid-log-002")
finally:
    reset_request_id(tok)
record2 = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
check("无上下文 filter 使用兜底", flt.filter(record2) and record2.request_id == "-")

# 3. FastAPI 真实入口：X-Request-Id 生成与透传
from app.channels import feishu as feishu_mod  # noqa: E402
_ORIG_ENABLED = feishu_mod.channel.enabled
feishu_mod.channel.enabled = lambda: False
try:
    from app.main import app  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    with TestClient(app) as client:
        generated = client.get("/healthz")
        check("HTTP 自动生成 request id", generated.status_code == 200
              and bool(generated.headers.get("X-Request-Id")))
        supplied = client.get("/healthz", headers={"X-Request-Id": "gateway-rid-003"})
        check("HTTP 透传 request id", supplied.headers.get("X-Request-Id") == "gateway-rid-003")
finally:
    feishu_mod.channel.enabled = _ORIG_ENABLED

# 4. ThreadPoolExecutor 校验并发继承 request_id
try:
    from app.service import verify as verify_mod
    seen_ids = []
    original_score = verify_mod._relevance_score
    def _fake_score(query, answer, prompt):
        seen_ids.append(get_request_id())
        return 5
    verify_mod._relevance_score = _fake_score
    tok = set_request_id("rid-thread-004")
    try:
        verify_mod._relevance_gate("问题", "答案")
        check("校验线程继承 request_id", seen_ids == ["rid-thread-004", "rid-thread-004"], str(seen_ids))
    finally:
        reset_request_id(tok)
        verify_mod._relevance_score = original_score
except Exception as exc:
    check("校验线程继承 request_id", False, repr(exc))

# 5. Feishu 入口：message_id 设上下文后恢复；用对象 stub 避免真实回复
try:
    from app.channels.feishu import FeishuChannel  # noqa: E402
    channel = FeishuChannel()
    channel._reply = lambda *args: None
    channel._handle_text_inner_impl = lambda *args: check(
        "Feishu 线程绑定 message_id", get_request_id() == "feishu-msg-005")
    check_before = get_request_id()
    channel._handle_text_inner("chat", "feishu-msg-005", "清除文件", "open")
    check("Feishu 处理后恢复上下文", get_request_id() == check_before)
except Exception as exc:
    check("Feishu 入口可测试", False, repr(exc))

print(f"\n{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
