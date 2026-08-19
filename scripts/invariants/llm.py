# -*- coding: utf-8 -*-
"""三、LLM 降级链契约（monkeypatch，不真调 API）——搬自原 test_invariants.py 第三段。

13 条 check：chat_chain 降级/全败/bug 不降级、chat_stream_chain 首块前/流式中断、
model_config 回退、生成链异厂商、render_prompt 严格插值、核心提示词装配。

monkeypatch 自包含：_orig_chat/_orig_stream 在本 run() 内捕获，
fake chat/chat_stream 在 try/finally 里恢复原值（原文件就是这么做的，保持）。
注意：chat_chain 调模块级全局 chat()、chat_stream_chain 调模块级全局 chat_stream()，
故 `llm.chat = _fake...` 的 patch 机制成立（已核对 app/llm.py）。
"""
import httpx  # 降级链测试用 httpx 异常当"真 LLM 失败"替身（dsh 一-9）

from app import config, llm, prompts


def run(check) -> None:
    _orig_chat = llm.chat

    def _fake_chain_chat(system, user, **kw):
        """main 失败（transient）、fallback 成功；记录被调用的 model"""
        calls.append(kw.get("model"))
        if kw.get("model") == config.MODEL_REGISTRY["main"]["model"]:
            raise httpx.TimeoutException("main down")
        return "fallback-ok"

    def _fake_both_fail(system, user, **kw):
        calls.append(kw.get("model"))
        raise httpx.TimeoutException("all down")

    def _fake_bug_chat(system, user, **kw):
        """编程 bug（非 httpx）——应直接抛不降级（dsh 一-9）"""
        calls.append(kw.get("model"))
        raise RuntimeError("code bug: data['choices'][0]")

    calls: list = []
    llm.chat = _fake_chain_chat
    try:
        r = llm.chat_chain("", "hi")
        check("chat_chain 降级换棒", r == "fallback-ok" and len(calls) == 2, repr(calls))
    finally:
        llm.chat = _orig_chat

    calls = []
    llm.chat = _fake_both_fail
    try:
        try:
            llm.chat_chain("", "hi")
            check("chat_chain 全败抛异常", False, "未抛异常")
        except httpx.HTTPError:
            check("chat_chain 全败抛异常", len(calls) == 2, repr(calls))
    finally:
        llm.chat = _orig_chat

    # 编程 bug（非 httpx）不降级——直接抛，不换棒（dsh 一-9：不拿降级掩盖 bug）
    calls = []
    llm.chat = _fake_bug_chat
    try:
        try:
            llm.chat_chain("", "hi")
            check("chat_chain bug 不降级直接抛", False, "未抛异常")
        except RuntimeError:
            check("chat_chain bug 不降级直接抛", len(calls) == 1, repr(calls))
    finally:
        llm.chat = _orig_chat

    _orig_stream = llm.chat_stream

    def _fake_pre_fail(system, user, **kw):
        if kw.get("model") == config.MODEL_REGISTRY["main"]["model"]:
            raise httpx.TimeoutException("main down")
        yield "a"
        yield "b"

    def _fake_mid_fail(system, user, **kw):
        yield "part"
        raise httpx.TimeoutException("mid-stream down")

    llm.chat_stream = _fake_pre_fail
    try:
        r = list(llm.chat_stream_chain("", "hi"))
        check("chat_stream_chain 首块前失败换棒", r == ["a", "b"], repr(r))
    finally:
        llm.chat_stream = _orig_stream

    llm.chat_stream = _fake_mid_fail
    try:
        r = list(llm.chat_stream_chain("", "hi"))
        check("chat_stream_chain 流式中断保留部分", r == ["part"], repr(r))
    finally:
        llm.chat_stream = _orig_stream

    # 14. model_config：未知用途回退主力
    check("model_config 未知回退 main",
          llm.model_config("nope") == config.MODEL_REGISTRY["main"])

    # 15. 注册表契约：生成链两棒异厂商（厂商级故障隔离的前提）
    check("生成链异厂商",
          config.MODEL_REGISTRY["main"]["base"] != config.MODEL_REGISTRY["main_fallback"]["base"]
          and "verify" in config.MODEL_REGISTRY and "verify_fallback" in config.MODEL_REGISTRY)

    # 15b. 提示词严格插值（dsh 一-10）：占位符与实参必须完全匹配，多/缺都 fail-loud
    check("render_prompt 正常插值",
          prompts.render_prompt("今天{date}问{q}", date="8/20", q="你好") == "今天8/20问你好")
    try:
        prompts.render_prompt("{a}{b}", a="1")  # 缺 b
        check("render_prompt 缺变量 fail-loud", False, "未抛")
    except ValueError:
        check("render_prompt 缺变量 fail-loud", True)
    try:
        prompts.render_prompt("{a}", a="1", b="2")  # 多 b
        check("render_prompt 多变量 fail-loud", False, "未抛")
    except ValueError:
        check("render_prompt 多变量 fail-loud", True)
    check("render_prompt {{}} 转义不算占位符",
          prompts.render_prompt("示例 {{json}} 值{v}", v="X") == "示例 {json} 值X")
    # 核心提示词装配不炸（占位符与实参匹配）
    check("SYSTEM_PROMPT 装配",
          "今天" in prompts.render_prompt(prompts.SYSTEM_PROMPT, date="2026-08-20"))
    check("VERIFY_PROMPT 装配",
          "资料" in prompts.render_prompt(prompts.VERIFY_PROMPT, missing="", spliced="",
                                          graph_only_rule="", context="C", answer="A"))
