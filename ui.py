"""Plain desktop UI. Workers send data through a queue, never mutate Tk widgets."""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from exporter import collection_paths, export_snapshot
from zotero_api import Library, ZoteroAPI


class ExportWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Zotero PDF 分类导出")
        self.root.geometry("850x670")
        self.root.minsize(720, 650)
        self.events: queue.Queue = queue.Queue()
        self.api: ZoteroAPI | None = None
        self.libraries: list[Library] = []
        self.collection_keys: list[str] = []
        self.busy = False
        self.current_scope = ""
        self.output = tk.StringVar()
        self.status = tk.StringVar(value="启动 Zotero 后，点击「读取分类」。")
        self.all_collections = tk.BooleanVar(value=True)
        self.descendants = tk.BooleanVar(value=True)
        self.verify = tk.BooleanVar(value=False)

        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="按 Zotero 分类，把 PDF 复制到普通文件夹", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="保留子分类层级 · 自动命名 · 重复导出时更新变化的文件").pack(anchor="w", pady=(7, 18))
        connection = ttk.Frame(frame)
        connection.pack(fill="x")
        ttk.Label(connection, text="文献库").pack(side="left", padx=(0, 10))
        self.library_box = ttk.Combobox(connection, state="readonly", width=40)
        self.library_box.pack(side="left", fill="x", expand=True)
        self.library_box.bind("<<ComboboxSelected>>", lambda event: self.load_collections())
        self.connect_button = ttk.Button(connection, text="读取分类", command=self.connect)
        self.connect_button.pack(side="left", padx=(10, 0))

        options = ttk.Frame(frame)
        options.pack(fill="x", pady=(15, 8))
        self.all_check = ttk.Checkbutton(options, text="导出整个文献库", variable=self.all_collections, command=self.selection_mode)
        self.all_check.pack(side="left")
        self.child_check = ttk.Checkbutton(options, text="包含所选分类的子分类", variable=self.descendants)
        self.child_check.pack(side="left", padx=16)
        ttk.Label(frame, text="取消全库导出后，可用 Ctrl / Shift 选择多个分类。").pack(anchor="w")
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill="both", expand=True, pady=(8, 14))
        self.collection_list = tk.Listbox(list_frame, selectmode="extended", exportselection=False,
                                         font=("Microsoft YaHei UI", 10), height=9,
                                         activestyle="none", borderwidth=1, relief="solid")
        self.collection_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.collection_list.yview)
        scroll.pack(side="right", fill="y")
        self.collection_list.configure(yscrollcommand=scroll.set)
        self.selection_mode()

        directory = ttk.Frame(frame)
        directory.pack(fill="x")
        ttk.Label(directory, text="导出目录").pack(side="left", padx=(0, 10))
        self.output_entry = ttk.Entry(directory, textvariable=self.output)
        self.output_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(directory, text="选择文件夹", command=self.choose_directory)
        self.browse_button.pack(side="left", padx=(10, 0))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(12, 14))
        self.verify_check = ttk.Checkbutton(row, text="完整校验 PDF 内容（较慢）", variable=self.verify)
        self.verify_check.pack(side="left")
        self.export_button = ttk.Button(row, text="导出 / 更新 PDF", command=self.export, state="disabled")
        self.export_button.pack(side="right")
        self.open_button = ttk.Button(row, text="打开导出目录", command=self.open_directory)
        self.open_button.pack(side="right", padx=10)
        self.bar = ttk.Progressbar(frame, mode="indeterminate")
        self.bar.pack(fill="x")
        ttk.Label(frame, textvariable=self.status, wraplength=780).pack(anchor="w", pady=(8, 6))
        self.log = tk.Text(frame, height=5, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.log.pack(fill="x")
        root.after(100, self.poll)

    def append_log(self, message: str):
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def selection_mode(self):
        self.collection_list.configure(state="disabled" if self.all_collections.get() or self.busy else "normal")

    def set_busy(self, value: bool):
        self.busy = value
        for widget in (self.connect_button, self.browse_button, self.output_entry,
                       self.all_check, self.child_check, self.verify_check):
            widget.configure(state="disabled" if value else "normal")
        self.library_box.configure(state="disabled" if value else "readonly")
        self.export_button.configure(state="normal" if self.api and self.current_scope and not value else "disabled")
        self.selection_mode()
        if value:
            self.bar.start(12)
        else:
            self.bar.stop()

    def work(self, operation, event: str):
        if self.busy:
            return
        self.set_busy(True)

        def worker():
            try:
                self.events.put((event, operation()))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def connect(self):
        self.status.set("正在读取 Zotero 文献库和分类……")
        self.current_scope = ""

        def operation():
            api = ZoteroAPI()
            api.connect()
            libraries = api.libraries()
            return api, libraries, api.collections(libraries[0])
        self.work(operation, "connected")

    def load_collections(self):
        if not self.api or self.busy:
            return
        library = self.libraries[self.library_box.current()]
        self.current_scope = ""
        self.status.set("正在读取分类……")
        self.work(lambda: (library, self.api.collections(library)), "collections")

    def show_collections(self, library: Library, collections: list[dict]):
        paths = collection_paths(collections)
        ordered = sorted(paths.items(), key=lambda pair: pair[1])
        self.collection_keys = [key for key, path in ordered]
        self.collection_list.configure(state="normal")
        self.collection_list.delete(0, "end")
        for key, path in ordered:
            self.collection_list.insert("end", " / ".join(component.rsplit("__", 1)[0] for component in path))
        self.current_scope = library.scope
        self.status.set(f"已读取 {library.name}，共 {len(paths)} 个分类。")
        self.set_busy(False)

    def choose_directory(self):
        directory = filedialog.askdirectory(title="选择独立的 PDF 导出目录")
        if directory:
            self.output.set(directory)

    def export(self):
        if not self.api or not self.output.get().strip():
            messagebox.showinfo("选择目录", "请先读取分类，并选择一个独立的导出目录。")
            return
        selected = None if self.all_collections.get() else [self.collection_keys[i] for i in self.collection_list.curselection()]
        if selected == []:
            messagebox.showinfo("选择分类", "请选择至少一个分类，或勾选「导出整个文献库」。")
            return
        library = self.libraries[self.library_box.current()]
        output = self.output.get().strip()
        descendants, verify = self.descendants.get(), self.verify.get()
        self.status.set("正在读取 PDF 列表并导出……")

        def operation():
            snapshot = self.api.snapshot(library)
            return export_snapshot(snapshot, output, lambda key: self.api.pdf_path(library, key),
                                   selected, descendants, verify,
                                   lambda msg: self.events.put(("progress", msg)))
        self.work(operation, "exported")

    def open_directory(self):
        path = Path(self.output.get().strip()) if self.output.get().strip() else None
        if path and path.is_dir():
            os.startfile(path)

    def poll(self):
        for _ in range(100):
            try:
                event, value = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                if event == "progress":
                    self.status.set(value)
                    self.append_log(value)
                elif event == "error":
                    self.set_busy(False)
                    self.status.set("操作未完成：" + value)
                    self.append_log(value)
                    messagebox.showerror("操作未完成", value)
                elif event == "connected":
                    self.api, self.libraries, collections = value
                    self.library_box["values"] = [lib.name for lib in self.libraries]
                    self.library_box.current(0)
                    self.show_collections(self.libraries[0], collections)
                elif event == "collections":
                    self.show_collections(*value)
                elif event == "exported":
                    self.set_busy(False)
                    self.status.set(value.summary())
                    self.append_log(value.summary())
                    for issue in value.missing + value.errors:
                        self.append_log(str(issue.get("title", issue.get("path", "PDF"))) + "：" + issue["reason"])
                    for warning in value.warnings:
                        self.append_log(warning)
                    text = value.summary()
                    if value.missing or value.errors:
                        messagebox.showwarning("导出部分完成", text + "\n详情见下方日志和 .zotero-export/last-report.json。")
                    else:
                        messagebox.showinfo("导出完成", text)
            except Exception as exc:
                self.set_busy(False)
                self.status.set(str(exc))
                messagebox.showerror("操作未完成", str(exc))
        self.root.after(100, self.poll)


def run():
    root = tk.Tk()
    ttk.Style(root).theme_use("vista" if "vista" in ttk.Style(root).theme_names() else "clam")
    ExportWindow(root)
    root.mainloop()
