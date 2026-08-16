# -*- coding: utf-8 -*-
"""
编排核心（Java 版 AgentOrchestrator 的 Python 移植）。

决策顺序 = 昨晚一晚试错得出的结论，全部移植：
1. 员工目录确定性直查（人数统计/个人信息）—— 不让模型掺和
2. OA 数据确定性直查（报销/经营数据）—— 直调 HTTP，答案确定性格式化
3. 流程制度问法 → 只走知识库
4. 闲聊 → 直接对话；过短/指代不明 → 追问澄清
5. 知识问答 → 预检索 →（无结果 → 兜底句式）→ LLM 生成 → 小模型校验
   → 确定性守卫（缺失术语/鉴伪断言/格式修复）

原则：能确定性算出来的事绝不再交给模型。
"""
import re
from datetime import datetime

from .. import config, demo_data, guards, llm
from ..oa_client import oa
from . import retrieval

# ---------------- 意图与关键词模式（全部从 Java 版原样移植） ----------------

_HEADCOUNT = re.compile(r"多少人|有几个人|有几名|几名员工|员工人数|员工总数|人数是多少")
_EMP_INFO = re.compile(r"入职|工号|部门|职位|岗位|什么时候|何时|哪一年|薪资|工资")
_EXPENSE_WORDS = re.compile(r"报销|花了多少钱|费用明细|开支")
_METRICS_WORDS = re.compile(r"经营数据|营收|成本|利润|业绩|销售额|经营情况|生意")
_DATA_PROCESS_WORDS = re.compile(
    r"流程|制度|规定|标准|政策|怎么|如何|条件|材料|发票|格式|模板|须知|方法|注意事项"
)
_DATA_DAYS = re.compile(r"(\d{1,3})\s*天")
# 短实体名词查询（"宫崎骏"3 字）不追问：去掉 ^.{1,8}$，让检索兜底（无结果走 NO_ANSWER）
_CLARIFY_GUARD = re.compile(r"这个|那个|它|他|她|怎么办|怎么弄|怎么用|什么意思")
_CHAT_WORDS = re.compile(
    r"^(你好|您好|hi|hello|谢谢|你是谁|你能做什么|你叫什么|在吗|早上好|晚上好|再见|拜拜|辛苦|谢谢啦|好的|OK|ok)"
)

# ---------------- 系统提示词（防编造规则的 Python 版） ----------------

_SYSTEM_PROMPT = """你是企业内网知识问答助手。请严格按以下规则回答：

1. 只依据【资料】内容回答，逐字核对；资料未涉及的，明确说「根据现有资料，暂无法回答该问题」，不得凭自身知识补充
2. 禁止编造：具体数字、百分比、日期年份、法规名称与条文、机构名称、人名——除非资料原文有
3. 回答使用规范中文术语，不要中英混写
4. 用 markdown 排版：小节标题用「**标题**」加粗，条目用「1. 2. 3.」编号
5. 引用资料事实时可注明来源文件名；不要输出「根据资料显示」之类的套话开头
6. 今天是{date}，涉及相对日期（明天/下周）时以今天为锚点计算
"""

_VERIFY_PROMPT = """你是答案事实核查员。对照【资料】逐句核查【答案】：
1. 删除答案中凭空出现的具体事实（数字、百分比、日期、法规名称、人名、机构、条款）——除非资料原文有
2. 【已知缺失术语】列出的术语在资料中不存在，含它们的句子必须删除或改写
3. 资料中确实存在的数字、术语、表述必须保留，不得误删
4. 保持结构与排版不变，不要补充任何资料之外的内容
直接输出核查后的答案，不要任何解释。

【已知缺失术语】
{missing}

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
            if term.lower() not in ctx and term not in ctx:
                missing.append(term)
    return missing[:12]


# ---------------- 1. 员工目录确定性直查 ----------------

def _try_employee_lookup(query: str) -> str | None:
    if _HEADCOUNT.search(query):
        dept = next((d for d in demo_data.DEPTS if d in query), None)
        if dept:
            count = sum(1 for e in demo_data.EMPLOYEES if e["dept"] == dept)
            return f"{dept}共有 {count} 人：\n" + demo_data.directory(dept)
        return f"根据员工目录，公司共有 {len(demo_data.EMPLOYEES)} 位员工：\n" + demo_data.directory()
    if not _EMP_INFO.search(query):
        return None
    hits = [demo_data.lookup(n) for n in demo_data.names() if n in query]
    hits = [h for h in hits if h]
    if not hits:
        return None
    return hits[0] + "。" if len(hits) == 1 else "\n".join(hits)


# ---------------- 2. OA 数据确定性直查 ----------------

def _fmt(v) -> str:
    """金额格式化：整数不带小数（"5890" 而非 "5890.0"）"""
    f = float(v or 0)
    return str(int(f)) if f == int(f) else str(f)


def _try_oa_data(query: str) -> tuple[str, dict | None] | None:
    """命中返回 (答案文本, 图表数据)；未命中/失败返回 None（回退常规路径）"""
    if _DATA_PROCESS_WORDS.search(query):
        return None
    m = _DATA_DAYS.search(query)
    days = min(90, max(1, int(m.group(1)) if m else 7))
    if _EXPENSE_WORDS.search(query):
        name = next((n for n in demo_data.names() if n in query), None)
        data = oa.expenses(days, name)
        if not data or not data.get("data", {}).get("items"):
            return None
        d = data["data"]
        total, count = _fmt(d.get("total", 0)), d.get("count", 0)
        who = name or "全员"
        head = f"{who}最近 {days} 天共报销 {total} 元，共 {count} 笔，明细如下：\n"
        lines = []
        dept = None
        same_dept = True
        for it in d["items"]:
            dn = it.get("deptName", "")
            if dept is None:
                dept = dn
            elif dept != dn:
                same_dept = False
            remark = it.get("remark") or ""
            line = (f"- {it.get('d')}：{it.get('expenseType')}"
                    + (f"（{remark}）" if remark else "")
                    + f"，金额 **{_fmt(it.get('amount'))} 元**")
            if name is None and it.get("empName"):
                line += f"（{it['empName']}）"
            lines.append(line)
        text = head + "\n".join(lines)
        if name and same_dept and dept:
            text += f"\n全部报销均归属{dept}。"
        chart = _expense_chart(d, name, days)
        return text, chart
    if _METRICS_WORDS.search(query):
        data = oa.metrics(days)
        if not data or not data.get("data", {}).get("dates"):
            return None
        d = data["data"]
        dates = d["dates"]
        s = d.get("summary", {})
        text = (f"近 {days} 天（{dates[0]} 至 {dates[-1]}）经营数据，"
                f"营收合计 {_fmt(s.get('revenue'))} 元，成本 {_fmt(s.get('cost'))} 元，"
                f"利润 {_fmt(s.get('profit'))} 元。每日明细见下表。")
        return text, _metrics_chart(d, days)
    return None


def _expense_chart(d: dict, name: str | None, days: int) -> dict:
    table = [["日期", "类型", "金额", "备注"]]
    for it in d["items"]:
        table.append([it.get("d"), it.get("expenseType"), str(it.get("amount")), it.get("remark", "")])
    return {
        "kind": "expenses",
        "chartType": "bar",
        "title": f"{(name or '全员')}近{d['count']}笔报销（元）",
        "table": table,
        "summary": f"合计 {d.get('total')} 元，共 {d.get('count')} 笔",
    }


def _metrics_chart(d: dict, days: int) -> dict:
    table = [["日期", "营收", "成本", "利润"]]
    for i, date in enumerate(d["dates"]):
        table.append([date, str(d["revenue"][i]), str(d["cost"][i]), str(d["profit"][i])])
    s = d.get("summary", {})
    return {
        "kind": "metrics",
        "chartType": "line",
        "title": f"近{len(d['dates'])}天经营数据（元）",
        "table": table,
        "summary": f"合计：营收 {s.get('revenue')}，成本 {s.get('cost')}，利润 {s.get('profit')}",
    }


# ---------------- 3. 答案校验（小模型核查 + 确定性守卫） ----------------

def _verify(answer: str, context: str) -> str:
    """小模型对照资料核查 → 确定性守卫收尾（Java 版 verify() 链的移植）"""
    if not config.VERIFY_ENABLED or not answer.strip() or not context.strip():
        return answer
    missing = _find_missing_terms(answer, context)
    missing_block = "、".join(missing) if missing else "（无）"
    try:
        checked = llm.chat(
            "", _VERIFY_PROMPT.format(missing=missing_block, context=context[:6000], answer=answer),
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
        pass   # 校验模型失败 → 原文下发（软依赖），确定性守卫仍会执行

    # 确定性守卫：鉴伪断言 → 格式修复
    answer = guards.strip_assertions(answer, context)
    return guards.fix_output(answer) or guards.NO_ANSWER


# ---------------- 4. 主入口 ----------------

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
    text = re.sub(r"\s+([。！？，；：、）】》])", r"", text)
    return text


def _today_line() -> str:
    now = datetime.now()
    week = "一二三四五六日"[now.weekday()]
    return f"今天是 {now.strftime('%Y-%m-%d')} 星期{week}"


def answer(query: str, conversation_id: str = "") -> dict:
    """非流式回答（Web /chat 端点；飞书桥复用）"""
    return _answer_impl(query)


def _answer_impl(query: str) -> dict:
    # 确定性守卫优先（顺序即 Java 版结论）
    emp = _try_employee_lookup(query)
    if emp:
        return {"answer": guards.fix_output(emp), "sources": [], "route": "KNOWLEDGE_QA", "chart": None}
    oa_data = _try_oa_data(query)
    if oa_data:
        text, chart = oa_data
        return {"answer": guards.fix_output(text), "sources": [], "route": "KNOWLEDGE_QA", "chart": chart}

    r = route(query)
    if r == "CHAT":
        return {"answer": llm.chat(_SYSTEM_PROMPT.format(date=_today_line()), query),
                "sources": [], "route": "CHAT", "chart": None}
    if r == "CLARIFY":
        return {"answer": "抱歉，我没太理解你的问题。可以再具体一点吗？比如补充对象、时间或背景。",
                "sources": [], "route": "CLARIFY", "chart": None}

    # 知识问答：预检索 → 兜底/生成 → 校验
    hits = retrieval.retrieve(query)
    if not hits:
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
    # 确定性守卫（员工目录/OA 数据）最优先：路由标记在守卫之后发出，
    # 守卫命中的答案路由固定是 KNOWLEDGE_QA（Java 版同款语义）
    emp = _try_employee_lookup(query)
    if emp:
        yield "route", "KNOWLEDGE_QA"
        yield "token", guards.fix_output(emp)
        yield "sources", []
        yield "done", "[DONE]"
        return
    oa_data = _try_oa_data(query)
    if oa_data:
        yield "route", "KNOWLEDGE_QA"
        text, chart = oa_data
        yield "token", guards.fix_output(text)
        if chart:
            yield "chart", chart
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

    # 知识问答：预检索（空手 → 确定性兜底）
    hits = retrieval.retrieve(query)
    if not hits:
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
