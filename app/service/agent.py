# -*- coding: utf-8 -*-
"""
编排核心（Java 版 AgentOrchestrator 的 Python 移植）。

决策顺序（昨晚一晚试错得出的结论）：
1. 意图规则表（app/intent/rules.py）——确定性意图（员工目录/OA 数据）直答，不让模型掺和
2. 闲聊 → 直接对话；指代不明 → 追问澄清
3. 知识问答 → 预检索 →（无结果 → 兜底句式）→ LLM 生成 → 小模型校验
   → 确定性守卫（缺失术语/鉴伪断言/格式修复）

原则：能确定性算出来的事绝不再交给模型；新增意图=规则表加一行。
"""
import re
import time
from datetime import datetime

from .. import config, guards, llm
from ..intent import rules as intent_rules
from ..learn import gap_discovery
from ..plan import sop as sop_engine
from . import docwizard, graph, retrieval

# ---------------- 意图与关键词模式 ----------------

# 短实体名词查询（"宫崎骏"3 字）不追问：让检索兜底（无结果走 NO_ANSWER）
_CLARIFY_GUARD = re.compile(r"这个|那个|它|他|她|怎么办|怎么弄|怎么用|什么意思")
_CHAT_WORDS = re.compile(
    r"^(你好|您好|hi|hello|谢谢|你是谁|你能做什么|你叫什么|在吗|早上好|晚上好|再见|拜拜|辛苦|谢谢啦|好的|OK|ok)"
)
_DATA_DAYS = re.compile(r"(\d{1,3})\s*天")

# ---------------- 系统提示词（防编造规则的 Python 版） ----------------

_SYSTEM_PROMPT = """你是企业内网知识问答助手。请严格按以下规则回答：

1. 只依据【资料】内容回答，逐字核对；资料未涉及的，明确说「根据现有资料，暂无法回答该问题」，不得凭自身知识补充
2. 禁止编造：具体数字、百分比、日期年份、法规名称与条文、机构名称、人名——除非资料原文有
3. 回答使用规范中文术语，不要中英混写
4. 用 markdown 排版：小节标题用「**标题**」加粗，条目用「1. 2. 3.」编号
5. 引用资料事实时可注明来源文件名；不要输出「根据资料显示」之类的套话开头
6. 今天是{date}，涉及相对日期（明天/下周）时以今天为锚点计算
"""

_GRAPH_ONLY_RULE = ("【重点】本次检索无文档片段命中，仅提供知识图谱关联。"
                   "回答中超出图谱关系本身的内容（症状、数值、检查项目、药物、诊断标准、建议等）全部删除。")

_VERIFY_PROMPT = """你是答案事实核查员。对照【资料】逐句核查【答案】：
1. 删除答案中凭空出现的具体事实（数字、百分比、日期、法规名称、人名、机构、条款）——除非资料原文有
2. 【已知缺失术语】列出的术语在资料中不存在，含它们的句子必须删除或改写
3. 资料中确实存在的数字、术语、表述必须保留，不得误删
4. 保持结构与排版不变，不要补充任何资料之外的内容
直接输出核查后的答案，不要任何解释。

【已知缺失术语】
{missing}

{graph_only_rule}
【资料】
{context}

【答案】
{answer}
"""

# 术语抽取（确定性缺失术语核对，Java 版 findMissingTerms 的移植）：
# 校验模型是小模型，术语级漏检率高——哪些术语"资料里没有"是能确定算出来的事
_TERM_PATTERNS = [
    re.compile(r"[A-Za-z]{2,}(?:[-/][A-Za-z0-9]+){0,3}"),      # 英文缩写/术语（HRBP、BNP）
    re.compile(r"\d+(?:\.\d+)?\s*(?:%|mmHg|mg|ml|g|kg|次|天|年|月|日|周|元)"),  # 数值+单位
    re.compile(r"[《「]([^》」]{2,40})[》」]"),                   # 书名号里的法规/文件名
]


def _find_missing_terms(answer: str, context: str) -> list[str]:
    """答案里的术语，凡资料（归一化后）中不出现的 → 缺失清单"""
    ctx = re.sub(r"\s+", "", context).lower()
    missing = []
    seen = set()
    for pattern in _TERM_PATTERNS:
        for m in pattern.finditer(answer):
            term = m.group()
            if term in seen:
                continue
            seen.add(term)
            if term.lower() not in ctx:
                missing.append(term)
    return missing[:12]


# ---------------- 答案校验（小模型核查 + 确定性守卫） ----------------

# 校验层自动熔断：qwen-plus 连续失败 3 次 → 停用 5 分钟（防延迟雪崩），
# 到点自动恢复。校验是增强不是必需——熔断期间答案只过确定性守卫。
_verify_failures = 0
_verify_suspended_until = 0.0


def verify_breaker_state() -> dict:
    return {"failures": _verify_failures,
            "suspended": time.time() < _verify_suspended_until,
            "suspended_until": _verify_suspended_until}


def _verify(answer: str, context: str) -> str:
    """小模型对照资料核查 → 确定性守卫收尾（Java 版 verify() 链的移植）"""
    global _verify_failures, _verify_suspended_until
    if not config.VERIFY_ENABLED or not answer.strip() or not context.strip():
        return answer
    if time.time() < _verify_suspended_until:
        return guards.fix_output(answer) or guards.NO_ANSWER   # 熔断中：只过确定性守卫
    missing = _find_missing_terms(answer, context)
    missing_block = "、".join(missing) if missing else "（无）"
    try:
        checked = llm.chat(
            "", _VERIFY_PROMPT.format(missing=missing_block, graph_only_rule="", context=context[:6000], answer=answer),
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

    # 确定性守卫：鉴伪断言 → 格式修复
    answer = guards.strip_assertions(answer, context)
    return guards.fix_output(answer) or guards.NO_ANSWER


# ---------------- 路由与工具函数 ----------------

def route(query: str) -> str:
    """意图路由（正则启发式，不调路由模型——模型路由不可靠已被验证过）"""
    if _CHAT_WORDS.search(query.strip()):
        return "CHAT"
    if _CLARIFY_GUARD.search(query.strip()) and not _DATA_DAYS.search(query):
        return "CLARIFY"
    return "KNOWLEDGE_QA"


def _clean_cjk_spacing(text: str) -> str:
    """CJK 字符间/标点前的多余空格清理（"你好 ！ 请问 有什么" → "你好！请问有什么"）"""
    text = re.sub(r"(?<=[一-龥])\s+(?=[一-龥])", "", text)
    text = re.sub(r"\s+([。！？，；：、）】》])", r"\1", text)
    return text


def _today_line() -> str:
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"今天是 {now.strftime('%Y-%m-%d')} 星期{week}"


# ---------------- 主入口 ----------------

def answer(query: str, conversation_id: str = "", file_block: str = "") -> dict:
    """非流式回答（Web /chat 端点；飞书桥复用）。
    file_block：会话文件上下文（图片 OCR 文本等），问答时作为首要依据注入。"""
    import time as _t
    _t0 = _t.time()
    result = _answer_inner(query, conversation_id, file_block)
    _record_audit(query, result, _t.time() - _t0)
    return result


def _record_audit(query: str, result: dict, seconds: float) -> None:
    """审计落库（audit_event 表，与 Java 版同 schema；软依赖）"""
    try:
        import json as _json
        import uuid as _uuid
        import pymysql as _pm
        from .. import config as _cfg
        conn = _pm.connect(host=_cfg.MYSQL_HOST, port=_cfg.MYSQL_PORT, user="root",
                           password=_cfg.MYSQL_PASSWORD, database=_cfg.MYSQL_DB,
                           charset="utf8mb4", connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_event (trace_id, timestamp, user, ip, endpoint, category, query, "
                "history_count, sources, retrieval_ms, rerank_degraded, graph_context_chars, "
                "answer_preview, answer_length, total_ms, status, error) "
                "VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (_uuid.uuid4().hex[:16], "admin", "", "agent/chat", result.get("route", "KNOWLEDGE_QA"),
                 query, 0, _json.dumps(result.get("sources") or [], ensure_ascii=False),
                 0, 0, 0, (result.get("answer") or "")[:500],
                 len(result.get("answer") or ""), round(seconds * 1000), "SUCCESS", ""))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _answer_inner(query: str, conversation_id: str = "", file_block: str = "") -> dict:
    # 意图规则表优先（确定性直答）
    intent = intent_rules.match(query)
    if intent:
        return {"answer": guards.fix_output(intent["answer"]), "sources": [],
                "route": "KNOWLEDGE_QA", "chart": intent.get("chart")}

    # 文档多轮修改（会话有进行中文档 + 修改指令 → 回写同一份云文档）
    revised = docwizard.revise(conversation_id, query)
    if revised:
        return {"answer": "已更新到云文档（在线可编辑）：\n" + revised,
                "sources": [], "route": "DOC_WIZARD", "chart": None}
    # 文档生成（一句话直达云文档；无标题时 Web 端给提示，飞书渠道弹表单卡片）
    doc = docwizard.try_generate(query, "", conversation_id)
    if doc:
        if doc.get("needs_form"):
            return {"answer": "请告诉我文档标题，例如：「帮我生成关于五一的放假通知」。",
                    "sources": [], "route": "DOC_WIZARD", "chart": None}
        return {"answer": doc["answer"], "sources": [], "route": "DOC_WIZARD", "chart": None}

    # SOP 状态机（执行准确层：请假四步走确定性流程）
    sop = sop_engine.match(query)
    if sop:
        result = sop_engine.execute(sop, query)
        return {"answer": guards.fix_output(result["answer"]), "sources": [],
                "route": "SOP", "chart": None}

    r = route(query)
    if r == "CHAT":
        return {"answer": _clean_cjk_spacing(
                    llm.chat(_SYSTEM_PROMPT.format(date=_today_line()), query)),
                "sources": [], "route": "CHAT", "chart": None}
    if r == "CLARIFY":
        return {"answer": "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。",
                "sources": [], "route": "CLARIFY", "chart": None}

    # 知识问答：预检索 →（空手 → 图谱-only 兜底 → 仍空 → 兜底句式）→ 生成 → 校验
    hits = retrieval.retrieve(query)
    if not hits:
        graph_ctx = graph.expand(query)
        if graph_ctx:
            # 图谱-only：上下文只有图谱关联，校验层加图谱收紧规则
            context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
                       + graph_ctx)
            user_prompt = f"【资料】\n{context}\n\n【用户问题】\n{query}"
            if file_block:
                user_prompt = f"【文件上下文】\n{file_block}\n\n" + user_prompt
            raw = llm.chat(_SYSTEM_PROMPT.format(date=_today_line()) + "\n" + _GRAPH_ONLY_RULE,
                           user_prompt)
            final = _verify(raw, context)
            return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
        gap_discovery.record_gap(query)   # 缺口信号：兜底句式触发
        return {"answer": guards.NO_ANSWER, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
    context = retrieval.build_context(hits)
    sources = sorted({h["source"] for h in hits if h.get("source")})
    raw = llm.chat(
        _SYSTEM_PROMPT.format(date=_today_line()),
        f"【资料】\n{context}\n\n【用户问题】\n{query}",
    )
    final = _verify(raw, context)
    return {"answer": final, "sources": sources, "route": "KNOWLEDGE_QA", "chart": None}


def answer_stream(query: str, conversation_id: str = ""):
    """
    流式回答生成器：yield (event, data) 元组。
    SSE 事件名与 Java 版完全一致（route/token/sources/chart/done），
    这样 rag-admin 前端一行都不用改。
    """
    # 意图规则表最优先：命中即确定性直答（路由标记 = KNOWLEDGE_QA）
    intent = intent_rules.match(query)
    if intent:
        yield "route", "KNOWLEDGE_QA"
        yield "token", guards.fix_output(intent["answer"])
        if intent.get("chart"):
            yield "chart", intent["chart"]
        yield "sources", []
        yield "done", "[DONE]"
        return

    revised = docwizard.revise(conversation_id, query)
    if revised:
        yield "route", "DOC_WIZARD"
        yield "token", "已更新到云文档（在线可编辑）：\n" + revised
        yield "sources", []
        yield "done", "[DONE]"
        return
    doc = docwizard.try_generate(query, "", conversation_id)
    if doc:
        yield "route", "DOC_WIZARD"
        yield "token", (doc["answer"] if not doc.get("needs_form")
                        else "请告诉我文档标题，例如：「帮我生成关于五一的放假通知」。")
        yield "sources", []
        yield "done", "[DONE]"
        return

    sop = sop_engine.match(query)
    if sop:
        yield "route", "SOP"
        result = sop_engine.execute(sop, query)
        yield "token", guards.fix_output(result["answer"])
        yield "sources", []
        yield "done", "[DONE]"
        return

    r = route(query)
    yield "route", r
    if r == "CHAT":
        # DeepSeek 流式中文偶发逐字带空格（"你好 ！ 请问"）→ 收齐后清理再整体下发
        parts = [c for c in llm.chat_stream(_SYSTEM_PROMPT.format(date=_today_line()), query)]
        yield "token", _clean_cjk_spacing("".join(parts))
        yield "sources", []
        yield "done", "[DONE]"
        return
    if r == "CLARIFY":
        yield "token", "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。"
        yield "sources", []
        yield "done", "[DONE]"
        return

    # 知识问答：预检索（空手 → 图谱-only 兜底 → 仍空 → 兜底句式）
    hits = retrieval.retrieve(query)
    if not hits:
        graph_ctx = graph.expand(query)
        if graph_ctx:
            context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
                       + graph_ctx)
            parts = []
            for chunk in llm.chat_stream(
                    _SYSTEM_PROMPT.format(date=_today_line()) + "\n" + _GRAPH_ONLY_RULE,
                    f"【资料】\n{context}\n\n【用户问题】\n{query}"):
                parts.append(chunk)
            yield "token", _verify("".join(parts), context)
            yield "sources", []
            yield "done", "[DONE]"
            return
        gap_discovery.record_gap(query)   # 缺口信号：兜底句式触发
        yield "token", guards.NO_ANSWER
        yield "sources", []
        yield "done", "[DONE]"
        return
    context = retrieval.build_context(hits)
    sources = sorted({h["source"] for h in hits if h.get("source")})

    # 流式链路同样先收齐全文再校验再下发（"无依据内容不出门"原则）
    parts = []
    for chunk in llm.chat_stream(
        _SYSTEM_PROMPT.format(date=_today_line()),
        f"【资料】\n{context}\n\n【用户问题】\n{query}",
    ):
        parts.append(chunk)
    final = _verify("".join(parts), context)
    yield "token", final
    yield "sources", sources
    yield "done", "[DONE]"
