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
import logging
log = logging.getLogger(__name__)

# 图片 OCR 门控（成本控制：单文档上限/尺寸上下限——图片是加分项不是必需品，
# 失败或超限一律跳过，不阻断文档摄入）
_MAX_IMAGE_OCR = 12
_MAX_IMAGE_BYTES = 3 * 1024 * 1024
_MIN_IMAGE_BYTES = 100
_MIN_TEXT_CHARS = 80
_MIN_IMAGE_COVERAGE = 0.10

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


def parse_pdf_with_diagnostics(data: bytes) -> tuple[str, dict]:
    """PDF 解析兼质量报告；正文仍由现有页级解析逻辑产生。"""
    text = _parse_pdf(data)
    chars = len(re.sub(r"\s+", "", text or ""))
    replacement = len(_PUA_RE.findall(text or ""))
    ratio = replacement / chars if chars else 0.0
    pages = sorted({int(n) for n in re.findall(r"\[\[page:(\d+)\]\]", text or "")})
    reasons = []
    if replacement:
        reasons.append("text_replacement_chars")
    terminal_count = sum(1 for line in (text or "").splitlines()
                         if _TERMINAL_FRAGMENT_RE.search(line.strip()))
    if terminal_count:
        reasons.append("terminal_fragment")
    action = "reject" if not text.strip() or (chars >= 50 and ratio >= 0.35) else ("warn" if reasons else "allow")
    return text, {"schema_version": 1, "action": action, "reasons": reasons,
                  "page_count": len(pages), "pages_with_markers": len(pages),
                  "usable_chars": chars, "replacement_count": replacement,
                  "replacement_ratio": ratio, "terminal_fragment_count": terminal_count}
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


def explain_parse_failure(filename: str, data: bytes) -> str:
    """空解析结果的失败原因（诊断细化——曾一律报"图片无法识别/不支持的
    格式"，老 .doc 改扩展名冒充 .docx 无从区分）"""
    if data[:4] == b"\xd0\xcf\x11\xe0":   # OLE 魔数：老 .doc/.xls 二进制
        return "检测到老 .doc 二进制格式（仅改扩展名不是真正的 docx）——请用 Word/WPS 打开后「另存为」docx 再上传"
    if data[:4] == b"PK\x03\x04":         # 真 docx（zip）但解析为空
        return "docx 解析不出内容（可能是空白文档、特殊加密或纯图片型文档）"
    if data[:5] == b"%PDF-":
        return "PDF 解析不出内容（扫描件图片无法识别，或文件损坏）"
    return "无法识别的文件格式"


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
    except Exception as e:
        # 解析异常必须可见（曾静默返空——上传失败无法区分"格式不对"和"解析 bug"）
        log.warning(f"docx 解析失败: {type(e).__name__}: {str(e)[:150]}")
        return ""


def _pdf_page_images(data: bytes) -> dict[int, list[bytes]]:
    """按页提取 PDF 内嵌图片；同页重复图片去重但保留页码关联。

    只返回符合尺寸门控的图片。跨页不去重：同一 logo 出现在不同的扫描页时，
    不能因为前一页已出现就让后一页失去 OCR 输入。
    """
    out: dict[int, list[bytes]] = {}
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for page_no, page in enumerate(reader.pages, 1):
            seen: set[str] = set()
            try:
                images = page.images
            except Exception:
                continue
            for img in images:
                raw = getattr(img, "data", None)
                if not raw or _MIN_IMAGE_BYTES > len(raw) or len(raw) > _MAX_IMAGE_BYTES:
                    continue
                digest = hashlib.md5(raw).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                out.setdefault(page_no, []).append(raw)
    except Exception:
        pass
    return out


def _pdf_page_diagnostics(text: str, image_rects: list[dict] | None = None,
                          page_width: float = 0.0,
                          page_height: float = 0.0) -> dict:
    """计算页面文字质量和图片覆盖率，供 OCR 闸门使用。"""
    text = (text or "").strip()
    rects: list[tuple[float, float, float, float]] = []
    for r in image_rects or []:
        try:
            x0 = max(0.0, min(float(r["x0"]), page_width))
            x1 = max(0.0, min(float(r["x1"]), page_width))
            y0 = max(0.0, min(float(r["y0"]), page_height))
            y1 = max(0.0, min(float(r["y1"]), page_height))
            if x1 > x0 and y1 > y0:
                rects.append((x0, x1, y0, y1))
        except (KeyError, TypeError, ValueError):
            continue
    area = max(0.0, page_width) * max(0.0, page_height)
    union = 0.0
    # x-slab union: exact for axis-aligned rectangles, overlap counted once.
    if area and rects:
        xs = sorted({x for r in rects for x in (r[0], r[1])})
        for left, right in zip(xs, xs[1:]):
            ys = sorted((r[2], r[3]) for r in rects if r[0] < right and r[1] > left)
            covered = 0.0
            end = None
            for start, stop in ys:
                if end is not None and start > end:
                    covered += end - current
                    current = start
                if end is None or stop > end:
                    if end is None:
                        current = start
                    end = stop
            if end is not None:
                covered += end - current
            union += (right - left) * covered
    coverage = min(1.0, union / area) if area else 0.0
    garbled = _is_garbled(text)
    chars = len(re.sub(r"\s+", "", text))
    return {"text_chars": chars, "garbled": garbled,
            "garbled_ratio": (len(_PUA_RE.findall(text)) / len(text) if text else 0.0),
            "embedded_image_count": len(rects), "image_coverage": coverage,
            "needs_ocr": _should_ocr_page(chars, garbled, coverage, len(rects))}


def _should_ocr_page(text_chars: int, garbled: bool, image_coverage: float,
                     image_count: int) -> bool:
    """文字不足/乱码且有实质图片覆盖时才触发 OCR。"""
    if not image_count:
        return False
    return garbled or text_chars < _MIN_TEXT_CHARS and image_coverage >= _MIN_IMAGE_COVERAGE

_CLOSING_CHARS = "。！？；：:，,、）)]】》」』"
_FRAGMENT_RE = re.compile(r"^[一-龥A-Za-z]{1,6}$")


_TERMINAL_FRAGMENT_RE = re.compile(
    r"(?:进行|完成|根据|按照|通过|可|应|需|并|及|与|或|如|在|对|为|是|有|从|将|以|于|由|被|其|该|各|每|若|当|如果|因为|所以|其中|同时|以及|包括|采用|达到|保持|减少|增加|控制|限制|保证|建议|注意|避免|选择|采纳|调整|合理)$"
)


_FOOTER_PAGE_RE = re.compile(r"^(?:page\s*\d+|第\s*\d+\s*页|\d+\s*页|\d+|[ivxlcdm]+)$", re.I)


def _strip_terminal_footer_lines(text: str) -> str:
    """移除页末通用页码行；只处理明确的页面家具形态。"""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    removed = 0
    while lines and _FOOTER_PAGE_RE.fullmatch(lines[-1].strip()):
        lines.pop()
        removed += 1
    if removed:
        log.debug("pdf_footer_lines_removed count=%d", removed)
    return "\n".join(lines)
def _drop_incomplete_terminal_lines(text: str) -> str:
    """丢弃页内末尾独立残句，保留同页前面的完整内容。"""
    lines = _strip_terminal_footer_lines(text).splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        return text
    last = lines[-1].strip()
    if (not last or len(last) > 80 or last.endswith(tuple(_CLOSING_CHARS))
            or last.startswith(("#", "-", "*", "•"))
            or last.isdigit() or not _TERMINAL_FRAGMENT_RE.search(last)):
        return text
    lines.pop()
    log.debug("pdf_terminal_fragment_dropped chars=%d", len(last))
    return "\n".join(lines)
def _repair_terminal_fragments(page_texts: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """修复高置信跨页残片并清理页内末尾残句。"""
    repaired = list(page_texts)
    merged = 0
    for idx in range(len(repaired)):
        page_no, text = repaired[idx]
        text = _drop_incomplete_terminal_lines(text)
        repaired[idx] = (page_no, text)
        if idx + 1 >= len(repaired):
            continue
        next_no, next_text = repaired[idx + 1]
        repaired[idx] = (page_no, text)
        lines = text.splitlines()
        if not lines or not next_text.strip():
            continue
        tail = lines[-1].strip()
        if tail.endswith(tuple(_CLOSING_CHARS)) or _is_garbled(tail):
            continue
        suffix_match = re.search(r"([一-龥A-Za-z]{1,3})$", tail)
        if not suffix_match or len(tail) - len(suffix_match.group(1)) < 2:
            continue
        prefix = tail[:-len(suffix_match.group(1))]
        suffix = suffix_match.group(1)
        if not prefix.strip():
            continue
        # 仅在短片段明显是句子续接（下一页不是标题/列表/页码）时合并。
        first = next_text.lstrip().splitlines()[0].strip()
        if (not first or first.isdigit() or first.startswith(("#", "-", "*", "•"))):
            continue
        repaired[idx] = (page_no, "\n".join(lines[:-1] + [prefix + suffix + first]))
        repaired[idx + 1] = (next_no, "\n".join(next_text.lstrip().splitlines()[1:]))
        merged += 1
    if merged:
        log.debug("pdf_terminal_fragment_repaired merged=%d", merged)
    return repaired
def _assemble_pdf_pages(page_texts: list[tuple[int, str]],
                        page_images: dict[int, list[bytes]],
                        page_metrics: dict[int, dict] | None = None) -> str:
    """按页质量闸门组装 PDF 文本；正常文字页不调用 OCR。"""
    parts: list[str] = []
    ocr_used = 0
    for page_no, text in page_texts:
        text = (text or "").strip()
        metrics = (page_metrics or {}).get(page_no)
        needs_ocr = (metrics.get("needs_ocr", False) if metrics is not None
                     else (not text or _is_garbled(text)))
        page_parts: list[str] = []
        if text and not _is_garbled(text):
            page_parts.append(text)
        elif needs_ocr:
            for idx, image in enumerate(page_images.get(page_no, []), 1):
                if ocr_used >= _MAX_IMAGE_OCR:
                    break
                ocr_used += 1
                ocr = _ocr_image(image)
                if ocr:
                    page_parts.append(f"【图片{idx}】{ocr}")
        if page_parts:
            parts.append(f"[[page:{page_no}]]\n" + "\n".join(page_parts))
    return "\n".join(parts)


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
        log.warning(f"图片 OCR 失败（跳过该图）: {e!r}")
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
    """逐页解析 PDF；正常文字页跳过 OCR，空页/乱码页才识别内嵌图片。"""
    page_texts: list[tuple[int, str]] = []
    page_metrics: dict[int, dict] = {}
    try:
        import pdfplumber
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
                    text = "\n".join(_with_title_marks(line_sizes))
                    image_rects = getattr(page, "images", []) or []
                    metrics = _pdf_page_diagnostics(
                        text, image_rects, float(page.width), float(page.height))
                    page_metrics[i] = metrics
                    log.debug("pdf_page_diagnostic page=%d text_chars=%d images=%d coverage=%.3f needs_ocr=%s",
                              i, metrics["text_chars"], metrics["embedded_image_count"],
                              metrics["image_coverage"], metrics["needs_ocr"])
                    page_texts.append((i, text))
                except Exception:
                    page_metrics[i] = _pdf_page_diagnostics("", [], 0, 0)
                    page_texts.append((i, ""))
        result = _assemble_pdf_pages(_repair_terminal_fragments(page_texts), _pdf_page_images(data), page_metrics)
        if result:
            return result
    except Exception as e:
        log.warning("pdfplumber 解析失败，回退 pypdf: %s", type(e).__name__)

    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        page_texts = [(i, page.extract_text() or "")
                      for i, page in enumerate(reader.pages, 1)]
        result = _assemble_pdf_pages(_repair_terminal_fragments(page_texts), _pdf_page_images(data), page_metrics)
        if result:
            return result
    except Exception as e:
        log.warning("pypdf 解析失败: %s", type(e).__name__)
    return ""
