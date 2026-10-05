"""Small, explicitly artificial fixtures for demonstrating file organization."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from exporter import export_snapshot
from zotero_api import Library, Snapshot


def sample_pdf(label: str) -> bytes:
    """A valid one-page PDF saying that it is a demonstration, not a paper."""
    text = label.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = ("BT /F1 18 Tf 50 740 Td (" + text + ") Tj "
               "0 -40 Td /F1 11 Tf (Demonstration file. Not research evidence.) Tj ET").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += str(number).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
    xref = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        result += f"{offset:010d} 00000 n \n".encode()
    result += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    return result


def make_snapshot() -> Snapshot:
    collections = [
        {"key": "COLLA001", "data": {"name": "研究主题", "parentCollection": False}},
        {"key": "COLLX001", "data": {"name": "子分类", "parentCollection": "COLLA001"}},
        {"key": "COLLB001", "data": {"name": "研究方法", "parentCollection": False}},
    ]
    items = [
        {"key": "ITEMA001", "data": {"itemType": "journalArticle", "title": "示例论文 A（演示文件）",
          "date": "2024", "creators": [{"lastName": "Example"}], "collections": ["COLLX001", "COLLB001"]}},
        {"key": "ITEMB001", "data": {"itemType": "journalArticle", "title": "示例论文 B（演示文件）",
          "date": "2025", "creators": [{"lastName": "Sample"}], "collections": ["COLLA001"]}},
        {"key": "PDFU0001", "data": {"itemType": "attachment", "title": "未分类示例（演示文件）",
          "contentType": "application/pdf", "filename": "Unfiled.pdf", "collections": [], "linkMode": "imported_file"}},
    ]
    attachments = [
        {"key": "PDFA0001", "data": {"itemType": "attachment", "parentItem": "ITEMA001",
          "contentType": "application/pdf", "filename": "Paper A.pdf", "linkMode": "imported_file"}},
        {"key": "PDFB0001", "data": {"itemType": "attachment", "parentItem": "ITEMB001",
          "contentType": "application/pdf", "filename": "Paper B.pdf", "linkMode": "imported_file"}},
        items[2],
        {"key": "IMAGE001", "data": {"itemType": "attachment", "parentItem": "ITEMA001",
          "contentType": "image/png", "filename": "Figure.png"}},
    ]
    return Snapshot(Library(name="示例库（非真实文献）"), collections, items, attachments, "DEMO-INSTANCE")


def run_demo(output: Path, progress=None):
    # This is scratch data, not content of the resulting export.
    scratch = Path.cwd() / "work" / "demo-source"
    scratch.mkdir(parents=True, exist_ok=True)
    origin = (scratch / ("pdf-" + uuid.uuid4().hex)).resolve()
    origin.relative_to(scratch.resolve())
    # A regular inherited ACL also works in restricted Windows execution tokens.
    origin.mkdir()
    try:
        source_files = {}
        for key in ("PDFA0001", "PDFB0001", "PDFU0001"):
            path = origin / (key + ".pdf")
            path.write_bytes(sample_pdf("Example " + key))
            source_files[key] = path
        return export_snapshot(make_snapshot(), output, lambda key: source_files[key], progress=progress)
    finally:
        origin.resolve().relative_to(scratch.resolve())
        shutil.rmtree(origin)
