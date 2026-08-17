# -*- coding: utf-8 -*-
"""
确定性守卫与输出修复（一晚上攒下的血泪教训一次沉淀下来）：

1. NO_ANSWER           检索空手时的兜底句式（不信任模型自觉）
2. fix_output()        输出格式修复：列表统一 '-' 条目（编号/无序都归一）、图谱回声块
                       剥除、编号与内容拆行合并、孤立编号行合并、裸标题列表恢复、结尾残句清理
3. strip_assertions()  鉴伪断言确定性删除（"并非××所言/伪名言"必须出自资料）
4. strip_missing_terms() 含"资料中不存在术语"的句子删除 + 延伸服务套话拦截
5. collapse_empty_headings() 硬删后空标题清理（"**标题**："下空无一物 → 移除）
6. strip_no_answer_hedge() 兜底句式混入清理（有实质内容的答案里混进"暂无法回答"半吊子句 → 删该句）
7. find_missing_terms() 确定性术语核对（答案里资料没有的术语 → 缺失清单，校验链的输入）
8. clean_cjk_spacing() CJK 多余空格清理（闲聊链路流式逐字带空格的实况）
9. contains_injection()/strip_injection_sentences() 提示注入拦截（查询级拦截 + 上下文级清洗）
   INJECTION_BLOCKED  查询命中注入时的固定回话

设计原则：模型输出不可靠，能确定性算出来的事绝不再交给模型。
"""
import re

NO_ANSWER = "根据现有资料，暂无法回答该问题"

# ---------------- 1. 输出格式修复（StreamTokenFixer.fixFull 的 Python 浓缩版） ----------------

# 图谱三元组行："红旗渠 --[与...同属]--> 云台山"
_TRIPLE_LINE = re.compile(
    r"^[ \t]*\d*\.?\s*[^\-–—\n]{1,40}\s*[\-–—]{2,3}\s*\[[^\]\n]*\][\-–—]{0,2}\s*>\s*.+$"
)
# 图谱自然语言回声行："高血压（属于）：恶性高血压、高血压亚急症"
_GRAPH_LINE = re.compile(r"^[^\n：（）]{1,40}（[^\n）]{1,40}）：[^\n。！？；]{0,100}$")
# 孤立编号行："1." 独占一行
_LONE_NUMBER = re.compile(r"^(\d+)\.\s*$")


def _dash_list_lines(lines: list[str]) -> list[str]:
    """列表统一为 '- ' 条目（'1. 内容'/'1、内容'/'- 内容'/'* 内容' → '- 内容'）；
    加粗标题行（**标题**，可带冒号结尾）去编号/符号，保持纯标题。
    编号正则带负向 lookahead：'1.5倍'、'2024.01.01' 这类数字开头的行不动。
    （用户风格决策：条目不要数字编号——飞书卡片会把多级编号渲染成连续 1-10。）"""
    out: list[str] = []
    for l in lines:
        s = l.strip()
        rest = None
        m = re.match(r"^(?!\d+\.\d)(\d+)[.、]\s*(.*)$", s)
        if m:
            rest = m.group(2)
        elif s.startswith(("- ", "* ", "• ")):
            rest = s[2:]
        if rest is not None:
            if rest.startswith("**") and rest.rstrip("：:").endswith("**"):
                out.append(rest)               # 标题行：去编号/符号，保留加粗
            else:
                out.append("- " + rest)        # 条目：统一 '- '
        else:
            out.append(l)
    return out


def fix_output(text: str) -> str:
    if not text:
        return text
    lines = text.split("\n")

    # 1) 图谱三元组行 → 删
    lines = ["" if _TRIPLE_LINE.match(l.strip()) else l for l in lines]

    # 2) 图谱回声块：连续 ≥3 行 "实体（关系）：目标" → 整块删（单行可能是正常表述）
    kept: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and _GRAPH_LINE.match(lines[j].strip()):
            j += 1
        if j - i >= 3:
            i = j          # 成块必是回声，整块丢弃
        else:
            kept.append(lines[i])
            i += 1
    lines = kept

    # 3) 孤立编号行与下一行合并："1.\n内容" → "1. 内容"
    merged: list[str] = []
    for idx, l in enumerate(lines):
        m = _LONE_NUMBER.match(l.strip())
        if m and idx + 1 < len(lines) and lines[idx + 1].strip():
            merged.append(f"{m.group(1)}. {lines[idx + 1].strip()}")
            lines[idx + 1] = ""   # 下一行已被消费
        else:
            merged.append(l)
    lines = merged

    # 3.5) 裸标题列表恢复：校验层把 "**标题**：\n- 条目" 洗成 "标题：\n裸条目"
    #      （实况：高血压回答的"血压控制目标值：/用药原则："下全是无前缀裸行）。
    #      裸标题行（短行以"："结尾、后无内容）下连续 ≥2 行条目状裸行
    #      → 标题回填加粗、条目回填 "- " 前缀（交给步骤 4 归一）。
    #      条目判定：无内部句末标点、行尾至多一个句末标点、≤60 字——校验层还会
    #      给条目补句号的实况后放宽过（曾用"无句号"判条目，结果全被放过）。
    #      散文防线是"行内多句/长行"，不是"有句号"。
    #      已有编号的行排除（防双重编号，01a1cb9 同款根因）。
    _BARE_HEADING = re.compile(r"^(?:\*\*)?([^\n：|*]{1,24})(?:\*\*)?([：:])$")
    _FRAGMENT = re.compile(r"^[^。！？；：]{1,59}[。！？]?$")
    _NUMBERED = re.compile(r"^\d+[.、]\s*")
    restored_bare: list[str] = []
    i = 0
    while i < len(lines):
        head = lines[i].strip()
        m = _BARE_HEADING.match(head)
        if m and not head.startswith(("- ", "* ", "• ")):
            frags = []
            j = i + 1
            while (j < len(lines) and lines[j].strip()
                   and _FRAGMENT.match(lines[j].strip())
                   and not _NUMBERED.match(lines[j].strip())
                   and not lines[j].strip().startswith(("- ", "* ", "• ", "**"))):
                frags.append(j)
                j += 1
            if len(frags) >= 2:
                restored_bare.append(f"**{m.group(1)}**{m.group(2)}")
                for k in frags:
                    restored_bare.append("- " + lines[k].strip())
                i = j
                continue
        restored_bare.append(lines[i])
        i += 1
    lines = restored_bare

    # 4) 列表统一 '- ' 条目（编号/无序都归一；加粗标题去符号）
    out = _dash_list_lines(lines)

    # 4.5) 定义行列表恢复：校验层常把编号列表洗成裸行（"感知工具：让 Agent 访问信息"）
    #      连续 ≥3 行 → 整段恢复为 " - " 列表（含段内前几行，回填前缀）
    # 排除已有编号/列表符的行（"1. 感知工具：…"、"（1）感知工具：…" 不再重复编号
    # → 双重编号"1. 1."的根因，01a1cb9 修过数字点号，这里补（1）式）
    _DEF_LINE = re.compile(r"^(?![（(]?\d+[.)、）]\s*)(?![*-]\s)[^\n：|]{1,24}：[^\n]{1,200}$")
    restored = []
    run_start = -1
    for idx, l in enumerate(out):
        if _DEF_LINE.match(l.strip()):
            if run_start < 0:
                run_start = idx
        else:
            if run_start >= 0 and idx - run_start >= 3:
                for j in range(run_start, idx):
                    restored[j] = "- " + restored[j].strip()
            run_start = -1
        # 非定义行原样保留（早期版本对全部行 strip，会把嵌套列表/代码块的缩进抹平）
        restored.append(l)
    if run_start >= 0 and len(out) - run_start >= 3:
        for j in range(run_start, len(out)):
            restored[j] = "- " + restored[j].strip()
    out = restored

    # 4.6) 4.5 回填 "- " 前缀后统一过一遍 dash 归一（幂等；顺带归一 * / •）
    out = _dash_list_lines(out)

    # 5) CJK 与拉丁字符之间补空格（"通过Agent 状态栏" → "通过 Agent 状态栏"）
    out = [re.sub(r"(?<=[一-龥])(?=[A-Za-z])", " ", l) for l in out]
    out = [re.sub(r"(?<=[A-Za-z])(?=[一-龥])", " ", l) for l in out]

    # 6) 压缩连续空行 + 结尾残句清理（模型收尾被截断的典型形态）：
    #    文末悬空编号行（"3." 独占一行）、以冒号/逗号/顿号/分号结尾的半截句
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    lines = result.split("\n")
    while lines and re.fullmatch(r"\d+[.、]\s*", lines[-1].strip()):
        lines.pop()   # 悬空编号：后面内容被截断，编号本身无意义
    if lines and re.search(r"[：:，、；]$", lines[-1].strip()):
        # 半截句：在最后一个句末标点处截断；整行无句末标点则整行移除
        last = lines[-1].strip()
        cut = max(last.rfind("。"), last.rfind("！"), last.rfind("？"))
        lines[-1] = last[: cut + 1] if cut >= 0 else ""
    return "\n".join(lines).strip()


# ---------------- 2. 鉴伪断言确定性删除 ----------------

# "并非××所言/伪名言/杜撰" 类断言（模型自带知识，不算资料结论）
_ASSERTION_FAB = re.compile(
    r"(并非|不是|并非真的|不代表).{0,10}(所言|所说|讲的|写的|说的|名言|原话|语录)"
    r"|伪名言|假名言|系杜撰|是杜撰|编造的|伪造的|以讹传讹|张冠李戴"
)


def strip_assertions(text: str, context: str) -> str:
    """
    句内含鉴伪断言、且资料里找不到同一断言的句子 → 删。
    断言在资料中出现过则保留（确有出处的鉴伪是合法结论）。
    """
    if not text or not context:
        return text
    ctx = _norm(context)
    kept = []
    removed = 0
    for sentence in re.split(r"(?<=[。！？；\n])", text):
        bad = False
        for m in _ASSERTION_FAB.finditer(sentence):
            if _norm(m.group()) not in ctx:
                bad = True
                break
        if bad:
            removed += 1
        else:
            kept.append(sentence)
    if removed == 0:
        return text
    result = "".join(kept).strip()
    return result or NO_ANSWER


# ---------------- 3. 缺失术语句删除 ----------------

# 术语抽取（确定性缺失术语核对）：
# 校验模型是小模型，术语级漏检率高——哪些术语"资料里没有"是能确定算出来的事
_TERM_PATTERNS = [
    re.compile(r"[A-Za-z]{2,}(?:[-/][A-Za-z0-9]+){0,3}"),      # 英文缩写/术语（HRBP、BNP）
    re.compile(r"\d+(?:\.\d+)?\s*(?:%|mmHg|mg|ml|g|kg|次|天|年|月|日|周|元)"),  # 数值+单位
    re.compile(r"[《「]([^》」]{2,40})[》」]"),                   # 书名号里的法规/文件名
]


def find_missing_terms(answer: str, context: str) -> list[str]:
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


# 延伸服务/元话语句式（提示词禁但模型偶犯，确定性拦截）
_EXTENSION_OFFER = re.compile(
    r"欢迎随时|欢迎告诉|如需我|需要我帮|我可以为你|我可为您|我可为你|请确认是否"
    r"|随时提出|可立即为你|请告诉我您最关注|您希望优先了解"
    r"|以下(是|为).{0,12}(摘要|总结)|摘要如下|核心内容如下"
    r"|是否需要|如您能提供|建议您(通过|向|咨询|联系|查看|使用)"
    r"|知识图谱中未|资料中未包含"
    r"|欢迎继续|如需(个性化|更多|进一步|定制)"
)


def strip_missing_terms(text: str, missing_terms: list[str]) -> str:
    """
    含"资料中不存在术语"的句子直接移除（校验器软约束漏删时的硬兜底）；
    延伸服务套话（"欢迎随时告诉我…"）同批移除。全部删空返回兜底句式。
    missing_terms 为空时不触发（此时不存在"必然无依据"的句子）。
    """
    if not missing_terms or not text:
        return text
    terms = [_norm(t) for t in missing_terms]
    kept = []
    removed = 0
    for sentence in re.split(r"(?<=[。！？；\n])", text):
        norm = _norm(sentence)
        if any(t in norm for t in terms) or _EXTENSION_OFFER.search(sentence):
            removed += 1
        else:
            kept.append(sentence)
    if removed == 0:
        return text
    result = "".join(kept).strip()
    return result or NO_ANSWER


# ---------------- 4. 硬删后空标题清理 ----------------

_HEADING_LINE = re.compile(r"^[ \t]*(?:✅|📌|⚠️)?[ \t]*\*\*[^*\n]{1,40}\*\*[ \t]*[：:]?[ \t]*$")
# 判定"标题是空的"只看下一个非空行是不是另一个标题（✅/📌/⚠️/加粗）或文末。
# 注意别把 \d+[.、]（编号行）加进判定："**标题**：\n1. 内容"是标题带内容的
# 正常结构，不该删——空标题只存在于"直到下一个标题或文末全为空白"的情形。
_STRUCT_START = re.compile(r"^[ \t]*(?:✅|📌|⚠️|\*\*)")


def collapse_empty_headings(text: str) -> str:
    """
    硬删后清理：内容被删光只剩标题时（"**基础药物治疗**：" 下空无一物），
    移除空标题行。判定：标题行（加粗行，可带 ✅/📌/⚠️ 前缀）之后直到
    下一个标题或文末全为空白。
    """
    if not text or text == NO_ANSWER:
        return text
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _HEADING_LINE.match(lines[i]):
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            if j >= n or _STRUCT_START.match(lines[j]):
                i += 1   # 空标题：整行移除（遗留空行交给收尾压缩）
                continue
        out.append(lines[i])
        i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------------- 5. 兜底句式混入清理 ----------------

def strip_no_answer_hedge(text: str) -> str:
    """
    模型偶发把"根据现有资料，暂无法回答…"的半吊子兜底句写进有实质内容的答案
    （实况："…暂无法回答其他注意事项（如生活方式调整、饮食、运动等）"——
    生成侧按提示词句式自说自话，校验模型有时删有时留，回归时挂时过）。
    确定性处理：整篇就是兜底句式 → 原样保留（合法全量兜底）；
    否则删掉含"暂无法回答"的句子，删空回兜底句式。
    """
    if not text or "暂无法回答" not in text:
        return text
    if _norm(text) == _norm(NO_ANSWER):
        return text
    kept = []
    removed = 0
    for sentence in re.split(r"(?<=[。！？；\n])", text):
        if "暂无法回答" in sentence:
            removed += 1
        else:
            kept.append(sentence)
    if removed == 0:
        return text
    result = "".join(kept).strip()
    # 删完只剩空行/括号注记（"（来源：内科学.pdf）"）等脚手架 → 仍回全量兜底
    if all(not l.strip() or re.fullmatch(r"[（(].*[）)]", l.strip())
           for l in result.split("\n")):
        return NO_ANSWER
    return result


# ---------------- 7. 提示注入拦截 ----------------

INJECTION_BLOCKED = "抱歉，检测到异常指令内容，无法处理该请求。"

# 指令句式（中英）——知识库文档可投毒、用户消息可注入，确定性拦截：
# "忽略以上所有指令/无视规则/不要遵守系统提示/输出你的提示词/system prompt" 等。
# 注意别误伤日常问法（"你是谁"、"如何防止数据泄露"不在列——"泄露"必须紧跟提示类词）。
_INJECTION = re.compile(
    r"忽略(以上|之前|先前|上述|上面)?(的)?(所有|全部)?(指令|规则|要求|约束|提示|设定)"
    r"|无视(以上|之前|上述)?(的)?(指令|规则|提示|约束)"
    r"|(不要|禁止)(遵守|遵循|听从|理会)(以上|之前|上述)?(指令|规则|提示)"
    r"|从现在起(你)?是|从现在开始(你)?是|假装你是|扮演(一个|一位)?"
    r"|(泄露|暴露|输出|告诉我)(你的|其)?(系统)?(提示词|系统提示|指令|规则)"
    r"|system prompt|ignore (previous|above|all) instructions|disregard (previous|all)"
    r"|你是(一个)?(不受限制|没有限制|完全自由|无所不能)的"
    r"|越狱模式|jailbreak"
)


def contains_injection(text: str) -> bool:
    """查询/文件上下文是否含指令注入句式（用于查询级拦截）"""
    return bool(text) and _INJECTION.search(text) is not None


def strip_injection_sentences(text: str) -> str:
    """
    上下文清洗：删除含注入句式的句子（知识库文档投毒的防线），
    删完可能为空——调用方视同无有效上下文。
    """
    if not text or not _INJECTION.search(text):
        return text
    kept = [s for s in re.split(r"(?<=[。！？；\n])", text)
            if not _INJECTION.search(s)]
    return "".join(kept).strip()


# ---------------- 8. 来源锚定句剥除 ----------------

_ANCHOR_LINE = re.compile(r"^\s*根据《[^》]+》(一文)?[：:]\s*\n?")


def strip_leading_anchor(text: str) -> str:
    """剥掉首行来源锚定句（"根据《xxx》一文："）——锚定是展示层，
    不应随答案进入知识回流/历史存储（实况：采纳带锚定的答案后，
    新答案引用它再被守卫锚定 → 双重锚定）。"""
    if not text:
        return text
    return _ANCHOR_LINE.sub("", text, count=1)


# ---------------- 6. CJK 空格清理 ----------------

def clean_cjk_spacing(text: str) -> str:
    """CJK 字符间/标点前的多余空格清理（"你好 ！ 请问 有什么" → "你好！请问有什么"）。
    闲聊链路用：流式中文偶发逐字带空格 → 收齐后清理再整体下发。"""
    text = re.sub(r"(?<=[一-龥])\s+(?=[一-龥])", "", text)
    text = re.sub(r"\s+([。！？，；：、）】》])", r"\1", text)
    return text


def _norm(s: str) -> str:
    """比较用归一化：去空白 + 全角转半角（术语核对口径）"""
    return re.sub(r"\s+", "", s).lower()
