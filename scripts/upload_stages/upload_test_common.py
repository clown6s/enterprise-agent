# -*- coding: utf-8 -*-
"""只读上传阶段公共 helper。"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from app.ingestion import parse
from app.ingestion.structured_chunking import classify_document, chunk_document

FILE_PATH = r"D:\datum\AI-Agents-in-Depth-zh-CN.pdf"


def get_file():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--file", default="")
    path = ap.parse_args().file or FILE_PATH
    with open(path, "rb") as f:
        return path, f.read()


def parsed():
    path, data = get_file()
    name = os.path.basename(path)
    if name.lower().endswith(".pdf"):
        # 先用本地文字层做确定性分类；只有技术书才整份送 VL。
        local_text, diagnostics = parse.parse_pdf_with_diagnostics(data)
        profile = classify_document(local_text, path)
        if profile.kind == "technical_book":
            from app.ingestion import ocr_paddle
            cache_path = ocr_paddle._cache_path(data)
            task_path = cache_path.with_suffix(".task.json")
            if cache_path.is_file():
                blocks = ocr_paddle.load_blocks_from_file(str(cache_path))
                text = ocr_paddle.assemble_text(blocks)
                diagnostics = {"vl_cache": True, "vl_blocks": len(blocks)}
                print(f"技术文档使用本地 VL 缓存: {cache_path}")
            elif task_path.is_file():
                ocr_paddle.query_document_debug(data)
                text, diagnostics = "", {"debug_only": True, "profile": profile.kind}
            else:
                ocr_paddle.request_document_debug(data, name)
                text, diagnostics = "", {"debug_only": True, "profile": profile.kind}
        else:
            text = local_text
            diagnostics = {**(diagnostics or {}), "vl_skipped": True, "profile": profile.kind}
    elif name.lower().endswith(".docx"):
        # DOCX 先本地解析分类；只有 technical_book 才二次送 VL。
        local_text = parse.parse_bytes(name, data)
        profile = classify_document(local_text, path)
        if profile.kind == "technical_book":
            text, layout_blocks = parse.parse_document_with_vl(name, data)
            diagnostics = {"vl_blocks": len(layout_blocks), "profile": profile.kind}
        else:
            text = local_text
            diagnostics = {"vl_skipped": True, "profile": profile.kind}
    elif name.lower().endswith((".png", ".jpg", ".jpeg")):
        # 独立图片没有可供分类的文字层，才直接使用 VL。
        text, layout_blocks = parse.parse_document_with_vl(name, data)
        diagnostics = {"vl_blocks": len(layout_blocks), "vl_enabled": True}
    else:
        text, diagnostics = parse.parse_bytes(name, data), None
    return path, data, text, diagnostics
