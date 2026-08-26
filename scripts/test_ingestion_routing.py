# -*- coding: utf-8 -*-
"""离线验证上传主流程的分类门控，不调用 OCR 或外部服务。"""
from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))

from app.ingestion import pipeline
from app.ingestion.structured_chunking import Classification


class IngestionRoutingTest(unittest.TestCase):
    def test_ordinary_pdf_uses_local_text_and_skips_vl(self):
        with patch.object(pipeline.parse, "parse_pdf_with_diagnostics",
                          return_value=("普通法规正文", {"action": "allow"})), \
             patch.object(pipeline, "classify_document",
                          return_value=Classification("legal", .9, ("article_structure",))), \
             patch("app.ingestion.ocr_paddle.create_document_task") as create:
            result = pipeline.ingest("law.pdf", b"pdf", title="law")
        self.assertNotEqual(result.get("pending"), True)
        create.assert_not_called()

    def test_technical_pdf_creates_task_before_parsing_result(self):
        task = {"status": "pending", "task_id": "task-test"}
        with patch.object(pipeline.parse, "parse_pdf_with_diagnostics",
                          return_value=("API Agent 代码 模块", {"action": "allow"})), \
             patch.object(pipeline, "classify_document",
                          return_value=Classification("technical_book", .95, ("technical_signals",))), \
             patch("app.ingestion.ocr_paddle.create_document_task", return_value=task) as create, \
             patch("app.ingestion.parse.parse_document_with_vl") as parse_vl:
            result = pipeline.ingest("tech.pdf", b"pdf", title="tech")
        self.assertTrue(result.get("pending"))
        self.assertEqual(result.get("task_id"), "task-test")
        create.assert_called_once()
        parse_vl.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
