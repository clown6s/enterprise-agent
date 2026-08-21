# -*- coding: utf-8 -*-
"""
文档生成（一句话直达云文档）：
触发词 → 标题推断 → LLM 生成（防编造规则 + 类型标准结构）→ 飞书云文档直出
（org 可读 + 发起者编辑权）→ 回复文档链接。
多轮修改闭环（revise）+ 表单卡片兜底（generate_with_form）已接；
docx 文件兜底待做（路线图）。
"""
import json
import logging
import re
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

from .. import guards
from ..service import verify

from .. import config, llm
from ..time_context import now as business_now
from ..channels.feishu import channel as feishu_channel

_ABSOLUTE_SOURCE_CLAIMS = re.compile(
    r"^\s*(?:>\s*)?(?:本计划书|本方案|本报告)?(?:完全依据|完全基于).{0,80}(?:资料|文档|知识库).*$"
    r"|^\s*(?:>\s*)?(?:未引入|没有引入).{0,80}(?:外部知识|外部信息).*$"
    r"|^\s*(?:>\s*)?(?:资料明确要求|资料规定|根据第\s*\d+\s*页).*$",
    re.I | re.M,
)


def _clean_source_claims(text: str) -> str:
    """删除模型无法证明的绝对来源声明，避免正文冒充系统证据。"""
    lines = [line for line in text.splitlines()
             if not _ABSOLUTE_SOURCE_CLAIMS.match(line)]
    return "\n".join(lines).strip()


def _clean_pdf_artifacts(text: str) -> str:
    """清理 PDF 提取伪影：中文之间/中英文之间的多余空格。"""
    # 中文之间多余空格 → 去掉
    text = re.sub(r"(?<=[一-鿿])\s{2,}(?=[一-鿿])", "", text)
    # 中文与英文之间多余空格 → 保留一个
    text = re.sub(r"(?<=[一-鿿])\s{2,}(?=[A-Za-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])\s{2,}(?=[一-鿿])", " ", text)
    return text


def _failure_result(stage: str, message: str = "文档内容暂时生成失败，请稍后重试。",
                    error_type: str = "") -> dict:
    """已命中文档意图后的稳定失败结果，禁止回落普通知识问答。"""
    log.warning("docwizard_failed", extra={
        "event": "docwizard_failed", "fields": {"stage": stage, "error_type": error_type or "unknown"}})
    return {"answer": message, "route": "DOC_WIZARD", "sources": [],
            "chart": None, "status": "generation_failed", "error_stage": stage}

# ---------------- 文档质量门（替代 Q&A verify） ----------------

def _doc_quality_gate(content: str, title: str, evidence: str) -> tuple[str, str | None]:
    """文档专用质量门：结构、切题、完整性、防编造。返回 (内容, 失败原因)。

    与 Q&A verify.verify() 的区别：不删术语、不做相关性评分、不替换内容；
    只做确定性检查，不通过就明确失败，不静默改坏文档结构。
    """
    from .. import guards
    # 1. 结构：至少一个 ## 标题
    if "## " not in content:
        return content, "no_structure"
    # 2. 完整性：至少 2 个 ## 标题
    headings = re.findall(r"^## .+", content, re.M)
    if len(headings) < 2:
        return content, "too_few_sections"
    # 3. 切题：标题关键词出现在正文中（防 LLM 写完全无关内容）
    title_keywords = [w for w in re.findall(r"[一-鿿A-Za-z]{2,}", title) if len(w) >= 2]
    if title_keywords:
        content_lower = content.lower()
        matched = sum(1 for kw in title_keywords if kw.lower() in content_lower)
        if matched == 0:
            return content, "off_topic"
    # 4. 防编造：正文中的数字/术语在资料或用户要点中
    missing = guards.find_missing_terms(content, evidence)
    if missing:
        warn = "、".join(missing[:8])
        try:
            rewritten = llm.chat_chain(
                "", "上一条撰写的文档不变。但以下术语/数字在正文中出现，"
                    "却不在知识库资料和用户要点中：" + warn +
                    "。请删除或改写这些内容，保持 ## 标题结构，重新输出完整正文。").strip()
            if rewritten and "## " in rewritten:
                content = rewritten
        except Exception:
            pass
    # 5. 确定性格式收口（只重建格式，不改内容语义）
    content = _clean_source_claims(content)
    content = guards.fix_output(content)
    if not content or "## " not in content:
        return content, "format_lost"
    return content, None


# ---------------- 文档意图、触发与标题 ----------------

# 文档意图按"请求壳/动作 + 文档类型信号"识别，避免为单个问法不断补正则。
# 其中"帮我这个学习 agent 的计划书"没有显式"写/生成"，但"帮我"本身
# 已表达动作；知识咨询（"计划书是什么/怎么写"）则必须留在知识问答。
_REQUEST_WORDS = re.compile(
    r"^(?:麻烦\s+|帮我\s*|请帮我\s*|请(?=\s*(?:帮我|生成|写|做|起草|拟|制定|规划|安排))|"
    r"给我\s*|帮忙\s*)"
)
_ACTION_WORDS = re.compile(
    r"^(?:生成|写|起草|拟|制定|规划|安排)\s*(?:一个|个|一份|份|一篇|篇)?\s*"
    r"|^做\s*(?:一个|个|一份|份)\s*"
    r"|^来\s*(?:一个|个|一份|份)\s*"
)
_FILLER_WORDS = re.compile(r"^(?:这个|这份|这篇|一个|一份|一篇)\s*")
_KNOWLEDGE_QUESTION = re.compile(
    r"(?:是什么|怎么写|如何写|有没有|有吗|什么意思|介绍一下|是什么东西|如何|怎样|怎么|有哪些|请问)"
)
_NON_CREATE_PREFIX = re.compile(
    r"^(?:请问|如何|怎样|怎么|为什么|是否|有哪些|查询|查一下|看看|介绍|解释|修改|调整|删除|审核)"
)
_DOC_TYPE_PATTERNS = (
    ("计划书", re.compile(r"(?:的)?计划书$", re.I)),
    ("学习计划", re.compile(r"(?:的)?学习计划书?$", re.I)),
    ("计划", re.compile(r"(?:的)?计划$", re.I)),
    ("方案", re.compile(r"(?:的)?方案$", re.I)),
    ("报告", re.compile(r"(?:的)?报告$", re.I)),
    ("通知", re.compile(r"(?:的)?通知$", re.I)),
    ("周报", re.compile(r"(?:的)?周报$", re.I)),
    ("纪要", re.compile(r"(?:的)?纪要$", re.I)),
    ("攻略", re.compile(r"(?:的)?(?:旅游|旅行)?攻略$", re.I)),
    ("手册", re.compile(r"(?:的)?手册$", re.I)),
    ("指南", re.compile(r"(?:的)?指南$", re.I)),
)
_LEGACY_TRIGGER = re.compile(
    r"生成.{0,10}(文档|通知|报告|方案|周报|纪要|安排|手册|指南)"
    r"|写.{0,10}(文档|通知|报告|方案|周报|纪要|安排|手册|指南)"
    r"|起草|帮我写|拟一份"
    r"|关于.{2,40}(通知|报告|方案|周报|纪要|安排|文档|手册|指南)"
    r"|做.{0,12}(攻略|手册|指南|方案|计划)|(旅游|旅行)?攻略$"
)

_GENERATE_PROMPT = """你是企业文档撰写助手。请按【{doc_type}】的标准结构撰写《{title}》。
写作要求：
1. 只输出正文，不要"好的""以下是"等前缀、解释或来源声明
2. 每个章节用「## 」开头的标题行，章节下条目统一用「- 」列表，不用数字编号
3. 严禁编造事实：数字、百分比、日期、法规、人名、机构、价格、时间表只能来自
   【知识库资料】或用户要点；时效信息无法确定时写「以官方发布为准」
4. 这是你要写的文档，不是对资料的摘要或复述：
   - 资料中的概念、术语、章节名等可以作为依据引用，但不要整段照搬资料原文
   - 文档的结构、阶段划分、方法建议是你作为撰写助手根据主题提出的，属于建议实践
   - 学习/执行计划中的阶段安排、时间分配、学习方法是你的建议，结合用户要点合理规划
   - 不要写资料里没有的具体数字、人名、机构名
5. 禁止绝对来源声明：不要写"完全依据资料""未引入外部知识""资料明确要求"等
   无法由系统证明的表述；来源由系统卡片/元数据展示
6. 通用文档按说明文体撰写，不写成"须/不得/应"式规章制度条文
7. 使用规范中文术语，不要中英混写
8. 今天是 {today}，涉及年份与星期必须与实际日历一致

【知识库资料】
{context}

用户要点：
{points}
"""

_DOC_STRUCTURES = {
    "计划书": "一、目标与范围\n二、现状与基础\n三、阶段安排\n四、学习/执行方法\n五、成果检验与调整",
    "计划": "一、目标与范围\n二、现状与基础\n三、阶段安排\n四、学习/执行方法\n五、成果检验与调整",
    "攻略": "一、景点概况\n二、必看景点（各景点看点说明）\n三、建议游览路线\n"
            "四、游览贴士（时间安排/穿着/注意事项）\n五、周边延伸\n"
            "（门票价格、开放时间、交通班次等时效信息写「以景区官方发布为准」）",
}


def _strip_request_shell(query: str) -> tuple[str, bool, bool]:
    text = re.sub(r"\s+", " ", (query or "").strip()).strip()
    # 只剥一轮请求壳/创建动作/结构填充词；填充词本身不代表用户要生成文档。
    text, request_n = _REQUEST_WORDS.subn("", text, count=1)
    text, action_n = _ACTION_WORDS.subn("", text, count=1)
    text, _ = _FILLER_WORDS.subn("", text, count=1)
    return text.strip(" ，,。；;"), bool(request_n), bool(action_n)


_POINT_SEPARATORS = re.compile(r"^[，,、；;：:。.!！？?\-/—–]\s*")


def _split_header_points(body: str) -> tuple[str, str]:
    """把"文档标题后缀 + 明确要点"拆开；不猜无分隔的中文尾巴。"""
    clean = body.strip()
    # 最长后缀优先，且只在后缀之后有明确分隔符时切点。
    patterns = sorted(_DOC_TYPE_PATTERNS, key=lambda item: len(item[0]), reverse=True)
    for _, pattern in patterns:
        match = re.search(pattern.pattern.replace("$", ""), clean)
        if not match:
            continue
        tail = clean[match.end():]
        if not tail or not _POINT_SEPARATORS.match(tail):
            continue
        points = _POINT_SEPARATORS.sub("", tail, count=1).strip()
        if points:
            return clean[:match.end()].strip(), points
    return clean, ""


def _doc_type_and_subject(body: str) -> tuple[str | None, str]:
    clean = re.sub(r"[。！？!?]+$", "", body.strip())
    # 具体别名优先于通用后缀（学习计划先于计划书、计划）。
    patterns = sorted(_DOC_TYPE_PATTERNS, key=lambda item: len(item[0]), reverse=True)
    for doc_type, pattern in patterns:
        match = pattern.search(clean)
        if not match:
            continue
        subject = clean[:match.start()].strip(" 的")
        # "学习计划/学习方案"本身就是完整标题，不重复拼接"学习"。
        if not subject and clean:
            return doc_type, clean
        suffix = "学习计划" if doc_type == "学习计划" and clean.endswith("学习计划") else doc_type
        if doc_type == "攻略":
            matched_suffix = clean[match.start():match.end()].lstrip("的") or "攻略"
            suffix = matched_suffix
        return doc_type, f"{subject}{suffix}" if subject else suffix
    return None, clean


def _normalize_title(title: str) -> str | None:
    title = re.sub(r"[\x00-\x1f\x7f]", " ", (title or ""))
    title = re.sub(r"\s+", " ", title.strip(" ，,。；;《》「」"))
    title = re.sub(r"(?i)agent", "Agent", title)
    # 仅补 CJK/Latin 边界空格，不改写未知英文品牌。
    title = re.sub(r"(?<=[一-鿿])(?=[A-Za-z])", " ", title)
    title = re.sub(r"(?<=[A-Za-z])(?=[一-鿿])", " ", title)
    return title if 2 <= len(title) <= 80 and "\n" not in title else None


def parse_request(query: str) -> dict:
    """解析一类文档请求，返回 matched/title/points/doc_type。

    返回 matched=True 且 title=None 时由飞书表单补标题；非文档咨询返回 matched=False。
    """
    raw = re.sub(r"\s+", " ", (query or "").strip()).strip()
    body, had_request, had_action = _strip_request_shell(raw)
    header, tail_points = _split_header_points(body)
    doc_type, title = _doc_type_and_subject(header)
    # legacy 只保留已知文档类型的旧写法；裸"帮我写/起草"不能触发文档表单。
    legacy = bool(_LEGACY_TRIGGER.search(raw) and doc_type)
    if (_NON_CREATE_PREFIX.search(raw)
            or re.search(r"帮我\s*(?:看看|查询|查一下|介绍|解释|如何|怎样|怎么)", raw)
            or re.search(r"^(?:请问|如何|怎样|怎么|为什么|是否)", raw)):
        return {"matched": False, "title": None, "points": "", "doc_type": None}
    # 创建动作之后的要点可以出现"如何安排"等词；没有动作/请求壳时，疑问句一律是知识问法。
    if _KNOWLEDGE_QUESTION.search(raw) and not (had_request or had_action):
        return {"matched": False, "title": None, "points": "", "doc_type": None}
    if not doc_type and not legacy:
        return {"matched": False, "title": None, "points": "", "doc_type": None}
    if not (had_request or had_action) and not legacy:
        return {"matched": False, "title": None, "points": "", "doc_type": None}
    # "帮我写代码/邮件"没有文档后缀，不应进入文档表单；只有明确文档类型
    # 的创建请求才命中。旧版宽触发词由 legacy 分支保留兼容。
    if had_request and not doc_type and body:
        return {"matched": False, "title": None, "points": "", "doc_type": None}
    if not title and legacy:
        title = _legacy_guess_title(raw)
    title = _normalize_title(title)
    if had_request and doc_type and title == doc_type:
        # "帮我写一份方案/通知"确认是文档创建，但主题缺失，交给表单补标题。
        title = None
    if title and not doc_type:
        doc_type = next((t for t, p in _DOC_TYPE_PATTERNS if p.search(title)), None)
    # 无尾部要点时由 _generate 以 title 作为最小上下文；避免 points 重复标题。
    points = tail_points
    return {"matched": True, "title": title, "points": points,
            "doc_type": doc_type or "通用文档", "source_query": f"{title or header} {points}".strip()}


def _legacy_guess_title(query: str) -> str | None:
    m = re.search(r"(关于[^，。；\n]{2,40})", query)
    if m:
        return m.group(1)
    m = re.search(r"[《「]([^》」]{2,40})[》」]", query)
    if m:
        return m.group(1)
    stripped = query.strip()
    if len(stripped) <= 30 and not re.search(r"[，。；、\n]", stripped):
        title = re.sub(
            r"^(给我|帮我|请|麻烦)?(生成|起草|拟一份|写一个|写一份|写份|写个|写|做一个|做个|做一份|做份|做)",
            "", stripped).strip()
        title = re.sub(r"^(去|来)", "", title).strip()
        if len(title) >= 4:
            if not title.startswith("关于") and re.search(r"(通知|报告|方案|周报|纪要)$", title):
                return "关于" + title
            if title.endswith("攻略"):
                title = re.sub(r"的旅游攻略$", "旅游攻略", title).replace("的攻略", "攻略")
            return title
    return None


def guess_title(query: str) -> str | None:
    request = parse_request(query)
    return request.get("title") if request.get("matched") else None


def matches(query: str) -> bool:
    return bool(parse_request(query).get("matched"))


# 按会话的进行中文档（chat_id → {title, doc_id, content}），群聊修改指令回写同一份
_active_docs: dict = {}
_REVISE_WORDS = re.compile(r"修改|改一下|精简|调整|更新|补充|删除|删掉|压缩|润色|第二段|第三段|第一段")


def revise(chat_id: str, query: str) -> str | None:
    """多轮修改闭环：会话有进行中文档且问法是修改指令 → LLM 改写 → 回写云文档。
    返回更新后的云文档链接；未命中/失败返回 None。"""
    doc = _active_docs.get(chat_id)
    if not doc or not _REVISE_WORDS.search(query):
        return None
    try:
        new_content = llm.chat_chain(
            "", "你是文档编辑。按用户指令修改原文，直接输出修改后的完整正文，不要解释。\n\n"
                "【原文】\n" + doc["content"] + "\n\n【修改指令】\n" + query,
        ).strip()
    except Exception:
        return None
    if not new_content or new_content == doc["content"]:
        return None
    doc["content"] = new_content
    if doc["doc_id"] and rewrite_cloud_doc(doc["doc_id"], new_content):
        return f"https://feishu.cn/docx/{doc['doc_id']}"
    return None


def try_generate(query: str, editor_open_id: str = "", chat_id: str = "", user: str = "") -> dict | None:
    """一句话直达：文档意图解析、标题规范化和用户要点只走一套规则。"""
    request = parse_request(query)
    if not request.get("matched"):
        return None
    if not request.get("title"):
        return {"needs_form": True}
    return _generate(request, editor_open_id, chat_id, user=user)


def generate_with_form(chat_id: str, title: str, points: str, editor_open_id: str = "", user: str = "") -> dict | None:
    """表单卡片兜底：标题同样经过安全清理后复用统一生成流程。"""
    title = _normalize_title(title)
    if not title:
        return None
    doc_type, _ = _doc_type_and_subject(title)
    request = {"matched": True, "title": title, "points": points or title,
               "doc_type": doc_type or "通用文档"}
    return _generate(request, editor_open_id, chat_id, user=user)


def _generate(request: dict, editor_open_id: str, chat_id: str, user: str = "") -> dict | None:
    title = _normalize_title(request.get("title"))
    if not title:
        return _failure_result("title")
    doc_type = request.get("doc_type") or "通用文档"
    # 学习计划/学习方案归入计划书结构，计划书后缀保持用户可见标题口径。
    structure_kind = "计划书" if doc_type in ("计划书", "学习计划") else doc_type
    structure = _DOC_STRUCTURES.get(structure_kind, "通用文档")
    points = request.get("points") or title
    # 知识库接地：生成通知/方案类文档时检索公司资料做依据——
    # "关于五一的放假通知"应引用库里的放假安排原文，而不是让模型想当然编日期
    from .. import guards
    from . import retrieval
    try:
        hits = retrieval.retrieve(f"{title} {points}", user=user)
    except Exception as e:
        return _failure_result("retrieval", error_type=type(e).__name__)
    context = (guards.strip_injection_sentences(_clean_pdf_artifacts(retrieval.build_context(hits)))
               if hits else "（知识库暂无相关资料）")
    try:
        content = llm.chat_chain(
            "", _GENERATE_PROMPT.format(
                doc_type=structure, title=title, points=points,
                today=business_now().strftime("%Y-%m-%d"), context=context),
        ).strip()
        # 结构校验：如果 LLM 没输出 ## 标题，重试一次（LLM 不稳定时偶尔输出裸文本）
        if "## " not in content:
            content = llm.chat_chain(
                "", _GENERATE_PROMPT.format(
                    doc_type=structure, title=title, points=points,
                    today=business_now().strftime("%Y-%m-%d"), context=context),
            ).strip()
        if "## " not in content:
            return _failure_result("generate_no_structure",
                                  "文档结构生成失败，请换一种问法重试。")
    except Exception as e:
        return _failure_result("generate", error_type=type(e).__name__)
    if not content:
        return _failure_result("generate_empty")
    # 保存结构完整的 LLM 原文——后续 verify/grounding 可能把结构改坏，此时回退。
    llm_structured_content = content
    # 生成侧确定性接地复核：资料中没有的术语/数字先要求模型删除或改写。
    # 最终还会经过 verify + guards，三层防线不因文档生成另走路径而缺失。
    evidence = (context + "\n" + points).strip()
    if hits:
        missing = guards.find_missing_terms(content, evidence)
        if missing:
            warn = "、".join(missing[:8])
            try:
                content = llm.chat_chain(
                    "", "上一条撰写要求不变。但以下术语/数字在正文里出现，"
                        "却不在知识库资料和用户要点中：" + warn +
                        "。请删除或改写这些内容，重新输出完整正文。").strip()
            except Exception:
                pass
    # 统一事实校验 + 格式重建：文档生成不能绕过问答校验/守卫链。
    # 依据同时包含资料和用户要点；资料为空时，用户要点仍是合法依据。
    pre_verify_content = content
    try:
        checked = verify.verify(content, evidence, query=title)
        if checked and checked != guards.NO_ANSWER:
            content = checked
    except Exception as e:
        return _failure_result("verify", error_type=type(e).__name__)
    # 确定性收口：删不可证明的绝对来源声明，再按语义阶段重建格式。
    try:
        content = _clean_source_claims(content)
        content = guards.fix_output(content)
    except Exception as e:
        return _failure_result("guards", error_type=type(e).__name__)
    # 如果后处理丢掉了 ## 标题结构，回退到 LLM 原文（verify/grounding 偶发把好结构改坏）。
    if "## " not in content and "## " in llm_structured_content:
        content = llm_structured_content
    if not content:
        return _failure_result("guards_empty")
    # 最终结构校验：经过 verify/guards 后仍需有 ## 标题——否则不发布垃圾。
    if "## " not in content:
        return _failure_result("final_no_structure",
                              "文档结构在校验后丢失，请换一种问法重试。")
    url = publish_cloud_doc(title, content, editor_open_id)
    doc_id = url.rsplit("/", 1)[-1] if url else ""
    sources = sorted({h.get("source") for h in hits if h.get("source")})
    if url:
        if chat_id:
            _active_docs[chat_id] = {"title": title, "doc_id": doc_id, "content": content}
        editable = bool(editor_open_id)
        label = "在线可编辑" if editable else "已生成，可查看（未绑定编辑身份）"
        return {"answer": f"已生成《{title}》云文档（{label}）：\n{url}\n"
                          "后续可直接在群聊回复修改要求（如「第二段精简」），我会同步更新。",
                "route": "DOC_WIZARD", "sources": sources, "status": "published",
                "doc_url": url, "editable": editable}
    # 云文档不可用 → 正文直接回复（降级），不能把已生成正文误报成生成失败。
    return {"answer": f"云文档暂时不可用，先为你提供正文：\n\n《{title}》\n\n{content[:4000]}",
            "route": "DOC_WIZARD", "sources": sources, "status": "content_fallback",
            "error_stage": "publish", "degrade_reason": "publish_unavailable",
            "editable": False}


def rewrite_cloud_doc(doc_id: str, content: str) -> bool:
    """云文档回写：清空全部 children → 重写（多轮修改闭环用）"""
    token = feishu_channel._app_access_token()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # 1. 收集现有 block id（分页）
        page_token = None
        ids = []
        while True:
            url = (f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks"
                   f"?page_size=500&document_revision_id=-1"
                   + (f"&page_token={page_token}" if page_token else ""))
            resp = httpx.get(url, headers=headers, timeout=15).json()
            for b in (resp.get("data", {}).get("items") or []):
                if b.get("parent_id") == doc_id:
                    ids.append(b["block_id"])
            if not resp.get("data", {}).get("has_more"):
                break
            page_token = resp.get("data", {}).get("page_token")
        # 2. 删除全部子块（逐个 DELETE；飞书无批量删接口）
        for bid in ids:
            httpx.delete(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{bid}"
                f"?document_revision_id=-1",
                headers=headers, timeout=15)
        # 3. 重写（反向 index-0 插入保持顺序）
        blocks = _md_to_blocks(content)
        for i in range((len(blocks) - 1) // 50, -1, -1):
            httpx.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"
                f"/blocks/{doc_id}/children?document_revision_id=-1",
                headers=headers, json={"children": blocks[i * 50: (i + 1) * 50],
                                       "index": 0}, timeout=30)
        return True
    except (httpx.HTTPError, KeyError, ValueError):
        # ValueError：飞书返回非 JSON（网关 500 页）时 .json() 抛——曾炸穿到 500
        return False


# ---------------- 云文档发布 ----------------

def publish_cloud_doc(title: str, content: str, editor_open_id: str = "") -> str | None:
    """创建 docx 云文档 → 写入正文（分块）→ org 可读 + 编辑者；返回 url，失败 None"""
    try:
        token = feishu_channel._app_access_token()
    except Exception as e:
        log.exception("docwizard_publish_token_failed", extra={
            "event": "docwizard_failed",
            "fields": {"stage": "publish_token", "error_type": type(e).__name__}})
        return None
    if not token:
        log.warning("docwizard_publish_token_missing", extra={
            "event": "docwizard_failed", "fields": {"stage": "publish_token"}})
        return None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        # 1. 创建空文档
        resp = httpx.post("https://open.feishu.cn/open-apis/docx/v1/documents",
                          headers=headers, json={"title": title}, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("code", 0) != 0:
            log.warning("docwizard_publish_create_failed", extra={
                "event": "docwizard_failed",
                "fields": {"stage": "publish_create", "code": payload.get("code") if isinstance(payload, dict) else "non-object"}})
            return None
        doc_id = (payload.get("data", {}).get("document", {}).get("document_id")
                  or "").strip()
        if not doc_id:
            return None
        # 2. 正文写入：markdown → blocks
        blocks = _md_to_blocks(content)
        content_written = False
        for i in range((len(blocks) - 1) // 50, -1, -1):
            batch = blocks[i * 50: (i + 1) * 50]
            block_resp = httpx.post(
                f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}"
                f"/blocks/{doc_id}/children?document_revision_id=-1",
                headers=headers, json={"children": batch, "index": 0}, timeout=30)
            if block_resp.status_code == 200:
                content_written = True
        # 3. 权限：尽量设可编辑，失败不阻断发布（正文已写入就返回 URL）。
        editable = False
        try:
            public_resp = httpx.patch(
                f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/public"
                f"?type=docx",
                headers=headers, json={"link_share_entity": "tenant_editable"}, timeout=15)
            if public_resp.status_code == 200:
                editable = True
        except httpx.HTTPError:
            pass
        if editor_open_id:
            try:
                member_resp = httpx.post(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_id}/members"
                    f"?type=docx",
                    headers=headers,
                    json={"member_type": "openid", "member_id": editor_open_id,
                          "perm": "full_access"}, timeout=15)
                if member_resp.status_code == 200:
                    editable = True
            except httpx.HTTPError:
                pass
        if not content_written:
            log.warning("docwizard_publish_blocks_failed", extra={
                "event": "docwizard_failed", "fields": {"stage": "publish_blocks", "doc_id": doc_id}})
            return None
        log.info("docwizard_publish_done", extra={
            "event": "docwizard_publish_done", "fields": {"doc_id": doc_id, "editable": editable}})
        return f"https://feishu.cn/docx/{doc_id}"
        return f"https://feishu.cn/docx/{doc_id}"
    except (httpx.HTTPError, KeyError, ValueError):
        # ValueError：飞书返回非 JSON（网关 500 页）时 .json() 抛——曾炸穿到 500
        return None


def _md_to_blocks(md: str) -> list[dict]:
    """markdown → docx blocks（## → heading1，- → bullet，N. → ordered，其余 text）"""
    blocks = []
    for line in md.split("\n"):
        s = line.strip()
        if not s:
            continue
        # 内联加粗剥除（v1 纯文本；样式版路线图）
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        if s.startswith("### "):
            blocks.append(_block(4, s[4:]))
        elif s.startswith("## "):
            blocks.append(_block(3, s[3:]))
        elif re.match(r"^\d+[.、] ", s):
            blocks.append(_block(13, re.sub(r"^\d+[.、] ", "", s)))
        elif s.startswith(("- ", "* ")):
            blocks.append(_block(12, s[2:]))
        else:
            blocks.append(_block(2, s))
    return blocks


def _block(block_type: int, text: str) -> dict:
    key = {2: "text", 3: "heading1", 4: "heading2", 12: "bullet", 13: "ordered"}[block_type]
    return {
        "block_type": block_type,
        key: {"elements": [{"text_run": {"content": text,
                                         "text_element_style": {}}}]},
    }
