# -*- coding: utf-8 -*-
"""
编排核心（只负责"什么时候走哪条路"，"怎么做"在各自模块里）：
- 提示词            app/prompts.py
- 答案校验链        app/service/verify.py（小模型核查 + 熔断 + 确定性守卫顺序）
- 确定性守卫/术语核对/格式修复  app/guards.py

决策顺序（一晚试错得出的结论）：
1. 意图规则表（app/intent/rules.py）——确定性意图（员工目录/OA 数据）直答，不让模型掺和
2. 闲聊 → 直接对话；指代不明 → 追问澄清
3. 知识问答 → 预检索 →（无结果 → 兜底句式）→ LLM 生成 → 校验链
   → 确定性守卫（缺失术语/鉴伪断言/格式修复）

原则：能确定性算出来的事绝不再交给模型；新增意图=规则表加一行。
"""
import re
from datetime import datetime

from .. import guards, llm, prompts
from ..intent import rules as intent_rules
from ..learn import feedback, gap_discovery
from ..plan import sop as sop_engine
from . import docwizard, graph, retrieval, verify
from .verify import verify_breaker_state   # 再导出：管理后台熔断状态查询仍从 agent 导入

# ---------------- 意图与关键词模式 ----------------

# 短实体名词查询（"宫崎骏"3 字）不追问：让检索兜底（无结果走 NO_ANSWER）
_CLARIFY_GUARD = re.compile(r"这个|那个|它|他|她|怎么办|怎么弄|怎么用|什么意思")
_CHAT_WORDS = re.compile(
    r"^(你好|您好|hi|hello|谢谢|你是谁|你能做什么|你叫什么|在吗|早上好|晚上好|再见|拜拜|辛苦|谢谢啦|好的|OK|ok)"
)
_DATA_DAYS = re.compile(r"(\d{1,3})\s*天")


# ---------------- 路由与工具函数 ----------------

def route(query: str) -> str:
    """意图路由（正则启发式，不调路由模型——模型路由不可靠已被验证过）"""
    if _CHAT_WORDS.search(query.strip()):
        return "CHAT"
    if _CLARIFY_GUARD.search(query.strip()) and not _DATA_DAYS.search(query):
        return "CLARIFY"
    return "KNOWLEDGE_QA"


def _today_line() -> str:
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"今天是 {now.strftime('%Y-%m-%d')} 星期{week}"


def _maybe_enqueue_review(conversation_id: str, query: str, answer: str) -> None:
    """低把握答案进人工审核队列；评测会话跳过（防回归跑分污染审核队列）"""
    if conversation_id.startswith("eval"):
        return
    feedback.enqueue_review(query, answer)


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
    """审计落库（audit_event 表，沿用既有 schema；软依赖）"""
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
    # 提示注入拦截（确定性，先于一切路由——用户消息级注入防线）
    if guards.contains_injection(query):
        return {"answer": guards.INJECTION_BLOCKED, "sources": [], "route": "GUARD", "chart": None}

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

    # 待确认单的确认/取消（人机协同闸门——先于 SOP 匹配，"确认"不会误入其他路由）
    pending = sop_engine.try_confirm_or_cancel(conversation_id, query)
    if pending:
        return {"answer": guards.fix_output(pending["answer"]), "sources": [],
                "route": "SOP", "chart": None}

    # SOP 状态机（执行准确层：请假四步走确定性流程 + 提交前确认）
    sop = sop_engine.match(query)
    if sop:
        result = sop_engine.execute(sop, query, conversation_id)
        return {"answer": guards.fix_output(result["answer"]), "sources": [],
                "route": "SOP", "chart": None}

    r = route(query)
    if r == "CHAT":
        return {"answer": guards.clean_cjk_spacing(
                    llm.chat(prompts.SYSTEM_PROMPT.format(date=_today_line()), query)),
                "sources": [], "route": "CHAT", "chart": None}
    if r == "CLARIFY":
        return {"answer": "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。",
                "sources": [], "route": "CLARIFY", "chart": None}

    # 知识问答：预检索 →（空手/上下文被注入清洗删空 → 图谱-only 兜底 → 仍空 → 兜底句式）→ 生成 → 校验
    hits = retrieval.retrieve(query)
    # 上下文注入清洗：知识库文档可投毒——含注入句式的句子确定性删除
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    if not context.strip():
        graph_ctx = guards.strip_injection_sentences(graph.expand(query))
        if graph_ctx:
            # 图谱-only：上下文只有图谱关联，校验层加图谱收紧规则
            context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
                       + graph_ctx)
            user_prompt = f"【资料】\n{context}\n\n【用户问题】\n{query}"
            if file_block:
                user_prompt = (f"【文件上下文】\n{guards.strip_injection_sentences(file_block)}\n\n"
                               + user_prompt)
            raw = llm.chat(prompts.SYSTEM_PROMPT.format(date=_today_line()) + "\n" + prompts.GRAPH_ONLY_RULE,
                           user_prompt)
            final = verify.verify(raw, context, graph_only=True)
            _maybe_enqueue_review(conversation_id, query, final)   # 图谱-only：低把握答案进人工审核
            return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
        gap_discovery.record_gap(query)   # 缺口信号：兜底句式触发
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        return {"answer": guards.NO_ANSWER, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
    sources = sorted({h["source"] for h in hits if h.get("source")})
    raw = llm.chat(
        prompts.SYSTEM_PROMPT.format(date=_today_line()),
        f"【资料】\n{context}\n\n【用户问题】\n{query}",
    )
    final = verify.verify(raw, context)
    return {"answer": final, "sources": sources, "route": "KNOWLEDGE_QA", "chart": None}


def answer_stream(query: str, conversation_id: str = ""):
    """
    流式回答生成器：yield (event, data) 元组。
    SSE 事件名保持 route/token/sources/chart/done，
    这样 rag-admin 前端一行都不用改。
    """
    # 提示注入拦截（与非流式路径同款——双路径必须同步）
    if guards.contains_injection(query):
        yield "route", "GUARD"
        yield "token", guards.INJECTION_BLOCKED
        yield "sources", []
        yield "done", "[DONE]"
        return

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

    pending = sop_engine.try_confirm_or_cancel(conversation_id, query)
    if pending:
        yield "route", "SOP"
        yield "token", guards.fix_output(pending["answer"])
        yield "sources", []
        yield "done", "[DONE]"
        return

    sop = sop_engine.match(query)
    if sop:
        yield "route", "SOP"
        result = sop_engine.execute(sop, query, conversation_id)
        yield "token", guards.fix_output(result["answer"])
        yield "sources", []
        yield "done", "[DONE]"
        return

    r = route(query)
    yield "route", r
    if r == "CHAT":
        # DeepSeek 流式中文偶发逐字带空格（"你好 ！ 请问"）→ 收齐后清理再整体下发
        parts = [c for c in llm.chat_stream(prompts.SYSTEM_PROMPT.format(date=_today_line()), query)]
        yield "token", guards.clean_cjk_spacing("".join(parts))
        yield "sources", []
        yield "done", "[DONE]"
        return
    if r == "CLARIFY":
        yield "token", "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。"
        yield "sources", []
        yield "done", "[DONE]"
        return

    # 知识问答：预检索（空手/上下文被注入清洗删空 → 图谱-only 兜底 → 仍空 → 兜底句式）
    hits = retrieval.retrieve(query)
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    if not context.strip():
        graph_ctx = guards.strip_injection_sentences(graph.expand(query))
        if graph_ctx:
            context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
                       + graph_ctx)
            parts = []
            for chunk in llm.chat_stream(
                    prompts.SYSTEM_PROMPT.format(date=_today_line()) + "\n" + prompts.GRAPH_ONLY_RULE,
                    f"【资料】\n{context}\n\n【用户问题】\n{query}"):
                parts.append(chunk)
            final = verify.verify("".join(parts), context, graph_only=True)
            _maybe_enqueue_review(conversation_id, query, final)   # 图谱-only：低把握答案进人工审核
            yield "token", final
            yield "sources", []
            yield "done", "[DONE]"
            return
        gap_discovery.record_gap(query)   # 缺口信号：兜底句式触发
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        yield "token", guards.NO_ANSWER
        yield "sources", []
        yield "done", "[DONE]"
        return
    sources = sorted({h["source"] for h in hits if h.get("source")})

    # 流式链路同样先收齐全文再校验再下发（"无依据内容不出门"原则）
    parts = []
    for chunk in llm.chat_stream(
        prompts.SYSTEM_PROMPT.format(date=_today_line()),
        f"【资料】\n{context}\n\n【用户问题】\n{query}",
    ):
        parts.append(chunk)
    final = verify.verify("".join(parts), context)
    yield "token", final
    yield "sources", sources
    yield "done", "[DONE]"
