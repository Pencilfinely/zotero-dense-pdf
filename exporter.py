"""Copy only PDF files into collection folders; keep ownership in a sidecar."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from zotero_api import ExportError, Snapshot

STATE_DIR = ".zotero-export"
SCHEMA = 1


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_name(value: str, limit: int = 60) -> str:
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .") or "未命名"
    value = value[:limit].rstrip(" .") or "未命名"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", value):
        value = "_" + value
    return value


def data(obj: dict) -> dict:
    return obj.get("data", obj)


def object_key(obj: dict) -> str:
    key = str(obj.get("key", data(obj).get("key", "")))
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ExportError("Zotero 对象编号格式异常。")
    return key


def collection_paths(collections: list[dict]) -> dict[str, tuple[str, ...]]:
    objects = {object_key(obj): data(obj) for obj in collections}
    result: dict[str, tuple[str, ...]] = {}

    def visit(key: str, stack: set[str]) -> tuple[str, ...]:
        if key in result:
            return result[key]
        if key in stack or len(stack) > 64:
            raise ExportError("Zotero 分类存在循环关系或层级过深，无法生成目录。")
        if key not in objects:
            raise ExportError("Zotero 分类的父分类不存在，请重新读取文献库。")
        obj = objects[key]
        name = clean_name(obj.get("name", "未命名分类"), 32) + "__" + key
        parent = obj.get("parentCollection")
        result[key] = (visit(str(parent), stack | {key}) if parent else ()) + (name,)
        return result[key]

    for key in objects:
        visit(key, set())
    return result


def included_keys(collections: list[dict], selected: list[str] | None,
                  descendants: bool = True) -> set[str]:
    objects = {object_key(obj): data(obj) for obj in collections}
    if selected is None:
        return set(objects)
    missing = set(selected) - set(objects)
    if missing:
        raise ExportError("所选分类已不存在：" + ", ".join(sorted(missing)))
    included = set(selected)
    if descendants:
        while True:
            additions = {key for key, obj in objects.items() if obj.get("parentCollection") in included}
            expanded = included | additions
            if expanded == included:
                break
            included = expanded
    return included


def _safe_path(root: Path, relative: str | Path) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise ExportError("导出记录中包含目录之外的路径。")
    target = root / rel
    try:
        target.resolve().relative_to(root)
    except (ValueError, OSError) as exc:
        raise ExportError("目标路径或链接指向导出目录之外。") from exc
    return target


def _signature(path: Path) -> dict:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_write(root: Path, relative: str, value: dict) -> None:
    target = _safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).replace(target)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _export_lock(root: Path):
    lock = _safe_path(root, STATE_DIR + "/export.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ExportError("该目录正在被另一个导出任务使用。") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ExportError("该目录正在被另一个导出任务使用。") from exc
            locked = True
            yield
        finally:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass
class Report:
    output: str
    library: str
    timestamp: str = field(default_factory=utc_stamp)
    pdf_count: int = 0
    copied: int = 0
    updated: int = 0
    unchanged: int = 0
    archived: int = 0
    missing: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"PDF {self.pdf_count} 份；新增副本 {self.copied}，更新 {self.updated}，"
                f"未变化 {self.unchanged}，归档 {self.archived}，"
                f"缺失 {len(self.missing)}，失败 {len(self.errors)}。")


def _filename(item: dict, item_key: str, attachment_key: str) -> str:
    date = str(item.get("date", ""))
    match = re.search(r"\b(?:18|19|20|21)\d{2}\b", date)
    year = match.group() if match else "年份不详"
    creators = item.get("creators") or []
    first = creators[0] if creators else {}
    author = clean_name(first.get("lastName", first.get("name", "作者不详")), 16)
    title = clean_name(item.get("title", "未命名文献"), 56)
    return f"{year} - {author} - {title}__{item_key}__{attachment_key}.pdf"


def _archive(root: Path, path: Path, run_id: str) -> Path:
    if not path.is_file() or path.is_symlink():
        raise ExportError("只允许归档普通 PDF 文件。")
    relative = path.relative_to(root).as_posix()
    token = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
    filename = token + "__" + clean_name(path.stem, 48) + ".pdf"
    target = _safe_path(root, Path(STATE_DIR) / "archive" / run_id / filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(root, path.relative_to(root))
    path.replace(target)
    return target


def _copy_prepared(source: Path, destination: Path, signature: dict, digest: str) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pdf-", suffix=".tmp", dir=destination.parent)
    path = Path(temporary)
    h = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as target, source.open("rb") as origin:
            for chunk in iter(lambda: origin.read(1024 * 1024), b""):
                h.update(chunk)
                target.write(chunk)
        if h.hexdigest() != digest or _signature(source) != signature:
            raise ExportError("源 PDF 在导出过程中发生变化，已保留旧副本，请重新导出。")
        os.utime(path, ns=(signature["mtime_ns"], signature["mtime_ns"]))
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def export_snapshot(snapshot: Snapshot, output: str | Path,
                    resolver: Callable[[str], Path], selected: list[str] | None = None,
                    descendants: bool = True, verify: bool = False,
                    progress: Callable[[str], None] | None = None) -> Report:
    root = Path(output).expanduser().resolve()
    if root == Path(root.anchor):
        raise ExportError("请选择一个专门的导出文件夹。")
    for parent in (root, *root.parents):
        if (parent / "zotero.sqlite").exists():
            raise ExportError("导出目录不能位于 Zotero 数据目录中，请选择独立文件夹。")
    paths = collection_paths(snapshot.collections)
    included = included_keys(snapshot.collections, selected, descendants)
    root.mkdir(parents=True, exist_ok=True)
    with _export_lock(root):
        return _export_locked(snapshot, root, resolver, selected, included, paths, verify,
                              progress or (lambda message: None))


def _export_locked(snapshot: Snapshot, root: Path, resolver, selected, included, paths,
                   verify: bool, progress) -> Report:
    manifest_path = _safe_path(root, STATE_DIR + "/manifest.json")
    manifest = {"schema": SCHEMA, "libraries": {}, "records": []}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (manifest.get("schema") != SCHEMA or not isinstance(manifest.get("records"), list)
                    or not isinstance(manifest.get("libraries"), dict)):
                raise ValueError
            for record in manifest["records"]:
                if (not isinstance(record, dict)
                        or not {"scope", "attachment_key", "collection_key", "path", "sha256"} <= record.keys()
                        or Path(record["path"]).suffix.lower() != ".pdf"):
                    raise ValueError
        except (ValueError, OSError) as exc:
            raise ExportError("导出记录无法读取。请保留现有目录并改用新的导出目录。") from exc
    scope = snapshot.library.scope
    stored_library = manifest["libraries"].get(scope, {})
    old_server = stored_library.get("server_id", "")
    if old_server and snapshot.server_id and old_server != snapshot.server_id:
        raise ExportError("当前 Zotero 数据库与此前导出的数据库不同，请使用新的导出目录。")
    # Source IDs form the identity; paths are only a presentation of that identity.
    def ident(rec):
        return (rec["scope"], rec["attachment_key"], rec["collection_key"])

    previous = {ident(rec): rec for rec in manifest["records"]}
    previous_by_attachment: dict[tuple[str, str], list[dict]] = {}
    for rec in previous.values():
        previous_by_attachment.setdefault((rec["scope"], rec["attachment_key"]), []).append(rec)
    def path_key(path):
        return os.path.normcase(str(Path(path)))

    owners = {path_key(rec["path"]): ident(rec) for rec in previous.values()}
    for rec in previous.values():
        _safe_path(root, rec["path"])
    records = dict(previous)
    report = Report(str(root), snapshot.library.name)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    # The library's directory stays stable if its display name changes.
    library_dir = stored_library.get("directory", clean_name(snapshot.library.name, 24) + "__" + scope)
    _safe_path(root, library_dir)
    for key in included:
        _safe_path(root, Path(library_dir).joinpath(*paths[key])).mkdir(parents=True, exist_ok=True)
    items = {object_key(obj): data(obj) for obj in snapshot.items if not data(obj).get("deleted")}
    wanted: set[tuple] = set()
    unavailable: set[str] = set()
    failed_ids: set[tuple] = set()
    archive_events: list[dict] = []

    for attachment in snapshot.attachments:
        att = data(attachment)
        if att.get("deleted") or att.get("itemType") != "attachment":
            continue
        filename = str(att.get("filename", ""))
        if att.get("contentType") != "application/pdf" and not filename.lower().endswith(".pdf"):
            continue
        if att.get("linkMode") == "linked_url":
            continue
        key = object_key(attachment)
        parent_key = str(att.get("parentItem") or key)
        if att.get("parentItem") and parent_key not in items:
            continue  # A child of a deleted or inaccessible parent must not be exported.
        item = items.get(parent_key, att)
        collection_keys = list(dict.fromkeys(item.get("collections") or []))
        if set(collection_keys) - set(paths):
            raise ExportError("条目引用了不存在的分类，请重新读取文献库。")
        targets = [c for c in collection_keys if c in included]
        if not collection_keys and selected is None:
            targets = ["unfiled"]
        if not targets:
            continue
        report.pdf_count += 1
        progress(f"读取 PDF：{item.get('title', filename)}")
        desired_ids = {(scope, key, c) for c in targets}
        wanted.update(desired_ids)
        try:
            source = Path(resolver(key)).resolve(strict=True)
            if not source.is_file():
                raise ExportError("附件路径不是文件。")
            with source.open("rb") as handle:
                if b"%PDF-" not in handle.read(1024):
                    raise ExportError("附件内容不是可识别的 PDF 文件。")
            signature = _signature(source)
            old_records = previous_by_attachment.get((scope, key), [])
            cached = next((rec for rec in old_records if rec.get("source") == str(source)
                           and rec.get("source_signature") == signature), None)
            digest = cached["sha256"] if cached and not verify else _digest(source)
            if _signature(source) != signature:
                raise ExportError("源 PDF 正在变化，请稍后重试。")
        except (ExportError, OSError) as exc:
            unavailable.add(key)
            report.missing.append({"item_key": parent_key, "attachment_key": key,
                                   "title": item.get("title", filename), "reason": str(exc)})
            progress(f"未导出：{item.get('title', filename)}（{exc}）")
            continue
        for collection_key in targets:
            identity = (scope, key, collection_key)
            components = paths[collection_key] if collection_key != "unfiled" else ("未分类__unfiled",)
            base_path = Path(library_dir).joinpath(*components, _filename(item, parent_key, key)).as_posix()
            old = previous.get(identity)
            relative = old["path"] if old and old.get("base_path") == base_path else base_path
            target = _safe_path(root, relative)
            # Preserve files created by the user, including accidental filename collisions.
            number = 1
            while ((target.exists() and (owners.get(path_key(relative)) != identity or not target.is_file()))
                   or target.is_symlink()
                   or (path_key(relative) in owners and owners[path_key(relative)] != identity)):
                candidate = Path(base_path)
                relative = (candidate.parent / (candidate.stem + f"__copy{number}" + candidate.suffix)).as_posix()
                target = _safe_path(root, relative)
                number += 1
            if relative != base_path and not (old and old["path"] == relative):
                report.warnings.append(f"保留已有文件，使用新的文件名：{relative}")
            temporary = None
            try:
                if source == target.resolve():
                    raise ExportError("源 PDF 与导出目标相同，请更换导出目录。")
                unchanged = False
                if target.is_file():
                    if (old and old["path"] == relative and old.get("sha256") == digest
                            and old.get("destination_signature") == _signature(target) and not verify):
                        unchanged = True
                    else:
                        unchanged = _digest(target) == digest
                if unchanged:
                    report.unchanged += 1
                else:
                    temporary = _copy_prepared(source, target, signature, digest)
                    if target.exists():
                        backup = _archive(root, target, run_id)
                        archive_events.append({"from": relative, "to": backup.relative_to(root).as_posix(),
                                               "reason": "replaced"})
                        report.archived += 1
                    temporary.replace(target)
                    if unchanged is False and old and old["path"] == relative:
                        report.updated += 1
                    else:
                        report.copied += 1
                rec = {"scope": scope, "item_key": parent_key, "attachment_key": key,
                       "collection_key": collection_key, "title": item.get("title", filename),
                       "path": relative, "base_path": base_path, "sha256": digest,
                       "source": str(source), "source_signature": signature,
                       "destination_signature": _signature(target)}
                records[identity] = rec
                owners[path_key(relative)] = identity
            except (OSError, ExportError) as exc:
                failed_ids.add(identity)
                report.errors.append({"attachment_key": key, "path": relative, "reason": str(exc)})
                progress(f"失败：{relative}（{exc}）")
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    for identity, old in previous.items():
        in_scope = old["scope"] == scope and (selected is None or old["collection_key"] in included)
        replacement = records.get(identity)
        moved = replacement and path_key(replacement["path"]) != path_key(old["path"])
        obsolete = identity not in wanted
        if (not in_scope or old["attachment_key"] in unavailable or identity in failed_ids
                or not (moved or obsolete)):
            continue
        old_path = _safe_path(root, old["path"])
        try:
            if old_path.is_file() and not old_path.is_symlink():
                backup = _archive(root, old_path, run_id)
                archive_events.append({"from": old["path"], "to": backup.relative_to(root).as_posix(),
                                       "reason": "moved" if moved else "removed_from_source_or_scope"})
                report.archived += 1
            if obsolete:
                records.pop(identity, None)
        except OSError as exc:
            if obsolete:
                records[identity] = old
            report.errors.append({"path": old["path"], "reason": str(exc)})
    # Remove only empty, previously managed directories; never remove a directory's contents.
    keep_dirs = set()
    for key in included:
        directory = _safe_path(root, Path(library_dir).joinpath(*paths[key]))
        keep_dirs.update(directory.parents)
        keep_dirs.add(directory)
    former_dirs = set()
    for old in previous.values():
        if old["scope"] == scope:
            directory = _safe_path(root, old["path"]).parent
            while directory != root:
                former_dirs.add(directory)
                directory = directory.parent
    for directory in sorted(former_dirs - keep_dirs, key=lambda p: len(p.parts), reverse=True):
        _safe_path(root, directory.relative_to(root))
        try:
            directory.rmdir()
        except OSError:
            pass  # Non-empty directories, including any user documents, are kept.
    manifest["libraries"][scope] = {"name": snapshot.library.name, "directory": library_dir,
                                    "server_id": snapshot.server_id or old_server,
                                    "api_base": snapshot.api_base, "last_export": report.timestamp}
    manifest["records"] = list(records.values())
    _json_write(root, STATE_DIR + "/manifest.json", manifest)
    report_data = asdict(report)
    report_data["archive_events"] = archive_events
    _json_write(root, STATE_DIR + "/last-report.json", report_data)
    if archive_events:
        _json_write(root, STATE_DIR + "/archive/" + run_id + "/mapping.json", {"events": archive_events})
    progress(report.summary())
    return report
