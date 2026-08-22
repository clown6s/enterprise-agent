# -*- coding: utf-8 -*-
"""Extensible document profiles and preview chunkers; production remains unchanged."""
from dataclasses import dataclass
import re

_TOC = re.compile(r"^\s*(.+?)(?:\.{2,}|…{2,}|\s{3,})(\d+)\s*$")
_ARTICLE = re.compile(r"^\s*(第\s*[0-9零一二三四五六七八九十百千万]+\s*条)(?![一-龥A-Za-z0-9])")
_ARTICLE_ANY = re.compile(r"第\s*[0-9零一二三四五六七八九十百千万]+\s*条(?![一-龥A-Za-z0-9])")
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


def _generic(text, max_chars=600):
    from .pipeline import chunk_text
    chunks, pages = chunk_text(text, max_chars=max_chars, overlap=0)
    return [{'text': x, 'page': y, 'section_path': []} for x, y in zip(chunks, pages)]


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
        if re.search(r'(?:本法|前款|前项|依照|根据|按照|上述|下列)\s*$', before):
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
            current = {'text': segment.strip(), 'page': page, 'article': re.sub(r'\s+', '', match.group(1)), 'section_path': [x[1] for x in stack], 'kind': 'legal'}
            in_body = True
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line: continue
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', line)
        if marker: flush(); page = int(marker.group(1)); continue
        if line in {'目录', '目 录'}: flush(); in_body = False; continue
        level = _LEVEL.match(line)
        if level:
            flush(); label = level.group(1).strip(); rank = ranks.get(label[-1], 2)
            stack = [x for x in stack if x[0] < rank] + [(rank, label)]; continue
        parts = _split_inline_articles(line)
        if parts:
            for part in parts: start(part)
            continue
        if parts:
            for part in parts: start(part)
            continue
        if in_body and current:
            current['text'] += ' ' + line
        elif not in_body:
            # 不能静默吞掉条文前导、附录或异常抽取文本；目录/页眉等结构行已在前面处理，
            # 其余内容保留为可追踪兜底块，调用方可按 kind 复核而非误当正文条文。
            units.append({'text': line, 'page': page, 'section_path': [x[1] for x in stack], 'kind': 'legal_unclassified', 'quality_flag': 'unclassified_legal_text'})
    flush()
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
        if marker: flush(); page = int(marker.group(1)); continue
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
    units = []; cur = []; page = 1; section = []
    def flush():
        if cur:
            body = ' '.join(x.strip() for x in cur if x.strip())
            if body: units.append({'text': body, 'page': page, 'section_path': list(section)})
            cur.clear()
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s: continue
        marker = re.fullmatch(r'\[\[page:(\d+)\]\]', s)
        if marker: flush(); page = int(marker.group(1)); continue
        if s.startswith('## '): flush(); section = [s[3:].strip()]; continue
        if re.match(r'^[⑴⑵⑶⑷⑸⑹⑺⑻⑼①②③④⑤]|^（\d+）', s) or ('|' in s and s.count('|') >= 2): flush()
        cur.append(s)
    flush(); return _split_units(units, max_chars)


def chunk_technical_book(text, max_chars=600):
    units = []; prose = []; code = []; page = 1; section = []; in_code = False
    def flush_prose():
        if prose:
            units.append({'text': '\n'.join(prose), 'page': page, 'section_path': list(section), 'kind': 'prose'}); prose.clear()
    def flush_code():
        if code:
            units.append({'text': '\n'.join(code), 'page': page, 'section_path': list(section), 'kind': 'code'}); code.clear()
    for raw in (text or '').splitlines():
        line = raw.rstrip(); stripped = line.strip()
        if re.fullmatch(r'\[\[page:\d+\]\]', stripped):
            (flush_code if in_code else flush_prose)(); page = int(re.search(r'\d+', stripped).group()); continue
        if stripped.startswith('```'):
            if not in_code: flush_prose(); in_code = True
            code.append(line)
            if stripped == '```' and len(code) > 1: in_code = False; flush_code()
            continue
        if in_code: code.append(line); continue
        if stripped.startswith('#'): flush_prose(); section = [stripped.lstrip('#').strip()]; continue
        prose.append(line)
    if in_code: flush_code()
    flush_prose(); return _split_units(units, max_chars)


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
