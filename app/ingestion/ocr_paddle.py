# -*- coding: utf-8 -*-
"""百度智能云 PaddleOCR-VL 文档解析 HTTP 客户端。

技术类 PDF/DOCX/图片整份提交云端解析；正文、表格、图片描述和版面信息只
来自 parse_result_url 返回的 JSON。原始 JSON 按文档 SHA256 缓存到 data/vl_cache。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)
_ENDPOINT = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task"
_QUERY_ENDPOINT = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task/query"
_CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "vl_cache"
_POLL_SECONDS = 2
_MAX_POLLS = 150


@dataclass
class VLBlock:
    kind: str
    text: str = ""
    markdown: str = ""
    page: int = 1
    bbox: list = field(default_factory=list)
    confidence: float = 0.0
    source: str = "baidu_paddleocr_vl"
    raw: Any = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "markdown": self.markdown,
                "page": self.page, "bbox": self.bbox, "confidence": self.confidence,
                "source": self.source}


def _cache_path(data: bytes) -> Path:
    return _CACHE_DIR / f"{hashlib.sha256(data).hexdigest()}.json"


def _load_cache(data: bytes) -> dict | None:
    path = _cache_path(data)
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("VL 缓存读取失败: %s", type(exc).__name__)
        return None


def _save_cache(data: bytes, payload: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_cache_path(data), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.warning("VL 缓存写入失败: %s", type(exc).__name__)


def _token() -> str:
    from ..config import BAIDU_OCR_ACCESS_TOKEN as token
    return token


def _request_task(data: bytes, filename: str, token: str) -> dict:
    encoded = base64.b64encode(data).decode("ascii")
    form = {
        "file_data": encoded,
        "file_name": os.path.basename(filename) or "document.pdf",
        "analysis_chart": "true",
        "merge_tables": "true",
        "relevel_titles": "true",
        "recognize_seal": "true",
        "return_span_boxes": "true",
    }
    response = httpx.post(
        _ENDPOINT, params={"access_token": token}, data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error_code"):
        raise RuntimeError(f"百度 PaddleOCR-VL 错误 {payload['error_code']}: {payload.get('error_msg', '')}")
    return payload


def _download_result(url: str) -> dict:
    response = httpx.get(url, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("PaddleOCR-VL parse_result_url 返回非 JSON 对象")
    return payload


def _query_task(task_id: str, token: str) -> dict:
    response = httpx.post(
        _QUERY_ENDPOINT, params={"access_token": token},
        data={"task_id": task_id},
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error_code"):
        raise RuntimeError(f"百度 PaddleOCR-VL 查询错误: {payload.get('error_msg', '')}")
    return payload.get("result") or payload


def _parse_result(task: dict, token: str) -> dict:
    current = task.get("result") or task
    status = str(current.get("status", ""))
    if status == "success":
        result_url = current.get("parse_result_url")
        if not result_url:
            raise RuntimeError("PaddleOCR-VL 成功但缺少 parse_result_url")
        return _download_result(result_url)
    if status == "failed":
        raise RuntimeError(str(current.get("task_error") or "PaddleOCR-VL 任务失败"))
    task_id = current.get("task_id")
    if not task_id:
        raise RuntimeError("PaddleOCR-VL 未返回 task_id")
    for _ in range(_MAX_POLLS):
        time.sleep(_POLL_SECONDS)
        current = _query_task(task_id, token)
        status = str(current.get("status", ""))
        if status == "success":
            result_url = current.get("parse_result_url")
            if not result_url:
                raise RuntimeError("PaddleOCR-VL 成功但缺少 parse_result_url")
            return _download_result(result_url)
        if status == "failed":
            raise RuntimeError(str(current.get("task_error") or "PaddleOCR-VL 任务失败"))
    raise TimeoutError("PaddleOCR-VL 任务轮询超时")


def _layout_kind(layout_type: str) -> str:
    if layout_type == "table": return "table"
    if layout_type in {"image", "chart", "figure_title", "header_image", "footer_image"}: return "figure"
    if layout_type in {"display_formula", "inline_formula", "formula_number"}: return "formula"
    if layout_type == "paragraph_title": return "heading"
    if layout_type == "content": return "toc"
    return "region"


def _normalize(payload: dict) -> list[VLBlock]:
    blocks: list[VLBlock] = []
    # 先收集哪些页有目录 layout，整页跳过
    toc_pages = set()
    for page in payload.get("pages", []):
        if any(l.get("type") == "content" for l in page.get("layouts", [])):
            toc_pages.add(int(page.get("page_num", 0)))
    for page in payload.get("pages", []):
        page_no = int(page.get("page_num", 1) or 1)
        if page_no in toc_pages:
            continue
        tables = {x.get("layout_id"): x for x in page.get("tables", [])}
        images = {x.get("layout_id"): x for x in page.get("images", [])}
        for layout in page.get("layouts", []):
            layout_id = layout.get("layout_id")
            kind = _layout_kind(str(layout.get("type", "text")))
            detail = tables.get(layout_id) or images.get(layout_id) or {}
            markdown = str(detail.get("markdown") or "").strip()
            description = str(detail.get("image_description") or "").strip()
            text = str(layout.get("text") or markdown or description).strip()
            blocks.append(VLBlock(kind=kind, text=text, markdown=markdown or description,
                                   page=page_no, bbox=layout.get("position") or detail.get("position") or [],
                                   raw={"page": page, "layout": layout, "detail": detail}))
        if page.get("text") and not page.get("layouts"):
            blocks.append(VLBlock(kind="region", text=str(page["text"]), page=page_no, raw=page))
    return blocks


def request_document_debug(data: bytes, filename: str = "") -> dict:
    """首次联调用：提交一次文件并保存创建任务响应。"""
    token = _token()
    if not token:
        raise RuntimeError("缺少 BAIDU_OCR_ACCESS_TOKEN")
    payload = _request_task(data, filename, token)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    debug_path = _CACHE_DIR / f"{hashlib.sha256(data).hexdigest()}.task.json"
    with open(debug_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n原始任务响应已保存: {debug_path}")
    return payload


def analyze_document(data: bytes, filename: str = "") -> list[VLBlock]:
    """整份文件 HTTP 提交百度 PaddleOCR-VL，并缓存完整 parse result JSON。"""
    cached = _load_cache(data)
    if cached is not None:
        return _normalize(cached)
    token = _token()
    if not token:
        raise RuntimeError("缺少 BAIDU_OCR_ACCESS_TOKEN")
    task = _request_task(data, filename, token)
    result = _parse_result(task.get("result") or task, token)
    _save_cache(data, result)
    return _normalize(result)


def assemble_text(blocks: list[VLBlock]) -> str:
    return "\n".join((b.markdown or b.text).strip() for b in blocks if (b.markdown or b.text).strip())


def recognize_image(image: bytes, page: int = 1) -> str:
    return assemble_text(analyze_document(image, ".png"))


def load_blocks_from_file(filepath: str) -> list[VLBlock]:
    with open(filepath, encoding="utf-8") as f:
        return _normalize(json.load(f))


def query_document_debug(data: bytes) -> dict:
    """后续联调用：只查询已保存 task_id；成功即下载并缓存完整结果。"""
    task_path = _cache_path(data).with_suffix(".task.json")
    if not task_path.is_file():
        raise RuntimeError(f"没有已保存的任务响应：{task_path}；请先执行一次首次提交")
    with open(task_path, encoding="utf-8") as f:
        created = json.load(f)
    task = created.get("result") or created
    task_id = task.get("task_id")
    if not task_id:
        raise RuntimeError(f"任务响应缺少 task_id：{task_path}")
    token = _token()
    if not token:
        raise RuntimeError("缺少 BAIDU_OCR_ACCESS_TOKEN")
    payload = _query_task(task_id, token)
    query_path = task_path.with_suffix(".query.json")
    with open(query_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n任务查询响应已保存: {query_path}")
    if str(payload.get("status")) == "success" and payload.get("parse_result_url"):
        result = _download_result(payload["parse_result_url"])
        _save_cache(data, result)
        print(f"解析结果已缓存: {_cache_path(data)}")
    return payload
