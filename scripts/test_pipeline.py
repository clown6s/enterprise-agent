# -*- coding: utf-8 -*-
"""
摄入管线测试（ingestion/pipeline.py）——补 16% 覆盖率缺口。

测试对象：chunk_text（切分核心）/ _archive_original / _find_duplicate /
_prune_stale / _record_ingested / _log / ingest（mock 嵌入+Milvus 端到端）。

策略：纯函数全离线测；_find_duplicate 用 monkeypatch 控 milvus_store.has_document；
ingest 端到端 mock embeddings.embed_batch + milvus_store.insert_documents，用 txt 文件
走真实解析+切分，断言入库行数/页码/去重。临时文件用 tmp 目录 + 测试后清理。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ingestion import pipeline as P  # noqa: E402
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


# ---------------- chunk_text：切分核心（离线，纯函数） ----------------

def test_chunk_title_boundary():
    """「## 标题」行作为块边界：标题成为新块开头，前块先 flush"""
    text = "前言内容第一段。\n## 第一章 标题\n第一章正文。"
    chunks, pages = P.chunk_text(text, max_chars=600, overlap=0)
    # 前言单独一块，标题块以 ## 开头
    check("标题分块数>=2", len(chunks) >= 2)
    title_chunk = next((c for c in chunks if c.startswith("## 第一章")), None)
    check("标题成新块开头", title_chunk is not None and "第一章正文" in title_chunk)
    check("标题块不含前言", title_chunk and "前言内容" not in title_chunk)


def test_chunk_page_mark():
    """[[page:N]] 标记追踪页码"""
    text = "[[page:3]]\n第三页内容。\n[[page:5]]\n第五页内容。"
    chunks, pages = P.chunk_text(text, max_chars=600, overlap=0)
    check("页码追踪", pages[0] == 3 and pages[-1] == 5, str(pages))
    # page 标记本身不应进 chunk 文本
    check("page标记不入文本", all("[[page:" not in c for c in chunks))


def test_chunk_page_footer_filter():
    """纯数字短块（页脚页码）丢弃——len<4 且 isdigit"""
    text = "正文段落内容。\n212\n下一段内容。"
    chunks, pages = P.chunk_text(text, max_chars=600, overlap=0)
    check("页脚数字块丢弃", all(c.strip() != "212" for c in chunks), str(chunks))


def test_chunk_aggregation():
    """多个短段落聚合到一块（<= max_chars）"""
    paras = [f"第{i}段内容。" for i in range(5)]
    text = "\n".join(paras)
    chunks, _ = P.chunk_text(text, max_chars=600, overlap=0)
    check("短段聚合一块", len(chunks) == 1 and "第0段" in chunks[0] and "第4段" in chunks[0])


def test_chunk_long_para_sentence_break():
    """超长段落按句号断；断点带 overlap 续接"""
    # 每句 20 字，造一段超过 max_chars 的长文
    sentence = "这是一句完整的话。" * 40   # ~400 字
    chunks, _ = P.chunk_text(sentence, max_chars=100, overlap=10)
    check("长段拆多块", len(chunks) > 1)
    check("拆块不超 max_chars 太多", all(len(c) <= 120 for c in chunks))
    # 新切分器按完整句边界切分，不复制字符尾部；重复由检索层处理，
    # 避免相邻 chunk 出现“半句重复”的假丢文。
    if len(chunks) >= 2:
        check("默认不复制字符 overlap", not chunks[1].startswith(chunks[0][-10:]),
              f"tail={chunks[0][-10:]!r} head={chunks[1][:12]!r}")


def test_chunk_empty():
    """空文本/纯空白 → 空结果不崩"""
    chunks, pages = P.chunk_text("", max_chars=600, overlap=0)
    check("空文本不崩", chunks == [] and pages == [])




def test_technical_book_toc_filtered():
    """技术书目录页不入检索块，正文页及正文中的“目录”仍保留。"""
    from app.ingestion.structured_chunking import chunk_technical_book
    text = ("[[page:1]]\n前言正文。\n"
            "[[page:2]]\n目录\n第一章 引言........9\n"
            "[[page:8]]\n第二章 实践........20\n"
            "[[page:9]]\n## 第一章 引言\n正文说明目录结构，但这是正文。\n"
            "def demo():\n    return '目录'\n")
    units = chunk_technical_book(text, max_chars=120)
    check("技术书目录不产出 toc", not any(u.get("kind") == "toc" for u in units))
    check("目录条目不进入正文块", not any("第一章 引言........9" in u["text"] for u in units))
    check("第1页正文保留", any(u.get("page") == 1 and "前言正文" in u["text"] for u in units))
    check("第9页正文保留", any(u.get("page") == 9 and "正文说明目录结构" in u["text"] for u in units))
    check("正文代码保留", any(u.get("kind") == "code" and "return '目录'" in u["text"] for u in units))
    check("目录过滤后长度合规", all(len(u["text"]) <= 120 for u in units))


def test_technical_book_pdf_whitespace():
    """PDF 视觉空格不应拆散正文术语，英文词间空格仍保留。"""
    from app.ingestion.structured_chunking import chunk_technical_book
    text = "[[page:9]]\n第二章（上下文工程）是关键。提示工程（Prompt        Engineering，包括流程化设计、工 具描述、业务规则细化）。"
    body = "\n".join(u["text"] for u in chunk_technical_book(text, max_chars=120))
    check("中文词内空格恢复", "工具描述" in body)
    check("英文术语空格保留", "Prompt Engineering" in body)


def test_technical_book_code_continuity():
    """代码按结构连续聚合，嵌套 JSON/括号尾行不能成为孤立块。"""
    from app.ingestion.structured_chunking import chunk_technical_book
    text = ("[[page:42]]\n"
            "from openai import OpenAI\nclient = OpenAI()\n"
            "tools = [{\n  \\\"type\\\": \\\"function\\\",\n  \\\"function\\\": {\n"
            "    \\\"name\\\": \\\"get_weather\\\",\n    \\\"parameters\\\": {\n"
            "      \\\"properties\\\": {\\\"city\\\": {\\\"type\\\": \\\"string\\\"}}\n"
            "    }\n  }\n}]\n"
            "def execute_tool(name, arguments):\n    return arguments\n"
            "正文从这里开始。")
    units = chunk_technical_book(text, max_chars=1000)
    codes = [u for u in units if u.get("kind") == "code"]
    body = "\n".join(u["text"] for u in codes)
    check("代码起始行保留", "from openai import OpenAI" in body and "client = OpenAI()" in body)
    check("嵌套结构连续", '"properties"' in body and "}]" in body)
    check("函数定义连续", "def execute_tool" in body and "return arguments" in body)
    check("无孤立结构尾块", not any(u["text"].strip() in {"]", "}", "},", ")]"} for u in codes))
    check("正文与代码分离", any(u.get("kind") == "prose" and "正文从这里开始" in u["text"] for u in units))

def test_build_vector_rows_preserves_unit_metadata():
    units = [{
        "text": "正文", "page": 2, "kind": "legal", "page_end": 3,
        "continuation": True, "continuation_of": "article-1",
        "continuation_index": 1, "section_path": ["第一章"],
    }]
    rows = P._build_vector_rows(units, [[0.1, 0.2]], "doc-1", "标题")
    row = rows[0]
    check("vector row保留基础字段", row["text"] == "正文" and row["page"] == 2
          and row["document_id"] == "doc-1")
    check("vector row保留全部unit元数据",
          all(row.get(k) == units[0][k] for k in (
              "kind", "page_end", "continuation", "continuation_of",
              "continuation_index", "section_path")))


def test_archive_original():
    tmp = tempfile.mkdtemp()
    orig_dir = P._UPLOADS_DIR
    P._UPLOADS_DIR = tmp
    try:
        P._archive_original("deadbeef", "报告.pdf", b"%PDF-CONTENT")
        path = os.path.join(tmp, "deadbeef.pdf")
        check("留底文件写入", os.path.exists(path))
        check("留底内容正确", open(path, "rb").read() == b"%PDF-CONTENT")

        # 超长扩展名（>8 字符，不匹配 ^[a-z0-9]{1,8}$）→ bin
        P._archive_original("cafe", "evil.abcdefghi", b"x")
        check("超长扩展名归一 bin", os.path.exists(os.path.join(tmp, "cafe.bin")))
    finally:
        P._UPLOADS_DIR = orig_dir
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- _find_duplicate / _prune_stale（mock has_document） ----------------

def test_find_duplicate():
    """去重注册表：无文件→None；残留→清理返None；命中→返回记录"""
    from app import milvus_store
    tmp = tempfile.mkdtemp()
    orig_file = P._INGESTED_FILE
    P._INGESTED_FILE = os.path.join(tmp, "ingested.jsonl")
    orig_has = milvus_store.has_document
    try:
        # 无文件
        check("无注册表 → None", P._find_duplicate("nope") is None)

        # 写入一条残留（has_document 返 False → 清理 → None）
        with open(P._INGESTED_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"sha": "stale1", "docId": "d1", "file": "x"}) + "\n")
        milvus_store.has_document = lambda d: False
        check("残留记录 → 清理返 None", P._find_duplicate("stale1") is None)
        check("残留被清理", not os.path.exists(P._INGESTED_FILE)
              or os.path.getsize(P._INGESTED_FILE) == 0)

        # 命中（has_document 返 True → 返回记录）
        with open(P._INGESTED_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"sha": "live1", "docId": "d2", "file": "y", "chunks": 5}) + "\n")
        milvus_store.has_document = lambda d: True
        r = P._find_duplicate("live1")
        check("命中返回记录", r is not None and r.get("docId") == "d2")
    finally:
        P._INGESTED_FILE = orig_file
        milvus_store.has_document = orig_has
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- ingest 端到端（mock 嵌入 + Milvus） ----------------

def test_ingest_end_to_end():
    """txt 文件走真实解析+切分，mock 嵌入与 Milvus 入库；断言返回 docId+chunks"""
    from app import embeddings, milvus_store
    tmp = tempfile.mkdtemp()
    orig_ingested = P._INGESTED_FILE
    orig_uploads = P._UPLOADS_DIR
    orig_log = P._LOG
    P._INGESTED_FILE = os.path.join(tmp, "ingested.jsonl")
    P._UPLOADS_DIR = tmp
    P._LOG = os.path.join(tmp, "ingestion.jsonl")
    orig_has = milvus_store.has_document
    orig_insert = milvus_store.insert_documents
    orig_embed = embeddings.embed_batch
    orig_embed_single = embeddings.embed
    try:
        milvus_store.has_document = lambda d: False
        captured = {"rows": None}
        milvus_store.insert_documents = lambda rows: (captured.__setitem__("rows", rows), True)[1]
        # mock 嵌入：每个 chunk 返回固定 3 维向量
        embeddings.embed_batch = lambda texts: [[0.1, 0.2, 0.3] for _ in texts]
        embeddings.embed = lambda t: [0.1, 0.2, 0.3]

        content = "## 测试标题\n" + ("这是一段测试内容。" * 10)
        data = content.encode("utf-8")
        r = P.ingest("测试.txt", data, title="测试文档")
        check("ingest 返回 docId", bool(r.get("documentId")))
        check("ingest 返回 chunks>0", r.get("chunks", 0) > 0, str(r))
        check("ingest 入库行数=chunks",
              captured["rows"] is not None
              and len(captured["rows"]) == r.get("chunks", -1))
        # 入库行带 page/title/document_id
        row = (captured["rows"] or [{}])[0]
        check("入库行带 document_id", "document_id" in row)
        check("入库行带 title", row.get("title") == "测试文档")
        # 注册表落库
        check("注册表落记录", os.path.exists(P._INGESTED_FILE) and os.path.getsize(P._INGESTED_FILE) > 0)

        # 同内容重复上传 → duplicate：第一次入库后 has_document 改返 True，
        # 否则去重查到记录却判定为残留（库空）→ 清理 → 不识别重复
        first_doc_id = r.get("documentId")
        milvus_store.has_document = lambda d: True
        r2 = P.ingest("测试.txt", data, title="测试文档")
        check("重复上传 duplicate=True", r2.get("duplicate") is True, str(r2))
        check("重复上传返回原 docId", r2.get("documentId") == first_doc_id,
              f"{r2.get('documentId')} vs {first_doc_id}")
    finally:
        P._INGESTED_FILE = orig_ingested
        P._UPLOADS_DIR = orig_uploads
        P._LOG = orig_log
        milvus_store.has_document = orig_has
        milvus_store.insert_documents = orig_insert
        embeddings.embed_batch = orig_embed
        embeddings.embed = orig_embed_single
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_ingest_parse_failure():
    """不可解析内容 → 返回 error（mock 防止真嵌入）"""
    from app import milvus_store
    orig_has = milvus_store.has_document
    tmp = tempfile.mkdtemp()
    orig_ingested = P._INGESTED_FILE
    P._INGESTED_FILE = os.path.join(tmp, "ingested.jsonl")
    try:
        milvus_store.has_document = lambda d: False
        # 空 txt → parse_bytes 返空 → error
        r = P.ingest("empty.txt", b"   \n  \n", title="空")
        check("空内容返 error", "error" in r, str(r))
    finally:
        P._INGESTED_FILE = orig_ingested
        milvus_store.has_document = orig_has
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_chunk_title_boundary()
    test_chunk_page_mark()
    test_chunk_page_footer_filter()
    test_chunk_aggregation()
    test_chunk_long_para_sentence_break()
    test_chunk_empty()
    test_technical_book_toc_filtered()
    test_technical_book_pdf_whitespace()
    test_technical_book_code_continuity()
    test_build_vector_rows_preserves_unit_metadata()
    test_archive_original()
    test_find_duplicate()
    test_ingest_end_to_end()
    test_ingest_parse_failure()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
