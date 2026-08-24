# -*- coding: utf-8 -*-
"""Extensible document profiles and preview chunkers; production remains unchanged."""
from dataclasses import dataclass
import re

_TOC = re.compile(r"^\s*(.+?)(?:\.{2,}|…{2,}|\s{3,})(\d+)\s*$")
_ARTICLE_SUFFIX = r"(?:之\s*[0-9零一二三四五六七八九十百千万]+)?"
_ARTICLE = re.compile(r"^\s*(第\s*[0-9零一二三四五六七八九十百千万]+\s*条\s*" + _ARTICLE_SUFFIX + r")(?![一-龥A-Za-z0-9])")
_ARTICLE_ANY = re.compile(r"第\s*[0-9零一二三四五六七八九十百千万]+\s*条\s*" + _ARTICLE_SUFFIX + r"(?![一-龥A-Za-z0-9])")
_LEVEL = re.compile(r"^\s*(第\s*[0-9零一二三四五六七八九十百千万]+\s*(?:编|篇|章|节))(?![一-龥A-Za-z0-9])")
_Q = re.compile(r"^\s*(\d+)[、.．]\s*(.+)$")
_A = re.compile(r"^\s*(?:答|答案|解析)\s*[:：]\s*(.*)$")
_SECTION = re.compile(r"^\s*(?:第?[一二三四五六七八九十百千万]+[、.．]|##\s*).+")

@dataclass(frozen=True)
class Classification:
    kind: str
    confidence: float
    evidence: tuple[str, ...] = ()
    fallback: str = "generic"


def qa_confidence(text):
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    q = sum(bool(_Q.match(x)) and ("？" in x or "?" in x or bool(re.search(r"什么|哪里|如何|为什么|哪些|多少|谁|何时|吗", x))) for x in lines)
    a = sum(bool(_A.match(x)) for x in lines)
    return {"kind": "qa" if q >= 5 and a >= 3 else "generic", "confidence": min(1.0, q / max(1, len(lines)) * 2 + a / max(1, len(lines)) * 2), "question_count": q, "answer_count": a}


def _drop_toc_units(text):
    out = []; toc = False
    for line in (text or '').splitlines():
        s = line.strip()
        if s in {'目录', '目 录'}:
            toc = True; continue
        if toc and _ARTICLE.match(s):
            toc = False
        if not toc: out.append(line)
    return '\n'.join(out)


def is_legal_confident(text):
    lines = _drop_toc_units(text).splitlines()
    articles = sum(bool(_ARTICLE.match(x.strip())) for x in lines)
    levels = sum(bool(_LEVEL.match(x.strip())) for x in lines)
    return articles >= 5 and (levels >= 1 or articles >= 10)


def segment_text(text, max_chars=600, *, overlap=0):
    """统一的段落/句子感知切分器。

    换行只是 PDF 的视觉换行，不能直接当作句子边界；先按显式空行、标题
    和页标记组织段落，再按完整句子装箱。只有单个句子本身超过上限才硬切，
    且默认不复制字符 overlap，避免相邻块出现“半句重复”的假丢文。
    """
    marks = set('。！？!?；;')
    paragraphs = []
    current = []
    page = 1
    def join_visual_lines(lines):
        out = ''
        for part in lines:
            part = part.strip()
            if not out:
                out = part
            elif re.search(r'[A-Za-z0-9]$', out) and re.match(r'^[A-Za-z0-9]', part):
                out += ' ' + part
            else:
                out += part
        return out
    for raw in (text or '').splitlines():
        line = raw.strip()
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', line)
        if marker:
            if current: paragraphs.append((' '.join(current), page)); current = []
            page = int(marker.group(1)); continue
        if not line:
            if current: paragraphs.append((' '.join(current), page)); current = []
            continue
        if line.startswith('## '):
            if current: paragraphs.append((' '.join(current), page)); current = []
            title = line[3:].strip()
            if title in {'目录', '目 录', 'Contents'} and page >= 9:
                continue
            current.append(line); continue
        # pdfplumber 可能把页眉和正文合成同一行：目录 + 正文。
        # 只剥离行首页眉，不删除正文中间正常出现的“目录”。
        if page >= 9:
            line = re.sub(r'^(?:目录|目\s*录|Contents|Ŀ¼)\s+', '', line).strip()
            if line in {'目录', '目 录', 'Contents', 'Ŀ¼'}:
                continue
            if not line:
                continue
        # PDF 视觉行不代表段落；仅在解析器明确留下空行时断段。
        current.append(line)
    if current: paragraphs.append((' '.join(current), page))

    def sentence_parts(value):
        out, start = [], 0
        for i, char in enumerate(value):
            if char not in marks: continue
            # 小数、版本号和英文缩写中的点不在 marks 中；中文句末不需猜测。
            if char == '.' and ((i and value[i - 1].isdigit()) or
                                (i + 1 < len(value) and value[i + 1].isdigit())):
                continue
            out.append(value[start:i + 1].strip()); start = i + 1
        tail = value[start:].strip()
        if tail: out.append(tail)
        return [x for x in out if x]

    chunks = []
    for value, page_no in paragraphs:
        units = [value] if value.startswith('## ') and len(value) <= max_chars else sentence_parts(value)
        buf = ''
        for unit in units:
            if len(unit) > max_chars:
                if buf: chunks.append({'text': buf, 'page': page_no, 'section_path': []}); buf = ''
                rest = unit
                while len(rest) > max_chars:
                    # 句子可能跨越 max_chars；向后找本句真正结束位置，不能在
                    # “我和团队/打造了 Pine”这种视觉换行处硬切。
                    ends = [rest.find(mark, max_chars) for mark in marks]
                    ends = [n for n in ends if n >= 0]
                    cut = min(ends) + 1 if ends else max_chars
                    chunks.append({'text': rest[:cut].strip(), 'page': page_no,
                                   'section_path': [], 'continuation': bool(ends)})
                    rest = rest[cut:].strip()
                buf = rest
            elif not buf:
                buf = unit
            elif len(buf) + 1 + len(unit) <= max_chars:
                buf += '\n' + unit
            else:
                # 完整句子是装箱原子：放不下就整句移到下一块，不能留下
                # “我和团队”在上一块、“打造了 Pine”在下一块的半句边界。
                chunks.append({'text': buf, 'page': page_no, 'section_path': []})
                buf = unit
        if buf:
            chunks.append({'text': buf, 'page': page_no, 'section_path': []})
            buf = ''
    return chunks


def _generic(text, max_chars=600):
    return segment_text(text, max_chars=max_chars)


def _split_inline_articles(line):
    matches = list(_ARTICLE_ANY.finditer(line)); starts = []
    for match in matches:
        before = line[:match.start()].rstrip()
        after = line[match.end():]
        separated = bool(line[:match.start()] and line[:match.start()][-1].isspace())
        if before and before[-1] not in '。！？；' and not separated:
            continue
        # 条文标题常被 DOCX 抽取为“第一百零一条　询问…”或“第一百零一条询问…”，
        # 不能要求条号后必须有空格/标点；前置引用词过滤才是避免误切的关键。
        # 条文标题常被 DOCX 抽取为“第一百零一条　询问…”或“第一百零一条询问…”，
        # 不能要求条号后必须有空格/标点；前置引用词过滤才是避免误切的关键。
        if re.search(r'(?:本法|前款|前项|依照|根据|按照|上述|下列)\s*$', before):
            continue
        # 条号后接顿号/逗号通常是正文中的引用列表（如“第二百零八条、第二百零九条”），
        # 不是新条文标题；PDF 换行时也不能因此切断当前条文。
        if after and after[0] in '、，,':
            continue
        if after and not after[0].isspace() and after[0] not in '　：:。！？；':
            continue
        starts.append(match.start())
    return [line[a:b].strip() for a, b in zip(starts, starts[1:] + [len(line)])] if starts else []


def _split_units(units, max_chars):
    out = []
    for unit in units:
        rest = unit['text']
        if len(rest) <= max_chars:
            out.append(unit); continue
        first = True; index = 0
        while rest:
            if len(rest) <= max_chars:
                part = {**unit, 'text': rest}
            else:
                cut = max(rest.rfind('。', 0, max_chars), rest.rfind('\n', 0, max_chars))
                cut = cut + 1 if cut > max_chars // 2 else max_chars
                part = {**unit, 'text': rest[:cut].strip()}
                rest = rest[cut:].strip()
            if not first and 'article' in unit:
                part.update(continuation=True, continuation_of=unit['article'], continuation_index=index)
            out.append(part); first = False; index += 1
            if len(part['text']) == len(rest) and rest:
                break
    return out


def chunk_legal(text, max_chars=600):
    units = []; stack = []; page = 1; current = None; in_body = False
    ranks = {'编': 1, '篇': 1, '章': 2, '节': 3}
    def flush():
        nonlocal current
        if current and current['text'].strip(): units.append(current)
        current = None
    def start(segment):
        nonlocal current, in_body
        flush(); match = _ARTICLE.match(segment)
        if match:
            current = {'text': segment.strip(), 'page': page, 'article': re.sub(r'\s+', '', match.group(1)), 'section_path': [x[1] for x in stack], 'kind': 'legal', '_page_parts': [[page, segment.strip()]]}
            in_body = True
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line: continue
        heading = re.sub(r'^##\s*', '', line).strip()
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', line)
        if marker:
            # 条文跨页时不能 flush：否则下一页正文会脱离原条文并被丢弃。
            page = int(marker.group(1)); continue
        if heading in {'目录', '目 录'}: flush(); in_body = False; stack = []; continue
        level = _LEVEL.match(heading)
        if level:
            flush(); label = level.group(1).strip(); rank = ranks.get(label[-1], 2)
            stack = [x for x in stack if x[0] < rank] + [(rank, label)]
            remainder = heading[level.end():].strip()
            if remainder:
                parts = _split_inline_articles(remainder)
                if parts:
                    for part in parts: start(part)
            continue
        parts = _split_inline_articles(line)
        if parts:
            for part in parts: start(part)
            continue
        if in_body and current:
            current['text'] += ' ' + line
            current.setdefault('_page_parts', []).append([page, line])
        elif not in_body:
            # 不能静默吞掉条文前导、附录或异常抽取文本；目录/页眉等结构行已在前面处理，
            # 其余内容保留为可追踪兜底块，调用方可按 kind 复核而非误当正文条文。
            units.append({'text': line, 'page': page, 'section_path': [x[1] for x in stack], 'kind': 'legal_unclassified', 'quality_flag': 'unclassified_legal_text'})
    flush()
    for unit in units:
        parts = unit.pop('_page_parts', None)
        if parts:
            pages = {p for p, _ in parts}
            unit['page_end'] = max(pages)
    return _split_units(units, max_chars)


def chunk_qa(text, max_chars=600):
    units = []; cur = []; section = ''; page = 1
    def flush():
        if cur:
            body = ' '.join(x.strip() for x in cur if x.strip())
            if body:
                number = re.match(r'\s*(\d+)', cur[0])
                units.append({'text': body, 'page': page, 'section_path': [section] if section else [], 'kind': 'qa', 'question_number': int(number.group(1)) if number else None, 'qa_status': 'paired' if any(_A.match(x) for x in cur) else 'unknown'})
            cur.clear()
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line: continue
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', line)
        if marker:
            # 条文跨页时不能 flush：否则下一页正文会脱离原条文并被丢弃。
            page = int(marker.group(1)); continue
        if _SECTION.match(line) and not _Q.match(line): flush(); section = line; continue
        if _Q.match(line): flush(); cur.append(line); continue
        if cur: cur.append(line)
        else: units.append({'text': line, 'page': page, 'section_path': [section] if section else [], 'kind': 'qa'})
    flush(); return _split_units(units, max_chars)


def chunk_glossary(text, max_chars=600):
    title = ''; category = ''; records = []; current = None; page = 1
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s or set(s) <= set('-—_ '): continue
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', s)
        if marker:
            if current: records.append(current); current = None
            page = int(marker.group(1)); continue
        category_match = re.match(r'^(?:[一二三四五六七八九十]+[、.．]|\d+[、.．])\s*(.+类)\s*$', s)
        if category_match:
            if current: records.append(current); current = None
            category = category_match.group(1); continue
        term_match = re.match(r'^([^：:]{1,80})\s*[：:](.*)$', s)
        if term_match:
            if current: records.append(current)
            term, definition = term_match.group(1).strip(), term_match.group(2).strip()
            if not title and not category: title = term
            current = {'term': term, 'definition': definition, 'page': page, 'category': category, 'title': title}
        elif current: current['definition'] += '\n' + s
        elif not title: title = s
    if current: records.append(current)
    units = []
    for record in records:
        body = f"{record['term']}：{record['definition']}"
        base = {k: record[k] for k in ('page', 'category', 'title', 'term', 'definition')}
        base.update(section_path=[x for x in (record['title'], record['category']) if x], kind='definition')
        units.append({**base, 'text': body})
    return _split_units(units, max_chars)


def chunk_outline(text, max_chars=600):
    units = []; cur = []; path = []; page = 1
    def flush():
        if cur:
            body = ' '.join(x.strip() for x in cur if x.strip())
            if body: units.append({'text': body, 'page': page, 'section_path': list(path), 'kind': 'outline_item'})
            cur.clear()
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s: continue
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', s)
        if marker: flush(); page = int(marker.group(1)); continue
        if re.match(r'^第?[一二三四五六七八九十百千万]+章', s): flush(); path = [s]; continue
        match = re.match(r'^(\d+(?:\.\d+)*)(?:[、.． ]+)(.*)$', s)
        if match and len(s) < 120 and not re.search(r'[。！？]$', s):
            flush(); path = path[:1] + [s]; cur.append(s); continue
        cur.append(s)
    flush(); return _split_units(units, max_chars)


def chunk_medical_mixed(text, max_chars=600):
    units = []; cur = []; page = 1; start_page = 1; section = []; entry_number = None
    item_re = re.compile(r'^\s*(\d+)[、.．]\s*(?:\*\s*)?(.*)$')
    def flush():
        nonlocal entry_number, start_page
        if cur:
            body = ' '.join(x.strip() for x in cur if x.strip())
            if body:
                units.append({'text': body, 'page': start_page, 'page_end': page,
                              'section_path': list(section), 'kind': 'medical_mixed',
                              'entry_number': entry_number})
            cur.clear(); entry_number = None
        start_page = page
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s: continue
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', s)
        if marker:
            page = int(marker.group(1)); continue
        if s.startswith('## '):
            flush(); section = [s[3:].strip()]; continue
        item = item_re.match(s)
        all_items = list(re.finditer(r'\d+[、.．]\s*', s))
        if item and len(all_items) == 1:
            flush(); entry_number = int(item.group(1)); cur.append(s); continue
        if item and len(all_items) > 1:
            first_end = all_items[1].start()
            flush(); entry_number = int(item.group(1)); cur.append(s[:first_end].rstrip()); flush()
            suffix = s[first_end:].lstrip(); next_item = item_re.match(suffix)
            if next_item:
                entry_number = int(next_item.group(1)); cur.append(suffix); continue
        # PDF 表格常把下一条编号粘在同一行；只有当前条目且编号前有空白/标点时拆分。
        inline = next((m for m in re.finditer(r'(?P<num>\d+)[、.．]\s*', s) if m.start() > 0), None)
        if entry_number is not None and inline:
            # 仅处理当前条目正文中的后续编号；正文里的年份/剂量没有“、/．”后缀。
            prefix_raw = s[:inline.start()]
            prefix = prefix_raw.rstrip()
            suffix = s[inline.start():].lstrip()
            if prefix and suffix and (prefix_raw[-1].isspace() or prefix[-1] in '。；：:，,　 ' or re.search(r'[一-龥)]$', prefix) or (entry_number >= 30 and prefix and suffix)):
                cur.append(prefix); flush()
                m = item_re.match(suffix)
                if m:
                    entry_number = int(m.group(1)); cur.append(suffix); continue
        # 表格行只在没有活动条目时开启新块；活动条目中的表格行属于该条目。
        # 仅医疗 profile 使用此边界，避免影响法律/技术书等其他类别。
        if ('|' in s and s.count('|') >= 2) and entry_number is None and cur:
            flush()
        elif re.match(r'^[⑴⑵⑶⑷⑸⑹⑺⑻⑼①②③④⑤]|^（\d+）', s) and entry_number is None:
            flush()
        cur.append(s)
    flush(); return _split_units(units, max_chars)


def _hard_split_structured(value, max_chars):
    """结构块的保真拆分：优先行边界，其次句末，最后才硬切。"""
    parts = []
    rest = value.strip()
    if not rest:
        return parts
    while len(rest) > max_chars:
        # 优先行边界
        cut = rest.rfind('\n', 0, max_chars)
        # 如果行边界太靠前，找句末
        if cut < max_chars // 2:
            cut = max((rest.rfind(mark, 0, max_chars) for mark in '。！？!?；;'), default=-1)
        # 如果句末也靠前，才硬切
        if cut < max_chars // 2:
            cut = max_chars
        else:
            cut = cut + 1
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


@dataclass
class CodeState:
    """代码识别状态；只用语法边界决定代码块的连续性。"""
    depth: int = 0
    quote: str | None = None
    started: bool = False


def _code_like(line):
    return (line.startswith(('from ', 'import ', 'def ', 'class ', 'async def ',
                             'const ', 'let ', 'function ', '#')) or line[:1] in '{}])' or
            re.match(r'^\w+\s*[:=]', line) is not None or
            line.startswith(('return ', 'raise ', 'yield ', 'if ', 'for ', 'while ',
                             'try:', 'except ', 'with ')))


def _scan_technical_blocks(text):
    """扫描技术书文本，先识别结构块，再进行切分。"""
    text = re.sub(r'(?<=[㐀-䶿一-鿿])[ \t]+(?=[㐀-䶿一-鿿])', '', text or '')
    text = re.sub(r'(?<=[㐀-䶿一-鿿])[ \t]+(?=[A-Za-z])', '', text)
    text = re.sub(r'(?<=[A-Za-z])[ \t]+(?=[㐀-䶿一-鿿])', '', text)
    text = re.sub(r'(?<=[A-Za-z])[ \t]{2,}(?=[A-Za-z])', ' ', text)
    # 转义引号是抽取 JSON 代码时常见的版面残留。
    text = text.replace('\\"', '"')
    blocks, para, code = [], [], []
    page = para_page = code_page = 1
    para_page_end = code_page_end = 1
    state = CodeState(); fenced = False; toc = False

    def flush_para():
        nonlocal para, para_page, para_page_end
        if para:
            blocks.append((para_page, para_page_end, 'para', ' '.join(para)))
            para = []
        para_page = para_page_end = page
    def flush_code():
        nonlocal code, code_page, code_page_end
        if code:
            blocks.append((code_page, code_page_end, 'code', '\n'.join(code)))
            code = []
        code_page = code_page_end = page
        state.depth = 0; state.quote = None; state.started = False
    def add_code(line):
        nonlocal code_page, code_page_end
        if not code: code_page = code_page_end = page
        else: code_page_end = page
        code.append(line); state.started = True
        escaped = False; quote = None
        for ch in line:
            if quote:
                if escaped: escaped = False
                elif ch == '\\': escaped = True
                elif ch == quote: quote = None
            elif ch in ('"', "'"): quote = ch
            elif ch in '([{': state.depth += 1
            elif ch in ')]}': state.depth = max(0, state.depth - 1)
        state.quote = quote

    for raw in (text or '').splitlines():
        line, stripped = raw.rstrip(), raw.strip()
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', stripped)
        if marker:
            new_page = int(marker.group(1)); entering = 2 <= new_page <= 8
            if entering or (toc and not entering): flush_para(); flush_code()
            page = new_page
            if para: para_page_end = page
            if code: code_page_end = page
            para_page = page if not para else para_page
            toc = entering; continue
        if not stripped: flush_para(); continue
        if toc: continue
        if stripped.startswith('## '):
            flush_para()
            blocks.append((page, page, 'heading', stripped[3:].strip()))
            continue
        if para and not code and stripped.startswith(('正文', '前言')):
            flush_para()
        if stripped.startswith('```'):
            flush_para()
            if fenced: fenced = False; flush_code()
            else: fenced = True; code = [stripped]; code_page = page
            continue
        if fenced: add_code(line); continue
        # 明确的正文标记会结束前面的代码，即使抽取代码的标点不完整。
        if code and stripped.startswith(('正文', '本文', '说明')):
            flush_code()
            para.append(stripped)
            para_page = page
            continue
        # 某些 PDF 抽取器会把正文标记和代码末行合并。
        embedded = re.search(r'(正文|本文|说明)', stripped) if code else None
        if embedded and embedded.start() > 0:
            add_code(stripped[:embedded.start()].rstrip())
            flush_code()
            para.append(stripped[embedded.start():].strip())
            para_page = page
            continue
        code_like = _code_like(stripped)
        # 容器结构未闭合时，即使 JSON/对象行不像赋值，也继续视为代码，
        # 例如带引号的属性名行。
        if code and state.depth > 0:
            flush_para(); add_code(line); continue
        if code and state.depth == 0 and not state.quote and not code_like and re.match(r'^[一-龥]', stripped):
            flush_code()
        if code_like:
            flush_para(); add_code(line); continue
        if stripped.count('|') >= 2:
            flush_para(); blocks.append((page, page, 'table', stripped)); continue
        para.append(stripped); para_page_end = page
    flush_para(); flush_code()
    return blocks


def _add_chunk_metadata(row, *, block_id, index, page_start, page_end):
    row['continuation_of'] = block_id
    row['continuation_index'] = index
    if page_end is not None and page_end != page_start:
        row['page_end'] = page_end
    return row


def _split_block_parts(value, max_chars, kind):
    if len(value) <= max_chars:
        return [value]
    return _hard_split_structured(value, max_chars)


def _safe_prose_parts(value, max_chars, marks):
    """Split prose at semantic boundaries and enforce the hard length limit."""
    parts = []
    rest = value.strip()
    while len(rest) > max_chars:
        cut = max((rest.rfind(mark, 0, max_chars) for mark in marks), default=-1)
        if cut <= max_chars // 2:
            cut = rest.rfind('\n', 0, max_chars)
        if cut <= max_chars // 2:
            cut = max_chars
        else:
            cut += 1
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return parts


def chunk_technical_book(text, max_chars=600):
    """技术书结构化切分：扫描结构块，再按既有规则切分。"""


    marks = set('。！？!?；;')
    blocks = _scan_technical_blocks(text)

    def sentence_parts(value):
        out, start = [], 0
        for i, char in enumerate(value):
            if char not in marks:
                continue
            if char == '.' and ((i and value[i - 1].isdigit()) or
                                (i + 1 < len(value) and value[i + 1].isdigit())):
                continue
            out.append(value[start:i + 1].strip()); start = i + 1
        tail = value[start:].strip()
        if tail:
            out.append(tail)
        return [x for x in out if x]

    chunks = []
    block_id = 0
    for page_start, page_end, kind, value in blocks:
        block_id += 1
        block_key = f"{kind}:{block_id}"
        if kind == 'toc':
            continue
        if kind == 'heading':
            heading_text = value
            parts = [heading_text] if len(heading_text) <= max_chars else _hard_split_structured(heading_text, max_chars)
            for n, part in enumerate(parts):
                row = _add_chunk_metadata({'text': part, 'page': page_start, 'section_path': [], 'kind': 'heading'}, block_id=block_key, index=n, page_start=page_start, page_end=page_end)
                if n: row['continuation'] = True
                chunks.append(row)
            continue
        if kind in ('code', 'table'):
            # 结构类型保持独立，但超长结构按行/句拆分，不能突破 max_chars。
            parts = _hard_split_structured(value, max_chars)
            for n, part in enumerate(parts):
                row = _add_chunk_metadata({'text': part, 'page': page_start, 'section_path': [], 'kind': kind}, block_id=block_key, index=n, page_start=page_start, page_end=page_end)
                if n: row['continuation'] = True
                chunks.append(row)
            continue
        if kind != 'para':
            continue
        units = sentence_parts(value)
        buf = ''
        for unit in units:
            if len(unit) > max_chars:
                if buf:
                    chunks.append(_add_chunk_metadata({'text': buf, 'page': page_start, 'section_path': [], 'kind': 'prose'}, block_id=block_key, index=0, page_start=page_start, page_end=page_end)); buf = ''
                rest = unit
                while len(rest) > max_chars:
                    # PDF 抽取的长段落可能在 max_chars 中间截断一句话；优先向后
                    # 找最近的句末（最多放宽 120 字），避免“进入”/“了工程落地”
                    # 这种把完整陈述拆成两个残句的 chunk。找不到时才回退到前方断点。
                    # 硬上限优先：句末只在 max_chars 之前才可作为断点，
                    # 不为完整句向后放宽，否则会产生 600+ 的超长 chunk。
                    backward = max((rest.rfind(mark, 0, max_chars) for mark in marks), default=-1)
                    cut = backward + 1 if backward > max_chars // 2 else max_chars
                    chunks.append(_add_chunk_metadata({'text': rest[:cut].strip(), 'page': page_start, 'kind': 'prose'}, block_id=block_key, index=0, page_start=page_start, page_end=page_end))
                    rest = rest[cut:].strip()
                buf = rest
            elif not buf:
                buf = unit
            elif len(buf) + 1 + len(unit) <= max_chars:
                buf += '\n' + unit
            else:
                row = _add_chunk_metadata({'text': buf, 'page': page_start, 'section_path': [], 'kind': 'prose'}, block_id=block_key, index=0, page_start=page_start, page_end=page_end)
                if not buf.endswith(tuple(marks)):
                    row['continuation'] = True
                chunks.append(row)
                buf = unit
        if buf:
            row = _add_chunk_metadata({'text': buf, 'page': page_start, 'section_path': [], 'kind': 'prose'}, block_id=block_key, index=0, page_start=page_start, page_end=page_end)
            if not buf.endswith(tuple(marks)):
                row['continuation'] = True
            chunks.append(row)
    return chunks


def _detect_legal(t, f=''): return Classification('legal', .9 if is_legal_confident(t) else 0, ('article_structure',))
def _detect_medical(t, f=''):
    n = len(re.findall(r'诊断|治疗|症状|患者|临床|高血压|肺炎|心衰|气胸|用药|mmHg|mg|PEF|COPD', t or ''))
    return Classification('medical_mixed', min(.95, n / 80), ('medical_signals',))
def _detect_technical(t, f=''):
    n = sum(bool(re.search(x, t or '', re.I)) for x in ('目录', r'第\s*\d+\s*章', 'Agent', 'API', '代码', '示例', '函数', '模块', '```'))
    return Classification('technical_book', min(.95, n / 5), ('technical_signals',))
def _detect_outline(t, f=''):
    lines = [x for x in (t or '').splitlines() if x.strip()]
    n = sum(bool(re.match(r'^(?:第?[一二三四五六七八九十百千万]+[、.．]|\d+(?:\.\d+)*[、.． ]|※)', x.strip())) for x in lines)
    chapter = any(re.match(r'^第?[一二三四五六七八九十百千万]+章', x.strip()) for x in lines)
    return Classification('outline', min(.95, n / max(1, len(lines)) * 3 + (.25 if chapter else 0)), ('outline_lines',))
def _detect_qa(t, f=''):
    q = qa_confidence(t)
    return Classification('qa', q['confidence'], ('question_answer_pairs',)) if q['kind'] == 'qa' else Classification('qa', 0)
def _detect_glossary(t, f=''):
    lines = [x for x in (t or '').splitlines() if x.strip()]
    n = sum(bool(re.match(r'^.{1,30}[：:].+', x.strip())) for x in lines)
    return Classification('glossary', min(.95, n / max(1, len(lines)) * 2), ('definition_density',))
def _detect_toc(t, f=''):
    lines = [x for x in (t or '').splitlines() if x.strip()]
    n = sum(bool(_TOC.match(x.strip())) for x in lines)
    return Classification('toc', min(.95, n / max(1, len(lines)) * 2), ('dotted_leaders',))


class DocumentProfileRegistry:
    _profiles = {}
    @classmethod
    def register(cls, kind, detector, chunker): cls._profiles[kind] = (detector, chunker)
    @classmethod
    def classify(cls, text, filename=''):
        scores = []
        for kind, (detector, _) in cls._profiles.items():
            if kind == 'generic':
                continue
            result = detector(text, filename)
            if result.confidence > 0:
                scores.append(result)
        scores.sort(key=lambda x: x.confidence, reverse=True)
        if not scores or scores[0].confidence < .65: return Classification('generic', 0, ('fallback',))
        strong = next((x for x in scores if x.kind in {'qa', 'legal'} and x.confidence >= .9), None)
        if strong: return strong
        if len(scores) > 1 and scores[0].confidence - scores[1].confidence < .10: return Classification('generic', scores[0].confidence, ('ambiguous',))
        return scores[0]
    @classmethod
    def chunk(cls, text, filename='', max_chars=600):
        kind = cls.classify(text, filename).kind
        try: return cls._profiles.get(kind, (None, _generic))[1](text, max_chars)
        except Exception: return _generic(text, max_chars)

DocumentProfileRegistry.register('legal', _detect_legal, chunk_legal)
DocumentProfileRegistry.register('medical_mixed', _detect_medical, chunk_medical_mixed)
DocumentProfileRegistry.register('technical_book', _detect_technical, chunk_technical_book)
DocumentProfileRegistry.register('outline', _detect_outline, chunk_outline)
DocumentProfileRegistry.register('qa', _detect_qa, chunk_qa)
DocumentProfileRegistry.register('glossary', _detect_glossary, chunk_glossary)
DocumentProfileRegistry.register('toc', _detect_toc, _generic)
DocumentProfileRegistry.register('generic', lambda t, f='': Classification('generic', 0), _generic)

def classify_document(text, filename=''): return DocumentProfileRegistry.classify(text, filename)
def chunk_document(text, filename='', max_chars=600): return DocumentProfileRegistry.chunk(text, filename, max_chars)
