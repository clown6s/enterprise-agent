# -*- coding: utf-8 -*-
"""
文档解析测试（ingestion/parse.py）——补 10% 覆盖率缺口。

测试对象：_decode / _is_garbled / parse_bytes / explain_parse_failure /
_parse_csv / _with_title_marks / _parse_docx（内存构造）。

策略：纯函数全离线；docx 用 python-docx 在内存构造（标题+正文+表格），
测标题感知 + 表格 " | " 拼接，不依赖真实文件、不调 OCR（_ocr_image 走 mock）。
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ingestion import parse  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


# ---------------- _decode：编码逐级回退 ----------------

def test_decode():
    check("decode utf-8", parse._decode("中文".encode("utf-8")) == "中文")
    check("decode gbk", parse._decode("中文".encode("gbk")) == "中文")
    check("decode gb18030", parse._decode("中文".encode("gb18030")) == "中文")
    # 无法解码的字节 → ignore 不崩
    check("decode 损坏字节不崩", isinstance(parse._decode(b"\xff\xfe\x00"), str))


# ---------------- _is_garbled：PDF 乱码判定 ----------------

def test_is_garbled():
    check("正常文本非乱码", not parse._is_garbled("这是一段正常的中文文本，用于测试。"))
    check("空文本非乱码", not parse._is_garbled(""))
    # 私用区字符占多数 → 乱码
    check("私用区字符判乱码", parse._is_garbled("一二三"))


# ---------------- parse_bytes：扩展名分发 ----------------

def test_parse_bytes_dispatch():
    check("txt 直读", "你好" in parse.parse_bytes("a.txt", "你好".encode("utf-8")))
    check("md 直读", "标题" in parse.parse_bytes("a.md", "标题".encode("utf-8")))
    check("无扩展名当 txt", "无扩展" in parse.parse_bytes("nofmt", "无扩展".encode("utf-8")))
    # 不支持的类型 → 空
    check("不支持类型返空", parse.parse_bytes("a.pptx", b"whatever") == "")


# ---------------- explain_parse_failure：魔数诊断 ----------------

def test_explain_parse_failure():
    r = parse.explain_parse_failure("a.doc", b"\xd0\xcf\x11\xe0xxxx")
    check("OLE 识别 .doc", "老 .doc" in r, r)
    r = parse.explain_parse_failure("a.docx", b"PK\x03\x04")
    check("PK 识别 docx", "docx" in r, r)
    r = parse.explain_parse_failure("a.pdf", b"%PDF-1.4")
    check("%PDF 识别", "PDF" in r, r)
    r = parse.explain_parse_failure("a.xyz", b"\x00\x01")
    check("未知格式兜底", "无法识别" in r, r)


# ---------------- _parse_csv：分隔符自检 ----------------

def test_parse_csv():
    # 逗号分隔
    out = parse._parse_csv("姓名,年龄\n张三,30".encode("utf-8"))
    check("csv 逗号分隔", "姓名 | 年龄" in out and "张三 | 30" in out, out)
    # 制表符分隔
    out = parse._parse_csv("a\tb\n1\t2".encode("utf-8"))
    check("csv 制表符分隔", "a | b" in out, out)
    # GBK 编码
    out = parse._parse_csv("项目,金额\n打车,50".encode("gbk"))
    check("csv GBK 编码", "打车" in out, out)
    # 空内容
    check("csv 空内容", parse._parse_csv(b"  \n  ") == "")


# ---------------- _with_title_marks：版面标题判定 ----------------

def test_with_title_marks():
    # 大字号短行 → 标题（字号须 >= median×1.25）
    lines = [("第一章", 24.0), ("正文内容较长的一段话。", 12.0)]
    out = parse._with_title_marks(lines)
    check("大字号短行→标题", out[0] == "## 第一章", out)
    check("小字号→正文", out[1] == "正文内容较长的一段话。", out)
    # 纯数字行（页码）→ 不判标题（字号够大但因 isdigit 排除）
    out2 = parse._with_title_marks([("212", 24.0), ("正文", 12.0)])
    check("纯数字不判标题", out2[0] == "212", out2)
    # 空
    check("空输入不崩", parse._with_title_marks([]) == [])


# ---------------- _parse_docx：内存构造文档 ----------------

def test_parse_docx():
    """构造 docx（标题段+正文段+表格），断言标题打 ## + 表格 | 拼接"""
    try:
        import docx
    except ImportError:
        check("docx 跳过（无库）", True)
        return
    buf = io.BytesIO()
    doc = docx.Document()
    # 标题段（heading 样式）
    h = doc.add_paragraph("制度总则")
    h.style = doc.styles["Heading 1"]
    doc.add_paragraph("本制度适用于全体员工。")
    # 表格
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "项目"
    table.rows[0].cells[1].text = "金额"
    table.rows[1].cells[0].text = "打车"
    table.rows[1].cells[1].text = "50"
    doc.save(buf)
    out = parse._parse_docx(buf.getvalue())
    check("docx 标题感知 ##", "## 制度总则" in out, out[:80])
    check("docx 正文提取", "全体员工" in out)
    check("docx 表格 | 拼接", "项目 | 金额" in out and "打车 | 50" in out, out)


def test_parse_docx_invalid():
    """非 docx 字节 → 解析失败返空（不崩）"""
    check("非法 docx 返空", parse._parse_docx(b"not a docx") == "")


def test_pdf_page_ocr_gate():
    """正常页跳过 OCR，空/乱码页才 OCR，且结果保留页码。"""
    original = parse._ocr_image
    calls = []
    try:
        parse._ocr_image = lambda image: calls.append(image) or "扫描页文字"
        out = parse._assemble_pdf_pages(
            [(1, "正常电子文本"), (2, ""), (3, "一")],
            {1: [b"healthy"], 2: [b"blank"], 3: [b"garbled"]},
        )
    finally:
        parse._ocr_image = original
    check("正常文字页跳过 OCR", calls == [b"blank", b"garbled"], calls)
    check("OCR 结果保留页码", "[[page:2]]" in out and "[[page:3]]" in out, out)
    check("健康页保留文本", "[[page:1]]\n正常电子文本" in out, out)


def test_pdf_page_ocr_failure():
    """单页 OCR 失败不影响其他正常页。"""
    original = parse._ocr_image
    try:
        parse._ocr_image = lambda image: ""
        out = parse._assemble_pdf_pages(
            [(1, "正常电子文本"), (2, "")], {2: [b"failed"]})
    finally:
        parse._ocr_image = original
    check("OCR 失败保留健康页", "正常电子文本" in out and "[[page:2]]" not in out, out)


if __name__ == "__main__":
    test_is_garbled()
    test_parse_bytes_dispatch()
    test_explain_parse_failure()
    test_parse_csv()
    test_with_title_marks()
    test_parse_docx()
    test_parse_docx_invalid()
    test_pdf_page_ocr_gate()
    test_pdf_page_ocr_failure()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
