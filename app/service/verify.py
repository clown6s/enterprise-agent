# -*- coding: utf-8 -*-
"""
答案校验链（从编排核心拆出的独立层）：小模型对照资料核查 → 确定性守卫收尾。

"怎么校验"全在这里：提示词、缺失术语、熔断、守卫顺序；
agent.py 只管"什么时候校验"。改守卫顺序/熔断参数只动这个文件。

守卫顺序（实况换来的，勿调换）：兜底句式混入清理 → 缺失术语硬删除 →
鉴伪断言 → 空标题清理 → 格式修复。
"""
import re
import time
from datetime import datetime

from .. import config, guards, llm, prompts

# 校验层自动熔断：qwen-plus 连续失败 3 次 → 停用 5 分钟（防延迟雪崩），
# 到点自动恢复。校验是增强不是必需——熔断期间答案只过确定性守卫。
_verify_failures = 0
_verify_suspended_until = 0.0


def verify_breaker_state() -> dict:
    """熔断状态（管理后台查询用）"""
    return {"failures": _verify_failures,
            "suspended": time.time() < _verify_suspended_until,
            "suspended_until": _verify_suspended_until}


def verify(answer: str, context: str, graph_only: bool = False) -> str:
    """
    小模型对照资料核查 → 确定性守卫收尾。
    graph_only：图谱-only 兜底时把收紧规则也注入校验提示词——否则小模型会放过
    图谱之外的内容（"图谱-only 授权过宽"的根因，恒传空串是漏接实况）。
    """
    global _verify_failures, _verify_suspended_until
    if (not config.VERIFY_ENABLED or not answer.strip() or not context.strip()
            or answer.strip() == guards.NO_ANSWER):
        return answer
    if time.time() < _verify_suspended_until:
        return guards.fix_output(answer) or guards.NO_ANSWER   # 熔断中：只过确定性守卫
    missing = guards.find_missing_terms(answer, context)
    missing_block = "、".join(missing) if missing else "（无）"
    try:
        checked = llm.chat(
            "", prompts.VERIFY_PROMPT.format(missing=missing_block,
                                             graph_only_rule=prompts.GRAPH_ONLY_RULE if graph_only else "",
                                             context=context[:6000], answer=answer),
            model=config.VERIFY_MODEL, base=config.DASHSCOPE_BASE, key=config.DASHSCOPE_KEY,
            temperature=0.2,
        ).strip()
        if checked.startswith("```"):
            checked = re.sub(r"^```\w*\s*", "", checked)
            checked = re.sub(r"\s*```$", "", checked)
        # 过度删除防护：小模型偶发把正文整个删空（只剩标题）——正文超过 200 字
        # 却被删掉一半以上时，判定为误删，回退原文走确定性守卫
        if checked and not (len(answer) > 200 and len(checked) < len(answer) * 0.5):
            answer = checked
    except Exception:
        # 校验模型失败 → 原文下发（软依赖），确定性守卫仍会执行；连续失败触发熔断
        _verify_failures += 1
        if _verify_failures >= 3:
            _verify_suspended_until = time.time() + 300
            _verify_failures = 0
            print(f"[熔断] 校验模型连续失败，verify 停用 5 分钟至 "
                  f"{datetime.fromtimestamp(_verify_suspended_until):%H:%M:%S}")
    else:
        _verify_failures = 0

    # 确定性守卫顺序：兜底句式混入清理 → 缺失术语硬删除 → 鉴伪断言 → 空标题清理 → 格式修复。
    # missing 用校验前的答案算：校验器已删掉的术语不误伤，漏删的在此兜底。
    answer = guards.strip_no_answer_hedge(answer)
    answer = guards.strip_missing_terms(answer, missing)
    answer = guards.strip_assertions(answer, context)
    answer = guards.collapse_empty_headings(answer)
    return guards.fix_output(answer) or guards.NO_ANSWER
