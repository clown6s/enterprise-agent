# -*- coding: utf-8 -*-
"""
答案校验链（从编排核心拆出的独立层）：小模型对照资料核查 → 确定性守卫收尾。

"怎么校验"全在这里：提示词、缺失术语、熔断、守卫顺序；
agent.py 只管"什么时候校验"。改守卫顺序/熔断参数只动这个文件。

守卫顺序（实况换来的，勿调换）：兜底句式混入清理 → 缺失术语硬删除 →
（图谱-only 数字硬删）→ 鉴伪断言 → 空标题清理 → 格式修复。
"""
import re
import time
from datetime import datetime

from .. import config, guards, llm, prompts

# 校验层自动熔断：校验模型连续失败 3 次 → 停用 5 分钟（防延迟雪崩），
# 到点自动恢复。校验是增强不是必需——熔断期间答案只过确定性守卫。
_verify_failures = 0
_verify_suspended_until = 0.0


def verify_breaker_state() -> dict:
    """熔断状态（管理后台查询用）"""
    return {"failures": _verify_failures,
            "suspended": time.time() < _verify_suspended_until,
            "suspended_until": _verify_suspended_until}


_NUM_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|mmHg|mg|ml|g|kg|次|天|年|月|日|周|元)")

# ---------------- 相关性闸门（问题 vs 答案 多轮评分，低分直接过掉） ----------------

_SCORE_PROMPT = """你是答案相关性评审员。判断【答案】是否**正面回应了问题想问的点**：
5 = 正面且充分地回应了问题想问的点
4 = 正面回应了问题想问的点（部分细节未覆盖可接受）
3 = 只答了相邻内容，没有正面回应问题 → 不合格
1 = 答非所问或对用户无用
只输出一个 1-5 的整数。

【问题】
{query}

【答案】
{answer}
"""

# 反向评审（提示词异构：同一模型换提问角度——曾两轮同 prompt 同模型，
# 系统性偏见要两个不同视角同时犯才漏）。评分标准与正面评审对齐：
# 主体回应=4/5，"有补充段落"不算跑题（曾把含补充段落的正常答案打 3
# 误杀——guard-005 实况）
_REVERSE_SCORE_PROMPT = """你是答案相关性评审员（反向检查）。只判断一件事：【答案】的主体内容是否跑题——
即答案大部分内容没有回应【问题】想问的点（例如问"饮食注意"却主要在讲
诊断分类或元信息）。注意事项类答案的多小节列表（饮食/运动/用药各一节）
都算回应，不算跑题；个别补充段落也不算跑题。
跑题输出 1，不跑题输出 5。只输出一个数字。

【问题】
{query}

【答案】
{answer}
"""

_RELEVANCE_MIN = 4   # 3 分（相邻内容）即不合格——问什么答什么，用户明确
_RELEVANCE_ROUNDS = 2
# 问句意图词：含任一 → 有明确诉求，进严格闸门；不含 → 名词查找类，豁免
_QUESTION_INTENT = re.compile(
    r"什么|怎么|如何|为什么|为何|注意|建议|推荐|哪些|几个|多少|哪年|哪一|吗|呢"
    r"|区别|流程|方法|方式|能否|可以|会不会|能不能|是不是|原因|时候|时间|多久")


def _relevance_score(query: str, answer: str, prompt: str) -> int | None:
    """单轮相关性评分（小模型评审）；失败返回 None"""
    try:
        raw = llm.chat("", prompt.format(query=query[:200], answer=answer[:2000]),
                       model=config.VERIFY_MODEL, base=config.DASHSCOPE_BASE,
                       key=config.DASHSCOPE_KEY, temperature=0.1,
                       extra={"enable_thinking": False}).strip()
        m = re.search(r"[1-5]", raw)
        return int(m.group()) if m else None
    except Exception:
        return None


def _relevance_gate(query: str, answer: str) -> str:
    """
    相关性闸门（用户明确要求的最后兜底）：问题+答案双轮评分（并行，
    延迟≈单轮），最低分 < 4 → 直接过掉，回兜底句式——只答相邻内容/
    答非所问的答案不出门。两轮提示词异构（正面+反向视角）——曾同
    prompt 双轮同模型，系统性偏见两个同视角同时犯才漏。评审模型失败
    时放行（软依赖；系统性故障由校验熔断机制兜底）。
    """
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=_RELEVANCE_ROUNDS) as pool:
        futures = [pool.submit(_relevance_score, query, answer, _SCORE_PROMPT),
                   pool.submit(_relevance_score, query, answer, _REVERSE_SCORE_PROMPT)]
        scores = [f.result() for f in futures]
    valid = [s for s in scores if s is not None]
    if not valid:
        return answer
    if min(valid) < _RELEVANCE_MIN:
        print(f"[verify] 相关性闸门拒绝（分数 {valid}）：{query[:40]}")
        return guards.NO_ANSWER
    return answer


def _grounded_number_retention(raw: str, checked: str, context: str) -> float:
    """
    原文中"资料里有据的数字"在校验后答案中的留存率（0~1）。
    只统计有据数字——原文编造的数字被删是正确行为，不算损失。
    归一化比对（去空白），"140 mmHg" 与 "140/90mmHg" 视为同款。
    """
    ctx = re.sub(r"\s+", "", context).lower()
    grounded = [n for n in _NUM_UNIT.findall(raw)
                if re.sub(r"\s+", "", n).lower() in ctx]
    if not grounded:
        return 1.0
    checked_norm = re.sub(r"\s+", "", checked).lower()
    kept = sum(1 for n in grounded if re.sub(r"\s+", "", n).lower() in checked_norm)
    return kept / len(grounded)


def verify(answer: str, context: str, graph_only: bool = False, query: str = "") -> str:
    """
    小模型对照资料核查 → 确定性守卫收尾 → 相关性闸门。
    graph_only：图谱-only 兜底时把收紧规则也注入校验提示词——否则小模型会放过
    图谱之外的内容（"图谱-only 授权过宽"的根因，恒传空串是漏接实况）。
    query：用户问题——相关性闸门的输入（问题 vs 最终答案多轮评分，低分回兜底）。
    """
    global _verify_failures, _verify_suspended_until
    if (not config.VERIFY_ENABLED or not answer.strip() or not context.strip()
            or answer.strip() == guards.NO_ANSWER):
        return answer
    if time.time() < _verify_suspended_until:
        return guards.fix_output(answer) or guards.NO_ANSWER   # 熔断中：只过确定性守卫
    missing = guards.find_missing_terms(answer, context)
    missing_block = "、".join(missing) if missing else "（无）"
    # 跨文档拼接检测（句级出处绑定）：疑似清单喂校验模型（软处置），
    # 高置信（短句全数字失配）并入缺失清单走硬删
    spliced_soft, spliced_hard = guards.find_mismatched_numbers(answer, context)
    spliced_block = "、".join(spliced_soft) if spliced_soft else "（无）"
    missing = (missing + spliced_hard)[:12]
    try:
        checked = llm.chat(
            "", prompts.VERIFY_PROMPT.format(missing=missing_block,
                                             spliced=spliced_block,
                                             graph_only_rule=prompts.GRAPH_ONLY_RULE if graph_only else "",
                                             context=context[:6000], answer=answer),
            model=config.VERIFY_MODEL, base=config.DASHSCOPE_BASE, key=config.DASHSCOPE_KEY,
            temperature=0.2, extra={"enable_thinking": False},
        ).strip()
        if checked.startswith("```"):
            checked = re.sub(r"^```\w*\s*", "", checked)
            checked = re.sub(r"\s*```$", "", checked)
        # 过度删除防护（两层）：
        # ① 小模型偶发把正文整个删空（只剩标题）——正文超过 200 字却被删掉一半以上 → 回退原文
        # ② 数字保全：原文中在资料里有据的数字，校验后留存率 <60% → 判定误删回退原文
        #    （实况：缺失术语误判曾诱导校验模型把 140/90 等数字剥掉，句壳留下）
        over_deleted = (len(answer) > 200 and len(checked) < len(answer) * 0.5)
        if not over_deleted:
            over_deleted = _grounded_number_retention(answer, checked, context) < 0.6
        if checked and not over_deleted:
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

    # 确定性守卫顺序：兜底句式混入清理 → 缺失术语硬删除 →（图谱-only 数字硬删）
    # → 鉴伪断言 → 空标题清理 → 格式修复。
    # missing 用校验前的答案算：校验器已删掉的术语不误伤，漏删的在此兜底。
    answer = guards.strip_no_answer_hedge(answer)
    answer = guards.strip_missing_terms(answer, missing)
    if graph_only:
        # 图谱-only 数字硬删（软约束管不住模型补数字——图谱编造最危险形态）
        answer = guards.strip_graph_only_numbers(answer, context)
    answer = guards.strip_assertions(answer, context)
    answer = guards.collapse_empty_headings(answer)
    answer = guards.fix_output(answer) or guards.NO_ANSWER
    if graph_only and answer == guards.NO_ANSWER and context.strip():
        # 数字硬删清空答案时回退确定性关系回显（数字编造拦住了，图谱关系
        # 本身仍可答——曾清空直接兜底，连合法关系内容也没了的 guard-014 实况）
        rels = context.replace(
            "## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）",
            "").strip()
        answer = guards.fix_output(rels) or guards.NO_ANSWER
    # 相关性闸门（最后一道）：问题 vs 答案多轮评分，低分直接过掉。
    # 豁免两类：① 名词查找类——≤2 字（"离别"）或无问句意图词（"高血压"，
    # 检索锚定的资料内容就是答案）不进严格闸门（实况：强裁判把裸词评 3 分
    # 误伤）；② 图谱-only——图谱关系是降级兜底，已带"仅供参考"前缀+收紧
    # 规则+数字硬删+进审核队列，且关系列表天然偏弱（"奔马律是什么"被评
    # [3,3] 导致图谱兜底永远兜不住的实况）；闸门管的是"有资料却答非所问"
    if query and answer and answer != guards.NO_ANSWER and not graph_only:
        q = re.sub(r"\s+", "", query)
        if len(q) > 2 and _QUESTION_INTENT.search(q):
            answer = _relevance_gate(query, answer)
    return answer
