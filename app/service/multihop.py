# -*- coding: utf-8 -*-
"""
多跳查询编排（智能路由架构 5/5 之一）：复合问题 → 拆解 → 分派确定性层 →
合并。拆解是模型建议，每片执行走 NL2SQL 数据面（2026-08-19 能力总线
退役后唯一确定性分派点）——拆解跑偏只影响该片（答"未查到"），
合并层每片带子问题标签。
护栏：子问题 ≤4 个、单片 ≤40 字、至少 1 片有答案才返回、全无漏回主路径。
"""
import json
import re

from .. import llm

# 复合判定：双问号 / 问号后仍有实质内容 / 并列连接词——问号后内容
# ≥3 字才算复合（"心衰如何诊断？"尾问号不误判；曾只有双问号规则，
# "A是什么职位？她最近7天B了多少"单问号漏判的实况）
_COMPOUND = re.compile(r"[？?][^。！!]{0,40}[？?]|[？?][^。！!]{3,}$|还有|以及|另外|同时还想|顺便|加.*(还|也)")

_DECOMPOSE_PROMPT = """把用户问题拆解成 1-4 个独立的子问题，每个子问题只问一件事。
只输出 JSON 数组，不要任何解释，例如：["A 是什么职位", "A 最近 7 天报销了多少"]。
无法拆解（问题本来就只问一件事）输出 []。

【用户问题】
{query}
"""


def try_answer(query: str, user: str = "") -> str | None:
    """复合问题拆解分派；不可拆/全片无答案返回 None（漏回主路径）"""
    if not _COMPOUND.search(query or ""):
        return None
    try:
        raw = llm.verify_chat("", _DECOMPOSE_PROMPT.format(query=query),
                              temperature=0.1).strip()
        m = re.search(r"\[.*\]", raw, re.S)
        subs = json.loads(m.group()) if m else []
        if not isinstance(subs, list):
            return None
        subs = [str(s).strip() for s in subs if str(s).strip()][:4]
    except (ValueError, AttributeError):
        return None
    if not subs:
        return None
    from . import nlsql
    parts = []
    for sub in subs:
        if len(sub) > 40:
            continue
        ans = None
        # 分派走 NL2SQL 数据面（2026-08-19 能力总线退役，员工/报销/经营已注册）
        try:
            ans = nlsql.try_answer(sub, user=user)
        except Exception:
            ans = None
        parts.append(f"- {sub}：{ans.strip()}" if ans
                     else f"- {sub}：未查询到。")
    if not any("未查询到" not in p for p in parts):
        return None
    return "\n".join(parts)
