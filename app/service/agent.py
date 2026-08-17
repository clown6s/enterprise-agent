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

from .. import guards, llm, prompts, tools
from ..intent import rules as intent_rules
from ..learn import feedback, gap_discovery
from ..plan import sop as sop_engine
from . import docwizard, graph, history, retrieval, verify
from .verify import verify_breaker_state   # 再导出：管理后台熔断状态查询仍从 agent 导入

# ---------------- 意图与关键词模式 ----------------

# 短实体名词查询（"宫崎骏"3 字）不追问：让检索兜底（无结果走 NO_ANSWER）
_CLARIFY_GUARD = re.compile(r"这个|那个|它|他|她|怎么办|怎么弄|怎么用|什么意思")
# 指代词：有对话历史可消解时不再追问（无历史仍走追问澄清）
_REFERENTS = re.compile(r"这个|那个|它|他|她|上述|前面|刚刚|刚才")
_CHAT_WORDS = re.compile(
    r"^(你好|您好|hi|hello|谢谢|你是谁|你能做什么|你叫什么|在吗|早上好|晚上好|再见|拜拜|辛苦|谢谢啦|好的|OK|ok)"
)
_DATA_DAYS = re.compile(r"(\d{1,3})\s*天")


# ---------------- 路由与工具函数 ----------------

def route(query: str, has_history: bool = False) -> str:
    """意图路由（正则启发式，不调路由模型——模型路由不可靠已被验证过）。
    has_history：指代词 + 有对话历史 → 不追问，走知识问答（历史消解指代）。"""
    if _CHAT_WORDS.search(query.strip()):
        return "CHAT"
    if _CLARIFY_GUARD.search(query.strip()) and not _DATA_DAYS.search(query):
        if has_history and _REFERENTS.search(query.strip()):
            return "KNOWLEDGE_QA"
        return "CLARIFY"
    return "KNOWLEDGE_QA"


def _rewrite_query(query: str) -> str:
    """
    口语化问句改写（检索空手时的冷路径兜底）：'血压高怎么降' → 规范问法，
    补全简称与口语。只输出改写后的问句；失败返回空串。"""
    try:
        return (llm.chat(
            "", "把下面的口语化问句改写为规范的检索问句（补全简称与口语，"
                "如'血压高'→'高血压'、'放假几天'→'节假日安排'）。"
                "只输出改写后的问句，不要解释：\n" + query,
            temperature=0.2, extra={"enable_thinking": False}).strip() or "")
    except Exception:
        return ""


def _resolve_referent(query: str, turns: list[dict]) -> str:
    """
    指代消解 v2：'那它的控制目标呢' 结合对话历史改写成完整问题（补上指代对象），
    用改写后的问题重新检索——指代问题从此有真实来源，而不是纯靠历史猜。
    失败返回空串（调用方回退原文检索）。"""
    try:
        return (llm.chat(
            "", "结合【对话历史】，把下面的用户问题改写为完整明确的问题"
                "（消解'它/这个/那个'等指代，补上指代对象）。只输出改写后的问题，"
                "不要任何解释：\n"
                f"【对话历史】\n{history.history_block(turns)}\n\n【用户问题】\n{query}",
            temperature=0.2, extra={"enable_thinking": False}).strip() or "")
    except Exception:
        return ""


def _today_line() -> str:
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"今天是 {now.strftime('%Y-%m-%d')} 星期{week}"


# 兜底句式 + 缺口告知（智能方向：答不上的时候告诉用户"缺口已被记录、会补"）
_NO_ANSWER_GAP = guards.NO_ANSWER + "（已记录为知识缺口，将定期汇总补库建议。）"

_ATTENTION_QUERY = re.compile(r"注意")
_LIFESTYLE_TERMS = re.compile(r"饮食|运动|锻炼|戒烟|限酒|饮酒|体重|睡眠|情绪|复查|监测|低盐|低脂")
# 维度词检查：问句中出现的维度，资料里必须有对应内容（问什么答什么，没有就答没有）
_DIM_CHECKS = [
    (re.compile(r"用药|服药|吃药|药物"), re.compile(r"用药|药物|制剂|剂量|药")),
    (re.compile(r"饮食|怎么吃|吃什么|低盐|低脂"), re.compile(r"饮食|低盐|低脂|盐")),
    (re.compile(r"运动|锻炼"), re.compile(r"运动|锻炼")),
    (re.compile(r"睡眠|失眠"), re.compile(r"睡眠|失眠")),
    (re.compile(r"监测|复查"), re.compile(r"监测|复查|测量")),
    (re.compile(r"戒烟|吸烟"), re.compile(r"戒烟|吸烟|烟")),
    (re.compile(r"限酒|饮酒|喝酒"), re.compile(r"酒")),
    (re.compile(r"体重|减肥|肥胖"), re.compile(r"体重|肥胖")),
]


def _attention_gap(query: str, context: str) -> bool:
    """
    资料覆盖闸门（用户明确的设计原则）：问什么答什么——
    ① 问句中出现的维度词（用药/饮食/运动/监测…）资料里没有 → 直接答没有
    ② "注意事项"泛指提问且资料无生活方式内容 → 直接答没有
    不得拿诊断、治疗等相邻内容凑数（凑数=乱扯的实况）。
    确定性拦截（不依赖模型评审）；模型层还有相关性闸门兜底。"""
    q, ctx = query or "", context or ""
    if not q or not ctx:
        return False
    for q_pat, c_pat in _DIM_CHECKS:
        if q_pat.search(q):
            return c_pat.search(ctx) is None
    if _ATTENTION_QUERY.search(q):
        return _LIFESTYLE_TERMS.search(ctx) is None
    return False


def _anchor_short_query(query: str, answer: str, source: str) -> str:
    """超短实体查询（≤2 字）的答案补来源锚定句——实况：用户问"离别"，
    模型把资料的阅读理解答案直接当回答（"描写告别的方法"），像自问自答。
    锚定后用户一眼知道在答哪份资料。只锚定检索路径；图谱/历史路径无来源、
    兜底句式不锚定。source 取重排后排名第一的命中（勿用去重集合首项——
    那是字母序，曾锚成次相关来源的实况）。
    答案自带锚定（模型引用了被采纳的带锚定内容）时先剥掉再补——
    双重锚定的实况根因。"""
    q = re.sub(r"\s+", "", query or "")
    if len(q) > 2 or not source or not answer or answer == guards.NO_ANSWER:
        return answer
    src = re.sub(r"\.(pdf|docx?|txt|md)$", "", source, flags=re.I)
    if not src:
        return answer
    answer = guards.strip_leading_anchor(answer)
    return f"根据《{src}》一文：\n{answer}"


def suggest_followups(query: str, answer: str) -> list[str]:
    """答案延伸推荐：根据问题+答案生成 2 个自然的追问（飞书卡片按钮用，
    点击即追问）。软依赖：失败返回空列表。"""
    try:
        raw = llm.chat(
            "", "根据下面的问题与回答，提出 2 个用户最可能接着问的相关问题。"
                "每行一个，只输出问题本身（≤20 字），不要编号和解释：\n\n"
                f"【问题】\n{query}\n\n【回答】\n{answer[:1500]}",
            temperature=0.3, extra={"enable_thinking": False}).strip()
        out = []
        for line in raw.splitlines():
            line = re.sub(r"^[\d.\-、*\s]+", "", line).strip()
            if line and len(line) <= 30 and line not in out:
                out.append(line)
            if len(out) >= 2:
                break
        return out
    except Exception:
        return []


def _maybe_enqueue_review(conversation_id: str, query: str, answer: str) -> None:
    """低把握答案进人工审核队列；评测/扫描会话跳过（防跑分污染审核队列）"""
    if history.is_test_conversation(conversation_id):
        return
    feedback.enqueue_review(query, answer)


# ---------------- 主入口 ----------------

def answer(query: str, conversation_id: str = "", file_block: str = "",
           user: str = "") -> dict:
    """非流式回答（Web /chat 端点；飞书桥复用）。
    file_block：会话文件上下文（图片 OCR 文本等），问答时作为首要依据注入。
    user：提问者标识（飞书 open_id/后台用户名）——检索 ACL 过滤与审计落库用。"""
    import time as _t
    _t0 = _t.time()
    result = _answer_inner(query, conversation_id, file_block, user)
    turns = history.recent_turns(conversation_id)
    _record_audit(query, result, _t.time() - _t0, user, len(turns))
    # 多轮历史：本轮问答落库（兜底句式/GUARD 拦截不落——无信息量/注入文本不可回流；
    # 锚定句为展示层，剥掉再存——否则历史注入后模型再引用锚定句 → 双重锚定；
    # 评测/扫描会话不落——防跑分污染历史表）
    if (result.get("route") != "GUARD" and not result.get("answer").startswith(guards.NO_ANSWER)
            and not history.is_test_conversation(conversation_id)):
        history.save_turn(conversation_id, query, guards.strip_leading_anchor(result["answer"]))
    return result


def _record_audit(query: str, result: dict, seconds: float, user: str = "admin",
                  history_count: int = 0) -> None:
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
                (_uuid.uuid4().hex[:16], user or "admin", "", "agent/chat", result.get("route", "KNOWLEDGE_QA"),
                 query, history_count, _json.dumps(result.get("sources") or [], ensure_ascii=False),
                 0, 0, 0, (result.get("answer") or "")[:500],
                 len(result.get("answer") or ""), round(seconds * 1000), "SUCCESS", ""))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _answer_inner(query: str, conversation_id: str = "", file_block: str = "",
                  user: str = "") -> dict:
    # 提示注入拦截（确定性，先于一切路由——用户消息级注入防线）
    if guards.contains_injection(query):
        return {"answer": guards.INJECTION_BLOCKED, "sources": [], "route": "GUARD", "chart": None}

    # 意图规则表优先（确定性直答）
    intent = intent_rules.match(query)
    if intent:
        return {"answer": guards.fix_output(intent["answer"]), "sources": [],
                "route": "KNOWLEDGE_QA", "chart": intent.get("chart")}

    # 文档多轮修改（会话有进行中文档 + 修改指令 → 回写同一份云文档）
    if tools.is_enabled("doc_wizard"):
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
    # 双重开关：skill 表（工具级） + sop 表（管理后台 SOP 页开关）
    sop = (sop_engine.match(query)
           if (tools.is_enabled("leave_action") and sop_engine.is_enabled()) else None)
    if sop:
        result = sop_engine.execute(sop, query, conversation_id)
        return {"answer": guards.fix_output(result["answer"]), "sources": [],
                "route": "SOP", "chart": None}

    turns = history.recent_turns(conversation_id)
    r = route(query, has_history=bool(turns))
    if r == "CHAT":
        user_prompt = (f"{history.history_block(turns)}\n{query}" if turns else query)
        return {"answer": guards.clean_cjk_spacing(
                    llm.chat(prompts.SYSTEM_PROMPT.format(date=_today_line()), user_prompt)),
                "sources": [], "route": "CHAT", "chart": None}
    if r == "CLARIFY":
        return {"answer": "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。",
                "sources": [], "route": "CLARIFY", "chart": None}

    # 知识问答：预检索 →（空手 → 有历史用历史作答 → 图谱-only 兜底 → 仍空 → 兜底句式）→ 生成 → 校验
    # 指代消解 v2：指代词 + 有历史 → 先改写为完整问题再检索（有真实来源）
    search_query = _resolve_referent(query, turns) if (turns and _REFERENTS.search(query)) else query
    hits = retrieval.retrieve(search_query, user=user)
    # 口语化改写重试：检索空手且问句值得改写（>4 字）→ 规范问法再试一次
    if not hits and len(re.sub(r"\s+", "", query)) > 4:
        rewritten = _rewrite_query(query)
        if rewritten and rewritten != query:
            hits = retrieval.retrieve(rewritten, user=user)
            if hits:
                search_query = rewritten
    # 上下文注入清洗：知识库文档可投毒——含注入句式的句子确定性删除
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    hblock = history.history_block(turns)
    fb = guards.strip_injection_sentences(file_block) if file_block else ""
    if not context.strip():
        if hblock:
            # 检索空手但有对话历史：历史消解指代/延续话题（校验依据=历史本身）
            user_prompt = (f"【资料】\n{hblock}\n\n【文件上下文】\n{fb}\n\n【用户问题】\n{query}"
                           if fb else f"【资料】\n{hblock}\n\n【用户问题】\n{query}")
            raw = llm.chat(prompts.SYSTEM_PROMPT.format(date=_today_line()), user_prompt)
            final = verify.verify(raw, f"{hblock}\n{fb}" if fb else hblock, query=query)
            _maybe_enqueue_review(conversation_id, query, final)
            return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
        graph_ctx = guards.strip_injection_sentences(graph.expand(query))
        if graph_ctx:
            # 图谱-only：上下文只有图谱关联，校验层加图谱收紧规则
            context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
                       + graph_ctx)
            user_prompt = (f"【资料】\n{context}\n\n{hblock}\n【用户问题】\n{query}" if hblock
                           else f"【资料】\n{context}\n\n【用户问题】\n{query}")
            if file_block:
                user_prompt = (f"【文件上下文】\n{guards.strip_injection_sentences(file_block)}\n\n"
                               + user_prompt)
            raw = llm.chat(prompts.SYSTEM_PROMPT.format(date=_today_line()) + "\n" + prompts.GRAPH_ONLY_RULE,
                           user_prompt)
            final = verify.verify(raw, f"{context}\n{hblock}" if hblock else context,
                                  graph_only=True, query=query)
            if final != guards.NO_ANSWER:
                final = "以下内容基于知识图谱关联（无文档来源，仅供参考）：\n" + final
            _maybe_enqueue_review(conversation_id, query, final)   # 图谱-only：低把握答案进人工审核
            return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
        gap_discovery.record_gap(query)   # 缺口信号：兜底句式触发
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        return {"answer": _NO_ANSWER_GAP, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
    sources = sorted({h["source"] for h in hits if h.get("source")})
    # 资料覆盖闸门：问"注意事项"但资料没该维度内容 → 直接答没有，不凑数
    if _attention_gap(query, context):
        gap_discovery.record_gap(query)
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        return {"answer": _NO_ANSWER_GAP, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
    # 文件上下文（图片 OCR 等）作为首要依据注入——此前只在图谱兜底分支注入，
    # 检索命中时被静默忽略（实况：发图后问"图里说什么"，答案只字不提图片）
    prefix = f"【资料】\n{context}"
    if fb:
        prefix += f"\n\n【文件上下文】\n{fb}"
    gen_prompt = (f"{prefix}\n\n{hblock}\n【用户问题】\n{query}" if hblock
                  else f"{prefix}\n\n【用户问题】\n{query}")
    raw = llm.chat(prompts.SYSTEM_PROMPT.format(date=_today_line()), gen_prompt)
    # 校验依据含文件上下文与历史：引用图片/上一轮事实时不被误判为"资料外"
    vctx = context
    if fb:
        vctx = f"{vctx}\n{fb}"
    if hblock:
        vctx = f"{vctx}\n{hblock}"
    # 相关性闸门用改写后的问题评判（指代问题评审员看不到历史，用原句会误杀）
    final = verify.verify(raw, vctx, query=search_query)
    if final == guards.NO_ANSWER:
        _maybe_enqueue_review(conversation_id, query, final)   # 相关性闸门拒绝的也进人工审核
    return {"answer": _anchor_short_query(query, final, hits[0].get("source", "")),
            "sources": sources, "route": "KNOWLEDGE_QA", "chart": None}


def answer_stream(query: str, conversation_id: str = "", user: str = "", file_block: str = ""):
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

    if tools.is_enabled("doc_wizard"):
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

    sop = (sop_engine.match(query)
           if (tools.is_enabled("leave_action") and sop_engine.is_enabled()) else None)
    if sop:
        yield "route", "SOP"
        result = sop_engine.execute(sop, query, conversation_id)
        yield "token", guards.fix_output(result["answer"])
        yield "sources", []
        yield "done", "[DONE]"
        return

    turns = history.recent_turns(conversation_id)
    r = route(query, has_history=bool(turns))
    yield "route", r
    if r == "CHAT":
        # 真流式：逐 chunk 下发（此前收齐后整体下发——闲聊无校验需求，
        # 纯损失）。增量 CJK 清理：留 4 字尾部缓冲，跨 chunk 的
        # "字 标点"粘连在下一段修正（流式中文逐字带空格的实况）
        user_prompt = (f"{history.history_block(turns)}\n{query}" if turns else query)
        carry = ""
        for chunk in llm.chat_stream(prompts.SYSTEM_PROMPT.format(date=_today_line()),
                                     user_prompt):
            text = carry + chunk
            carry = ""
            if len(text) >= 6:
                carry = text[-4:]
                text = text[:-4]
            if text:
                yield "token", guards.clean_cjk_spacing(text)
        if carry:
            yield "token", guards.clean_cjk_spacing(carry)
        yield "sources", []
        yield "done", "[DONE]"
        return
    if r == "CLARIFY":
        yield "token", "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。"
        yield "sources", []
        yield "done", "[DONE]"
        return

    # 知识问答：预检索（空手 → 有历史用历史作答 → 图谱-only 兜底 → 仍空 → 兜底句式）
    search_query = _resolve_referent(query, turns) if (turns and _REFERENTS.search(query)) else query
    hits = retrieval.retrieve(search_query, user=user)
    # 口语化改写重试：检索空手且问句值得改写（>4 字）→ 规范问法再试一次
    if not hits and len(re.sub(r"\s+", "", query)) > 4:
        rewritten = _rewrite_query(query)
        if rewritten and rewritten != query:
            hits = retrieval.retrieve(rewritten, user=user)
            if hits:
                search_query = rewritten
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    hblock = history.history_block(turns)
    fb = guards.strip_injection_sentences(file_block) if file_block else ""
    if not context.strip():
        if hblock:
            # 检索空手但有对话历史：历史消解指代/延续话题（校验依据=历史本身）
            user_prompt = (f"【资料】\n{hblock}\n\n【文件上下文】\n{fb}\n\n【用户问题】\n{query}"
                           if fb else f"【资料】\n{hblock}\n\n【用户问题】\n{query}")
            parts = []
            for chunk in llm.chat_stream(prompts.SYSTEM_PROMPT.format(date=_today_line()),
                                          user_prompt):
                parts.append(chunk)
            final = verify.verify("".join(parts), f"{hblock}\n{fb}" if fb else hblock, query=query)
            _maybe_enqueue_review(conversation_id, query, final)
            yield "token", final
            yield "sources", []
            yield "done", "[DONE]"
            return
        graph_ctx = guards.strip_injection_sentences(graph.expand(query))
        if graph_ctx:
            context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
                       + graph_ctx)
            user_prompt = (f"【资料】\n{context}\n\n{hblock}\n【用户问题】\n{query}" if hblock
                           else f"【资料】\n{context}\n\n【用户问题】\n{query}")
            parts = []
            for chunk in llm.chat_stream(
                    prompts.SYSTEM_PROMPT.format(date=_today_line()) + "\n" + prompts.GRAPH_ONLY_RULE,
                    user_prompt):
                parts.append(chunk)
            final = verify.verify("".join(parts), f"{context}\n{hblock}" if hblock else context,
                                  graph_only=True, query=query)
            if final != guards.NO_ANSWER:
                final = "以下内容基于知识图谱关联（无文档来源，仅供参考）：\n" + final
            _maybe_enqueue_review(conversation_id, query, final)   # 图谱-only：低把握答案进人工审核
            yield "token", final
            yield "sources", []
            yield "done", "[DONE]"
            return
        gap_discovery.record_gap(query)   # 缺口信号：兜底句式触发
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        yield "token", _NO_ANSWER_GAP
        yield "sources", []
        yield "done", "[DONE]"
        return
    sources = sorted({h["source"] for h in hits if h.get("source")})

    # 资料覆盖闸门：问"注意事项"但资料没该维度内容 → 直接答没有，不凑数
    if _attention_gap(query, context):
        gap_discovery.record_gap(query)
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        yield "token", _NO_ANSWER_GAP
        yield "sources", []
        yield "done", "[DONE]"
        return

    # 流式链路同样先收齐全文再校验再下发（"无依据内容不出门"原则）
    prefix = f"【资料】\n{context}"
    if fb:
        prefix += f"\n\n【文件上下文】\n{fb}"
    gen_prompt = (f"{prefix}\n\n{hblock}\n【用户问题】\n{query}" if hblock
                  else f"{prefix}\n\n【用户问题】\n{query}")
    parts = []
    for chunk in llm.chat_stream(prompts.SYSTEM_PROMPT.format(date=_today_line()), gen_prompt):
        parts.append(chunk)
    # 校验依据含文件上下文与历史：引用图片/上一轮事实时不被误判为"资料外"
    vctx = context
    if fb:
        vctx = f"{vctx}\n{fb}"
    if hblock:
        vctx = f"{vctx}\n{hblock}"
    # 相关性闸门用改写后的问题评判（指代问题评审员看不到历史，用原句会误杀）
    final = verify.verify("".join(parts), vctx, query=search_query)
    if final == guards.NO_ANSWER:
        _maybe_enqueue_review(conversation_id, query, final)   # 相关性闸门拒绝的也进人工审核
    yield "token", _anchor_short_query(query, final, hits[0].get("source", ""))
    yield "sources", sources
    yield "done", "[DONE]"
