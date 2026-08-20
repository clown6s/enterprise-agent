# -*- coding: utf-8 -*-
"""
编排核心（只负责"什么时候走哪条路"，"怎么做"在各自模块里）：
- 提示词            app/prompts.py
- 答案校验链        app/service/verify.py（小模型核查 + 熔断 + 确定性守卫顺序）
- 确定性守卫/术语核对/格式修复  app/guards.py

决策顺序（2026-08-19 去 OA 化后）：
1. 采纳直答 → 多跳编排 → NL2SQL 表查询（员工/报销/经营等事实型问法，注册即用）
2. 闲聊 → 直接对话；指代不明 → 追问澄清
3. 知识问答 → 预检索 →（无结果 → 兜底句式）→ LLM 生成 → 校验链
   → 确定性守卫（缺失术语/鉴伪断言/格式修复）

原则：能确定性算出来的事绝不再交给模型；新数据源=后台注册表，不写代码
（能力总线 2026-08-19 退役：5 条种子规则全并入 NL2SQL 数据面）。
"""
import logging
import re
import time
from datetime import datetime

from .. import embeddings, guards, llm, milvus_store, prompts, tools
from .. import invariants as _invariants
from ..request_context import get_request_id
from ..domains import (attention_pattern, dimension_pairs, lifestyle_pattern,
                       synonym_groups)
from ..learn import adopted as adopted_qa
from ..learn import feedback, gap_discovery, synonyms
from ..plan import sop as sop_engine
from ..plan import approval as approval_flow
from ..plan import approvals as _approvals  # noqa: F401  导入即注册（报销/出差/采购/合同 SOP 挂进注册表）
from . import acl, docwizard, graph, history, multihop, nlsql, retrieval, verify
from .verify import verify_breaker_state   # 再导出：管理后台熔断状态查询仍从 agent 导入

log = logging.getLogger(__name__)


class Route:
    """对外 route 字符串的单一登记处；值保持前端/飞书协议兼容。"""
    DATA = "DATA"
    SOP = "SOP"
    APPROVAL = "APPROVAL"
    KNOWLEDGE_QA = "KNOWLEDGE_QA"
    CHAT = "CHAT"
    CLARIFY = "CLARIFY"
    GUARD = "GUARD"
    DOC_WIZARD = "DOC_WIZARD"
    ERROR = "ERROR"


def _result(answer: str, route: str, sources: list | None = None,
            chart=None, **extra) -> dict:
    """统一答案结果结构，防止新增出口漏 sources/chart/route。"""
    return {"answer": answer, "sources": sources or [], "route": route,
            "chart": chart, **extra}


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
# NL2SQL 无预闸（2026-08-19 用户定调：不做写死词表预判——知识库定义回答什么、
# skills 定义能干什么、NL2SQL 自己判断能不能答）。曾白名单/黑名单/lookahead
# 预判正则补丁三轮全废（口语碎片漏预闸、黑名单误伤"是什么时候"）；
# 现无条件尝试：模型生成 SQL 或空模板（=无法确定→None 漏回后续层），
# 多一次模型调用是正确架构的代价（用户明确不在乎成本）


# ---------------- 路由与工具函数 ----------------

def route(query: str, has_history: bool = False) -> str:
    """意图路由（正则启发式，不调路由模型——模型路由不可靠已被验证过）。
    has_history：追问形态（指代/衔接/省略三类，与 _is_followup 同口径）+
    有对话历史 → 不追问，走知识问答（指代走改写补全；"什么意思"走历史作答）。
    曾只放行指代与元问题——"怎么办"（省略句）有历史仍被打发去 CLARIFY，
    与追问统一解析口径不一致（不变量测试抓出的实况）。"""
    q = query.strip()
    if _CHAT_WORDS.search(q):
        return Route.CHAT
    if _CLARIFY_GUARD.search(q) and not _DATA_DAYS.search(q):
        if has_history and (_REFERENTS.search(q) or _META_QUESTION.search(q)
                            or _CONNECTORS.match(q) or _QUESTION_ELIDE.match(q)):
            return Route.KNOWLEDGE_QA
        return Route.CLARIFY
    return Route.KNOWLEDGE_QA


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
    失败返回空串（调用方回退原文检索）。
    2026-08-19 飞书实况：多实体历史（王强→赵敏两段对话）里"他"被消解成
    更早的王强——提示词加"最近一轮"规则，追问对象几乎总是刚提到的实体。"""
    try:
        return (llm.chat_chain(
            "", "结合【对话历史】，把下面的用户问题改写为完整明确的问题"
                "（消解'它/这个/那个/他/她'等指代，补上指代对象）。"
                "规则：指代对象优先取【对话历史】中最近一轮提到的人或物。"
                "只输出改写后的问题，不要任何解释：\n"
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
    raw = llm.chat_chain(prompts.render_prompt(prompts.SYSTEM_PROMPT, date=_today_line()), gen_prompt)
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
            "route": Route.KNOWLEDGE_QA, "chart": None,
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
    raw = llm.chat_chain(prompts.render_prompt(prompts.SYSTEM_PROMPT, date=_today_line()) + "\n" + prompts.GRAPH_ONLY_RULE,
                   user_prompt)
    final = verify.verify(raw, context, graph_only=True, query=query)
    if final == guards.NO_ANSWER:
        return None
    final = "以下内容基于知识图谱关联（无文档来源，仅供参考）：\n" + final
    _maybe_enqueue_review(conversation_id, query, final)
    return _result(final, Route.KNOWLEDGE_QA)


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


def _content_words(q: str) -> list[str]:
    """问句内容词（延伸推荐预检用）：去疑问词/虚词后的 2-8 字片段"""
    q = re.sub(r"(是多少|有哪些|是什么|多少|如何|怎么|怎样|什么|哪些|为什么|为何"
               r"|吗|呢|请问|的|了|是|有|在)", " ", q or "")
    return [t for t in re.findall(r"[一-龥]{2,8}|[A-Za-z]{2,}", q)]


def suggest_followups(query: str, answer: str, user: str = "") -> list[str]:
    """答案延伸推荐：根据问题+答案生成 2 个自然的追问（飞书卡片按钮用，
    点击即追问）。软依赖：失败返回空列表。
    推荐预检（确定性）：候选追问的判别词（追问里有、原问题里没有的内容词
    及其 2-3 字片段）必须出现在检索命中文本里，否则不推荐——曾推荐
    "心衰如何诊断"而资料只有 NYHA 分级+治疗原则（点击必兜底）的实况。
    误杀方向宁缺毋滥：资料有答案但写法不同 → 少一个按钮，不伤主答案。"""
    try:
        raw = llm.chat_chain(
            "", "根据下面的问题与回答，提出 2 个用户最可能接着问的相关问题。"
                "只提与回答内容直接相关的话题，不要提回答里没有涉及的内容。"
                "每行一个，只输出问题本身（≤20 字），不要编号和解释：\n\n"
                f"【问题】\n{query}\n\n【回答】\n{answer[:1500]}",
            temperature=0.3).strip()
        out = []
        base = " ".join(_content_words(query))
        for line in raw.splitlines():
            line = re.sub(r"^[\d.\-、*\s]+", "", line).strip()
            if not line or len(line) > 30 or line in out:
                continue
            discr = [w for w in _content_words(line) if w not in base]
            if not discr:
                continue   # 无新内容词（纯指代/复述原问题）不推荐
            keys: set[str] = set()
            for w in discr:
                keys.add(w)
                for n in (3, 2):
                    keys.update(w[i:i + n] for i in range(len(w) - n + 1))
            try:
                vec = embeddings.embed(line)
                hits = milvus_store.search(vec, line, top_k=3,
                                           expr=acl.allowed_expr(user))
            except Exception:
                continue
            texts = "".join(h["text"] for h in hits)
            if any(k in texts for k in keys):
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
    _t0 = time.time()
    log.info("answer_start query=%s endpoint=%s conversation_id=%s",
             query[:40], endpoint, conversation_id or "-")
    try:
        result = _answer_inner(query, conversation_id, file_block, user)
    except Exception as e:
        # 异常也落审计（曾只记 SUCCESS——看板 errorCount 恒 0，故障不可见）
        if not history.is_test_conversation(conversation_id):
            _record_audit(query, {"answer": "", "route": Route.ERROR, "sources": []},
                          _t.time() - _t0, user, 0, endpoint=endpoint,
                          status="ERROR", error=str(e)[:200])
        raise
    turns = history.recent_turns(conversation_id)
    # 评测/扫描会话不落审计（曾 eval 流量灌进看板——热门问法里"你好"65 次
    # 全是跑分噪音，监控数据失真）
    if not history.is_test_conversation(conversation_id):
        _record_audit(query, result, _t.time() - _t0, user, len(turns), endpoint=endpoint)
    log.info("answer_done route=%s answer_chars=%d duration_ms=%d",
             result.get("route", Route.ERROR), len(result.get("answer") or ""),
             int((_t.time() - _t0) * 1000))
    # 多轮历史：本轮问答落库（兜底句式/GUARD 拦截不落——无信息量/注入文本不可回流；
    # 锚定句为展示层，剥掉再存——否则历史注入后模型再引用锚定句 → 双重锚定；
    # 评测/扫描会话不落——防跑分污染历史表）
    if (result.get("route") != "GUARD" and not result.get("answer").startswith(guards.NO_ANSWER)
            and not history.is_test_conversation(conversation_id)):
        history.save_turn(conversation_id, query, guards.strip_leading_anchor(result["answer"]))
    # 运行时不变量：出口契约检查（route 合法/answer 非空）——生产自证，失败写告警表
    if not history.is_test_conversation(conversation_id):
        ok, detail = _invariants.check_answer_contract(result)
        if not ok:
            _invariants.fail("answer_contract", detail)
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
                (get_request_id() or _uuid.uuid4().hex[:16], user or "admin", "", endpoint,
                 result.get("route", "KNOWLEDGE_QA"),
                 query, history_count, _json.dumps(result.get("sources") or [], ensure_ascii=False),
                 0, 0, 0, (result.get("answer") or "")[:500],
                 len(result.get("answer") or ""), round(seconds * 1000), status, error))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("审计落库失败: %s（trace_id=%s）", e, get_request_id("-"))


def _route_early(query: str, conversation_id: str, user: str,
                file_block: str, turns: list[dict]) -> dict | None:
    """共享路由决策（步骤 1-9）：injection→adopted→multihop→nlsql→docwizard→
    pending→approval→sop。命中早期路由返回 dict，未命中返回 None 进入 route() 分支。

    流式/非流式共用此函数——早期路由步骤都用 llm.chat_chain（非流式 LLM 调用），
    两路径完全一致，只有返回格式不同（dict vs yield）。提取此函数消除 90% 重复代码
    （曾双路径漏接出过三次事——CLAUDE.md 设计原则 3）。
    """
    # 提示注入拦截（确定性，先于一切路由——用户消息级注入防线）
    if guards.contains_injection(query):
        return _result(guards.INJECTION_BLOCKED, Route.GUARD)

    # 采纳升级直答层
    hit = adopted_qa.lookup(query)
    if hit:
        return {"answer": hit["answer"],
                "sources": [hit["source"]] if hit.get("source") else [],
                "route": Route.KNOWLEDGE_QA, "chart": None}

    # 多跳编排（复合问题拆解分派后合并）
    try:
        mh_ans = multihop.try_answer(query, user=user)
    except Exception:
        mh_ans = None
    if mh_ans:
        return {"answer": guards.fix_output(mh_ans), "sources": [],
                "route": Route.DATA, "chart": None}

    # NL2SQL 表查询层（追问改写前置：指代追问先补全再进数据面）
    nlsql_q = _resolve_query(query, turns)
    try:
        sql_ans = nlsql.try_answer(nlsql_q, user=user)
    except Exception:
        sql_ans = None
    if sql_ans:
        return {"answer": guards.fix_output(sql_ans), "sources": [],
                "route": Route.DATA, "chart": None}

    # 文档多轮修改 + 文档生成
    if tools.is_enabled("doc_wizard"):
        revised = docwizard.revise(conversation_id, query)
        if revised:
            return {"answer": "已更新到云文档（在线可编辑）：\n" + revised,
                    "sources": [], "route": Route.DOC_WIZARD, "chart": None}
        doc = docwizard.try_generate(query, "", conversation_id)
        if doc:
            if doc.get("needs_form"):
                return {"answer": "请告诉我文档标题，例如：「帮我生成关于五一的放假通知」。",
                        "sources": [], "route": Route.DOC_WIZARD, "chart": None}
            return {"answer": doc["answer"], "sources": [], "route": Route.DOC_WIZARD, "chart": None}

    # 待确认单的确认/取消（人机协同闸门——先于 SOP 匹配）
    pending = sop_engine.try_confirm_or_cancel(conversation_id, query)
    if pending:
        return {"answer": guards.fix_output(pending["answer"]), "sources": [],
                "route": Route.SOP, "chart": None}

    # 审批指令（审批人回复"同意/拒绝 [单号]"）
    approval_hit = approval_flow.try_approval_command(query, user)
    if approval_hit:
        return {"answer": guards.fix_output(approval_hit["answer"]), "sources": [],
                "route": Route.APPROVAL, "chart": None}

    # SOP 状态机（双重开关：skill 表 + sop 表）
    _sop_def = sop_engine.match(query)
    sop = (_sop_def if _sop_def and tools.is_enabled(_sop_def.code)
           and sop_engine.is_enabled(_sop_def.code) else None)
    if sop:
        result = sop_engine.execute(sop, query, conversation_id)
        return {"answer": guards.fix_output(result["answer"]), "sources": [],
                "route": Route.SOP, "chart": None}

    return None   # 未命中早期路由，进入 route() 分支


def _answer_knowledge_qa(query: str, conversation_id: str, user: str,
                          file_block: str, turns: list[dict],
                          *, stream: bool = False) -> dict:
    """知识问答共享函数（流式/非流式共用——消除 ~140 行重复代码）。

    检索→空手→元问题→文件上下文→图谱兜底→兜底句式→生成→校验→NO_ANSWER 恢复→锚定。
    stream=True 时用 chat_stream_chain 收集后校验（流式 KNOWLEDGE_QA 实际不是真流式——
    先收齐再校验再下发，与非流式语义等价）。
    """
    search_query = _resolve_query(query, turns)
    hits = retrieval.retrieve(search_query, extra_queries=_expand_queries(search_query), user=user)
    if not hits and search_query != query:
        hits = retrieval.retrieve(query, extra_queries=_expand_queries(query), user=user)
        if hits:
            search_query = query
    if not hits and len(re.sub(r"\s+", "", query)) > 4:
        rewritten = _rewrite_query(query)
        if rewritten and rewritten != query:
            hits = retrieval.retrieve(rewritten, extra_queries=_expand_queries(rewritten), user=user)
            if hits:
                search_query = rewritten
    context = guards.strip_injection_sentences(retrieval.build_context(hits)) if hits else ""
    log.info("knowledge_retrieval query=%s search_query=%s hits=%d context_chars=%d",
             query[:30], search_query[:30], len(hits), len(context))
    hblock = history.history_block(turns)
    fb = guards.strip_injection_sentences(file_block) if file_block else ""
    if not context.strip():
        if hblock and _META_QUESTION.search(query):
            user_prompt = (f"【资料】\n{hblock}\n\n【文件上下文】\n{fb}\n\n【用户问题】\n{query}"
                           if fb else f"【资料】\n{hblock}\n\n【用户问题】\n{query}")
            raw = _llm_call(user_prompt, stream=stream)
            final = verify.verify(raw, f"{hblock}\n{fb}" if fb else hblock, query="")
            _maybe_enqueue_review(conversation_id, query, final)
            return _result(final, Route.KNOWLEDGE_QA)
        if fb:
            user_prompt = f"【资料】\n{fb}\n\n【用户问题】\n{query}"
            raw = _llm_call(user_prompt, stream=stream)
            final = verify.verify(raw, fb, query=query)
            _maybe_enqueue_review(conversation_id, query, final)
            return _result(final, Route.KNOWLEDGE_QA)
        log.info("knowledge_empty_context query=%s", query[:40])
        graph_fb = _try_graph_fallback(search_query, turns, fb, conversation_id)
        if graph_fb:
            return graph_fb
        gap_discovery.record_gap(query, conversation_id)
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        return _result(_NO_ANSWER_GAP, Route.KNOWLEDGE_QA)
    sources = sorted({h["source"] for h in hits if h.get("source")})
    ctx_main = guards.strip_injection_sentences(
        retrieval.build_context([h for h in hits if not h.get("_expanded")]))
    if _attention_gap(query, ctx_main):
        gap_discovery.record_gap(query, conversation_id)
        _maybe_enqueue_review(conversation_id, query, guards.NO_ANSWER)
        return _result(_NO_ANSWER_GAP, Route.KNOWLEDGE_QA)
    prefix = f"【资料】\n{context}"
    if fb:
        prefix += f"\n\n【文件上下文】\n{fb}"
    gen_prompt = (f"{prefix}\n\n{hblock}\n【用户问题】\n{query}" if hblock
                  else f"{prefix}\n\n【用户问题】\n{query}")
    raw = _llm_call(gen_prompt, stream=stream)
    vctx = context
    if fb:
        vctx = f"{vctx}\n{fb}"
    if hblock:
        vctx = f"{vctx}\n{hblock}"
    final = verify.verify(raw, vctx, query=search_query)
    if final == guards.NO_ANSWER:
        _maybe_enqueue_review(conversation_id, query, final)
        recovered = _try_synonym_recovery(query, search_query, turns, user, fb, conversation_id)
        if recovered:
            return recovered
        log.info("knowledge_empty_context query=%s", query[:40])
        graph_fb = _try_graph_fallback(search_query, turns, fb, conversation_id)
        if graph_fb:
            return graph_fb
        gap_discovery.record_gap(query, conversation_id)
        return _result(final, Route.KNOWLEDGE_QA)
    return {"answer": _anchor_short_query(query, final, hits[0].get("source", "")),
            "sources": sources, "route": Route.KNOWLEDGE_QA, "chart": None}


def _llm_call(user_prompt: str, *, stream: bool = False) -> str:
    """LLM 调用适配器：stream=False 用 chat_chain，stream=True 用 chat_stream_chain 收集。

    流式 KNOWLEDGE_QA 先收齐全部 chunk 再拼成字符串，校验链行为与非流式一致。
    """
    system = prompts.render_prompt(prompts.SYSTEM_PROMPT, date=_today_line())
    if not stream:
        return llm.chat_chain(system, user_prompt)
    parts = []
    for chunk in llm.chat_stream_chain(system, user_prompt):
        parts.append(chunk)
    return "".join(parts)


def _answer_inner(query: str, conversation_id: str = "", file_block: str = "",
                  user: str = "") -> dict:
    # 提示注入拦截 + 早期路由（injection→adopted→multihop→nlsql→docwizard→
    # pending→approval→sop）——与流式路径共用 _route_early（消除 90% 重复代码）
    turns = history.recent_turns(conversation_id)
    early = _route_early(query, conversation_id, user, file_block, turns)
    if early is not None:
        return early

    r = route(query, has_history=bool(turns))
    if r == "CHAT":
        # 闲聊不带历史（曾先提报销确认单后说"你好"，模型复读历史里的
        # "请确认报销申请：王强…"话术——闲聊是开场语，历史无益且污染）
        return {"answer": guards.clean_cjk_spacing(
                    llm.chat_chain(prompts.render_prompt(prompts.SYSTEM_PROMPT, date=_today_line()), query)),
                "sources": [], "route": Route.CHAT, "chart": None}
    if r == "CLARIFY":
        return {"answer": "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。",
                "sources": [], "route": Route.CLARIFY, "chart": None}

    # 知识问答与流式路径共用核心（stream=False：完整 chat_chain）
    return _answer_knowledge_qa(query, conversation_id, user, file_block, turns, stream=False)


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
    sources_val: list = []
    try:
        for event, data in _answer_stream_impl(query, conversation_id, user, file_block):
            if event == "sources":
                sources_val = data or []
            if event == "token" and isinstance(data, str):
                tokens.append(data)
            elif event == "route":
                route_val = data
            yield event, data
    except Exception as e:
        if not history.is_test_conversation(conversation_id):
            _record_audit(query, {"answer": "".join(tokens), "route": route_val, "sources": sources_val},
                          _t.time() - _t0, user, 0, endpoint=endpoint,
                          status="ERROR", error=str(e)[:200])
        raise
    if history.is_test_conversation(conversation_id):
        return   # 评测/扫描会话不落审计（与非流式同口径）
    hc = len(history.recent_turns(conversation_id))   # 与 answer() 审计口径一致
    _record_audit(query, {"answer": "".join(tokens), "route": route_val, "sources": sources_val},
                  _t.time() - _t0, user, hc, endpoint=endpoint)


def _answer_stream_impl(query: str, conversation_id: str = "", user: str = "", file_block: str = ""):
    """
    流式回答实现（answer_stream 的包装层做审计，这里只管生成）。

    早期路由（injection→adopted→multihop→nlsql→docwizard→pending→approval→sop）
    与非流式共用 _route_early——消除 90% 重复代码（曾双路径漏接出过三次事）。
    """
    # 对话历史提前加载（与非流式同口径——追问改写进数据面）
    turns = history.recent_turns(conversation_id)

    # 共享路由决策（步骤 1-9）：命中早期路由 → yield dict 内容为流式事件
    early = _route_early(query, conversation_id, user, file_block, turns)
    if early is not None:
        yield "route", early.get("route", "KNOWLEDGE_QA")
        yield "token", early.get("answer", "")
        yield "sources", early.get("sources", [])
        yield "done", "[DONE]"
        return

    r = route(query, has_history=bool(turns))
    yield "route", r
    if r == "CHAT":
        # 真流式：逐 chunk 下发（此前收齐后整体下发——闲聊无校验需求，
        # 纯损失）。增量 CJK 清理：留 4 字尾部缓冲，跨 chunk 的
        # "字 标点"粘连在下一段修正（流式中文逐字带空格的实况）
        # 闲聊不带历史（与非流式同口径——曾复读待确认单话术）
        user_prompt = query
        carry = ""
        for chunk in llm.chat_stream_chain(prompts.render_prompt(prompts.SYSTEM_PROMPT, date=_today_line()),
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

    # 知识问答与非流式共用核心（stream=True：chat_stream_chain 收集后校验）
    result = _answer_knowledge_qa(query, conversation_id, user, file_block, turns, stream=True)
    yield "token", result["answer"]
    yield "sources", result["sources"]
    yield "done", "[DONE]"
