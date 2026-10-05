# zotero-dense-pdf

Zotero PDF 分类导出工具。

这是一个 Windows 本地原型：读取 Zotero 的分类和子分类，把已经位于本机的 PDF 复制到独立目录。重新导出时检查变化并更新文件。导出内容是普通 PDF 和文件夹，任何软件都可以读取。

需要 Python 3.10 或更新版本，带 tkinter，以及提供本地 API 的较新版本 Zotero。全部使用 Python 标准库，无需安装额外依赖。目前已在 Python 3.13 / Windows 和本机 Zotero 9.0.6 下验证。

## 使用

1. 启动 Zotero。
2. 在 Zotero 的 **设置 → 高级** 中开启 **允许此计算机上的其他应用程序与 Zotero 通信**。本机接口使用端口 23119。
3. 双击本目录的 **启动.cmd**。启动脚本会优先使用 `py -3`，再检查 PATH 中其他可用的 Python，并跳过 Microsoft Store 的占位别名。也可在终端运行 `py -3 app.py gui` 或 `python app.py gui`。
4. 点击 **读取分类**，选择文献库。可以导出全库，或取消全库选项后选择多个分类；所选分类默认包含子分类。
5. 选择一个独立的导出文件夹，点击 **导出 / 更新 PDF**。

以后选择同一个导出目录，再次导出即可更新。只导出本地可读取的 PDF；尚未下载的附件、失效的链接附件会显示在日志里。需要先在 Zotero 中下载或修复，再重试。

## 输出结构

```text
导出目录/
  我的文献库__user-0/
    分类名称__分类Key/
      子分类名称__分类Key/
        年份 - 作者 - 标题__条目Key__附件Key.pdf
    未分类__unfiled/
      ...pdf
  .zotero-export/
    manifest.json        # 文件对应关系和校验信息
    last-report.json     # 最近一次导出结果
    archive/             # 被替换、改名、移出分类或失效的旧副本
```

- 文件夹附加分类编号，PDF 附加条目和附件编号，避免同名冲突。
- 保留 Zotero 子分类层级。只导出选定子分类时仍保留其父分类路径。
- 同一篇文献属于多个分类时，在各分类中复制一份 PDF，因此会额外占用磁盘空间。
- 一个条目有多个 PDF 附件时分别导出。全库导出包含未分类条目及独立 PDF 附件。
- 文献库本地元数据每次重新扫描；PDF 按文件大小、修改时间和 SHA-256 判断是否需要更新。勾选 **完整校验** 会重新计算哈希，适合核对保留了原修改时间的文件变化。
- 分类或标题改名后，生成新路径，旧副本进入归档。源条目或分类关系移除后，相关旧副本进入归档。
- PDF 在本地暂不可读时，保留已导出的旧副本，并报告缺失。
- 只更新本工具记录中的 PDF。用户自行放入的 Markdown、其他 PDF 和目录内容会保留。发生同名冲突时改用带后缀的新文件名。
- 分类范围之外、之前导出过的文件会保留。选择全库导出时才对全库已管理的副本进行整理。
- 归档不自动清理，可通过 `archive` 中的 `mapping.json` 查看旧路径与归档文件的对应关系。
- 不要手动改动 `.zotero-export`，它用来识别哪些 PDF 由工具管理。建议使用较短的导出路径，避免 Windows 的长路径限制。

此版本只通过 GET 请求读取本机 Zotero，并复制原始 PDF 文件。Zotero 内置阅读器的批注通常在其数据库中；直接复制 PDF 不会把这些批注嵌入文件。笔记、批注、全文解析和研究记录不在本工具的导出范围中。

## 命令行

```powershell
# 读取文献库及分类编号
python app.py probe

# 导出个人文献库
python app.py export --output "D:\ResearchPDFs"

# 导出指定分类及其子分类；参数可以重复
python app.py export --output "D:\ResearchPDFs" --collection ABCD1234

# 导出群组文献库
python app.py export --output "D:\ResearchPDFs" --library group:123456

# 完整校验文件内容
python app.py export --output "D:\ResearchPDFs" --verify

# 使用明确标注的演示 PDF 测试分类结构
python app.py demo --output "D:\ZoteroExportDemo"

# 测试（工作目录下的 work/ 用于临时文件）
python -B -m unittest discover -s tests -v
```

命令行退出码：`0` 完成；`1` 连接、配置或整体导出失败；`2` 部分完成，存在缺失或失败的 PDF。

## 验证范围

自动测试覆盖：子分类、多分类、未分类、重复导出、源文件更新、完整校验、用户文件保留、改名、局部导出、附件缺失、文献移除、同名冲突、数据库切换、路径边界、分页以及本地文件地址重定向。

27 项测试中，26 项通过；1 项符号链接测试因当前 Windows 权限不支持创建符号链接而跳过。另已使用模拟的本地 HTTP API 和实际示例 PDF 完成两次命令行导出验证，并检查桌面窗口的初始化、分类选择和任务状态切换。

已使用 Zotero 9.0.6 的真实本地个人库验证完整导出及重复导出。首次导出没有缺失或失败；再次完整计算 SHA-256 时，全部副本均判定为未变化。

仓库包含程序、测试和演示数据生成代码。实际文献 PDF、同步记录，以及 `work/`、`outputs/` 中的本地文件由 `.gitignore` 排除。

接口依据：[Zotero Local API](https://www.zotero.org/support/dev/web_api/v3/local_api)、[Zotero 分类模型](https://www.zotero.org/support/collections_and_tags)、[批注存储说明](https://www.zotero.org/support/kb/annotations_in_database)。
