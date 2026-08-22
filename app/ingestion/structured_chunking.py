# -*- coding: utf-8 -*-
"""Extensible document profiles and preview chunkers; production remains unchanged."""
from dataclasses import dataclass
from typing import Callable
import re

_TOC = re.compile(r"^\s*(.+?)(?:\.{2,}|…{2,}|\s{3,})(\d+)\s*$")
_ARTICLE = re.compile(r"^\s*(第\s*[0-9一二三四五六七八九十百千万]+\s*条)(?![一-龥A-Za-z0-9])")
_LEVEL = re.compile(r"^\s*(第\s*[0-9一二三四五六七八九十百千万]+\s*(?:编|篇|章|节))(?![一-龥A-Za-z0-9])")
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
    lines=[x.strip() for x in (text or '').splitlines() if x.strip()]
    q=sum(bool(_Q.match(x)) and ("？" in x or "?" in x or bool(re.search(r"什么|哪里|如何|为什么|哪些|多少|谁|何时|吗",x))) for x in lines)
    a=sum(bool(_A.match(x)) for x in lines)
    return {"kind":"qa" if q>=5 and a>=3 else "generic","confidence":min(1.0,q/max(1,len(lines))*2+a/max(1,len(lines))*2),"question_count":q,"answer_count":a}


def _drop_toc_units(text):
    out=[]; toc=False
    for line in (text or '').splitlines():
        s=line.strip()
        if s in {'目录','目 录'}: toc=True; continue
        if toc and _ARTICLE.match(s): toc=False
        if not toc: out.append(line)
    return '\n'.join(out)


def is_legal_confident(text):
    t=_drop_toc_units(text); lines=t.splitlines()
    a=sum(bool(_ARTICLE.match(x.strip())) for x in lines); l=sum(bool(_LEVEL.match(x.strip())) for x in lines)
    return a>=5 and (l>=1 or a>=10)


def _generic(text,max_chars=600):
    from .pipeline import chunk_text
    c,p=chunk_text(text,max_chars=max_chars,overlap=0)
    return [{'text':x,'page':y,'section_path':[]} for x,y in zip(c,p)]


def chunk_legal(text,max_chars=600):
    units=[]; sec=[]; art=None; buf=[]; page=1
    def flush():
        if buf:
            body=' '.join(x.strip() for x in buf if x.strip())
            if body: units.append({'text':body,'page':page,'article':art,'section_path':list(sec)})
            buf.clear()
    for raw in _drop_toc_units(text).splitlines():
        line=raw.strip()
        if not line: continue
        pm=re.fullmatch(r'\[\[page:(\d+)\]\]',line)
        if pm: flush(); page=int(pm.group(1)); continue
        lm=_LEVEL.match(line)
        if lm: flush(); sec=[lm.group(1)]; art=None; continue
        am=_ARTICLE.match(line)
        if am: flush(); art=am.group(1); buf=[line]; continue
        buf.append(line)
    flush(); out=[]
    for u in units:
        if len(u['text'])<=max_chars: out.append(u); continue
        rest=u['text']; first=True
        while len(rest)>max_chars:
            cut=rest.rfind('。',0,max_chars); cut=cut+1 if cut>max_chars//2 else max_chars
            out.append({**u,'text':rest[:cut].strip() if first else f"{u['article']}（续） {rest[:cut].strip()}"}); rest=rest[cut:].strip(); first=False
        if rest: out.append({**u,'text':rest if first else f"{u['article']}（续） {rest}"})
    return out


def chunk_qa(text,max_chars=600):
    units=[]; cur=[]; sec=''; page=1
    def flush():
        if cur:
            b=' '.join(x.strip() for x in cur if x.strip())
            if b: units.append({'text':b,'page':page,'section_path':[sec] if sec else [],'kind':'qa','question_number':None,'qa_status':'unknown'})
            cur.clear()
    for raw in (text or '').splitlines():
        line=raw.strip()
        if not line: continue
        pm=re.fullmatch(r'\[\[page:(\d+)\]\]',line)
        if pm: flush(); page=int(pm.group(1)); continue
        if _SECTION.match(line) and not _Q.match(line): flush(); sec=line; continue
        if _Q.match(line): flush(); cur.append(line); continue
        if cur: cur.append(line)
        else: units.append({'text':line,'page':page,'section_path':[sec] if sec else []})
    flush(); return units


def chunk_glossary(text,max_chars=600):
    units=[]; cur=[]; page=1
    def flush():
        if cur:
            b=' '.join(x.strip() for x in cur if x.strip())
            if b: units.append({'text':b,'page':page,'section_path':[]})
            cur.clear()
    for raw in (text or '').splitlines():
        s=raw.strip()
        if not s: continue
        if re.fullmatch(r'\[\[page:\d+\]\]',s): flush(); page=int(re.search(r'\d+',s).group()); continue
        if re.match(r'^(?:\d+[、.．]|[一二三四五六七八九十]+[、.．])',s): flush()
        cur.append(s)
    flush(); out=[]; buf=[]
    for u in units:
        body=u['text']
        if len(body)>max_chars:
            if buf:
                out.append({'text':'\n'.join(x['text'] for x in buf),'page':buf[0]['page'],'section_path':[]}); buf=[]
            rest=body
            while len(rest)>max_chars:
                cut=rest.rfind('。',0,max_chars); cut=cut+1 if cut>max_chars//2 else max_chars
                out.append({'text':rest[:cut].strip(),'page':u['page'],'section_path':[]}); rest=rest[cut:].strip()
            if rest: out.append({'text':rest,'page':u['page'],'section_path':[]})
            continue
        if buf and (u['section_path'] != buf[0]['section_path']
                   or len('\n'.join(x['text'] for x in buf))+len(body)>max_chars):
            out.append({'text':'\n'.join(x['text'] for x in buf),'page':buf[0]['page'],'section_path':[]}); buf=[]
        buf.append(u)
    if buf: out.append({'text':'\n'.join(x['text'] for x in buf),'page':buf[0]['page'],'section_path':[]})
    return out
def chunk_outline(text,max_chars=600):
    units=[]; cur=[]; path=[]; page=1
    def flush():
        if cur:
            b=' '.join(x.strip() for x in cur if x.strip())
            if b: units.append({'text':b,'page':page,'section_path':list(path)})
            cur.clear()
    for raw in (text or '').splitlines():
        s=raw.strip()
        if not s: continue
        pm=re.fullmatch(r'\[\[page:(\d+)\]\]',s)
        if pm: flush(); page=int(pm.group(1)); continue
        if re.match(r'^第?[一二三四五六七八九十百千万]+章',s): flush(); path=[s]; continue
        if re.match(r'^\d+(?:\.\d+)*[、.． ]',s): flush(); path=path[:1]+[s]; cur.append(s); continue
        if s.startswith('※'): cur.append(s); continue
        cur.append(s)
    flush(); out=[]; buf=[]
    for u in units:
        body=u['text']
        if len(body)>max_chars:
            if buf:
                out.append({'text':'\n'.join(x['text'] for x in buf),'page':buf[0]['page'],'section_path':buf[0]['section_path']}); buf=[]
            rest=body
            while len(rest)>max_chars:
                cut=rest.rfind('。',0,max_chars); cut=cut+1 if cut>max_chars//2 else max_chars
                out.append({'text':rest[:cut].strip(),'page':u['page'],'section_path':u['section_path']}); rest=rest[cut:].strip()
            if rest: out.append({'text':rest,'page':u['page'],'section_path':u['section_path']})
            continue
        if buf and (u['section_path'] != buf[0]['section_path']
                   or len('\n'.join(x['text'] for x in buf))+len(body)>max_chars):
            out.append({'text':'\n'.join(x['text'] for x in buf),'page':buf[0]['page'],'section_path':buf[0]['section_path']}); buf=[]
        buf.append(u)
    if buf: out.append({'text':'\n'.join(x['text'] for x in buf),'page':buf[0]['page'],'section_path':buf[0]['section_path']})
    return out

    return Classification('legal',.9 if is_legal_confident(t) else 0,('article_structure',))

def _detect_qa(t,f=''):
    q=qa_confidence(t); return Classification('qa',q['confidence'],('question_answer_pairs',)) if q['kind']=='qa' else Classification('qa',0)
def _detect_glossary(t,f=''):
    ls=[x for x in (t or '').splitlines() if x.strip()]; n=sum(bool(re.match(r'^.{1,30}[：:].+',x.strip())) for x in ls)
    return Classification('glossary',min(.95,n/max(1,len(ls))*2),('definition_density',))
def chunk_medical_mixed(text,max_chars=600):
    units=[]; cur=[]; page=1; section=[]
    def flush():
        if cur:
            b=' '.join(x.strip() for x in cur if x.strip())
            if b: units.append({'text':b,'page':page,'section_path':list(section)})
            cur.clear()
    for raw in (text or '').splitlines():
        s=raw.strip()
        if not s: continue
        pm=re.fullmatch(r'\[\[page:(\d+)\]\]',s)
        if pm: flush(); page=int(pm.group(1)); continue
        if s.startswith('## '): flush(); section=[s[3:].strip()]; continue
        if re.match(r'^[⑴⑵⑶⑷⑸⑹⑺⑻⑼①②③④⑤]|^（\d+）',s): flush()
        if '|' in s and s.count('|')>=2: flush()
        cur.append(s)
    flush(); return _split_units(units,max_chars)


def chunk_technical_book(text,max_chars=600):
    units=[]; cur=[]; page=1; section=[]; in_code=False
    def flush():
        if cur:
            b='\n'.join(x for x in cur if x.strip())
            if b: units.append({'text':b,'page':page,'section_path':list(section)})
            cur.clear()
    for raw in (text or '').splitlines():
        s=raw.rstrip()
        if re.fullmatch(r'\[\[page:\d+\]\]',s.strip()): flush(); page=int(re.search(r'\d+',s).group()); continue
        if s.strip().startswith('```'):
            if not in_code: flush(); in_code=True
            cur.append(s)
            if in_code and s.strip() != '```' and s.strip().startswith('```'): pass
            elif in_code and s.strip()=='```': in_code=False
            continue
        if not in_code and s.strip().startswith('#'):
            flush(); section=[s.lstrip('#').strip()]; continue
        if not in_code and '|' in s and s.count('|')>=2: flush()
        cur.append(s)
    flush(); return _split_units(units,max_chars)


def _split_units(units,max_chars):
    out=[]
    for u in units:
        if len(u['text'])<=max_chars: out.append(u); continue
        rest=u['text']
        while len(rest)>max_chars:
            cut=max(rest.rfind('。',0,max_chars),rest.rfind('\n',0,max_chars))
            cut=cut+1 if cut>max_chars//2 else max_chars
            out.append({**u,'text':rest[:cut].strip()}); rest=rest[cut:].strip()
        if rest: out.append({**u,'text':rest})
    return out


    return Classification('legal',.9 if is_legal_confident(t) else 0,('article_structure',))

def chunk_technical_book(text,max_chars=600):
    units=[]; cur=[]; page=1; section=[]; in_code=False; code=[]
    def flush():
        if cur:
            units.append({'text':'\n'.join(cur),'page':page,'section_path':list(section),'kind':'prose'}); cur.clear()
    def flush_code():
        if code:
            units.append({'text':'\n'.join(code),'page':page,'section_path':list(section),'kind':'code'}); code.clear()
    for raw in (text or '').splitlines():
        s=raw.rstrip()
        if re.fullmatch(r'\[\[page:\d+\]\]',s.strip()):
            (flush_code if in_code else flush)(); page=int(re.search(r'\d+',s).group()); continue
        if s.strip().startswith('```'):
            if not in_code: flush(); in_code=True
            code.append(s)
            if s.strip() != '```' and s.strip().startswith('```'): pass
            elif s.strip()=='```': in_code=False; flush_code()
            continue
        if in_code: code.append(s); continue
        if s.strip().startswith('#'):
            flush(); section=[s.lstrip('#').strip()]; continue
        cur.append(s)
    if in_code: flush_code()
    flush(); out=[]
    for u in units:
        if len(u['text'])<=max_chars: out.append(u); continue
        rest=u['text']; lines=rest.splitlines(); buf=[]
        for line in lines:
            if buf and len('\n'.join(buf))+len(line)+1>max_chars:
                out.append({**u,'text':'\n'.join(buf)}); buf=[]
            buf.append(line)
        if buf: out.append({**u,'text':'\n'.join(buf)})
    return out


def _detect_legal(t,f=''):
    return Classification('legal', .9 if is_legal_confident(t) else 0, ('article_structure',))


def _detect_medical(t,f=''):
    n = len(re.findall(r'诊断|治疗|症状|患者|临床|高血压|肺炎|心衰|气胸|用药|mmHg|mg|PEF|COPD', t or ''))
    return Classification('medical_mixed', min(.95, n / 80), ('medical_signals',))


def _detect_technical(t,f=''):
    n = sum(bool(re.search(x, t or '', re.I)) for x in
            ('目录', r'第\s*\d+\s*章', 'Agent', 'API', '代码', '示例', '函数', '模块', '```'))
    return Classification('technical_book', min(.95, n / 5), ('technical_signals',))


def _detect_outline(t,f=''):
    ls = [x for x in (t or '').splitlines() if x.strip()]
    n = sum(bool(re.match(r'^(?:第?[一二三四五六七八九十百千万]+[、.．]|\d+(?:\.\d+)*[、.． ]|※)', x.strip())) for x in ls)
    chapter = any(re.match(r'^第?[一二三四五六七八九十百千万]+章', x.strip()) for x in ls)
    return Classification('outline', min(.95, n / max(1, len(ls)) * 3 + (.25 if chapter else 0)), ('outline_lines',))


def _detect_toc(t,f=''):
    ls = [x for x in (t or '').splitlines() if x.strip()]
    n = sum(bool(_TOC.match(x.strip())) for x in ls)
    return Classification('toc', min(.95, n / max(1, len(ls)) * 2), ('dotted_leaders',))


class DocumentProfileRegistry:
    _profiles = {}

    @classmethod
    def register(cls, kind, detector, chunker):
        cls._profiles[kind] = (detector, chunker)

    @classmethod
    def classify(cls, text, filename=''):
        scores = [detector(text, filename) for kind, (detector, _) in cls._profiles.items()
                  if kind != 'generic']
        scores = sorted((x for x in scores if x.confidence > 0),
                        key=lambda x: x.confidence, reverse=True)
        if not scores or scores[0].confidence < .65:
            return Classification('generic', 0, ('fallback',))
        strong = next((x for x in scores if x.kind in {'qa', 'legal'} and x.confidence >= .9), None)
        if strong:
            return strong
        if len(scores) > 1 and scores[0].confidence - scores[1].confidence < .10:
            return Classification('generic', scores[0].confidence, ('ambiguous',))
        return scores[0]

    @classmethod
    def chunk(cls, text, filename='', max_chars=600):
        classification = cls.classify(text, filename)
        chunker = cls._profiles.get(classification.kind, (None, _generic))[1]
        try:
            return chunker(text, max_chars)
        except Exception:
            return _generic(text, max_chars)


DocumentProfileRegistry.register('legal', _detect_legal, chunk_legal)
DocumentProfileRegistry.register('medical_mixed', _detect_medical, chunk_medical_mixed)
DocumentProfileRegistry.register('technical_book', _detect_technical, chunk_technical_book)
DocumentProfileRegistry.register('outline', _detect_outline, chunk_outline)
DocumentProfileRegistry.register('qa', _detect_qa, chunk_qa)
DocumentProfileRegistry.register('glossary', _detect_glossary, chunk_glossary)
DocumentProfileRegistry.register('toc', _detect_toc, _generic)
DocumentProfileRegistry.register('generic', lambda t,f='': Classification('generic', 0), _generic)


def classify_document(text, filename=''):
    return DocumentProfileRegistry.classify(text, filename)


def chunk_document(text, filename='', max_chars=600):
    return DocumentProfileRegistry.chunk(text, filename, max_chars)

