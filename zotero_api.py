"""Read-only adapter for Zotero's local HTTP API. No third-party packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, unquote
from urllib.request import (
    HTTPRedirectHandler, ProxyHandler, Request, build_opener, url2pathname,
)


class ExportError(Exception):
    """A problem that can be shown directly to the user."""


class APIError(ExportError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Library:
    kind: str = "user"
    id: int = 0
    name: str = "我的文献库"

    @property
    def prefix(self) -> str:
        return f"{'users' if self.kind == 'user' else 'groups'}/{self.id}"

    @property
    def scope(self) -> str:
        return f"{self.kind}-{self.id}"

    @classmethod
    def parse(cls, value: str) -> "Library":
        try:
            kind, number = value.split(":", 1)
            if kind not in {"user", "group"} or not number.isdecimal():
                raise ValueError
            return cls(kind, int(number), "我的文献库" if kind == "user" else f"群组 {number}")
        except ValueError as exc:
            raise ExportError("文献库格式应为 user:0 或 group:群组编号。") from exc


@dataclass
class Snapshot:
    library: Library
    collections: list[dict]
    items: list[dict]
    attachments: list[dict]
    server_id: str = ""
    api_base: str = ""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def file_uri_to_path(uri: str) -> Path:
    parts = urlsplit(uri.strip())
    if parts.scheme.lower() != "file":
        raise ExportError("Zotero 没有返回本地附件路径；请先在 Zotero 中下载该 PDF。")
    if parts.query or parts.fragment:
        raise ExportError("附件文件地址包含异常参数。")
    # url2pathname handles percent escapes and Windows drive letters.
    path = url2pathname(parts.path)
    if parts.netloc and parts.netloc.lower() != "localhost":
        path = "//" + unquote(parts.netloc) + path
    return Path(path)


class ZoteroAPI:
    def __init__(self, base: str = "http://127.0.0.1:23119/api/", timeout: float = 15):
        parts = urlsplit(base)
        if (parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path.rstrip("/") != "/api"):
            raise ExportError("此工具只连接本机 Zotero 的 http://localhost:23119/api/ 接口。")
        self.base = base.rstrip("/") + "/"
        self.timeout = timeout
        self.server_id = ""
        self._versions: set[str] = set()
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, route: str, params: dict | None = None) -> tuple[bytes, dict]:
        url = self.base + route.lstrip("/")
        if params:
            url += "?" + urlencode(params)
        headers = {"Zotero-API-Version": "3", "User-Agent": "ZoteroPDFExporter/0.1"}
        if self.server_id:
            headers["Zotero-Server-ID"] = self.server_id
        request = Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                response_headers = dict(response.headers.items())
                self._observe(response.headers)
                return response.read(), response_headers
        except HTTPError as exc:
            self._observe(exc.headers)
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location", "")
                if route.endswith("/file") and urlsplit(location).scheme == "file":
                    return location.encode("utf-8"), dict(exc.headers.items())
                raise APIError("Zotero 返回了不支持的重定向。", exc.code) from exc
            messages = {
                403: "请在 Zotero 的「设置 → 高级」中开启「允许此计算机上的其他应用程序与 Zotero 通信」。",
                404: "本地 API 未找到该资源或 PDF 附件。",
                412: "Zotero 数据库在读取过程中发生切换，请重新连接。",
            }
            raise APIError(messages.get(exc.code, f"Zotero 接口返回 HTTP {exc.code}。"), exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise APIError("无法连接本机 Zotero。请先启动 Zotero，并开启设置中的本地应用通信。") from exc

    def _observe(self, headers) -> None:
        server = headers.get("Zotero-Server-ID", "")
        if server:
            if self.server_id and self.server_id != server:
                raise APIError("读取期间 Zotero 数据库发生切换，请重新连接。", 412)
            self.server_id = server
        version = headers.get("Last-Modified-Version", "")
        if version:
            self._versions.add(version)

    def connect(self) -> None:
        try:
            self._request("")
        except APIError as exc:
            if exc.status != 404:
                raise
            # Older implementations may not serve the bare /api/ route.
            self._request("users/0/collections", {"limit": 1, "format": "json"})

    def all(self, route: str, params: dict | None = None) -> list[dict]:
        result: list[dict] = []
        start = 0
        while True:
            query = {"format": "json", "limit": 100, "start": start}
            query.update(params or {})
            raw, headers = self._request(route, query)
            try:
                page = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError) as exc:
                raise APIError("Zotero 返回的 JSON 无法读取。") from exc
            if not isinstance(page, list) or not all(isinstance(obj, dict) for obj in page):
                raise APIError("Zotero 返回的数据格式与预期不符。")
            result.extend(page)
            start += len(page)
            total = next((v for k, v in headers.items() if k.lower() == "total-results"), "")
            if (total.isdecimal() and start >= int(total)) or len(page) < 100:
                return result

    def libraries(self) -> list[Library]:
        libraries = [Library()]
        try:
            groups = self.all("users/0/groups")
        except APIError as exc:
            if exc.status not in {400, 404, 501}:
                raise
            groups = []
        for group in groups:
            data = group.get("data", group)
            number = group.get("id", data.get("id"))
            if number is not None:
                libraries.append(Library("group", int(number), str(data.get("name", f"群组 {number}"))))
        return libraries

    def collections(self, library: Library) -> list[dict]:
        return self.all(library.prefix + "/collections")

    def snapshot(self, library: Library) -> Snapshot:
        for _ in range(3):
            self._versions.clear()
            collections = self.collections(library)
            # Do not fetch note bodies or annotations. Only item headers and attachments.
            items = self.all(library.prefix + "/items/top", {"itemType": "-note"})
            attachments = self.all(library.prefix + "/items", {"itemType": "attachment"})
            if len(self._versions) <= 1:
                return Snapshot(library, collections, items, attachments, self.server_id, self.base)
        raise APIError("读取期间 Zotero 文献库持续变化，请稍后再导出。尚未改动导出目录。")

    def pdf_path(self, library: Library, key: str) -> Path:
        if not key.isalnum():
            raise ExportError("附件编号格式异常。")
        route = library.prefix + "/items/" + key
        try:
            raw, _ = self._request(route + "/file/view/url")
        except APIError as exc:
            if exc.status not in {404, 405, 501}:
                raise
            raw, _ = self._request(route + "/file")
        try:
            return file_uri_to_path(raw.decode("utf-8").strip())
        except UnicodeError as exc:
            raise ExportError("附件本地路径编码无法读取。") from exc
