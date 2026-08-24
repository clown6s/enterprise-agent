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
        text, diagnostics = parse.parse_pdf_with_diagnostics(data)
        c = classify_document(text, path)
        if c.kind == 'technical_book':
            text, ocr_diagnostics = parse.parse_technical_figures(data, text)
            diagnostics = {**(diagnostics or {}), 'technical_figure_ocr': ocr_diagnostics}
    else:
        text, diagnostics = parse.parse_bytes(name, data), None
    return path, data, text, diagnostics
