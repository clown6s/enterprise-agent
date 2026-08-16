# -*- coding: utf-8 -*-
"""
确定性守卫与输出修复（把 Java 版一晚上攒下的血泪教训一次搬过来）：

1. NO_ANSWER           检索空手时的兜底句式（不信任模型自觉）
2. fix_output()        输出格式修复：无序列表转编号、图谱回声块剥除、
                       编号与内容拆行合并、孤立编号行合并、结尾残句清理
3. strip_assertions()  鉴伪断言确定性删除（"并非××所言/伪名言"必须出自资料）
4. strip_missing_terms() 含"资料中不存在术语"的句子删除 + 延伸服务套话拦截
5. collapse_empty_headings() 硬删后空标题清理（"**标题**："下空无一物 → 移除）

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


def _number_list_lines(lines: list[str]) -> list[str]:
    """'- /*/• 开头 → 有序编号；全行加粗的行视为标题（不编号并打断计数）"""
    out: list[str] = []
    counter = 0
    for l in lines:
        s = l.strip()
        if s.startswith("- **") and s.endswith("**"):
            counter = 0
            out.append(s[2:].strip())
        elif s.startswith(("- ", "* ", "• ")):
            counter += 1
            out.append(f"{counter}. {s[2:]}")
        elif s:
            counter = 0
            out.append(l)
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

    # 4) 无序列表 → 有序编号（"- 内容" → "1. 内容"；全行加粗的行是标题，不编号并打断计数）
    out = _number_list_lines(lines)

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

    # 4.6) 恢复出的列表统一转有序编号（4.5 在 4 之后补的 "- " 前缀，这里再跑一次编号）
    out = _number_list_lines(out)

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

# 延伸服务/元话语句式（提示词禁但模型偶犯，确定性拦截——Java EXTENSION_OFFER 同款）
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
    与 Java 版一致：missing_terms 为空时不触发（此时不存在"必然无依据"的句子）。
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
# Java 正则里还含 \d+[.、]（编号行），但按其后注释"直到下一个标题或文末"的意图，
# "**标题**：\n1. 内容"是标题带内容的正常结构，不该删——照注释不照正则。
_STRUCT_START = re.compile(r"^[ \t]*(?:✅|📌|⚠️|\*\*)")


def collapse_empty_headings(text: str) -> str:
    """
    硬删后清理：内容被删光只剩标题时（"**基础药物治疗**：" 下空无一物），
    移除空标题行。判定：标题行（加粗行，可带 ✅/📌/⚠️ 前缀）之后直到
    下一个标题或文末全为空白。Java collapseEmptyHeadings 的行式移植。
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


def _norm(s: str) -> str:
    """比较用归一化：去空白 + 全角转半角（术语核对口径）"""
    return re.sub(r"\s+", "", s).lower()
