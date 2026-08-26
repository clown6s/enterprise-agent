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
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)
_ENDPOINT = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task"
_QUERY_ENDPOINT = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task/query"


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




def _token() -> str:
    from ..config import BAIDU_OCR_ACCESS_TOKEN as token
    return token


def _ensure_task_table() -> None:
    from ..mysql_store import transaction
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS document_ocr_task (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                document_sha256 CHAR(64) NOT NULL,
                file_name VARCHAR(512) NOT NULL,
                provider VARCHAR(64) NOT NULL DEFAULT 'baidu_paddleocr_vl',
                task_id VARCHAR(128) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                create_response JSON NULL,
                query_response JSON NULL,
                parse_result JSON NULL,
                parse_result_url TEXT NULL,
                error_message TEXT NULL,
                query_count INT UNSIGNED NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                completed_at DATETIME NULL,
                PRIMARY KEY (id), UNIQUE KEY uk_document_sha256 (document_sha256),
                UNIQUE KEY uk_task_id (task_id), KEY idx_status_updated (status, updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")


def _db_task(data: bytes) -> dict | None:
    try:
        _ensure_task_table()
        from ..mysql_store import connection
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT task_id,status,parse_result,parse_result_url FROM document_ocr_task WHERE document_sha256=%s", (hashlib.sha256(data).hexdigest(),))
                row = cur.fetchone()
        return ({"task_id": row[0], "status": row[1], "parse_result": row[2], "parse_result_url": row[3]} if row else None)
    except Exception as exc:
        log.warning("OCR 任务数据库查询失败: %s: %s", type(exc).__name__, exc)
        return None


def _db_create(data: bytes, filename: str, payload: dict, task_id: str) -> None:
    sha = hashlib.sha256(data).hexdigest()
    from ..mysql_store import transaction
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO document_ocr_task
                (document_sha256,file_name,provider,task_id,status,create_response)
                VALUES (%s,%s,'baidu_paddleocr_vl',%s,'pending',%s)
                ON DUPLICATE KEY UPDATE task_id=VALUES(task_id),
                create_response=VALUES(create_response),status='pending',updated_at=CURRENT_TIMESTAMP""",
                (sha, os.path.basename(filename), task_id, json.dumps(payload, ensure_ascii=False)))


def _db_status(value: str) -> str:
    value = str(value or "").lower()
    if value in {"success", "succeeded", "done", "completed", "complete"}:
        return "success"
    if value in {"failed", "failure", "error"}:
        return "failed"
    if value in {"processing", "running", "in_progress"}:
        return "processing"
    return "pending"


def _db_update_query(data: bytes, payload: dict, result_url: str = "", result: dict | None = None) -> None:
    from ..mysql_store import transaction
    with transaction() as conn:
        status = _db_status(payload.get("status", "pending"))
        with conn.cursor() as cur:
            cur.execute("""UPDATE document_ocr_task SET status=%s,query_response=%s,
                parse_result_url=COALESCE(NULLIF(%s,''),parse_result_url),
                parse_result=COALESCE(%s,parse_result),query_count=query_count+1,
                error_message=%s,completed_at=IF(%s='success',CURRENT_TIMESTAMP,completed_at)
                WHERE document_sha256=%s""", (status, json.dumps(payload, ensure_ascii=False), result_url,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                payload.get("task_error"), status, hashlib.sha256(data).hexdigest()))


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


def create_document_task(data: bytes, filename: str = "") -> dict:
    """只创建异步任务并持久化 task_id，不轮询、不下载结果。"""
    db = _db_task(data)
    if db and db.get("task_id"):
        return {"status": db.get("status", "pending"), "task_id": db["task_id"], "reused": True}
    token = _token()
    if not token:
        raise RuntimeError("缺少 BAIDU_OCR_ACCESS_TOKEN")
    payload = _request_task(data, filename, token)
    task = payload.get("result") or payload
    task_id = str(task.get("task_id") or "")
    if not task_id:
        raise RuntimeError("PaddleOCR-VL 创建任务未返回 task_id")
    try:
        _db_create(data, filename, payload, task_id)
    except Exception as exc:
        log.warning("OCR 任务数据库写入失败: %s", type(exc).__name__)
    return {"status": "pending", "task_id": task_id, "created": True}


def request_document_debug(data: bytes, filename: str = "") -> dict:
    """兼容旧调试入口，改为只创建任务。"""
    result = create_document_task(data, filename)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def analyze_document(data: bytes, filename: str = "") -> list[VLBlock]:
    """优先使用数据库/本地完整结果；没有结果才不应直接重复提交。"""
    db = _db_task(data)
    if db and db.get("parse_result"):
        result = db["parse_result"]
        if isinstance(result, str):
            result = json.loads(result)
        return _normalize(result)
    raise RuntimeError("OCR 任务尚未完成，请先调用 query_document_task")


def assemble_text(blocks: list[VLBlock]) -> str:
    return "\n".join((b.markdown or b.text).strip() for b in blocks if (b.markdown or b.text).strip())


def recognize_image(image: bytes, page: int = 1) -> str:
    return assemble_text(analyze_document(image, ".png"))


def query_task_by_id(task_id: str) -> dict:
    """按 task_id 查询并持久化状态，供独立接口使用。"""
    from ..mysql_store import transaction
    token = _token()
    if not token:
        raise RuntimeError("缺少 BAIDU_OCR_ACCESS_TOKEN")
    payload = _query_task(task_id, token)
    status = _db_status(payload.get("status", "pending"))
    result = None
    if status == "success" and payload.get("parse_result_url"):
        result = _download_result(payload["parse_result_url"])
    with transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("""UPDATE document_ocr_task SET status=%s, query_response=%s,
                parse_result_url=COALESCE(NULLIF(%s,''),parse_result_url),
                parse_result=COALESCE(%s,parse_result), query_count=query_count+1,
                error_message=%s, completed_at=IF(%s='success',CURRENT_TIMESTAMP,completed_at)
                WHERE task_id=%s""", (status, json.dumps(payload, ensure_ascii=False),
                payload.get("parse_result_url", ""),
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                payload.get("task_error"), status, task_id))
    return {"status": status, "task_id": task_id, "cached": result is not None}


def query_document_task(data: bytes) -> dict:
    """只查询数据库中的 task_id；成功后把完整结果写回 parse_result。"""
    db = _db_task(data)
    if not db:
        raise RuntimeError("数据库中没有 OCR 任务，请先创建任务")
    if db.get("parse_result"):
        return {"status": "success", "task_id": db.get("task_id"), "cached": True}
    task_id = db.get("task_id")
    token = _token()
    if not task_id or not token:
        raise RuntimeError("数据库任务或 BAIDU_OCR_ACCESS_TOKEN 缺失")
    payload = _query_task(task_id, token)
    raw_status = str(payload.get("status", "pending"))
    status = _db_status(raw_status)
    result = None
    if status == "success" and payload.get("parse_result_url"):
        result = _download_result(payload["parse_result_url"])
    _db_update_query(data, payload, payload.get("parse_result_url", ""), result)
    return {"status": status, "task_id": task_id, "cached": result is not None}


def query_document_debug(data: bytes) -> dict:
    result = query_document_task(data)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result
