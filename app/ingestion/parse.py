# -*- coding: utf-8 -*-
"""
文档解析：文件名 + 字节 → 纯文本（docx 保留标题结构标记）。

本地解析：
- txt/md  文本直读（UTF-8，失败回退 GBK——Windows 环境常见）
- docx    python-docx：标题段落标成「## 标题」行（标题感知切分用）
- pdf     pypdf 逐页抽取（扫描件 PDF 抽不出文字 → 空文本，后续可接 OCR，路线图）
"""
import io

_CHUNKABLE_EXT = {"txt", "md", "docx", "pdf", "doc"}


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def parse_bytes(filename: str, data: bytes) -> str:
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext in ("txt", "md", ""):
        return _decode(data)
    if ext == "docx":
        return _parse_docx(data)
    if ext == "pdf":
        return _parse_pdf(data)
    return ""   # 不支持的类型 → 空文本（调用方回错误）


def _parse_docx(data: bytes) -> str:
    try:
        import docx
        doc = docx.Document(io.BytesIO(data))
        parts = []
        for para in doc.paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            if style.startswith("heading"):
                parts.append(f"## {text}")
            else:
                parts.append(text)
        return "\n".join(parts)
    except Exception:
        return ""


def _parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages)
    except Exception:
        return ""
