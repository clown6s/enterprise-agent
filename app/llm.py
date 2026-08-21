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


def _provider_name(base: str) -> str:
    """只从 host 推导厂商名，不把完整 URL 写入日志。"""
    host = (base or "").lower()
    if "bigmodel" in host or "zhipu" in host:
        return "zhipu"
    if "dashscope" in host or "aliyun" in host:
        return "dashscope"
    if "siliconflow" in host:
        return "siliconflow"
    return "unknown"


def _classify_err(e: Exception) -> str:
    """异常分类（dsh 一-9：降级只对确认的 LLM/网络失败，不拿降级掩盖 bug）。
    http=HTTPStatusError（4xx/5xx，明确服务端失败）；transient=httpx 网络层
    （超时/连接，换厂商可能通）；bug=非 httpx 异常（KeyError/ValueError/
    RuntimeError 等编程错误）——这类直接抛不降级（曾任何异常都换棒，把代码
    bug 当 LLM 失败掩盖，难排查）。chat() 先 raise_for_status 再解析，故响应
    解析期 KeyError 多为真 bug 而非坏响应。"""
    if isinstance(e, httpx.HTTPStatusError):
        return "http"
    if isinstance(e, httpx.HTTPError):
        return "transient"
    return "bug"


def chat_chain(system: str, user: str, *,
               purposes: tuple[str, ...] = _GEN_CHAIN, **kw) -> str:
    """带降级链的一次性对话：按 purposes 顺序尝试，全部失败抛最后一个异常。
    只对确认的 LLM/网络失败降级（dsh 一-9）；编程 bug（非 httpx）直接抛——
    不拿换棒掩盖 bug。kw（temperature 等）由调用方给出；厂商特有参数来自
    注册表条目 extra——调用方不传厂商参数（换厂商参数随条目走）。"""
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
            cat = _classify_err(e)
            if cat == "bug":
                raise   # 编程错误不降级——不拿换棒掩盖 bug（dsh 一-9）
            last = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            provider = _provider_name(m.get("base", ""))
            fallback_to = None
            # chain 中下一个条目仅用于观测，不改变降级逻辑
            try:
                fallback_to = model_config(purposes[purposes.index(purpose) + 1])["model"]
            except (ValueError, IndexError, KeyError):
                pass
            log.warning("llm_fallback", extra={"event": "llm_fallback", "fields": {
                "purpose": purpose, "model": m.get("model"), "provider": provider,
                "fallback_to": fallback_to, "error_class": cat,
                "error_type": type(e).__name__, "status_code": status}})
    raise last


def chat_stream_chain(system: str, user: str, *,
                      purposes: tuple[str, ...] = _GEN_CHAIN) -> Iterator[str]:
    """带降级链的流式生成：首块前失败换下一棒；流式中断保留已生成部分
    （部分答案仍会走校验链再出门，比整体 500 强）。全部未出块失败抛最后一个异常。
    编程 bug（非 httpx）首块前直接抛不降级（dsh 一-9）。"""
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
                log.warning("llm_stream_interrupted", extra={"event": "llm_stream_interrupted", "fields": {
                    "purpose": purpose, "partial": True}})
                return
            cat = _classify_err(e)
            if cat == "bug":
                raise   # 首块前编程错误不降级
            last = e
            log.warning(f"{purpose} 流式失败（{cat}）降级下一棒: {e!r}")
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
    if resp.is_error:
        # OCR 模型参数/接口不兼容时，保留服务端错误码和消息；仅记录状态码会无法区分
        # 模型名、内容块格式、图片大小等问题，曾导致上传看似一直卡住。
        log.warning("chat_image HTTP 失败 model=%s status=%d body=%s",
                    m["model"], resp.status_code, resp.text[:1000])
    resp.raise_for_status()
    data = resp.json()
    out = data["choices"][0]["message"]["content"] or ""
    elapsed = time.time() - _t0
    usage.record(m["model"], len(system) + len(user), len(out), elapsed)
    log.info("chat_image 成功 model=%s prompt_chars=%d completion_chars=%d duration_ms=%d",
             m["model"], len(system) + len(user), len(out), int(elapsed * 1000))
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
    elapsed = time.time() - _t0
    usage.record(model, len(system) + len(user), len(content), elapsed)
    log.info("chat 成功 model=%s prompt_chars=%d completion_chars=%d duration_ms=%d",
             model, len(system) + len(user), len(content), int(elapsed * 1000))
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
    elapsed = time.time() - _t0
    usage.record(model, len(system) + len(user), total, elapsed)
    log.info("chat_stream 成功 model=%s prompt_chars=%d completion_chars=%d duration_ms=%d",
             model, len(system) + len(user), total, int(elapsed * 1000))
