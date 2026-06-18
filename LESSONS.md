# 项目经验教训

本文档记录 CHM 转 Markdown GUI 工具开发过程中踩过的坑和应对策略，供后续项目参考。

---

## 1. TOC 层级编号策略

**问题**：CHM 目录层级可能很深（实测有 4 级，理论上无上限）。最初考虑字母编号（A. B. C...），但英文字母只有 26 个，如果某层超过 26 个节点就会溢出。

**方案**：采用**纯数字点分隔**，如 `1.2.7.5`。数字无上限，天然支持任意深度和广度。

**教训**：设计编号/标识系统时，优先考虑无边界方案，避免预设限制。

---

## 2. 死代码清理

**问题**：代码审查发现 4 个已实现但从未被调用的函数：
- `_save_toc_outputs()` — 保存 TOC 调试信息
- `_create_metadata()` — 生成 metadata.json
- `_create_readme()` — 生成输出目录 README
- `_copy_images()` — 复制图片资源

**修复**：在 `convert_chm()` 主流程返回前接入这 4 个函数。

**教训**：发布代码前做一次完整的调用链审查。IDE 的 linter 能检测未使用变量，但很多时候检测不到未调用的函数（特别是在动态调用环境中）。手动审查不可替代。

---

## 3. 临时文件清理

**问题**：程序解压 CHM 创建临时目录，如果中途崩溃或用户强制关闭，临时目录会残留在磁盘上。

**修复**：使用 `atexit.register()` 注册清理回调：
```python
_temp_dirs = set()
def _register_temp_dir(path):
    _temp_dirs.add(path)
def _cleanup_temp_dirs():
    for d in _temp_dirs:
        shutil.rmtree(d, ignore_errors=True)
atexit.register(_cleanup_temp_dirs)
```

**教训**：任何创建临时资源的程序都应该注册退出清理。`atexit` 覆盖正常退出，`ignore_errors=True` 防止清理本身抛异常。

---

## 4. 数据边界保护

**问题**：ITSF 解析 CHM 文件时，计算 `end_data = 4096 - free_space`，如果 `free_space` 接近 0，`end_data` 可能为负值，导致 `seek()` / `read()` 崩溃。

**修复**：加边界钳制：
```python
end_data = max(20, min(4096 - free_space, 4096))
```

**教训**：任何由运行时数据派生的计算结果，只要用作索引、偏移量、长度等参数，都必须加边界保护。宁愿少读一些数据，不能让程序崩溃。

---

## 5. `subprocess` 中 `shell=True` 的陷阱

**问题**：原代码 `subprocess.run([cmd, ...], shell=True)` 混用了列表参数和 `shell=True`。在 Windows 下，`shell=True` 期望一个字符串命令，传列表会导致命令无法正确执行。

**修复**：
- 方案 A：用字符串 + `shell=True`：`subprocess.run(f'"{cmd}" ...', shell=True)`
- 方案 B（推荐）：用列表 + `shell=False`（默认）

**教训**：`shell=True` 与列表参数不兼容。除非必须使用 shell 特性（管道、重定向），否则用列表参数且不加 `shell=True`，更安全、跨平台。

---

## 6. While 循环防护

**问题**：`_write_page_md()` 中解析 HTML 时使用了 `while True` 循环，如果结构异常可能死循环。

**修复**：加迭代上限：
```python
c = 0
while True:
    c += 1
    if c > 100:
        break
```

**教训**：任何 `while True` 都应该有明确的退出保护机制。约定用计数器或超时来防止死循环。

---

## 7. 数据结构的全局一致性

**问题**：TOC 数据从 `TOCNode` 到扁平列表、再到 `toc_map`、再到输出文件，经过多层转换。每增加一个字段（如 `number`），所有环节都需要适配。

**涉及的结构**：
```
TOCNode (类) → 6元组 (parent, fn, title, local, has_children, number) → toc_map
```

**修复**：逐一更新 `_toc_to_flat()`、`_remap_flat()`、`_write_index()`、`_toc_to_markdown()`、`convert_chm()`。

**教训**：核心数据结构的变更需要全文搜索所有引用点，用一个独特的字段名（如 `numbers` 参数）辅助搜索能提高效率。

---

## 8. GitHub 发布清单

发布到 GitHub 前的检查清单：

- [ ] `git config user.name` 和 `user.email` 已配置
- [ ] GitHub 邮箱隐私设置：要么公开邮箱，要么用 `@users.noreply.github.com`
- [ ] `.gitignore` 已创建，排除 `__pycache__`、临时目录、IDE 配置
- [ ] `LICENSE` 文件已添加（本项目用 MIT）
- [ ] `README.md` 包含：功能说明、安装步骤、使用方法、输出结构
- [ ] `requirements.txt` 版本号精确（如 `>=4.12.0` 而非 `>=4.12`）
- [ ] 敏感信息（token、密码、内部路径）已清除
- [ ] 提交信息清晰，用 [Conventional Commits](https://www.conventionalcommits.org/) 格式

**教训**：GitHub 的 "Push declined due to email privacy restrictions" 是常见坑，原因是邮箱设置为隐私但提交中使用了真实邮箱。

---

## 9. 版本号规范

**问题**：`requirements.txt` 中写了 `beautifulsoup4>=4.12`，这种写法不够精确。

**修复**：统一改为 `>=4.12.0` 三补丁位格式。

**教训**：Python 包的版本号建议始终使用三位（主.次.补丁），让 pip 的依赖解析更可靠。

---

*记录于 2026-06-18*
