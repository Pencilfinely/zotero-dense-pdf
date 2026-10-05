"""Entry point: a Windows-friendly GUI or a small command-line exporter."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from exporter import collection_paths, export_snapshot
from zotero_api import ExportError, Library, ZoteroAPI


def main() -> int:
    parser = argparse.ArgumentParser(description="按 Zotero 分类导出本地 PDF，支持重复导出和更新。")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="打开桌面窗口（默认）")
    probe = sub.add_parser("probe", help="读取文献库和分类，检查本地接口")
    probe.add_argument("--api", default="http://127.0.0.1:23119/api/")
    export = sub.add_parser("export", help="导出 PDF")
    export.add_argument("--output", required=True, help="独立的导出目录")
    export.add_argument("--library", default="user:0", help="user:0 或 group:群组编号")
    export.add_argument("--collection", action="append", help="分类 Key，可重复指定；省略则导出全库")
    export.add_argument("--no-subcollections", action="store_true", help="仅导出所选分类自身的条目")
    export.add_argument("--verify", action="store_true", help="重新计算文件哈希，完整核对内容")
    export.add_argument("--api", default="http://127.0.0.1:23119/api/")
    export.add_argument("--verbose", action="store_true", help="显示逐文件进度")
    demo = sub.add_parser("demo", help="使用明确标注的示例库演示导出，不连接 Zotero")
    demo.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command in {None, "gui"}:
            from ui import run
            run()
            return 0
        if args.command == "demo":
            from demo_data import run_demo
            report = run_demo(Path(args.output), print)
        else:
            api = ZoteroAPI(args.api)
            api.connect()
            if args.command == "probe":
                result = []
                for library in api.libraries():
                    paths = collection_paths(api.collections(library))
                    result.append({"library": f"{library.kind}:{library.id}", "name": library.name,
                                   "collections": [{"key": key, "path": " / ".join(path)}
                                                   for key, path in paths.items()]})
                print(json.dumps({"libraries": result}, ensure_ascii=False, indent=2))
                return 0
            library = Library.parse(args.library)
            if library.kind == "group":
                library = next((lib for lib in api.libraries() if lib.scope == library.scope), library)
            snapshot = api.snapshot(library)
            report = export_snapshot(snapshot, args.output, lambda key: api.pdf_path(library, key),
                                     args.collection, not args.no_subcollections, args.verify,
                                     print if args.verbose else None)
        print(report.summary())
        print("导出目录：" + report.output)
        for issue in report.missing + report.errors:
            print("未完成：" + json.dumps(issue, ensure_ascii=False), file=sys.stderr)
        return 2 if report.missing or report.errors else 0
    except (ExportError, OSError) as exc:
        print("导出未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
