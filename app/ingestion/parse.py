# -*- coding: utf-8 -*-
"""
文档解析：文件名 + 字节 → 纯文本（docx 保留标题结构标记）。

本地解析：
- txt/md  文本直读（UTF-8 → GBK → GB18030 逐级回退——Windows/Excel 导出常见）
- csv      中文 CSV（Excel 导出常见 GBK 编码——曾整类不支持直接拒绝）
- docx    python-docx：标题段落标成「## 标题」行（标题感知切分用）
- pdf     pdfplumber 版式感知抽取 + [[page:N]] 页码标记（表格/双栏稳），
          失败回退 pypdf；扫描件抽不出文字 → 空文本（OCR 路线图）；
          文本层乱码（CID 字体）→ 拒绝入库（曾把乱码当正文切块入库）
- doc     老格式不支持——请转存 docx 后上传
"""
import hashlib
import io
import re
import statistics

# 图片 OCR 门控（成本控制：单文档上限/尺寸上下限——图片是加分项不是必需品，
# 失败或超限一律跳过，不阻断文档摄入）
_MAX_IMAGE_OCR = 12
_MAX_IMAGE_BYTES = 3 * 1024 * 1024
_MIN_IMAGE_BYTES = 100

# 乱码判定：私用区字符（CID 字体 ToUnicode 映射失败产物）+ 替换符
_PUA_RE = re.compile("[-�]")


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _is_garbled(text: str) -> bool:
    """PDF 文本层乱码判定：CID 字体的 ToUnicode 映射失败时复制出
    私用区/替换符字符；合法文本几乎不含这些码位。"""
    if not text:
        return False
    return len(_PUA_RE.findall(text)) / len(text) > 0.05


def parse_bytes(filename: str, data: bytes) -> str:
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    if ext in ("txt", "md", ""):
        return _decode(data)
    if ext == "csv":
        return _parse_csv(data)
    if ext == "docx":
        return _parse_docx(data)
    if ext == "pdf":
        return _parse_pdf(data)
    return ""   # 不支持的类型 → 空文本（调用方回错误）


def _parse_csv(data: bytes) -> str:
    """CSV 解析：逐级编码回退 + 分隔符自检（逗号/制表符/分号）。
    输出行 " | " 拼接（与 docx 表格同一格式，切分不感知差异）。"""
    import csv as _csv
    text = _decode(data)
    if not text.strip():
        return ""
    # 分隔符自检：取第一行统计候选分隔符出现次数，取最多者
    first = text.splitlines()[0] if text.splitlines() else ""
    delim = max((",", "\t", ";"), key=lambda d: first.count(d))
    rows = []
    try:
        for row in _csv.reader(io.StringIO(text), delimiter=delim):
            cells = [c.strip() for c in row if c and c.strip()]
            if cells:
                rows.append(" | ".join(cells))
    except _csv.Error:
        return ""
    return "\n".join(rows)


def _docx_images(doc) -> list[bytes]:
    """docx 内嵌图字节（按内容去重——同一张图多处引用只 OCR 一次）"""
    seen: set[str] = set()
    out: list[bytes] = []
    try:
        for part in doc.part.related_parts.values():
            if "image" not in (part.content_type or ""):
                continue
            raw = getattr(part, "blob", None)
            if not raw or _MIN_IMAGE_BYTES > len(raw) or len(raw) > _MAX_IMAGE_BYTES:
                continue
            h = hashlib.md5(raw).hexdigest()
            if h not in seen:
                seen.add(h)
                out.append(raw)
    except Exception:
        pass
    return out


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
        # 内嵌图片 OCR（曾正文插图全部丢失——截图/示意图是企业文档常见信息）
        images = _docx_images(doc)
        if images:
            ocr_parts = []
            for idx, img in enumerate(images[:_MAX_IMAGE_OCR]):
                ocr = _ocr_image(img)
                if ocr:
                    ocr_parts.append(f"【图片{idx + 1}】{ocr}")
            if ocr_parts:
                parts.append("## 图片内容")
                parts.extend(ocr_parts)
        return "\n".join(parts)
    except Exception:
        return ""


def _pdf_images(data: bytes) -> list[bytes]:
    """pypdf 提取 PDF 内嵌图字节（按页序、按内容去重——同 logo 每页重复只算一次）"""
    seen: set[str] = set()
    out: list[bytes] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages:
            try:
                images = page.images
            except Exception:
                continue
            for img in images:
                raw = getattr(img, "data", None)
                if not raw or _MIN_IMAGE_BYTES > len(raw) or len(raw) > _MAX_IMAGE_BYTES:
                    continue
                h = hashlib.md5(raw).hexdigest()
                if h not in seen:
                    seen.add(h)
                    out.append(raw)
    except Exception:
        pass
    return out


def _ocr_image(image: bytes) -> str:
    """图片 → 文字（qwen-vl，注册表 ocr 条目）。失败返回空——调用方跳过。"""
    try:
        from .. import llm
        out = llm.chat_image(
            "你是一个文档图片 OCR 助手。",
            "把图片中的文字原样输出（表格按行输出，行内用 | 分隔）。"
            "如果没有文字就回复：无。不要添加任何解释。", image).strip()
        norm = out.rstrip("。.!！?？;；")
        return "" if norm in ("", "无", "没有文字", "无文字") else out
    except Exception as e:
        print(f"[parse] 图片 OCR 失败（跳过该图）: {e!r}")
        return ""


def _with_title_marks(line_sizes: list[tuple[str, float]]) -> list[str]:
    """版面识别-标题判定（确定性）：行字号 >= 页中位数×1.25 且短行 → 「## 」。
    标题感知切分由此获得（曾 PDF 无标题边界，chunk 块边界靠猜）。
    排除纯数字行（页脚页码/编号——曾会把"3"标成标题，chunk 边界割裂）。"""
    if not line_sizes:
        return []
    med = statistics.median(s for _, s in line_sizes)
    out = []
    for text, size in line_sizes:
        if med and size >= med * 1.25 and 2 <= len(text) <= 40 and not text.isdigit():
            text = f"## {text}"
        out.append(text)
    return out


def _parse_pdf(data: bytes) -> str:
    """
    PDF 解析：pdfplumber 版式感知抽取（表格/双栏比 pypdf 稳——流式拍平
    导致列混排的历史失分点），逐页注入 [[page:N]] 标记（切分时落 page 字段，
    溯源到页码）。版面识别：按行字号判定标题打「## 」标记（标题感知切分——
    曾只有 docx 有标题感知，PDF 标题块边界靠猜）。内嵌图片走 OCR 独立成段
    （「## 图片内容」节，上限门控）。pdfplumber 失败回退 pypdf（不丢文档）。
    """
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, 1):
                try:
                    line_sizes = []
                    for ln in (page.extract_text_lines(layout=True, return_chars=True) or []):
                        t = (ln.get("text") or "").strip()
                        if not t:
                            continue
                        chars = ln.get("chars") or []
                        mean = statistics.mean(c["size"] for c in chars) if chars else 0.0
                        line_sizes.append((t, mean))
                    out_lines = _with_title_marks(line_sizes)
                    if out_lines:
                        parts.append(f"[[page:{i}]]\n" + "\n".join(out_lines))
                except Exception:
                    # 单页异常隔离：一页坏不拖垮整份文档（曾整本 PDF 一起失败）
                    continue
        full = "\n".join(parts)
        if full and _is_garbled(full):
            print("[parse] PDF 文本层乱码（CID 字体），拒绝入库——请提供可检索版 PDF")
            return ""
        # 内嵌图片 OCR：无论有无文本层都跑（扫描件 PDF 无文本层，
        # 内容全靠图片 OCR——曾 parts 为空时直接回退，图片段没机会执行）
        images = _pdf_images(data)
        if images:
            ocr_parts = []
            for idx, img in enumerate(images[:_MAX_IMAGE_OCR]):
                ocr = _ocr_image(img)
                if ocr:
                    ocr_parts.append(f"【图片{idx + 1}】{ocr}")
            if ocr_parts:
                full = (full + "\n" if full else "") + "## 图片内容\n" + "\n".join(ocr_parts)
        if full:
            return full
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        full = "\n".join(pages)
        if _is_garbled(full):
            print("[parse] PDF 文本层乱码（CID 字体），拒绝入库——请提供可检索版 PDF")
            return ""
        return full
    except Exception:
        return ""
