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
    """按用途取模型注册表条目（{model, base, key}）——模型注册表入口，
    未知用途回退主力模型。"""
    return config.MODEL_REGISTRY.get(purpose, config.MODEL_REGISTRY["main"])


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
         extra: dict | None = None) -> str:
    """一次性对话，返回完整文本；失败抛异常（调用方决定兜底策略）。
    extra：附加请求参数（如 {"enable_thinking": False} 关闭思考模式省延迟）。"""
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
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""
    usage.record(model, len(system) + len(user), len(content), time.time() - _t0)
    return content


def chat_stream(system: str, user: str, *,
                model: str = config.MAIN_MODEL,
                base: str = config.MAIN_BASE,
                key: str = config.MAIN_KEY) -> Iterator[str]:
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
        timeout=180,
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
