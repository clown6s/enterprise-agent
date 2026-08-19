# -*- coding: utf-8 -*-
"""
LLM 客户端：手写 OpenAI 兼容接口调用（httpx），不引 SDK。
好处：代码即文档——你能直接看到 HTTP 请求和流式解析的完整过程；
坏处：没有 SDK 的重试/连接池，但对单机场景足够了。

本文件两个函数：
- chat()        一次性对话（校验层/非流式答案用）
- chat_stream() 生成器，逐段吐出增量文本（SSE 流式用）
"""
import base64
import json
import time
from typing import Iterator

import httpx

from . import config
from .learn import usage
import logging
log = logging.getLogger(__name__)

# OpenAI 兼容的对话消息角色名（系统/用户/助手）
_ROLES = ("system", "user", "assistant")


def _messages(system: str, user: str) -> list[dict]:
    """把系统提示词 + 用户输入拼成 messages"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return msgs


def model_config(purpose: str) -> dict:
    """按用途取模型注册表条目（{model, base, key, extra?}）——模型注册表入口，
    未知用途回退主力模型。"""
    return config.MODEL_REGISTRY.get(purpose, config.MODEL_REGISTRY["main"])


# 降级链（类机制）：按用途顺序尝试注册表条目，前一棒异常接下一棒。
# 生成链 = 主力 + 异厂商第二棒（百炼挂了生成仍有智谱）；
# 校验链 = 独立裁判 + 旧裁判（智谱挂了校验回退 qwen3-max）。
# 加新用途只加注册表条目，不写散规则。
_GEN_CHAIN = ("main", "main_fallback")
_VERIFY_CHAIN = ("verify", "verify_fallback")


def chat_chain(system: str, user: str, *,
               purposes: tuple[str, ...] = _GEN_CHAIN, **kw) -> str:
    """带降级链的一次性对话：按 purposes 顺序尝试，全部失败抛最后一个异常。
    kw（temperature 等）由调用方给出；厂商特有参数来自注册表条目 extra——
    调用方不传厂商参数（换厂商参数随条目走）。"""
    last: Exception | None = None
    for purpose in purposes:
        m = model_config(purpose)
        try:
            opts = {}
            if m.get("extra"):
                opts["extra"] = m["extra"]
            if "timeout" in m:
                opts["timeout"] = m["timeout"]
            return chat(system, user, model=m["model"], base=m["base"], key=m["key"],
                        **opts, **kw)
        except Exception as e:
            last = e
            log.warning(f"{purpose} 调用失败，降级下一棒: {e!r}")
    raise last


def chat_stream_chain(system: str, user: str, *,
                      purposes: tuple[str, ...] = _GEN_CHAIN) -> Iterator[str]:
    """带降级链的流式生成：首块前失败换下一棒；流式中断保留已生成部分
    （部分答案仍会走校验链再出门，比整体 500 强）。全部未出块失败抛最后一个异常。"""
    last: Exception | None = None
    for purpose in purposes:
        m = model_config(purpose)
        started = False
        try:
            opts = {"timeout": m["timeout"]} if "timeout" in m else {}
            for chunk in chat_stream(system, user, model=m["model"], base=m["base"],
                                     key=m["key"], **opts):
                started = True
                yield chunk
            return
        except Exception as e:
            if started:
                log.warning(f"{purpose} 流式中断，保留已生成部分")
                return
            last = e
            log.warning(f"{purpose} 流式失败，降级下一棒: {e!r}")
    raise last


def verify_chat(system: str, user: str, temperature: float = 0.2) -> str:
    """校验链调用（独立裁判 → 旧裁判）——校验/提取类小任务的统一入口"""
    return chat_chain(system, user, purposes=_VERIFY_CHAIN, temperature=temperature)


def _image_mime(image: bytes) -> str:
    """按魔数识别图片类型（data URL 的 MIME 必须与内容一致，qwen-vl 会校验）"""
    if image[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image[:4] == b"GIF8":
        return "image/gif"
    return "image/png"


def chat_image(system: str, user: str, image: bytes) -> str:
    """图片理解（OCR/图注）：走注册表 ocr 条目，OpenAI 兼容 content 数组 +
    base64 data URL。失败抛异常（调用方决定是否降级跳过该图）。"""
    m = model_config("ocr")
    _t0 = time.time()
    content = [
        {"type": "text", "text": user},
        {"type": "image_url", "image_url": {
            "url": f"data:{_image_mime(image)};base64,{base64.b64encode(image).decode()}"}},
    ]
    payload = {
        "model": m["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": content}],
        "temperature": 0.1,
        "stream": False,
    }
    resp = httpx.post(
        f"{m['base']}/chat/completions",
        headers={"Authorization": f"Bearer {m['key']}"},
        json=payload,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    out = data["choices"][0]["message"]["content"] or ""
    usage.record(m["model"], len(system) + len(user), len(out), time.time() - _t0)
    return out


def chat(system: str, user: str, *,
         model: str = config.MAIN_MODEL,
         base: str = config.MAIN_BASE,
         key: str = config.MAIN_KEY,
         temperature: float = 0.7,
         extra: dict | None = None,
         timeout: float = 120) -> str:
    """一次性对话，返回完整文本；失败抛异常（调用方决定兜底策略）。
    extra：附加请求参数（如 {"enable_thinking": False} 关闭思考模式省延迟）。
    timeout：超时秒数（注册表条目可按用途声明，降级链据此控制换棒时机）。"""
    _t0 = time.time()
    payload = {
        "model": model,
        "messages": _messages(system, user),
        "temperature": temperature,
        "stream": False,
    }
    if extra:
        payload.update(extra)
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""
    usage.record(model, len(system) + len(user), len(content), time.time() - _t0)
    return content


def chat_stream(system: str, user: str, *,
                model: str = config.MAIN_MODEL,
                base: str = config.MAIN_BASE,
                key: str = config.MAIN_KEY,
                timeout: float = 180) -> Iterator[str]:
    """
    流式对话生成器：每次 yield 一段增量文本。
    流式协议是 SSE：每行 "data: {json}"，[DONE] 结束。
    """
    _t0 = time.time()
    total = 0
    with httpx.stream(
        "POST",
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "messages": _messages(system, user),
            "temperature": 0.7,
            "stream": True,
        },
        timeout=timeout,
    ) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
                delta = chunk["choices"][0]["delta"]
                if delta.get("content"):
                    total += len(delta["content"])
                    yield delta["content"]
            except (json.JSONDecodeError, KeyError, IndexError):
                # 心跳/首帧（role 字段）等非内容帧直接跳过
                continue
    usage.record(model, len(system) + len(user), total, time.time() - _t0)
