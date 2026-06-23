# 项目经验教训（跨项目全局共享）

本文档记录所有自编小程序开发过程中踩过的坑和应对策略，不限于某个具体项目。新增条目追加到末尾，按编号递增。涉及的项目包括 CHM 转 Markdown、知乎爬虫、微博爬虫等。

---

## 1. TOC 层级编号策略

**问题**：CHM 目录层级可能很深（实测有 4 级，理论上无上限）。最初考虑字母编号（A. B. C...），但英文字母只有 26 个，如果某层超过 26 个节点就会溢出。

**方案**：采用**纯数字点分隔**，如 `1.2.7.5`。数字无上限，天然支持任意深度和广度。

**教训**：设计编号/标识系统时，优先考虑无边界方案，避免预设限制。

---

## 2. 死代码清理 — 用户只需要转换结果

**问题**：项目最初生成了大量辅助文件：`_index.md`、`metadata.json`、`README.md`、`toc.json`、`TOC.md`、`file_mapping.json`、`assets/` 目录。用户反馈"我不需要看到这些额外的文件，我只需要看到转换结果"。

**修复**：直接**删除**这些文件的生成代码，而非接入流程。同时将 `.debug/` 调试目录移到系统临时目录下。

**教训**：
- 发布前一定要问自己：用户真正需要什么？不要从开发者视角堆砌文件。
- 这些函数写了但从未走通用户视角的审视——用"如果我是用户，打开输出目录最想看到什么"来过滤。
- 删代码和写代码同等重要。死代码留着既不维护又不调用，纯粹是噪音。

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

## 10. 批量转换的跨文件编号

**问题**：文件夹模式下有 46 个 CHM 文件，每个独立编号（都从1开始），46个"1.1概述"根本无法分辨来源。

**方案**：增加"整体统一编号"模式，按文件名排序后分配书序号（1-N），所有内部编号加书号前缀：`1.2.3` → `5.1.2.3`。

**关键设计决策**：
- **弹窗询问而非强制**：用 `messagebox.askyesno` 让用户自己决定，提供清晰的[是]/[否]说明
- **按文件名排序**：保证编号稳定可复现（如果按文件系统顺序则不可控）
- **目录名也带序号**：`01_xxx/`、`02_yyy/`，方便在文件管理器中排序
- **单文件模式不弹窗**：别给简单场景增加操作负担

**教训**：
- 批量处理场景必须考虑编号的全局唯一性，局部编号在多文件场景下是信息丢失
- 给用户选择权（弹窗），但要有合理的默认建议
- `Path.glob()` 返回顺序不保证排序，必须显式 `sorted()`

---

## 11. 用户反馈驱动迭代

**本轮对话中用户提出的改进**：

| 用户反馈 | 改进 |
|---|---|
| "为什么没有数字编码？" | 编号从标题层面扩展到目录名和文件名 |
| "我不需要看到这些额外的文件" | 删除所有辅助输出文件 |
| ".debug 文件是什么？不需要看见" | 调试目录移到系统临时目录 |
| "文件夹内有很多 chm，需要整体考虑编号" | 文件夹模式加统一编号 |
| "多选时也应该适用" | 多选模式同样支持 |
| "应该有弹窗询问" | 添加 askyesno 对话框 |
| "把第二层直接命名在最外层，三层变两层" | 折叠根级目录，输出结构从三层简化为两层 |
| "文件名里的中文拼音太冗余" | ASCII 优先命名，纯中文时才兜底拼音 |
| "这四个函数定义但未调用，为什么不直接删除？" | 删除 _copy_images/_save_toc_outputs/_toc_to_markdown/_create_metadata/_create_readme |
| "VBScript和help.chm仍然深层嵌套" | 循环折叠 → 递归折叠，直到根级无has_children条目 |
| "需要重新转换验证" | 46个CHM全量重转，输出1514个.md文件，零子目录嵌套 |
| "文件大于10MB了，需要分割" | 手动分割1次后 → 集成进程序，转换完成自动检测+弹窗询问 |

**教训**：用户视角和开发者视角天然不同。开发者会想"这些文件可能有用"，用户只想"我需要的在哪里"。每次发布后，让用户实际使用并听取反馈，比闭门造车高效得多。

---

## 12. 目录结构扁平化（三层 → 两层）

**问题**：统一编号模式下，输出结构原为三层：
```
12_inovancetcpnet/                    ← 包裹层（拼音名）
└── 12.1 InovanceTcpNet驱动/          ← 实际内容层
    ├── InovanceTcpNet驱动.md
    ├── 12.1.1 概述.md
    └── ...
```
用户指出外层目录多余，应直接用 TOC 第一级条目命名。

**方案**：
1. **命名优化**：放弃拼音全转，改为 ASCII 优先。`InovanceTcpNet驱动` → `InovanceTcpNet`（中文尾巴丢弃），纯中文才兜底用拼音
2. **结构折叠**：将根级 `has_children` 条目的内容页提升到根目录，子节点提升一级，消除无意义的包裹层

**修复后**：
```
12_InovanceTcpNet驱动/     ← 直接用 TOC 首条目标题
├── InovanceTcpNet驱动.md   ← 目录页直接放根下
├── 12.1.1 概述.md
└── 12.1.2 配置.md
```

**关键代码**：`_safe_fn` 改为 ASCII 优先；`convert_chm` 中增加折叠逻辑，遍历 `flat` 列表将 `parent="" & has_children=True` 的条目提升。

**教训**：
- 输出结构应层层追问：这一层对用户有价值吗？
- 混合中英文命名时，保留 ASCII 信息通常比全转拼音更可读
- 统一编号下第一个 TOC 条目天然适合做根目录名，无需额外包装

---

## 13. 递归折叠 — 单次不够，循环直到收敛

**问题**：初始折叠逻辑只执行**一轮**——提升一层根级目录节点的子节点。对于只有 1-2 层嵌套的驱动文档（01-44）够了，但 VBScript（4层）和 help.chm（8层）只被剥掉最外皮，里面仍然深层嵌套：

```
修复前（单次折叠）：
24_欢迎使用InPlant SCADA软件在线帮助/
└── 24.1 InPlant SCADA简介/
    └── 24.1.1 系统架构/        ← 仍然嵌套！
        └── 24.1.1.1 概述.md    ← 文件埋在深处

修复后（递归折叠）：
09_欢迎使用InPlant SCADA软件在线帮助/
├── 欢迎使用InPlant SCADA软件在线帮助.md
├── 9.1.1 系统概述.md           ← 零嵌套！
├── 9.1.2 快速入门.md
├── ...
└── (1028 个文件全部平铺)
```

**方案**：将单次折叠改为 `while` 循环，每轮折叠一层，直到 `flat` 中根级不再有 `has_children` 条目。同时加入：
- **收敛检测**：`if new_flat == flat: break`，防止无变化时的死循环
- **迭代上限**：`while collapse_rounds < 50`，硬保护防止失控
- **日志**：输出折叠轮数 `📐 折叠根级目录 (N轮)`，便于调试

**验证**：46 个 CHM、1637 个 HTML 页面，输出 1514 个 `.md` 文件，分布在 46 个目录中，所有目录最大嵌套深度 = **0**。

**教训**：
- 写折叠/递归逻辑时，默认按深度未知来设计——用循环而非单次 if
- 循环必须有"无变化即停止"的比较逻辑，不能靠固定次数
- 始终加硬上限（如 50/100）作为最终防线
- 日志输出轮数，出问题时能一眼看到折叠了基层

---

## 14. 大文件自动检测与分割

**问题**：部分 CHM 中包含大量 base64 内嵌图片的页面，生成的 .md 文件超过 25 MB。GitHub 虽然支持单文件最大 100 MB，但超大文件影响在 IDE 中的浏览和检索体验。

**方案**：
1. 转换完成后扫描输出目录，检测 >10 MB 的 .md 文件
2. 弹窗 `messagebox.askyesno` 列出所有超大文件，询问是否分割
3. 用户确认后，按 H2 标题边界将文件切分为 <10 MB 的多个部分
4. 文件名保留原编号，加 `(1)(2)(3)` 后缀

**实现关键点**：
- **线程安全**：检测在 worker 线程完成，弹窗通过 `self.r.after(0, ...)` 调度到主线程
- **分割策略**：优先按 H2 标题边界分割（保持语义完整性）；单章节超限时回退到行级分割
- **Greedy 拼装**：逐 section 累积，超过阈值时输出当前 chunk 并开始新 chunk

**教训**：
- 用户先手动操作一次 → 发现这是重复性需求 → 集成进程序，这是最自然的迭代路径
- `rglob('*.md')` 比 `glob('*.md')` 更能兜底（万一输出目录还有嵌套子目录）
- GUI 中线程切换：耗时 IO 在子线程，弹窗在主线程，用 `after(0, ...)` 桥接
- `_log` 的 tag 不仅用于分类，也用于颜色——新增日志级别时记得配置 `tag_configure`

---

## 15. GitHub 发布前 .gitignore 的精确性

**问题**：知乎爬虫项目的 `.gitignore` 写了 `*.json` + `!config.json` 例外。这导致 `id_list.json`（用户 ID 管理数据，需要跟踪）被误拦截，同时 `browser_data/zhihu_state.json`（含完整 Cookie）依赖目录级忽略才逃过一劫——如果哪天 `browser_data/` 规则被误删，敏感数据会直接暴露。

**方案**：放弃 `*.json` 通配，改用精确规则：
```gitignore
# 个人配置文件（含本地 Chrome 路径等）
config.json

# 爬虫输出 & 敏感数据（整目录忽略）
output/
browser_data/
```

**教训**：
- `.gitignore` 中通配规则越宽，越容易产生意外（误伤或漏网）。优先使用目录级忽略 + 明确的文件名。
- `!exception` 规则叠加宽泛通配会增加心智负担——不如直接不写宽泛规则。
- 总是在发布前执行 `git add --dry-run .` 验证哪些文件会被跟踪。

---

## 16. 个人配置与模板分离

**问题**：`config.json` 包含用户本地 Chrome 路径（`C:\Program Files\...`），直接入库会暴露个人信息且无法被他人使用。但项目又需要一个配置参考，让新用户知道有哪些选项。

**方案**：双文件策略：
- `config.json` → 加入 `.gitignore`，不入库
- `config.example.json` → 入库跟踪，所有字段填入示例/空值，加 `_comment` 提示"复制此文件为 config.json"

**同样适用于** `id_list.json`：入库前重置为 `{"users":[],"updated":""}` 空白模板，避免泄露个人追踪数据。

**教训**：
- 任何含个人路径、用户名、API Key 的配置都应提供 `.example` 模板而非直接入库
- 用户数据文件（如历史记录、追踪列表）入库前必须清空为模板状态
- `git add --dry-run` 是发布前最后一道防线

---

## 17. README 与代码同步

**问题**：知乎爬虫重构后（移除 3 种输出模式复选框、新增法务模式独立勾选），README 仍然显示旧的 UI 示意图（含"☑下载图片 ☐无头模式"）和过时的配置表（缺少 `forensic_mode`、`test_mode` 等字段）。

**修复**：
- 移除 ASCII 示意图（GUI 界面变化频繁，文字图跟不上），改为项目结构列表
- 补全配置参数表所有字段
- 增加混合输出模式、法务证据模式的文字说明

**教训**：
- 每次重构结束必须同步更新 README，作为发布流程的最后一步
- 文字描述比 ASCII 示意图更易维护——图需要逐行对齐，改一个字符可能整幅图错位
- 配置表用 Markdown 表格形式，增删字段只需加一行，比示意图低维护成本

---

## 18. 未使用依赖的清理

**问题**：知乎爬虫的 `requirements.txt` 列出 `httpx>=0.27.0`，但全局搜索代码无任何 `import httpx`。这是早期开发时预备引入但最终用了 `requests` 的遗留。

**修复**：直接删除，保持 `requirements.txt` 精确反映实际依赖。

**教训**：
- 发布前用 `grep -r "import httpx"` 或 IDE 全局搜索验证每个依赖是否真正被使用
- 每个未使用的依赖都会增加安装时间、依赖冲突风险和安全攻击面
- 这也是 `pip freeze > requirements.txt` 的问题——它会倾倒所有传递依赖，应该手工维护顶层依赖列表

---

## 19. 模块级单例与 UI 实例的双对象陷阱

**问题**：知乎爬虫 GUI 中，`config.py` 在模块导入时创建全局单例 `config = Config.from_file()`。GUI 又通过 `self._cfg = Config.from_file()` 创建了**第二个实例**。用户勾选"测试模式"后，`_read_config_from_ui()` 只修改了 `self._cfg.test_mode = True` 并保存到 `config.json`，但爬虫线程读取的是模块级 `config.test_mode`——这个单例自导入后再未被更新，始终为 `False`。

结果是：GUI 显示"🧪 测试模式: 开启"，但爬虫仍然滚动了 134 条回答才停。

**修复**：在 `_read_config_from_ui()` 末尾，`save()` 之后增加同步：
```python
# 同步到模块级 config 单例（爬虫线程读取此对象）
for f in self._cfg.__dataclass_fields__:
    setattr(config, f, getattr(self._cfg, f))
```

**教训**：
- 模块级单例 + UI 层实例 = 双对象，两边必须同步，否则静默不一致
- 测试模式这种"看起来对但实际无效"的 bug 远比崩溃难发现——日志显示"测试模式开启"但行为不符
- 简单自查：项目中 `from xxx import obj` 的模块级单例有几处被修改？修改都同步回去了吗？
- 如果可能，设计上避免双对象——要么 UI 直接改 `config`，要么爬虫始终从文件重读

---

## 20. Playwright `:has-text` 伪类仅在 locator() 可用

**问题**：知乎爬虫在 `page.query_selector('button:has-text("评论")')` 和 `card.query_selector('button:has-text("阅读全文")')` 中使用 `:has-text`，运行时报错：
```
':has-text' was detected as a pseudo-class and is either unsupported or invalid
```

**根因**：`:has-text()` 是 Playwright 专有伪类，仅在其 `locator()` API 中生效。`page.query_selector()` 和 `element_handle.query_selector()` 走的是浏览器原生 CSS 选择器，不支持 `:has-text`。

**修复**：
- 页面级搜索 → 改用 `page.locator('button:has-text("阅读全文")').first`，通过 `.count() > 0` 判断存在
- 元素级搜索 → 先尝试标准 CSS 选择器，不命中时手动遍历 `card.query_selector_all('button')` 再按 `inner_text()` 筛选

**教训**：
- Playwright 中 `locator()` 和 `query_selector()` 是两套选择器引擎，伪类不通用
- 凡是含 `:has-text`、`:has()`、`:is()` 等非标准伪类的选择器，必须走 `locator()`
- 元素级搜索没有 `locator()` 可用时，fallback 到手动遍历 + 文本匹配

---

## 21. BS4 不支持 `:has-text`；用 find 代替

**问题**：`converter.py` 中 `soup.select_one('button:has-text("评论")')` 同样报了 `:has-text` 不支持。

**修复**：去掉 `'button:has-text("评论")'` 选择器，改用 BeautifulSoup 的 `soup.find('button', string=re.compile(r'评论'))`

**教训**：
- BeautifulSoup 的 `select_one/select` 走的是 CSS 选择器（基于 SoupSieve），仅支持标准 CSS 伪类
- 需要按文本内容查找时 → `soup.find(tag, string=regex)` 或手动遍历
- `:has-text`、`:has()` 这类"快捷语法"在不同库间不可移植

---

*记录于 2026-06-18，最后更新于 2026-06-19*

---

## 22. 爬虫长时任务需要短时缓存避免重复网络请求

**场景**：知乎爬虫测试模式下爬了 5 条，二次运行时（比如上次没正确保存）需要重新滚动加载回答列表、逐条打开回答页、截图转 Markdown，即使距离上次运行只有几分钟。

**根因**：爬虫无状态，每次启动从头开始收集链接→逐条请求，不记忆最近已获取的内容。

**解决方案**：引入双层短时缓存（TTL 可配，默认 30 分钟）：

| 缓存层 | 存储位置 | 内容 | 命中后跳过 |
|--------|---------|------|-----------|
| 链接列表缓存 | `cache/links.json` | 滚动收集的回答链接 + 时间戳 | `collect_answer_links()` 全流程 |
| 回答内容缓存 | `cache/{answer_id}.json` | meta/md_text/html/screenshot | `crawl_answer_combined()` 单页请求 |

**实现要点**：
- 缓存独立于 progress.json（进度保存失败不影响缓存命中）
- 每次爬取成功后写入缓存，下次运行命中时跳过网络 + 截图开销
- GUI 提供 "缓存有效期(分)" 设置，设 0 可完全禁用
- `collect_answer_links` 开头检查链接缓存，命中直接 return
- 逐条爬取循环中 `load_answer_cache()` → 命中则跳过 `crawl_answer_combined()`

**教训**：
- 爬虫项目的"断点续传"不应只依赖磁盘文件，内存级的短时缓存是好补充
- 两层缓存各自独立：链接缓存过期=重新滚动，内容缓存过期或用完=重新请求单条
- TTL 设为 0 = 禁用，这是配置开关的标准设计模式

---

## 23. HTML 提取时嵌套按钮文字会污染数据

**场景**：知乎爬虫提取回答作者名时，`extract_answer_meta` 从 `.AuthorInfo-name` 取 `get_text(strip=True)`，结果得到"祈祈关注"——因为作者名元素内嵌套了"关注"按钮，`get_text()` 会把子孙文本全部拼接。

**修复**：提取后加清洗步骤：`re.sub(r'关注\s*$', '', author).strip()`

**教训**：
- `get_text(strip=True)` 会递归提取所有后代文本节点，包括嵌套按钮文字
- 爬虫中凡是提取"名称"类字段（作者、标题、分类），都应怀疑是否有 UI 控件文字混入
- 清洗正则要保守：`关注\s*$` 只去尾部，避免误删名字中间含"关注"的合法用词

---

## 24. 截图应裁剪到内容区而非整页

**场景**：知乎回答页右侧有"相关推荐""创作者信息"等侧边栏，`page.screenshot(full_page=True)` 截下整页，文件膨胀且偏离证据目的。

**方案**：三级回退策略截取内容区：
1. 定位主内容列容器（`.Question-mainColumn`）→ `element.screenshot()`
2. 拼接问题标题 + 回答卡片的 bounding box → `page.screenshot(clip=...)`
3. 回退整页截图

**教训**：
- `element.screenshot()` 比整页截图更精准，字节量更小（base64 嵌入 MD 最敏感）
- `clip` 参数需要同时知道 x/y/width/height，跨元素拼接时注意取 min/max 计算矩形
- 三级回退保证健壮性：知乎改版导致选择器失效不会让截图功能全灭
- alt 文本要与实际截图内容匹配——从「整页截图」改为「问题与回答截图」

---

## 25. 短时缓存在测试迭代中会成为"静默陷阱"

**场景**：改完爬虫代码（author清洗/影响力数据/截图裁剪）后重跑，输出 MD 毫无变化。排查发现两个旧数据源拦截了新代码：`progress.json` 跳过已完成的回答 ID（"⏭ 跳过 5 条已完成"），回答内容缓存直接返回旧 `md_text/base64`。

**修复**：新增 `force_no_cache` 配置项 + GUI 复选框，启用后三重拦截生效：
1. `storage.py`：`load_links_cache`/`load_answer_cache` 直接返回 None
2. `crawler.py`：`crawl_user_answers` 清空 `completed_set`，全部视为新条目
3. `gui.py`：启动日志提示"🔄 强制忽略缓存: 开启"

**教训**：
- 缓存系统必须配套"一键旁路"开关，否则每次改代码验证都得手动删 `cache/` + `progress.json`
- 旁路开关应覆盖所有缓存层：内存层(TTL检查)、存储层(JSON文件)、进度层(completed_set)
- 开关命名要直白——"强制忽略缓存"比"禁用TTL"或"清理模式"更能让用户立即理解用途
- GUI 应当有视觉提示：勾选后爬取量可能翻倍，需和测试模式搭配使用

*记录于 2026-06-18，最后更新于 2026-06-19*

---

## 26. Markdown 中 data:image/png;base64 超大单行会导致渲染失败

**场景**：知乎爬虫截图从 1280×800 改为 1920×1080（含 `device_scale_factor: 2` Retina 渲染）后，单张截图产生近 80 万字符的 base64 字符串。嵌入在 `![screenshot](data:image/png;base64,AAAAAA...)` 这行时，部分 Markdown 渲染器（VS Code、GitHub、Typora 等）因单行过长无法解析，显示 "load image failed"。

**修复**：
1. 将截图从 base64 内嵌改为**独立 PNG 文件 + 相对路径引用**：
   - `crawl_answer_combined` 返回 `screenshot_bytes`（原始 PNG 字节流）
   - MD 中用 `![截图](./{answer_id}.png)` 引用
   - `crawl_user_answers` 在同目录下保存 `{answer_id}.png`
2. 缓存兼容：JSON 不支持 bytes，缓存时 `base64.b64encode` / 读取时 `base64.b64decode`
3. 同步更新 `crawl_answer_screenshot`（死代码）保持返回键一致

**教训**：
- Markdown 的 `data:` URI 嵌入不适合大型二进制数据——单行长度超过几十万字符时几乎所有解析器都有问题
- 截图应作为独立资源文件管理：可独立打开查看、不污染 MD 文件大小、支持增量备份
- 缓存序列化时注意类型兼容：Python dict→JSON 需要 bytes→base64 转换，读取时反转
- 输出结构需要预先考虑资源文件（PNG/JPG/HTML）的命名和引用方式，避免后期重构

*记录于 2026-06-19*

---

## 27. Tkinter `grab_set()` 在 Windows 下导致窗口切换死锁

**场景**：GUI 弹出对话框（管理分组、Cookie编辑等）时使用 `dialog.grab_set()` 创建模态锁。用户 Alt+Tab 切换到其他程序后，grab 锁阻止返回主窗口和对话框，界面彻底卡死。

**修复**：全部 `grab_set()` → `focus_set()`。对话框仍然前置并获得焦点，但不阻塞窗口切换。

**教训**：
- Tkinter 的 `grab_set()` 创建的是**本地模态锁**（local grab），不仅阻塞父窗口，在 Windows 下还会干扰全局事件路由
- 非关键确认流程（保存/编辑/管理）用 `focus_set()` + `transient()` 即可，模态行为应为 `wait_window()` 的副作用而非 `grab_set()`
- 跨平台 GUI 库的"模态"语义差异大：Windows 的 local grab ≠ modal dialog，需实测验证

*记录于 2026-06-20*

---

## 28. Tkinter Listbox：`exportselection=True` + 编辑区交互的三重陷阱

**场景**：管理分组对话框，用户选中 Listbox 中的分组 → 在 Text 编辑区粘贴新关键词 → 点"保存修改"。三个 bug 叠加导致粘贴内容无法正确保存：

1. **`exportselection=True`（默认）**：点击 Text 使得 Listbox 失焦，选中被清除，`curselection()` 返回空 → 保存时弹窗"请先选择一个分组"
2. **`<<ListboxSelect>>` 事件绑定**：原代码将 `on_select` 绑定到该事件，任何点击分组都会**无条件清空 Text 并填入旧关键词** — 用户刚粘贴的内容瞬间丢失
3. **初始预填旧数据**：对话框打开时自动 `load_selected()` 填入第一个分组的关键词，用户粘贴时若未 Ctrl+A 全选，旧关键词残留混入

**修复（三管齐下）**：
- `Listbox(exportselection=False)` — 失焦不清选中
- 移除 `<<ListboxSelect>>` 绑定 — 选中分组不触发任何编辑区操作
- 初始不调用 `load_selected()` — 编辑区空白，由用户决定何时"📥 加载"

**教训**：
- **Tkinter 的 `exportselection` 默认值是反直觉的**：99% 的场景你不想让它清空选中，但默认却是 True
- **事件驱动的 UI 中，"选中 = 加载"的耦合是错误的**：选中（导航）和加载（编辑）是独立操作，应分离
- **自动化预填是双刃剑**：方便快速编辑，但隐藏了"旧数据污染新输入"的风险；初始空白 + 显式加载按钮更安全
- **多次"修好了"同一功能时，说明没有穷尽事件链**：Tkinter 的事件模型需要逐帧推演所有焦点转移路径

*记录于 2026-06-20*

---

## 29. API 默认语义：合并 vs 替换 — 用户期望优先

**场景**：`keyword_mgr.add_group()` 同名分组时执行关键词合并（`existing + new`），但用户的直觉操作是"粘贴新关键词 → 保存到已有分组 → 替换旧关键词"。合并导致旧关键词"污染"新内容。

**修复**：`add_group()` 新增 `replace=False` 参数，默认保持合并以兼容旧调用，`_save_as_group` 传 `replace=True`。

**教训**：
- **用户操作模型决定 API 语义**：用户"保存为分组"的心理模型是"覆盖式写文件"，不是"追加"。合并应作为可选功能（如"追加到分组"），而非默认
- **默认值优先考虑最常用的调用场景**：如果 80% 的调用来自 GUI 的保存操作（期望替换），那默认应为替换
- **命名要诚实**：`add_group` 暗示"新增"，但同名时却是"合并更新" — 这种语义矛盾是 bug 的温床。`save_group` 或 `upsert_group` 更准确

*记录于 2026-06-20*

---

## 30. GUI 下拉框选中 ≠ 数据已进入目标控件 — "选中即应用" vs "选中只预览"

**场景**：关键词分组管理，用户从下拉框选中分组 → GUI 日志显示"📂 分组「蔚来」: 蔚来, 换电, ..." → 用户认为关键词已生效 → 点击"开始爬取"。结果日志中无任何关键词过滤信息，所有 145 条回答被无过滤爬取。

**根因**：`_on_group_selected` 的实现是"选中只预览"——只在日志打印关键词列表（`self._log(f"📂 分组「{name}」: {preview}")`），**不填入关键词输入框**。`_start_crawl()` 从输入框 `_keyword_entry.get()` 读取关键词，永远为空 `""`，过滤逻辑从不触发。

**上下联动的欺骗性**：管理分组对话框里粘贴/保存看似成功（`✏ 已更新分组「蔚来」(19个关键词)`），但关闭后主窗口输入框从未被更新。用户选分组看到预览日志以为已生效，实际关键词数据停留在数据库（keyword_mgr），从未进入爬虫流水线。

**修复（三处联动）**：
- `_on_group_selected`：从"仅打印预览"改为"自动填入 `_keyword_entry`"，日志改为 `✅ 已应用分组`
- `_manage_groups` → `do_update()`：保存后同步更新主窗口 `_keyword_entry` + `_group_var`
- 对话框提示："（已自动填入关键词输入框）"

**教训**：
- **GUI 中数据流有多环节，不可假设"可见即生效"**：下拉框选中 → 打印预览 → 用户认为已生效，但实际数据流是：用户选择 → ComboboxSelected 事件 → `_on_group_selected`回调 → 仅 `self._log()` → 结束。`_keyword_entry` 从未被写入
- **"选中查看"和"选中应用"是两种不可混用的交互模型**：前者适合数据多需要逐一浏览的场景（如文件列表），后者适合"选一个就用它"的决策场景（如关键词分组）。实现前必须确定当前场景需要哪个
- **中间状态日志制造了虚假安全感**：`📂 分组「蔚来」: 蔚来, 换电, 李斌...` 这行日志让用户以为"系统已经知道了我要用这些关键词"，但实际上只是打印了一行字。**日志内容 ≠ 数据已就位**
- **功能测试应验证端到端数据流**，而非逐控件测试。在这个案例中，每个控件单独测试都"正常"（下拉框能选中、预览能显示、保存成功），但合在一起就不工作，因为数据流断了

*记录于 2026-06-20*

---

### #31 Git 仓库重建策略：先 pull 远程再叠加新功能

**场景**：本地 `.git` 目录意外丢失（如重建项目），但远程仓库有完整历史。

**错误做法**：重新 `git init` 后直接 force push 新代码，这会丢失所有历史 commit。
**正确做法**：
1. `git init` → `git remote add origin` → `git fetch origin master`
2. `git reset --hard origin/master` 恢复远程历史
3. 在远程版本代码基础上精确叠加新功能（`replace_in_file` 而非全量覆盖）
4. 保留原有的所有细节（如 TempFileManager、经验教训、递归分割等）
5. `git add -A && git commit && git push`

**教训**：
- **重建 ≠ 重写**：项目文件丢失后重建，应以远程仓库为唯一信源，而非凭记忆重写
- **全量覆盖是危险的**：新写的代码可能遗漏远程版本积累的边界处理（如 `_split_large_md` 的递归分割逻辑）
- **diff 对比是必修课**：push 前必须确认 diff 只包含预期的增量改动，没有意外删除

### #32 MHTML 解析：Python email 标准库是最佳选择

**场景**：浏览器"另存为单个文件"(.mhtml) 是 MIME multipart/related 格式。

**技术选型**：
- `email.message_from_bytes()` 解析 MIME 结构
- `.walk()` 遍历所有 part，提取 `text/html`
- `.get_payload(decode=True)` 自动处理 quoted-printable/base64 编码
- `email.header.decode_header()` 解码 RFC 2047 Subject 头获取标题

**教训**：
- **不要自己解析 MIME**：Python 标准库 email 模块已成熟处理所有边界情况
- **encoding 回退链**：utf-8 → gbk → latin-1，因为中文 MHTML 可能用 gbk 编码
- **标题优先级**：HTML `<title>` > MIME Subject > 文件名，因为 Subject 可能被截断

### #33 bs4.Comment 导入方式：直接用 `from bs4 import Comment`

**错误写法**：
```python
soup.find_all(string=lambda t: isinstance(t, type(soup('<!-- -->')[0])))
```
这在 BeautifulSoup 4.12+ 中会触发 `IndexError`，因为空文档中找不到注释。

**正确写法**：
```python
from bs4 import Comment
soup.find_all(string=lambda t: isinstance(t, Comment))
```

*记录于 2026-06-23*
