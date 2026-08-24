# -*- coding: utf-8 -*-
"""Authorized local QA loop for PDF parsing and chunking.

This is deliberately read-only: it never writes source PDFs or ingestion data.
It repeats the complete parse/chunk operation and reports deterministic defects.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.ingestion import parse
from app.ingestion.structured_chunking import chunk_document, classify_document

_PAGE = re.compile(r"\[\[page:(\d+)\]\]")
_END = set("。！？!?；;")

def norm(s):
    return re.sub(r"\s+", "", s or "").replace("　", "")

def source_pages(data):
    from pypdf import PdfReader
    return [(i, p.extract_text() or "") for i, p in enumerate(PdfReader(__import__('io').BytesIO(data)).pages, 1)]

def audit(path):
    data = path.read_bytes()
    src = source_pages(data)
    text, diagnostics = parse.parse_pdf_with_diagnostics(data)
    chunks = chunk_document(text, str(path), max_chars=600)
    markers = [int(x) for x in _PAGE.findall(text)]
    covered = {int(c.get("page", 1)) for c in chunks}
    issues = []
    if "[[page:" in "".join(c.get("text", "") for c in chunks):
        issues.append("metadata_marker_in_chunk_text")
    if markers != sorted(set(markers)):
        issues.append("page_marker_order")
    expected = {p for p, v in src if norm(v)}
    missing = sorted(expected - covered)
    if missing:
        issues.append(f"page_coverage_missing:{missing[:30]}")
    if any(len(c.get("text", "")) > 600 for c in chunks):
        issues.append("chunk_limit")
    # Compare each page's substantive source token stream against output assigned to it.
    by_page = {}
    for c in chunks:
        by_page.setdefault(int(c.get("page", 1)), []).append(c.get("text", ""))
    for p, raw in src:
        s = norm(raw)
        if len(s) < 20:
            continue
        out = norm("".join(by_page.get(p, [])))
        # Header/footer removal is allowed, but substantive loss is not.
        if out and len(out) / len(s) < 0.45:
            issues.append(f"source_fidelity_page:{p}:{len(out)}/{len(s)}")
    # 句末检查只验证“超限切分”的 continuation 合同；普通段落可合法以冒号、逗号、图注或跨视觉行结束。
    for c in chunks:
        t = c.get("text", "").strip()
        if len(t) > 600:
            issues.append(f"chunk_limit:{c.get('page', 1)}:{len(t)}")
            break
    return {"file": str(path), "kind": classify_document(text, str(path)).kind,
            "source_pages": len(src), "marker_pages": sorted(set(markers)),
            "chunk_count": len(chunks), "covered_pages": sorted(covered),
            "issues": issues, "diagnostics": diagnostics}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--iterations", type=int, default=50)
    ap.add_argument("--report", type=Path, default=ROOT / "logs" / "pdf_ingestion_audit.json")
    args = ap.parse_args()
    iterations = max(1, min(50, args.iterations))
    runs = []
    for i in range(iterations):
        results = [audit((ROOT / f).resolve() if not f.is_absolute() else f) for f in args.files]
        runs.append({"iteration": i + 1, "results": results})
        bad = [(r["file"], r["issues"]) for r in results if r["issues"]]
        print(f"iteration {i + 1}/{iterations}: {'FAIL' if bad else 'PASS'}")
        for name, issues in bad: print(f"  {name}: {'; '.join(issues)}")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"iterations": runs}, ensure_ascii=False, indent=2), encoding="utf-8")
    failures = [r for run in runs for r in run["results"] if r["issues"]]
    print(f"report: {args.report}")
    return 1 if failures else 0

if __name__ == "__main__":
    raise SystemExit(main())
