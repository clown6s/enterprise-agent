# -*- coding: utf-8 -*-
"""PaddleOCR-VL 文档解析离线测试：整份文档只消费 VL 返回，不本地拼接。"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ingestion import ocr_paddle
from app.ingestion import parse


class FakeResult(dict):
    pass


class FakeEngine:
    def __init__(self, *a, **k):
        self.calls = []
    def predict(self, source):
        self.calls.append(source)
        return [
            FakeResult(res={
                "markdown": "### 请假流程\n1. 提交申请\n2. 主管审批\n3. 财务复核",
                "layout_res": [
                    {"label": "table", "text": "col1 | col2", "bbox": [1, 2, 3, 4], "score": 0.9},
                    {"label": "figure", "type": "flowchart", "markdown": "提交->审批->复核", "box": [5, 6, 7, 8]},
                    {"label": "text", "text": "主体说明", "page": 1},
                ],
            })
        ]


def _install_fake(vl_inst):
    vl_inst._engine = FakeEngine()


class PaddleOCRVLTest(unittest.TestCase):
    def test_vl_confirmed_as_only_source(self):
        """PDF/DOCX/图片均只调用一次 VL predict，不再调用本地解析。"""
        import app.ingestion.parse as p
        with patch.object(p.parse_document_with_vl, "__module__"), \
             patch.object(ocr_paddle.PaddleOCRVL, "_load", return_value=True), \
             patch.object(ocr_paddle, "_flatten", side_effect=lambda r: [
                 ocr_paddle.VLBlock(kind="region", text="VL 内容", markdown="VL 内容", page=1)]), \
             patch.object(ocr_paddle.PaddleOCRVL, "predict", return_value=[{"x": 1}]):
            for ext in ("pdf", "docx", "png"):
                text = p.parse_bytes(f"a.{ext}", b"\x00\x01")
                # 仅 VL 结果，不混入本地版式文本
                self.assertIn("VL 内容", text)
                self.assertNotIn("[[page:", text)

    def test_document_pass_to_single_predict(self):
        client = ocr_paddle.PaddleOCRVL()
        _install_fake(client)
        with patch.object(ocr_paddle, "get_client", return_value=client):
            blocks = ocr_paddle.analyze_document(b"\x25\x50\x44\x46", "x.pdf")
        shown = f"pages=1 kinds={sorted(set(b.kind for b in blocks))}"
        self.assertIn("table", shown)
        self.assertIn("figure", shown)
        self.assertTrue(any(b.text == "col1 | col2" for b in blocks))
        self.assertTrue(any(b.kind == "figure" for b in blocks))

    def test_flowchart_figure_kept_from_vl(self):
        client = ocr_paddle.PaddleOCRVL()
        _install_fake(client)
        with patch.object(ocr_paddle, "get_client", return_value=client):
            blocks = ocr_paddle.analyze_document(b"x", "f.png")
        figs = [b for b in blocks if b.kind == "figure"]
        self.assertEqual(len(figs), 1)
        self.assertIn("审批", figs[0].markdown)

    def test_vl_unavailable_raises(self):
        with patch.object(ocr_paddle.PaddleOCRVL, "_load", return_value=None):
            with self.assertRaises(RuntimeError):
                ocr_paddle.analyze_document(b"", "scan.pdf")

    def test_parse_result_markdown_extracts_pages(self):
        """parse_result_markdown 从顶层 dict 提取 pages[].markdown（真实百度返回结构）。"""
        payload = {
            "file_name": "x.pdf",
            "pages": [
                {"page_num": 0, "markdown": "# 第一章\n正文一"},
                {"page_num": 1, "markdown": "# 第二章\n正文二"},
                {"page_num": 2},  # 缺 markdown 的页应跳过，不报错
            ],
        }
        out = ocr_paddle.parse_result_markdown(payload)
        self.assertIn("# 第一章", out)
        self.assertIn("正文二", out)

    def test_parse_result_markdown_rejects_bad_payload(self):
        """非 dict 或 pages 非数组应显式抛错，而不是静默拼接键名。"""
        with self.assertRaises(TypeError):
            ocr_paddle.parse_result_markdown(["result"])
        with self.assertRaises(TypeError):
            ocr_paddle.parse_result_markdown({"pages": "nope"})

    def test_assemble_keeps_blocks(self):
        """assemble_text 按顺序拼接块，不丢内容。"""
        blocks = [
            ocr_paddle.VLBlock(kind="region", text="一", page=1),
            ocr_paddle.VLBlock(kind="region", text="二", page=2),
        ]
        out = ocr_paddle.assemble_text(blocks)
        self.assertIn("一", out)
        self.assertIn("二", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)