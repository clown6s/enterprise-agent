# -*- coding: utf-8 -*-
"""
文档解析：文件名 + 字节 → 纯文本（docx 保留标题结构标记）。

本地解析：
- txt/md  文本直读（UTF-8，失败回退 GBK——Windows 环境常见）
- docx    python-docx：标题段落标成「## 标题」行（标题感知切分用）
- pdf     pdfplumber 版式感知抽取 + [[page:N]] 页码标记（表格/双栏稳），
          失败回退 pypdf；扫描件抽不出文字 → 空文本（OCR 路线图）
- doc     老格式不支持——请转存 docx 后上传
"""
import io


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
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        doc = docx.Document(io.BytesIO(data))
        parts = []

        def add_para(para) -> None:
            text = (para.text or "").strip()
            if not text:
                return
            style = (para.style.name or "").lower() if para.style else ""
            # 中文 Word/WPS 的样式名是"标题 1"——只认 heading 会静默丢失标题感知
            if style.startswith(("heading", "标题", "title")):
                parts.append(f"## {text}")
            else:
                parts.append(text)

        # 按文档实际顺序交错提取段落与表格。doc.paragraphs 不含表格——
        # 企业制度文档的表格内容曾整体丢失；表格行以 " | " 拼接为一行
        for child in doc.element.body.iterchildren():
            if child.tag.endswith("}p"):
                add_para(Paragraph(child, doc))
            elif child.tag.endswith("}tbl"):
                for row in Table(child, doc).rows:
                    cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                    cells = [c for c in cells if c]
                    if cells:
                        parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception:
        return ""


def _parse_pdf(data: bytes) -> str:
    """
    PDF 解析：pdfplumber 版式感知抽取（表格/双栏比 pypdf 稳——流式拍平
    导致列混排的历史失分点），逐页注入 [[page:N]] 标记（切分时落 page 字段，
    溯源到页码）。pdfplumber 失败回退 pypdf（不丢文档）。
    """
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                text = (page.extract_text() or "").strip()
                if text:
                    parts.append(f"[[page:{i}]]\n{text}")
        if parts:
            return "\n".join(parts)
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        return "\n".join(pages)
    except Exception:
        return ""
