from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from demo_data import make_snapshot, sample_pdf
from exporter import STATE_DIR, clean_name, collection_paths, export_snapshot
from zotero_api import APIError, ExportError, Library, ZoteroAPI, file_uri_to_path


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path.cwd() / "work" / "exporter-tests"
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.root = (self.scratch / ("case-" + uuid.uuid4().hex[:12])).resolve()
        self.root.relative_to(self.scratch.resolve())
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.output = self.root / "output"
        self.source = self.root / "source"
        self.source.mkdir()
        self.files = {}
        for key in ("PDFA0001", "PDFB0001", "PDFU0001"):
            path = self.source / (key + ".pdf")
            path.write_bytes(sample_pdf(key))
            self.files[key] = path
        self.snapshot = make_snapshot()

    def cleanup(self):
        self.root.resolve().relative_to(self.scratch.resolve())
        shutil.rmtree(self.root)

    def export(self, **kwargs):
        return export_snapshot(self.snapshot, self.output, lambda key: self.files[key], **kwargs)

    def manifest(self):
        return json.loads((self.output / STATE_DIR / "manifest.json").read_text(encoding="utf-8"))

    def exported(self):
        return [self.output / r["path"] for r in self.manifest()["records"]]

    def test_nested_multi_collection_unfiled_and_pdf_only(self):
        originals = {key: path.read_bytes() for key, path in self.files.items()}
        report = self.export()
        self.assertEqual((report.pdf_count, report.copied), (3, 4))
        self.assertEqual(len(list(self.output.rglob("*.pdf"))), 4)
        self.assertFalse(list(self.output.rglob("*.md")))
        records = self.manifest()["records"]
        a = [r for r in records if r["attachment_key"] == "PDFA0001"]
        self.assertEqual(len(a), 2)
        self.assertTrue(any("子分类" in r["path"] and "研究主题" in r["path"] for r in a))
        self.assertTrue(any("未分类" in r["path"] for r in records))
        for rec in records:
            self.assertEqual((self.output / rec["path"]).read_bytes(), originals[rec["attachment_key"]])
        for key, source in self.files.items():
            self.assertEqual(source.read_bytes(), originals[key])

    def test_repeat_export_does_not_rewrite_files(self):
        self.export()
        signatures = {str(p): p.stat().st_mtime_ns for p in self.exported()}
        report = self.export()
        self.assertEqual((report.copied, report.updated, report.unchanged, report.archived), (0, 0, 4, 0))
        self.assertEqual(signatures, {str(p): p.stat().st_mtime_ns for p in self.exported()})

    def test_changed_source_updates_all_copies_and_archives_old(self):
        self.export()
        old_bytes = self.files["PDFA0001"].read_bytes()
        new_bytes = sample_pdf("Changed source contents")
        self.files["PDFA0001"].write_bytes(new_bytes)
        report = self.export()
        self.assertEqual((report.updated, report.archived), (2, 2))
        for rec in self.manifest()["records"]:
            if rec["attachment_key"] == "PDFA0001":
                self.assertEqual((self.output / rec["path"]).read_bytes(), new_bytes)
        backups = list((self.output / STATE_DIR / "archive").rglob("*.pdf"))
        self.assertEqual([p.read_bytes() for p in backups], [old_bytes, old_bytes])
        self.assertEqual(self.files["PDFA0001"].read_bytes(), new_bytes)

    def test_complete_verification_detects_changes_with_same_stat(self):
        self.export()
        source = self.files["PDFA0001"]
        before = source.stat()
        changed = source.read_bytes().replace(b"PDFA0001", b"CHANGED1")
        self.assertEqual(len(changed), before.st_size)
        source.write_bytes(changed)
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        report = self.export(verify=True)
        self.assertEqual(report.updated, 2)

    def test_user_pdf_edit_is_preserved_before_refresh(self):
        self.export()
        edited = self.exported()[0]
        edited_bytes = sample_pdf("Locally edited export")
        edited.write_bytes(edited_bytes)
        report = self.export()
        self.assertEqual(report.updated, 1)
        backups = list((self.output / STATE_DIR / "archive").rglob("*.pdf"))
        self.assertEqual(backups[0].read_bytes(), edited_bytes)

    def test_rename_collection_preserves_user_documents(self):
        self.export()
        old = next(p for p in self.exported() if "子分类" in str(p))
        user_note = old.parent / "user-research.md"
        user_note.write_text("Do not change this document.", encoding="utf-8")
        self.snapshot.collections[1]["data"]["name"] = "改名后的子分类"
        report = self.export()
        self.assertEqual(report.archived, 1)
        self.assertFalse(old.exists())
        self.assertTrue(any("改名后的子分类" in str(p) for p in self.exported()))
        self.assertEqual(user_note.read_text(encoding="utf-8"), "Do not change this document.")

    def test_title_rename_updates_filename_and_archives_old(self):
        self.export()
        self.snapshot.items[0]["data"]["title"] = "New title"
        report = self.export()
        self.assertEqual(report.archived, 2)
        paths = [r["path"] for r in self.manifest()["records"] if r["attachment_key"] == "PDFA0001"]
        self.assertTrue(all("New title" in p for p in paths))

    def test_case_only_rename_keeps_exports_readable(self):
        self.snapshot.items[0]["data"]["title"] = "Case sensitive title"
        self.export()
        self.snapshot.items[0]["data"]["title"] = "CASE SENSITIVE TITLE"
        report = self.export()
        self.assertFalse(report.errors)
        self.assertTrue(all(p.is_file() for p in self.exported()))

    def test_partial_export_preserves_other_collections(self):
        self.export()
        other = next(p for p in self.exported() if "研究方法" in str(p))
        report = self.export(selected=["COLLA001"])
        self.assertEqual(report.pdf_count, 2)
        self.assertEqual(report.archived, 0)
        self.assertTrue(other.exists())
        self.assertEqual(len(self.manifest()["records"]), 4)

    def test_selected_parent_without_children(self):
        report = self.export(selected=["COLLA001"], descendants=False)
        self.assertEqual((report.pdf_count, report.copied), (1, 1))
        self.assertIn("ITEMB001", self.manifest()["records"][0]["path"])

    def test_missing_source_keeps_previous_copy(self):
        self.export()
        self.files["PDFA0001"].unlink()
        report = self.export()
        self.assertEqual(len(report.missing), 1)
        self.assertEqual(report.archived, 0)
        self.assertTrue(all(p.exists() for p in self.exported()))

    def test_removed_source_item_archives_only_managed_pdfs(self):
        self.export()
        user_pdf = self.output / "my-file.pdf"
        user_pdf.write_bytes(sample_pdf("User file"))
        self.snapshot.attachments = [a for a in self.snapshot.attachments if a["key"] != "PDFA0001"]
        report = self.export()
        self.assertEqual(report.archived, 2)
        self.assertTrue(user_pdf.exists())
        self.assertEqual(len(self.manifest()["records"]), 2)

    def test_unowned_filename_collision_is_not_overwritten(self):
        self.export()
        conflict = self.exported()[0]
        user_bytes = sample_pdf("Existing user file")
        conflict.write_bytes(user_bytes)
        (self.output / STATE_DIR / "manifest.json").unlink()
        report = self.export()
        self.assertEqual(conflict.read_bytes(), user_bytes)
        self.assertEqual(report.archived, 0)
        self.assertTrue(report.warnings)
        self.assertNotIn(conflict, self.exported())
        second = self.export()
        self.assertEqual(second.copied, 0)

    def test_directory_at_previous_pdf_path_is_preserved(self):
        self.export()
        target = self.exported()[0]
        target.unlink()
        target.mkdir()
        user_file = target / "document.md"
        user_file.write_text("Keep this user directory.", encoding="utf-8")
        report = self.export()
        self.assertEqual(report.archived, 0)
        self.assertTrue(target.is_dir())
        self.assertEqual(user_file.read_text(encoding="utf-8"), "Keep this user directory.")
        self.assertTrue(all(path.is_file() for path in self.exported()))

    def test_changed_zotero_instance_is_rejected(self):
        self.export()
        originals = {str(p): p.read_bytes() for p in self.exported()}
        self.snapshot.server_id = "OTHER-DATABASE"
        with self.assertRaisesRegex(ExportError, "数据库不同"):
            self.export()
        self.assertEqual(originals, {str(p): p.read_bytes() for p in self.exported()})

    def test_corrupt_manifest_cannot_touch_outside_files(self):
        self.export()
        outside = self.root / "outside.pdf"
        outside.write_bytes(sample_pdf("Outside file"))
        manifest = self.manifest()
        manifest["records"][0]["path"] = "../outside.pdf"
        (self.output / STATE_DIR / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ExportError):
            self.export()
        self.assertEqual(outside.read_bytes(), sample_pdf("Outside file"))

    def test_non_pdf_manifest_record_is_rejected(self):
        self.export()
        manifest = self.manifest()
        manifest["records"][0]["path"] = "notes.md"
        (self.output / STATE_DIR / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ExportError):
            self.export()

    def test_directory_symlink_cannot_escape_output(self):
        self.export()
        outside = self.root / "outside"
        outside.mkdir()
        link = self.output / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Symbolic links are unavailable on this Windows installation.")
        manifest = self.manifest()
        manifest["records"][0]["path"] = "linked/victim.pdf"
        (self.output / STATE_DIR / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ExportError):
            self.export()
        self.assertFalse(list(outside.iterdir()))

    def test_invalid_pdf_is_reported(self):
        self.files["PDFA0001"].write_bytes(b"Not a PDF")
        report = self.export()
        self.assertEqual(len(report.missing), 1)
        self.assertEqual(report.copied, 2)

    def test_collection_cycles_are_rejected_before_export(self):
        self.snapshot.collections[0]["data"]["parentCollection"] = "COLLX001"
        with self.assertRaises(ExportError):
            self.export()
        self.assertFalse(self.output.exists())

    def test_duplicate_names_are_disambiguated_and_windows_names_safe(self):
        self.snapshot.collections[2]["data"]["name"] = "研究主题"
        paths = collection_paths(self.snapshot.collections)
        self.assertNotEqual(paths["COLLA001"], paths["COLLB001"])
        self.assertEqual(clean_name("CON"), "_CON")
        self.assertNotIn("/", clean_name("a/b:c?"))
        self.assertEqual(clean_name("..."), "未命名")


class APITests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.forbidden = False
        self.changing = False
        self.real_collections = False
        self.sources = {}
        self.snapshot = make_snapshot()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path))
                parts = urlsplit(self.path)
                route = parts.path
                query = parse_qs(parts.query)
                if owner.forbidden:
                    self.send_error(403)
                    return
                if "/items/" in route and (route.endswith("/file/view/url") or route.endswith("/file")):
                    key = route.split("/")[5]
                    file_path = owner.sources.get(key, Path.cwd().joinpath("work", "空 格.pdf").resolve())
                    if route.endswith("/file/view/url"):
                        if key == "PDFA0001":
                            self.send_error(404)
                            return
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(file_path.as_uri().encode("utf-8"))
                    else:
                        self.send_response(302)
                        self.send_header("Location", file_path.as_uri())
                        self.end_headers()
                    return
                if route == "/api/users/0/collections":
                    objects = (owner.snapshot.collections if owner.real_collections else
                               [{"key": f"COL{i:05d}", "data": {"name": f"分类 {i}"}} for i in range(125)])
                elif route.endswith("/items/top"):
                    objects = owner.snapshot.items
                elif route.endswith("/items"):
                    objects = owner.snapshot.attachments
                else:
                    objects = []
                start = int(query.get("start", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                body = json.dumps(objects[start:start + limit]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Total-Results", str(len(objects)))
                self.send_header("Zotero-Server-ID", "TEST-SERVER")
                self.send_header("Last-Modified-Version", "4" if owner.changing and start > 0 else "3")
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = ZoteroAPI(f"http://127.0.0.1:{self.server.server_port}/api/")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_pagination_and_only_get_requests(self):
        self.api.connect()
        snapshot = self.api.snapshot(Library())
        self.assertEqual(len(snapshot.collections), 125)
        self.assertTrue(any("start=100" in path for method, path in self.requests))
        self.assertTrue(all(method == "GET" for method, path in self.requests))
        self.assertFalse(any("itemType=note" in path for method, path in self.requests))

    def test_file_redirect_fallback_and_unicode_path(self):
        self.api.connect()
        path = self.api.pdf_path(Library(), "PDFA0001")
        self.assertEqual(path, Path.cwd().joinpath("work", "空 格.pdf").resolve())

    def test_disabled_local_api_has_actionable_message(self):
        self.forbidden = True
        with self.assertRaisesRegex(APIError, "设置"):
            self.api.connect()

    def test_inconsistent_snapshot_is_not_accepted(self):
        self.changing = True
        self.api.connect()
        with self.assertRaisesRegex(APIError, "持续变化"):
            self.api.snapshot(Library())

    def test_remote_api_and_non_file_uri_are_rejected(self):
        with self.assertRaises(ExportError):
            ZoteroAPI("https://api.zotero.org/")
        with self.assertRaises(ExportError):
            file_uri_to_path("https://example.com/file.pdf")

    def test_cli_export_with_http_api_and_real_pdf_files(self):
        scratch = (Path.cwd() / "work" / "integration-tests").resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        case = (scratch / ("case-" + uuid.uuid4().hex[:12])).resolve()
        case.relative_to(scratch)
        case.mkdir()
        try:
            self.real_collections = True
            for key in ("PDFA0001", "PDFB0001", "PDFU0001"):
                path = case / ("空 格 " + key + ".pdf")
                path.write_bytes(sample_pdf(key))
                self.sources[key] = path
            output = case / "export"
            app = Path(__file__).resolve().parents[1] / "app.py"
            args = [sys.executable, "-B", str(app), "export", "--output", str(output), "--api", self.api.base]
            for run in range(2):
                result = subprocess.run(args, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                report = json.loads((output / STATE_DIR / "last-report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["pdf_count"], 3)
                self.assertEqual(report["copied"] if run == 0 else report["unchanged"], 4)
            self.assertTrue(all(method == "GET" for method, path in self.requests))
        finally:
            case.resolve().relative_to(scratch)
            shutil.rmtree(case)


if __name__ == "__main__":
    unittest.main()
