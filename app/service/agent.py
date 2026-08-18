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
from ..domains import (attention_pattern, dimension_pairs, lifestyle_pattern,
                       synonym_groups)
from ..intent import rules as intent_rules
from ..learn import feedback, gap_discovery, synonyms
from ..plan import sop as sop_engine
from . import docwizard, graph, history, retrieval, verify
from .verify import verify_breaker_state   # 再导出：管理后台熔断状态查询仍从 agent 导入

# ---------------- 意图与关键词模式 ----------------

# 短实体名词查询（"宫崎骏"3 字）不追问：让检索兜底（无结果走 NO_ANSWER）
_CLARIFY_GUARD = re.compile(r"这个|那个|它|他|她|怎么办|怎么弄|怎么用|什么意思")
# 追问形态三类（统一判定，有历史时走改写补全——曾只认显式指代，
# "怎么发生的"零指代/省略主语追问漏网，检索空手后用历史凑数的实况）：
# ① 指代词（含衔接代词 那/这）② 衔接词开头的短句 ③ 疑问省略句
_REFERENTS = re.compile(r"这个|那个|它|他|她|上述|前面|刚刚|刚才|那|这")
_CONNECTORS = re.compile(r"^(那|还有|另外|其次|然后|接着|顺便)")
_QUESTION_ELIDE = re.compile(r"^(怎么|为什么|为何|有哪些|有什么|能不能|可以|会不会|是什么|怎么办|咋|如何|怎样|啥|什么|呢)")
# 元问题：问的是"上一轮回答本身"——唯一合法用历史作答的场景
_META_QUESTION = re.compile(r"什么意思|没听懂|再说|重复|复述|你刚才|你说的是|刚才说的")
_CHAT_WORDS = re.compile(
    r"^(你好|您好|hi|hello|谢谢|你是谁|你能做什么|你叫什么|在吗|早上好|晚上好|再见|拜拜|辛苦|谢谢啦|好的|OK|ok)"
)
_DATA_DAYS = re.compile(r"(\d{1,3})\s*天")


# ---------------- 路由与工具函数 ----------------

def route(query: str, has_history: bool = False) -> str:
    """意图路由（正则启发式，不调路由模型——模型路由不可靠已被验证过）。
    has_history：追问形态（指代/衔接/省略三类，与 _is_followup 同口径）+
    有对话历史 → 不追问，走知识问答（指代走改写补全；"什么意思"走历史作答）。
    曾只放行指代与元问题——"怎么办"（省略句）有历史仍被打发去 CLARIFY，
    与追问统一解析口径不一致（不变量测试抓出的实况）。"""
    q = query.strip()
    if _CHAT_WORDS.search(q):
        return "CHAT"
    if _CLARIFY_GUARD.search(q) and not _DATA_DAYS.search(q):
        if has_history and (_REFERENTS.search(q) or _META_QUESTION.search(q)
                            or _CONNECTORS.match(q) or _QUESTION_ELIDE.match(q)):
            return "KNOWLEDGE_QA"
        return "CLARIFY"
    return "KNOWLEDGE_QA"


def _rewrite_query(query: str) -> str:
    """
    口语化问句改写（检索空手时的冷路径兜底）：'血压高怎么降' → 规范问法，
    补全简称与口语。只输出改写后的问句；失败返回空串。"""
    try:
        return (llm.chat_chain(
            "", "把下面的口语化问句改写为规范的检索问句（补全简称与口语，"
                "如'血压高'→'高血压'、'放假几天'→'节假日安排'）。"
                "只输出改写后的问句，不要解释：\n" + query,
            temperature=0.2).strip() or "")
    except Exception:
        return ""


def _resolve_referent(query: str, turns: list[dict]) -> str:
    """
    指代消解 v2：'那它的控制目标呢' 结合对话历史改写成完整问题（补上指代对象），
    用改写后的问题重新检索——指代问题从此有真实来源，而不是纯靠历史猜。
    失败返回空串（调用方回退原文检索）。"""
    try:
        return (llm.chat_chain(
            "", "结合【对话历史】，把下面的用户问题改写为完整明确的问题"
                "（消解'它/这个/那个'等指代，补上指代对象）。只输出改写后的问题，"
                "不要任何解释：\n"
                f"【对话历史】\n{history.history_block(turns)}\n\n【用户问题】\n{query}",
            temperature=0.2).strip() or "")
    except Exception:
        return ""


def _is_followup(query: str, turns: list[dict]) -> bool:
    """追问判定（确定性，一类机制）：有历史且属三类追问形态之一。
    ① 指代词 ② 衔接词开头短句 ③ 疑问省略句（去掉疑问词后 ≤4 字且无数字/
    书名号——"治疗高血压"5 字是动宾完整短语不判省略，曾用 8 字误判
    完整问句浪费改写调用）"""
    q = (query or "").strip()
    if not turns or not q:
        return False
    if _REFERENTS.search(q):
        return True
    if _CONNECTORS.match(q) and len(q) <= 15:
        return True
    m = _QUESTION_ELIDE.match(q)
    if m:
        rest = re.sub(r"\s+", "", q[m.end():])
        if rest and len(rest) <= 4 and not re.search(r"\d|《|》", rest):
            return True
    return False


def _resolve_query(query: str, turns: list[dict]) -> str:
    """追问统一解析：追问形态 → LLM 补全改写（失败回退原句）。
    曾只对显式指代改写——零指代/衔接追问漏网是"无来源凑数"类实况的根因。"""
    if not _is_followup(query, turns):
        return query
    rewritten = _resolve_referent(query, turns)
    return rewritten or query


def _today_line() -> str:
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"今天是 {now.strftime('%Y-%m-%d')} 星期{week}"


# 兜底句式 + 缺口告知（智能方向：答不上的时候告诉用户"缺口已被记录、会补"）
_NO_ANSWER_GAP = guards.NO_ANSWER + "（已记录为知识缺口，将定期汇总补库建议。）"

# 领域知识已插件化（app/domains/，.env DOMAIN_PLUGINS 开关）——
# 行业词表不再硬编码主流程：dimension_pairs/synonym_groups/attention/lifestyle
# 均由启用插件聚合提供（换行业 KB=换插件列表，主流程零改动；用户 2026-08-18 定调）


def _expand_queries(query: str) -> list[str]:
    """确定性查询扩展（多查询并联，不拼串——曾把扩展词拼进主查询，
    高频词如"处罚/罚款/拘留"稀释 BM25：纯"结伙斗殴"检索 GT 排第一，
    扩展串检索反而掉出前 12 的实况）。返回 ≤2 条扩展检索短语，与主查询
    并联检索后并集重排。来源：领域插件（domains）+ 拒绝路径
    自学习沉淀的词对（synonyms.json）。"""
    extra = []
    for q_pat, c_pat in dimension_pairs():
        if q_pat.search(query):
            extra.append(c_pat.pattern.replace("|", " "))
    for group in synonym_groups():
        if any(t in query for t in group["terms"]):
            extra.append(group["expand"])
    extra.extend(synonyms.expand_queries(query))
    return extra[:2]


def _first_trigger(query: str) -> str:
    """查询中第一个命中维度/同义词组的词（词对沉淀的触发键）；
    无命中回退查询前 8 字"""
    for q_pat, _ in dimension_pairs():
        m = q_pat.search(query)
        if m:
            return m.group()
    for group in synonym_groups():
        for t in group["terms"]:
            if t in query:
                return t
    return re.sub(r"\s+", "", query)[:8]


def _try_synonym_recovery(query: str, search_query: str, turns: list[dict],
                          user: str, fb: str, conversation_id: str) -> dict | None:
    """闸门拒绝后的自学习恢复（一次机会）：LLM 生成检索变体 → 并联再检索
    → 有新资料则重新生成+评审 → 通过即答出并沉淀词对。
    曾每发现词汇不对齐人工给词表加一行——现在系统第一次遇到就自愈并记住。"""
    try:
        variants_raw = llm.chat_chain(
            "", "把下面的问题改写为 2 个检索用的同义词短语。每行一个短语，"
                "短语内用空格分隔多个同义词，例如：\n饮食 膳食 食物 低盐 低脂\n"
                "只输出短语，不要解释：\n" + search_query,
            temperature=0.2).strip()
        variants = [l.strip() for l in variants_raw.splitlines() if l.strip()][:2]
    except Exception:
        return None
    variants = [v for v in variants if v and v != search_query]
    if not variants:
        return None
    hits = retrieval.retrieve(search_query, extra_queries=variants, user=user)
    if not hits:
        return None
    context = guards.strip_injection_sentences(retrieval.build_context(hits))
    hblock = history.history_block(turns)
    prefix = f"【资料】\n{context}"
    if fb:
        prefix += f"\n\n【文件上下文】\n{fb}"
    gen_prompt = (f"{prefix}\n\n{hblock}\n【用户问题】\n{query}" if hblock
                  else f"{prefix}\n\n【用户问题】\n{query}")
    raw = llm.chat_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()), gen_prompt)
    vctx = context
    if fb:
        vctx = f"{vctx}\n{fb}"
    if hblock:
        vctx = f"{vctx}\n{hblock}"
    final = verify.verify(raw, vctx, query=search_query)
    if final == guards.NO_ANSWER:
        return None
    trigger = _first_trigger(search_query)
    for v in variants:
        synonyms.learn(trigger, v)
    _maybe_enqueue_review(conversation_id, query, final)
    # _learned_pairs：本次恢复使用的词对——渠道层在用户点👎时回调
    # penalize（负向强化闭环：恢复答案被否定 → 词对失败计数）
    return {"answer": _anchor_short_query(query, final, hits[0].get("source", "")),
            "sources": sorted({h["source"] for h in hits if h.get("source")}),
            "route": "KNOWLEDGE_QA", "chart": None,
            "_learned_pairs": [(trigger, v) for v in variants]}


def _try_graph_fallback(query: str, turns: list[dict], fb: str,
                        conversation_id: str) -> dict | None:
    """图谱兜底二段尝试：检索有命中但答案兜底（噪音 chunk 挡住/闸门拒绝/
    模型自觉兜底）时，图谱关系仍有答案价值——曾只在检索空手时走图谱，
    1 条无关 chunk 挡住图谱路径的实况（guard-014）。"""
    try:
        graph_ctx = guards.strip_injection_sentences(graph.expand(query))
    except Exception:
        return None
    if not graph_ctx:
        return None
    context = ("## 知识图谱关联（背景参考：请转述为自然语言融入回答，不要逐条复述）\n"
               + graph_ctx)
    # 图谱兜底生成不注入【对话历史】：历史在追问统一解析层已消费（调用方传的
    # query 是补全后的 search_query），图谱关系作答是自包含的。曾注入历史被
    # 模型整块回显成答案正文（"心衰如何诊断"实况），且 vctx 含历史使校验层
    # 无从拦截（历史是合法依据）——回显源头直接拿掉
    user_prompt = f"【资料】\n{context}\n\n【用户问题】\n{query}"
    if fb:
        user_prompt = f"【文件上下文】\n{fb}\n\n" + user_prompt
    raw = llm.chat_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()) + "\n" + prompts.GRAPH_ONLY_RULE,
                   user_prompt)
    final = verify.verify(raw, context, graph_only=True, query=query)
    if final == guards.NO_ANSWER:
        return None
    final = "以下内容基于知识图谱关联（无文档来源，仅供参考）：\n" + final
    _maybe_enqueue_review(conversation_id, query, final)
    return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}


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
    for q_pat, c_pat in dimension_pairs():
        if q_pat.search(q):
            return c_pat.search(ctx) is None
    if attention_pattern().search(q):
        return lifestyle_pattern().search(ctx) is None
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
        raw = llm.chat_chain(
            "", "根据下面的问题与回答，提出 2 个用户最可能接着问的相关问题。"
                "每行一个，只输出问题本身（≤20 字），不要编号和解释：\n\n"
                f"【问题】\n{query}\n\n【回答】\n{answer[:1500]}",
            temperature=0.3).strip()
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
           user: str = "", endpoint: str = "agent/chat") -> dict:
    """非流式回答（Web /chat 端点；飞书桥复用）。
    file_block：会话文件上下文（图片 OCR 文本等），问答时作为首要依据注入。
    user：提问者标识（飞书 open_id/后台用户名）——检索 ACL 过滤与审计落库用。
    endpoint：渠道标识（feishu/chat 等）——看板按渠道监控用。"""
    import time as _t
    _t0 = _t.time()
    try:
        result = _answer_inner(query, conversation_id, file_block, user)
    except Exception as e:
        # 异常也落审计（曾只记 SUCCESS——看板 errorCount 恒 0，故障不可见）
        if not history.is_test_conversation(conversation_id):
            _record_audit(query, {"answer": "", "route": "ERROR", "sources": []},
                          _t.time() - _t0, user, 0, endpoint=endpoint,
                          status="ERROR", error=str(e)[:200])
        raise
    turns = history.recent_turns(conversation_id)
    # 评测/扫描会话不落审计（曾 eval 流量灌进看板——热门问法里"你好"65 次
    # 全是跑分噪音，监控数据失真）
    if not history.is_test_conversation(conversation_id):
        _record_audit(query, result, _t.time() - _t0, user, len(turns), endpoint=endpoint)
    # 多轮历史：本轮问答落库（兜底句式/GUARD 拦截不落——无信息量/注入文本不可回流；
    # 锚定句为展示层，剥掉再存——否则历史注入后模型再引用锚定句 → 双重锚定；
    # 评测/扫描会话不落——防跑分污染历史表）
    if (result.get("route") != "GUARD" and not result.get("answer").startswith(guards.NO_ANSWER)
            and not history.is_test_conversation(conversation_id)):
        history.save_turn(conversation_id, query, guards.strip_leading_anchor(result["answer"]))
    return result


def _record_audit(query: str, result: dict, seconds: float, user: str = "admin",
                  history_count: int = 0, endpoint: str = "agent/chat",
                  status: str = "SUCCESS", error: str = "") -> None:
    """审计落库（audit_event 表，沿用既有 schema；软依赖）。
    endpoint 区分渠道（曾所有渠道都写 agent/chat，飞书流量与 Web 混在一起）。"""
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
                (_uuid.uuid4().hex[:16], user or "admin", "", endpoint,
                 result.get("route", "KNOWLEDGE_QA"),
                 query, history_count, _json.dumps(result.get("sources") or [], ensure_ascii=False),
                 0, 0, 0, (result.get("answer") or "")[:500],
                 len(result.get("answer") or ""), round(seconds * 1000), status, error))
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
                    llm.chat_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()), user_prompt)),
                "sources": [], "route": "CHAT", "chart": None}
    if r == "CLARIFY":
        return {"answer": "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。",
                "sources": [], "route": "CLARIFY", "chart": None}

    # 知识问答：预检索 →（空手 → 元问题用历史 / 有文件上下文用文件 → 图谱-only
    # 兜底 → 仍空 → 兜底句式）→ 生成 → 校验
    # 追问统一解析：指代/衔接/省略三类追问 → 补全改写再检索（曾只认显式
    # 指代——"怎么发生的"零指代追问漏网后用历史凑数，sources=[] 实况）
    search_query = _resolve_query(query, turns)
    # 检索词=查询扩展版（维度词追加资料侧同义词）；闸门评审仍用 clean 的
    # search_query——扩展词会干扰评审员（"…饮食 低盐 低脂 膳食"vs 答案）
    hits = retrieval.retrieve(search_query, extra_queries=_expand_queries(search_query), user=user)
    # 检索双保险：改写句空手且与原句不同 → 原句再试（防改写改错）
    if not hits and search_query != query:
        hits = retrieval.retrieve(query, extra_queries=_expand_queries(query), user=user)
        if hits:
            search_query = query
    # 口语化改写重试：仍空手且问句值得改写（>4 字）→ 规范问法再试一次
    if not hits and len(re.sub(r"\s+", "", query)) > 4:
        rewritten = _rewrite_query(query)
        if rewritten and rewritten != query:
            hits = retrieval.retrieve(rewritten, extra_queries=_expand_queries(rewritten), user=user)
            if hits:
                search_query = rewritten
    # 上下文注入清洗：知识库文档可投毒——含注入句式的句子确定性删除
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    hblock = history.history_block(turns)
    fb = guards.strip_injection_sentences(file_block) if file_block else ""
    if not context.strip():
        if hblock and _META_QUESTION.search(query):
            # 元问题（"什么意思/再说一遍"）：问上一轮回答本身——唯一合法
            # 历史作答场景。曾任何追问空手都拿历史凑数（"怎么发生的"被
            # 套上"发生机制"标题重炒分类内容——无来源+凑数双实况）。
            # 相关性闸门豁免（query=""）：评审员看不到历史，"什么意思"vs
            # 历史转述必评 1 分误杀——与指代问题用改写句评审的对称处理
            user_prompt = (f"【资料】\n{hblock}\n\n【文件上下文】\n{fb}\n\n【用户问题】\n{query}"
                           if fb else f"【资料】\n{hblock}\n\n【用户问题】\n{query}")
            raw = llm.chat_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()), user_prompt)
            final = verify.verify(raw, f"{hblock}\n{fb}" if fb else hblock, query="")
            _maybe_enqueue_review(conversation_id, query, final)
            return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
        if fb:
            # 文件上下文作答（图片 OCR 等）：依据=文件内容本身。曾 fb 只在
            # 检索命中时注入——检索空手时图片问答错走历史/图谱兜底
            user_prompt = f"【资料】\n{fb}\n\n【用户问题】\n{query}"
            raw = llm.chat_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()), user_prompt)
            final = verify.verify(raw, fb, query=query)
            _maybe_enqueue_review(conversation_id, query, final)
            return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
        graph_fb = _try_graph_fallback(search_query, turns, fb, conversation_id)
        if graph_fb:
            return graph_fb
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
    raw = llm.chat_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()), gen_prompt)
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
        # 自学习恢复：LLM 生成检索变体再试一次（词汇不对齐的自我补全）
        recovered = _try_synonym_recovery(query, search_query, turns, user, fb,
                                          conversation_id)
        if recovered:
            return recovered
        # 图谱兜底二段：噪音 chunk 挡住时图谱关系仍有答案价值（曾直接兜底）
        graph_fb = _try_graph_fallback(search_query, turns, fb, conversation_id)
        if graph_fb:
            return graph_fb
        # 恢复也失败：记知识缺口（曾闸门拒绝不进缺口聚类——知识库健康度
        # 看板上这类"答不上"隐形，与空手/维度闸门路径口径不一致）
        gap_discovery.record_gap(query)
        # 闸门拒绝=无依据，不得带来源（曾"暂无法回答"+来源并存的矛盾实况）
        return {"answer": final, "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
    return {"answer": _anchor_short_query(query, final, hits[0].get("source", "")),
            "sources": sources, "route": "KNOWLEDGE_QA", "chart": None}


def answer_stream(query: str, conversation_id: str = "", user: str = "", file_block: str = "",
                  endpoint: str = "agent/chat"):
    """
    流式回答生成器：yield (event, data) 元组。
    SSE 事件名保持 route/token/sources/chart/done，
    这样 rag-admin 前端一行都不用改。
    包装层做审计收尾（曾流式路径完全不落审计——SSE 用量在看板上不可见；
    异常也落 ERROR，与 answer() 口径一致）。"""
    import time as _t
    _t0 = _t.time()
    tokens: list[str] = []
    route_val = "UNKNOWN"
    try:
        for event, data in _answer_stream_impl(query, conversation_id, user, file_block):
            if event == "token" and isinstance(data, str):
                tokens.append(data)
            elif event == "route":
                route_val = data
            yield event, data
    except Exception as e:
        if not history.is_test_conversation(conversation_id):
            _record_audit(query, {"answer": "".join(tokens), "route": route_val, "sources": []},
                          _t.time() - _t0, user, 0, endpoint=endpoint,
                          status="ERROR", error=str(e)[:200])
        raise
    if history.is_test_conversation(conversation_id):
        return   # 评测/扫描会话不落审计（与非流式同口径）
    hc = len(history.recent_turns(conversation_id))   # 与 answer() 审计口径一致
    _record_audit(query, {"answer": "".join(tokens), "route": route_val, "sources": []},
                  _t.time() - _t0, user, hc, endpoint=endpoint)


def _answer_stream_impl(query: str, conversation_id: str = "", user: str = "", file_block: str = ""):
    """
    流式回答实现（answer_stream 的包装层做审计，这里只管生成）。
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
        for chunk in llm.chat_stream_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()),
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

    # 知识问答：预检索（空手 → 元问题用历史 / 有文件上下文用文件 → 图谱-only
    # 兜底 → 仍空 → 兜底句式）——与非流式路径同步
    search_query = _resolve_query(query, turns)
    hits = retrieval.retrieve(search_query, extra_queries=_expand_queries(search_query), user=user)
    # 检索双保险：改写句空手且与原句不同 → 原句再试（防改写改错）
    if not hits and search_query != query:
        hits = retrieval.retrieve(query, extra_queries=_expand_queries(query), user=user)
        if hits:
            search_query = query
    # 口语化改写重试：仍空手且问句值得改写（>4 字）→ 规范问法再试一次
    if not hits and len(re.sub(r"\s+", "", query)) > 4:
        rewritten = _rewrite_query(query)
        if rewritten and rewritten != query:
            hits = retrieval.retrieve(rewritten, extra_queries=_expand_queries(rewritten), user=user)
            if hits:
                search_query = rewritten
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    hblock = history.history_block(turns)
    fb = guards.strip_injection_sentences(file_block) if file_block else ""
    if not context.strip():
        if hblock and _META_QUESTION.search(query):
            # 元问题：问上一轮回答本身——唯一合法历史作答场景（同步非流式；
            # 相关性闸门豁免同非流式）
            user_prompt = (f"【资料】\n{hblock}\n\n【文件上下文】\n{fb}\n\n【用户问题】\n{query}"
                           if fb else f"【资料】\n{hblock}\n\n【用户问题】\n{query}")
            parts = []
            for chunk in llm.chat_stream_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()),
                                          user_prompt):
                parts.append(chunk)
            final = verify.verify("".join(parts), f"{hblock}\n{fb}" if fb else hblock, query="")
            _maybe_enqueue_review(conversation_id, query, final)
            yield "token", final
            yield "sources", []
            yield "done", "[DONE]"
            return
        if fb:
            # 文件上下文作答（同步非流式——曾 fb 只在检索命中时注入）
            parts = []
            for chunk in llm.chat_stream_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()),
                                          f"【资料】\n{fb}\n\n【用户问题】\n{query}"):
                parts.append(chunk)
            final = verify.verify("".join(parts), fb, query=query)
            _maybe_enqueue_review(conversation_id, query, final)
            yield "token", final
            yield "sources", []
            yield "done", "[DONE]"
            return
        graph_fb = _try_graph_fallback(search_query, turns, fb, conversation_id)
        if graph_fb:
            yield "token", graph_fb["answer"]
            yield "sources", graph_fb["sources"]
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
    for chunk in llm.chat_stream_chain(prompts.SYSTEM_PROMPT.format(date=_today_line()), gen_prompt):
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
        # 自学习恢复（与非流式同口径——双路径同步）
        recovered = _try_synonym_recovery(query, search_query, turns, user, fb,
                                          conversation_id)
        if recovered:
            yield "token", recovered["answer"]
            yield "sources", recovered["sources"]
            yield "done", "[DONE]"
            return
        # 图谱兜底二段（与非流式同口径）
        graph_fb = _try_graph_fallback(search_query, turns, fb, conversation_id)
        if graph_fb:
            yield "token", graph_fb["answer"]
            yield "sources", graph_fb["sources"]
            yield "done", "[DONE]"
            return
        # 恢复也失败：记知识缺口（与非流式同口径）
        gap_discovery.record_gap(query)
        # 闸门拒绝=无依据，不得带来源（与非流式同口径）
        yield "token", final
        yield "sources", []
        yield "done", "[DONE]"
        return
    yield "token", _anchor_short_query(query, final, hits[0].get("source", ""))
    yield "sources", sources
    yield "done", "[DONE]"
