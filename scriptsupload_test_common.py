# -*- coding: utf-8 -*-
"""上传阶段测试公共 helper；只读解析，不触碰外部服务。"""
import argparse
import os
import sys

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,ROOT)
from app.ingestion import parse
from app.ingestion.structured_chunking import classify_document, chunk_document

FILE_PATH = r"D:\datum\古代诗歌常用意象.txt"

def get_file():
    ap=argparse.ArgumentParser(add_help=True)
    ap.add_argument("--file", default="")
    args=ap.parse_args()
    path=args.file or FILE_PATH
    with open(path,'rb') as f: data=f.read()
    return path,data

def parsed():
    path,data=get_file(); name=os.path.basename(path)
    if name.lower().endswith('.pdf'):
        text, diagnostics=parse.parse_pdf_with_diagnostics(data)
    else:
        text=parse.parse_bytes(name,data); diagnostics=None
    return path,data,text,diagnostics

if __name__=='__main__':
    p,b,t,d=parsed(); print({'file':p,'bytes':len(b),'chars':len(t),'diagnostics':d})
