#!/usr/bin/env python3
"""
CHM/HTML/MHTML/URL → Markdown Converter (GUI)
将 CHM 帮助文档、HTML 网页、MHTML 存档、在线 URL 转换为带层级编号的结构化 Markdown 文件。

支持 6 种输入模式:
  · CHM 单文件 / 多个 CHM / CHM 文件夹
  · HTML 文件 (.html/.htm)
  · MHTML 文件 (.mhtml, 浏览器单文件保存)
  · 网页 URL (在线抓取)

灵感来源: chy5301/chm-to-markdown-converter
依赖安装: pip install -r requirements.txt
"""

import os, re, json, base64, subprocess, tempfile, shutil, threading, time, atexit, email
from pathlib import Path
from urllib.parse import unquote, urlparse
from datetime import datetime, timedelta

from bs4 import BeautifulSoup, Comment
import html2text
try:
    import chardet
    HAS_CHARDET = True
except ImportError:
    HAS_CHARDET = False
try:
    from pypinyin import lazy_pinyin
    HAS_PYPINYIN = True
except ImportError:
    HAS_PYPINYIN = False
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


# ═══════════════════ Utilities ═══════════════════

_temp_dirs = set()  # 跟踪所有提取临时目录，确保退出时清理


def _register_temp_dir(tmp_dir):
    """注册临时目录，程序退出时自动清理"""
    _temp_dirs.add(str(tmp_dir))


def _cleanup_temp_dirs():
    """atexit 处理器：清理所有注册的临时目录"""
    for d in list(_temp_dirs):
        try:
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    _temp_dirs.clear()


atexit.register(_cleanup_temp_dirs)

def _clean_path(text):
    """Clean text for use as filename/dir, preserving Chinese"""
    if not text: return "untitled"
    text = text.replace('/', '／').replace('\\', '＼')
    text = re.sub(r'[<>:"|?*]', '', text).strip()
    return text or "untitled"

def _safe_fn(text):
    """ASCII优先命名：保留英文/数字，丢弃中文；纯中文才兜底拼音"""
    if not text: return "untitled"
    # 优先：只保留 ASCII 字符，丢弃中文等非 ASCII 部分
    ascii_only = re.sub(r'[^\x00-\x7F]+', '', text)
    ascii_only = re.sub(r'[<>:"/\\|?*]', '', ascii_only).strip()
    ascii_only = re.sub(r'\s+', '_', ascii_only)
    ascii_only = re.sub(r'[^a-zA-Z0-9_\-.]', '', ascii_only).lstrip('._')
    result = ascii_only.lower()[:100]
    if result:
        return result
    # 兜底：纯中文等无 ASCII 内容时，用拼音
    if HAS_PYPINYIN:
        return '_'.join(lazy_pinyin(text)).lower()[:100]
    return "untitled"


# ═══════════════════ Temp File Manager (auto-cleanup) ═══════════════════

class TempFileManager:
    """管理调试用中间文件，自动在TTL后清理"""
    _instance = None
    _DEFAULT_TTL = 1800          # 默认30分钟后自动删除
    _CLEANUP_INTERVAL = 300      # 每5分钟检查一次

    def __init__(self):
        self._dir: Path | None = None
        self._ttl = self._DEFAULT_TTL
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._running = False

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def init(self, parent_dir: Path, ttl: int = None):
        """初始化临时目录 (e.g. md_output/.debug/)"""
        if ttl is not None:
            self._ttl = ttl
        self._dir = parent_dir / '.debug'
        self._dir.mkdir(parents=True, exist_ok=True)
        # 写入 .gitignore 避免误提交
        gi = self._dir / '.gitignore'
        if not gi.exists():
            gi.write_text('*\n!.gitignore\n', encoding='utf-8')
        # 清理旧的残留
        self._purge_expired()
        # 启动定期清理
        self._start_periodic()

    @property
    def path(self) -> Path:
        if self._dir is None:
            raise RuntimeError("TempFileManager 未初始化")
        return self._dir

    def write(self, name: str, content: str) -> Path:
        """写入文本文件，返回路径"""
        fp = self.path / name
        fp.write_text(content, encoding='utf-8')
        return fp

    def write_json(self, name: str, obj) -> Path:
        """写入JSON文件"""
        fp = self.path / name
        fp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
        return fp

    def write_bytes(self, name: str, data: bytes) -> Path:
        """写入二进制文件"""
        fp = self.path / name
        fp.write_bytes(data)
        return fp

    def _purge_expired(self):
        """清理过期文件"""
        if self._dir is None or not self._dir.exists():
            return
        cutoff = time.time() - self._ttl
        for f in self._dir.iterdir():
            if f.is_file() and f.name != '.gitignore':
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass

    def _start_periodic(self):
        """启动定期清理定时器"""
        if self._running:
            return
        self._running = True

        def _loop():
            while self._running:
                time.sleep(self._CLEANUP_INTERVAL)
                if self._running:
                    self._purge_expired()

        t = threading.Thread(target=_loop, daemon=True)
        t.start()

    def shutdown(self):
        """停止定时器（程序退出时调用）"""
        self._running = False

    def schedule_delete(self, filepath: Path, delay_seconds: int = None):
        """计划在指定秒数后删除单个文件"""
        delay = delay_seconds or self._ttl

        def _del():
            time.sleep(delay)
            try:
                if filepath.exists():
                    filepath.unlink()
            except OSError:
                pass

        threading.Thread(target=_del, daemon=True).start()


# ═══════════════════ Encoding Detection (from GitHub) ═══════════════════

def detect_html_encoding(raw_data):
    """检测HTML编码 — 优先meta标签，其次chardet，默认gb18030"""
    # 尝试从meta标签中提取charset
    try:
        text = raw_data.decode("ascii", errors="ignore")
        charset_match = re.search(
            r'charset\s*=\s*["\']?([^"\'>\s]+)', text, re.IGNORECASE)
        if charset_match:
            charset = charset_match.group(1).lower()
            if charset in ["gb2312", "gbk", "gb18030"]:
                return "gb18030"
            elif charset in ["utf-8", "utf8"]:
                return "utf-8"
            else:
                return charset
    except Exception:
        pass

    # 使用chardet检测编码
    if HAS_CHARDET:
        try:
            result = chardet.detect(raw_data)
            if result and result["encoding"]:
                encoding = result["encoding"].lower()
                if "gb" in encoding or "chinese" in encoding:
                    return "gb18030"
                return encoding
        except Exception:
            pass

    # 默认使用gb18030（兼容中文CHM）
    return "gb18030"


def decode_html(raw):
    """解码HTML字节内容为字符串"""
    if isinstance(raw, str):
        return raw
    enc = detect_html_encoding(raw)
    try:
        return raw.decode(enc, errors='replace')
    except Exception:
        return raw.decode('utf-8', errors='ignore')


# ═══════════════════ HTML Cleaner (from GitHub) ═══════════════════

class HTMLCleaner:
    """HTML清理器 — 移除无用标签/属性/注释，提取标题"""
    REMOVE_TAGS = ["script", "style", "nav", "footer", "iframe", "noscript", "object"]
    REMOVE_ATTRS = ["style", "class", "id", "onclick", "onload", "onmouseover"]
    # 文档网站常见的导航/侧边栏容器（id/class 包含这些关键词则移除）
    SIDEBAR_KEYWORDS = ["sidenav", "sideaffix", "sidetoc", "sidefilter",
                        "toc-toggle", "navbar", "breadcrumb"]

    @classmethod
    def clean(cls, html_content):
        if not html_content:
            return ""
        # 优先使用lxml，不行则html.parser
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")

        # 移除注释
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            comment.extract()
        # 移除无用标签
        for tag_name in cls.REMOVE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()
        # 移除 anchorjs 空链接（生成无意义 [](<url>) 的元凶）
        for a in soup.find_all('a', class_=lambda c: c and 'anchorjs' in ' '.join(c) if isinstance(c, list) else 'anchorjs' in str(c)):
            a.decompose()
        # 移除文档网站侧边栏/导航容器（避免导航链接混入正文）
        for kw in cls.SIDEBAR_KEYWORDS:
            for el in soup.find_all(attrs={'class': lambda c: c and kw in ' '.join(c) if isinstance(c, list) else kw in str(c)}):
                el.decompose()
            for el in soup.find_all(attrs={'id': lambda i: i and kw in str(i)}):
                el.decompose()
        # 清理属性
        for tag in soup.find_all(True):
            for attr in cls.REMOVE_ATTRS:
                if attr in tag.attrs:
                    del tag.attrs[attr]
        # 规范化空白
        return str(soup)

    @classmethod
    def is_empty(cls, html_content):
        if not html_content:
            return True
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")
        text = soup.get_text().strip()
        return len(text) < 10

    @classmethod
    def extract_title(cls, html_content):
        try:
            soup = BeautifulSoup(html_content, "lxml")
        except Exception:
            soup = BeautifulSoup(html_content, "html.parser")
        # <title>
        t = soup.find('title')
        if t and t.get_text(strip=True):
            return t.get_text(strip=True)
        # <h1>
        h1 = soup.find('h1')
        if h1:
            return h1.get_text(strip=True)
        # <h2>
        h2 = soup.find('h2')
        if h2:
            return h2.get_text(strip=True)
        return "untitled"


# ═══════════════════ Markdown Converter (from GitHub) ═══════════════════

class MDConverter:
    """Markdown转换器 — 配置完善的html2text"""

    def __init__(self):
        self.h2t = html2text.HTML2Text()
        self.h2t.ignore_links = False
        self.h2t.ignore_images = False
        self.h2t.ignore_emphasis = False
        self.h2t.body_width = 0
        self.h2t.unicode_snob = True
        self.h2t.mark_code = True
        self.h2t.protect_links = True
        self.h2t.wrap_links = False
        self.h2t.default_image_alt = ""
        self.h2t.skip_internal_links = False
        self.h2t.inline_links = True
        self.h2t.ul_item_mark = "-"

    def convert(self, html_content):
        if not html_content:
            return ""
        try:
            md = self.h2t.handle(html_content)
            md = self._post_process(md)
            return md
        except Exception:
            return ""

    def _post_process(self, md):
        # 修复多余空行
        md = re.sub(r'\n{4,}', '\n\n\n', md)
        # 表格列对齐
        md = self._align_tables(md)
        # 表格周围空行
        md = re.sub(r'\n{2,}(\|)', r'\n\n\1', md)
        md = re.sub(r'(\|)\n{2,}', r'\1\n\n', md)
        # 代码块周围空行
        md = re.sub(r'\n{2,}(```)', r'\n\n\1', md)
        md = re.sub(r'(```)\n{2,}', r'\1\n\n', md)
        # 列表项之间空行
        md = re.sub(r'(\n[-\*]\s+.*)\n{2,}([-\*]\s+)', r'\1\n\2', md)
        # 移除行尾空白
        lines = [line.rstrip() for line in md.split('\n')]
        md = '\n'.join(lines)
        return md.rstrip() + '\n'

    # Unicode whitespace chars (including NBSP from HTML &nbsp;)
    _UNI_SPACE = '\u00a0\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000'

    @staticmethod
    def _display_width(s):
        """计算字符串显示宽度，考虑 ** 标记，跳过不可见空白"""
        s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
        s = re.sub(r'\[(.+?)\]\(.*?\)', r'\1', s)  # 链接显示文本
        width = 0
        for ch in s:
            if ch in MDConverter._UNI_SPACE:
                continue  # 跳过不可见的 unicode 空白
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f' or '\uff00' <= ch <= '\uffef':
                width += 2  # 中文/全角字符按2宽
            else:
                width += 1
        return width

    def _align_tables(self, md):
        """对齐 Markdown 表格列宽，使排版美观"""
        lines = md.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # 检测表格行（以 | 开头和结尾，或纯 | 分隔）
            if '|' in line and not line.strip().startswith('```'):
                # 收集连续表格行
                table_lines = [line]
                j = i + 1
                while j < len(lines) and '|' in lines[j] and not lines[j].strip().startswith('```'):
                    table_lines.append(lines[j])
                    j += 1
                if len(table_lines) >= 2:  # 至少要有表头+分隔行
                    aligned = self._pad_table(table_lines)
                    result.extend(aligned)
                    i = j
                    continue
            result.append(line)
            i += 1
        return '\n'.join(result)

    def _pad_table(self, lines):
        """对单个表格的列进行对齐填充"""
        import unicodedata

        def _full_strip(s):
            """彻底移除前后空白：ASCII空格 + unicode空白 + 零宽字符"""
            # 先处理常见的 unicode 空白
            s = s.strip()
            # 剔除前导/尾随的 NBSP 和其他 unicode 空格
            while s and s[0] in self._UNI_SPACE:
                s = s[1:]
            while s and s[-1] in self._UNI_SPACE:
                s = s[:-1]
            # 剔除零宽字符
            while s and unicodedata.category(s[0]) in ('Cf',):
                s = s[1:]
            while s and unicodedata.category(s[-1]) in ('Cf',):
                s = s[:-1]
            return s

        # 解析所有行，同时记录原始列数
        rows = []
        orig_cols = []  # 记录每行的原始列数（padding 之前）
        for line in lines:
            cells = [_full_strip(c) for c in line.split('|')]
            # 去掉首尾可能的空cell
            if cells and cells[0] == '':
                cells = cells[1:]
            if cells and cells[-1] == '':
                cells = cells[:-1]
            if cells:
                orig_cols.append(len(cells))
                rows.append(cells)

        if not rows:
            return lines

        # 确保所有行列数一致
        max_cols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < max_cols:
                r.append('')

        # ── 计算每列显示宽度 ──
        # 关键修复：只使用列数与表头一致的行来计算列宽，
        # 跳过因 HTML colspan 导致列数异常的行，避免宽列被误判。
        header_cols = orig_cols[0] if orig_cols else max_cols
        col_all_widths = [[] for _ in range(max_cols)]  # 收集每列所有宽度
        for i, r in enumerate(rows):
            if orig_cols[i] != header_cols and orig_cols[i] > 1:
                continue  # 跳过列数异常的离群行
            for ci, cell in enumerate(r):
                col_all_widths[ci].append(self._display_width(cell))

        # 对每列：排除极端离群值（超过 3 倍中位数的），取合理最大值
        col_widths = [0] * max_cols
        for ci, widths in enumerate(col_all_widths):
            if not widths:
                col_widths[ci] = 3
                continue
            widths.sort()
            median = widths[len(widths) // 2]
            # 只保留 ≤ 3×中位数的值（过滤超长离群单元格）
            filtered = [w for w in widths if median == 0 or w <= max(3 * median, 80)]
            if not filtered:
                filtered = widths
            col_widths[ci] = filtered[-1]  # 取合理最大值

        # 最少3，最多80
        col_widths = [min(max(w, 3), 80) for w in col_widths]

        # 重新格式化
        result = []
        for r in rows:
            padded = []
            for ci, cell in enumerate(r):
                dw = self._display_width(cell)
                pad = max(col_widths[ci] - dw, 0)
                padded.append(f" {cell}{' ' * pad} ")
            result.append('|'.join(padded))
        return result


# ═══════════════════ Link & Image Fixer (from GitHub) ═══════════════════

def fix_markdown_links(markdown):
    """修复Markdown中的链接和图片路径"""
    # 1. 修复 .html/.htm 链接为 .md
    def fix_html_link(m):
        url = m.group(1)
        if url.startswith(('http://', 'https://')):
            return m.group(0)
        return f']({url}.md)'

    markdown = re.sub(r'\]\(<?([^)>]+)\.html>?\)', fix_html_link, markdown)
    markdown = re.sub(r'\]\(<?([^)>]+)\.htm>?\)', fix_html_link, markdown)

    # 2. 去除链接 URL 中不必要的尖括号包裹（html2text 对含特殊字符 URL 的保守处理）
    #    [text](<url>) → [text](url) （URL 不含空格/未转义括号时是安全的）
    def unwrap_url(m):
        text = m.group(1) or ''
        url = m.group(2)
        if url.startswith('<') and url.endswith('>'):
            url = url[1:-1]
        # 跳过 data URI 和图片链接的修改（由 fix_image_path 处理）
        if m.group(0).startswith('!['):
            return f'![{text}]({url})'
        return f'[{text}]({url})'

    markdown = re.sub(r'\[([^\]]*)\]\(<?([^)>]+)>?\)', unwrap_url, markdown)

    # 3. 修复图片路径指向 assets/images/
    def fix_image_path(m):
        alt_text = m.group(1) or ""
        img_path = m.group(2)
        # 跳过 data URI 和外部链接，不修改
        if img_path.startswith(('data:', 'http://', 'https://', 'assets/')):
            return m.group(0)
        img_filename = Path(img_path).name
        return f'![{alt_text}](assets/images/{img_filename})'

    markdown = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', fix_image_path, markdown)

    # 4. 移除HTML锚点链接
    markdown = re.sub(r'\]\(<#[^>]+>\)', '](#)', markdown)

    return markdown


# ═══════════════════ Image Embedding ═══════════════════

def _embed_images(html, files_dict):
    """将图片内联为base64 data URI"""
    soup = BeautifulSoup(html, 'html.parser')
    img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico', '.webp'}
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if not src or src.startswith('data:'):
            continue
        sn = unquote(src).replace('\\', '/').lstrip('/').lower()
        sn_basename = sn.rsplit('/', 1)[-1] if '/' in sn else sn  # 纯文件名
        for k, v in files_dict.items():
            kk = k.replace('\\', '/').lower()
            kk_basename = kk.rsplit('/', 1)[-1]
            # 精确匹配文件名（忽略路径前缀）
            if kk_basename == sn_basename or sn == kk_basename:
                ext = Path(kk_basename).suffix.lower()
                if ext in img_exts:
                    try:
                        m = {
                            '.png': 'image/png', '.jpg': 'image/jpeg',
                            '.jpeg': 'image/jpeg', '.gif': 'image/gif',
                            '.bmp': 'image/bmp', '.svg': 'image/svg+xml',
                            '.ico': 'image/x-icon', '.webp': 'image/webp'
                        }.get(ext, 'image/png')
                        img['src'] = f"data:{m};base64,{base64.b64encode(v).decode()}"
                    except Exception:
                        pass
                    break
    return str(soup)


# ═══════════════════ TOC Parser (improved from GitHub) ═══════════════════

class TOCNode:
    """目录树节点"""
    def __init__(self, title, file_path=None, level=0, children=None, merge_ref=None):
        self.title = title
        self.file_path = file_path
        self.level = level
        self.children = children or []
        self.merge_ref = merge_ref  # 合并引用的子CHM路径 (e.g. "opc.chm::/opc.hhc")
        self.number = ""  # 层级数字编号, 如 "1.2.7.5"

    def to_dict(self):
        result = {"title": self.title, "level": self.level}
        if self.number:
            result["number"] = self.number
        if self.file_path:
            result["file_path"] = re.sub(r'\.html?$', '.md', self.file_path)
        if self.children:
            result["children"] = [c.to_dict() for c in self.children]
        return result


class TOCParser:
    """CHM目录解析器 — 基于.hhc文件"""

    def __init__(self):
        self.file_mapping = {}
        self.merge_refs = []  # 收集所有合并引用

    def parse(self, hhc_data, encoding="gb18030"):
        """解析.hhc内容"""
        for enc in [encoding, 'gbk', 'gb2312', 'utf-8', 'latin-1']:
            try:
                text = hhc_data.decode(enc)
                break
            except Exception:
                continue
        else:
            text = hhc_data.decode('utf-8', errors='replace')

        soup = BeautifulSoup(text, "lxml") if self._has_lxml() else BeautifulSoup(text, "html.parser")
        root_ul = soup.find("ul")
        if not root_ul:
            return TOCNode(title="Root", level=0)

        root = TOCNode(title="Root", level=0)
        self._parse_ul(root_ul, root, level=1)
        return root

    def _has_lxml(self):
        try:
            import lxml; return True
        except ImportError:
            return False

    def _parse_ul(self, ul_element, parent_node, level):
        """递归解析UL元素"""
        for li in ul_element.find_all("li", recursive=False):
            obj = li.find("object", {"type": "text/sitemap"})
            if not obj:
                continue

            params = obj.find_all("param")
            title = None
            file_path = None
            merge_ref = None
            for param in params:
                name = param.get("name", "").lower()
                value = param.get("value", "")
                if name == "name":
                    title = value
                elif name == "local":
                    file_path = value
                elif name == "merge":
                    merge_ref = value

            # 合并引用：有 Merge 参数但没有完整的子树
            if merge_ref:
                if not title:
                    # 尝试从合并路径提取名称
                    title = merge_ref.split('::')[0].replace('.chm', '').replace('.CHM', '')
                node = TOCNode(title=title, file_path=file_path, level=level, merge_ref=merge_ref)
                parent_node.children.append(node)
                self.merge_refs.append((title, merge_ref, parent_node.title))
                continue

            if not title:
                continue

            node = TOCNode(title=title, file_path=file_path, level=level)
            parent_node.children.append(node)

            if file_path:
                self.file_mapping[file_path] = title

            child_ul = li.find("ul", recursive=False)
            if child_ul:
                self._parse_ul(child_ul, node, level + 1)


# ═══════════════════ Companion Folder Detection ═══════════════════

def _find_companion_folder(html_path):
    """检测浏览器保存网页时生成的配套文件夹。

    浏览器"另存为"网页时，生成的文件夹命名规则：
      - Chrome/Edge/Firefox:  文件名_files/
      - IE 旧版:              文件名.files/
    返回 Path 对象或 None。
    """
    fp = Path(html_path)
    # 尝试两种命名规则
    for suffix in ['_files', '.files']:
        candidate = fp.parent / (fp.stem + suffix)
        if candidate.is_dir():
            return candidate
    # 有些浏览器可能用不同后缀，扫描同名前缀的目录
    prefix = fp.stem
    for item in fp.parent.iterdir():
        if item.is_dir() and item.name.startswith(prefix) and item != fp:
            # 检查是否像配套文件夹（包含常见网页资源文件）
            for ext in ['.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.svg']:
                if any(item.rglob(f'*{ext}')):
                    return item
    return None


def _load_companion_images(folder, log=None):
    """从配套文件夹加载所有图片，返回 {相对路径: bytes} 字典。

    同时建立文件名索引（key 为纯文件名），方便按文件名匹配。
    支持子目录中的图片。
    """
    def _log(msg, tag='info'):
        if log: log(msg, tag)

    img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico', '.webp'}
    files_dict = {}
    count = 0

    for f in sorted(folder.rglob('*')):
        if f.is_file() and f.suffix.lower() in img_exts:
            try:
                data = f.read_bytes()
                if data:
                    # 以相对于配套文件夹的路径为 key
                    rel = str(f.relative_to(folder)).replace('\\', '/')
                    files_dict[rel] = data
                    # 也加纯文件名 key（兼容 HTML 中只用文件名的引用）
                    files_dict[f.name] = data
                    count += 1
            except Exception:
                pass

    if count:
        _log(f"从配套文件夹加载了 {count} 个图片文件", 'info')
    return files_dict


# ═══════════════════ MHTML / HTML / URL Input ═══════════════════

def _parse_mhtml(filepath, log=None):
    """解析 MHTML 文件，返回 (html_content, title, images_dict)

    MHTML 是浏览器"另存为单个文件"的格式，MIME multipart/related 包裹。
    提取 text/html 和所有内嵌图片（image/* MIME parts）。
    标题优先从 HTML <title> 提取，其次从 MIME Subject 头。
    """
    def _log(msg, tag='info'):
        if log: log(msg, tag)

    raw = Path(filepath).read_bytes()
    msg = email.message_from_bytes(raw)

    # 尝试从 Subject 头获取标题
    mime_title = None
    subject = msg.get('Subject', '')
    if subject:
        try:
            from email.header import decode_header
            parts = decode_header(subject)
            mime_title = ''
            for text, charset in parts:
                if isinstance(text, bytes):
                    mime_title += text.decode(charset or 'utf-8', errors='replace')
                else:
                    mime_title += text
        except Exception:
            mime_title = subject

    # 提取 text/html 和图片
    html_body = None
    images_dict = {}

    for part in msg.walk():
        ct = part.get_content_type()
        if ct == 'text/html' and html_body is None:
            payload = part.get_payload(decode=True)
            if payload:
                try:
                    html_body = payload.decode('utf-8')
                except UnicodeDecodeError:
                    try:
                        html_body = payload.decode('gbk')
                    except UnicodeDecodeError:
                        html_body = payload.decode('latin-1')
        elif ct.startswith('image/'):
            # 提取内嵌图片
            payload = part.get_payload(decode=True)
            if payload:
                cid = part.get('Content-ID', '').strip('<>')
                cdl = part.get('Content-Location', '')
                # 尝试从 Content-Type 确定文件扩展名
                subtype = ct.split('/')[-1]
                if subtype == 'jpeg':
                    ext = '.jpg'
                elif subtype == 'svg+xml':
                    ext = '.svg'
                else:
                    ext = '.' + subtype
                # 以 Content-Location 文件名或 Content-ID 为 key
                name = cdl.split('/')[-1] if cdl else (cid + ext if cid else None)
                if name and payload:
                    images_dict[name] = payload
                    # 也加纯文件名 key
                    if '/' in cdl or '\\' in cdl:
                        images_dict[Path(name).name] = payload

    if not html_body:
        raise ValueError("MHTML 文件中未找到 text/html 内容")

    if images_dict:
        _log(f"MHTML 内嵌图片: {len(images_dict)} 个", 'info')

    # 从 HTML <title> 提取标题
    soup = BeautifulSoup(html_body, 'html.parser')
    html_title = None
    if soup.title and soup.title.string:
        html_title = soup.title.string.strip()

    title = html_title or mime_title or Path(filepath).stem
    _log(f"MHTML 标题: {title}", 'info')
    return html_body, title, images_dict


def _read_html_file(filepath, log=None):
    """读取 HTML 文件，自动检测编码，返回 (html_content, title)"""
    def _log(msg, tag='info'):
        if log: log(msg, tag)

    raw = Path(filepath).read_bytes()
    html = decode_html(raw)
    soup = BeautifulSoup(html, 'html.parser')
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = title or Path(filepath).stem
    _log(f"HTML 标题: {title}", 'info')
    return html, title


def _fetch_url(url, log=None):
    """抓取网页 URL，返回 (html_content, title, images_dict)

    优先使用 Playwright 无头浏览器（完整 JS 渲染 + 图片下载），
    未安装 Playwright 时回退到 requests（仅静态 HTML，无图片）。"""
    def _log(msg, tag='info'):
        if log: log(msg, tag)

    if HAS_PLAYWRIGHT:
        try:
            return _fetch_url_headless(url, log)
        except Exception as e:
            _log(f"Playwright 抓取失败: {e}，回退到 requests 模式（可能丢失动态内容）", 'warn')
            # 回退到 requests

    # ── requests 回退模式 ──
    if HAS_REQUESTS:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        if resp.encoding and resp.encoding.lower() != 'iso-8859-1':
            resp.encoding = resp.encoding
        else:
            match = re.search(rb'charset=["\']?([a-zA-Z0-9\-]+)', resp.content[:4096])
            if match:
                try:
                    resp.encoding = match.group(1).decode('ascii')
                except Exception:
                    pass
        html = resp.text
    else:
        import urllib.request
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            encoding = resp.headers.get_content_charset()
            if not encoding:
                match = re.search(rb'charset=["\']?([a-zA-Z0-9\-]+)', raw[:4096])
                encoding = match.group(1).decode('ascii') if match else 'utf-8'
            try:
                html = raw.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                html = raw.decode('utf-8', errors='replace')

    soup = BeautifulSoup(html, 'html.parser')
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    title = title or urlparse(url).netloc or 'webpage'
    _log(f"URL 标题: {title}", 'info')
    return html, title, {}


def _fetch_url_first_pass(url, log=None):
    """URL 模式第一步：抓取首页并提取侧边栏导航结构。

    返回 (html, title, images_dict, nav_entries)
    nav_entries: [(section_text, section_url, level), ...]
    """
    def _log(msg, tag='info'):
        if log:
            log(msg, tag)

    if not HAS_PLAYWRIGHT:
        # 无 Playwright 时回退普通抓取，无导航信息
        html, title, images_dict = _fetch_url(url, log)
        return html, title, images_dict, []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            page = context.new_page()

            try:
                _log("启动无头浏览器 (Chromium)...", 'info')
                _log(f"导航到: {url}", 'info')
                page.goto(url, wait_until='networkidle', timeout=60000)
                page.wait_for_timeout(2000)

                html = page.content()
                title = page.title() or urlparse(url).netloc or 'webpage'
                _log(f"页面标题: {title}", 'info')

                # 提取侧边栏导航
                nav_entries = _extract_sidebar_links(page, url)
                if nav_entries:
                    _log(f"检测到侧边栏导航: {len(nav_entries)} 个链接", 'info')

                # 下载图片
                images_dict = {}
                img_count = _download_page_images(page, context, images_dict)
                if img_count:
                    _log(f"首页图片: {img_count} 张", 'info')

                return html, title, images_dict, nav_entries

            finally:
                browser.close()

    except Exception as e:
        _log(f"Playwright 抓取失败: {e}，回退到 requests 模式", 'warn')
        html, title, images_dict = _fetch_url(url, log)
        return html, title, images_dict, []


def _fetch_url_headless(url, log=None):
    """使用 Playwright 无头浏览器抓取网页，返回 (html, title, images_dict)

    优势：完整执行 JavaScript（React/Vue/Angular 等 SPA 的内容都能拿到），
         同时下载页面中所有图片并内嵌到输出 MD。
    """
    def _log(msg, tag='info'):
        if log: log(msg, tag)

    _log("启动无头浏览器 (Chromium)...", 'info')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = context.new_page()

        try:
            # 导航到目标 URL
            _log(f"导航到: {url}", 'info')
            page.goto(url, wait_until='networkidle', timeout=60000)
            # 额外等待以确保 JS 渲染完成（特别是懒加载内容）
            page.wait_for_timeout(2000)

            # 获取完整渲染后的 HTML
            html = page.content()
            title = page.title() or urlparse(url).netloc or 'webpage'
            _log(f"页面标题: {title}", 'info')

            # ── 下载页面中的所有图片 ──
            images_dict = {}
            img_count = 0
            img_ext_to_mime = {
                'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                'gif': 'image/gif', 'bmp': 'image/bmp', 'svg': 'image/svg+xml',
                'ico': 'image/x-icon', 'webp': 'image/webp'
            }
            img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico', '.webp'}

            # 从渲染后的 DOM 提取所有 img 元素信息
            img_elements = page.evaluate('''() => {
                const imgs = document.querySelectorAll('img');
                return Array.from(imgs).map(img => ({
                    src: img.src || img.getAttribute('src') || '',
                    currentSrc: img.currentSrc || '',
                    datasetSrc: img.dataset?.src || ''
                }));
            }''')

            for entry in img_elements:
                # 取最优 src（currentSrc > src > data-src）
                src = entry['currentSrc'] or entry['src'] or entry['datasetSrc']
                if not src or src.startswith('data:'):
                    continue  # 跳过空链接和已内嵌的

                # 构建相对路径作为 key（从 URL 中提取路径部分）
                try:
                    parsed = urlparse(src)
                    path_part = unquote(parsed.path).lstrip('/')
                    if not path_part:
                        continue
                    # key: 纯文件名 + 相对路径
                    fname = path_part.rsplit('/', 1)[-1] if '/' in path_part else path_part
                    ext = Path(fname).suffix.lower()
                    if ext not in img_exts:
                        continue
                except Exception:
                    continue

                if fname in images_dict:
                    continue  # 已下载

                # 通过 browser context 下载图片（复用 cookie/session）
                try:
                    img_resp = context.request.get(src, timeout=15000)
                    if img_resp and img_resp.ok:
                        data = img_resp.body()
                        if data:
                            images_dict[fname] = data
                            if path_part != fname:
                                images_dict[path_part] = data
                            img_count += 1
                except Exception:
                    pass  # 个别图片下载失败不中断整体流程

            if img_count:
                _log(f"已下载 {img_count} 张图片", 'info')

            return html, title, images_dict

        finally:
            browser.close()


# ──────────────────────────────────────────────
#  多页整站爬取
# ──────────────────────────────────────────────

def _extract_sidebar_links(page, base_url):
    """从已渲染的 Playwright 页面提取侧边栏导航链接。

    返回 [(text, href, level), ...]
    level: 1=顶层章节, 2=子章节, ...
    仅保留同域名、非锚点、非 javascript: 的有效链接，已去重。
    """
    entries = page.evaluate('''() => {
        // 优先匹配常见文档站侧边栏结构
        const selectors = [
            '#sidetoc a', '.sidetoc a',
            '#sidenav a', '.sidenav a',
            'nav.sidebar a', 'aside.sidebar a',
            '.sidebar a.toc-link',
            '[role="navigation"] a[href]',
            'nav a[href]', 'aside a[href]',
            '.toc a', '#toc a'
        ];
        let best = [];
        for (const sel of selectors) {
            const nodes = document.querySelectorAll(sel);
            if (nodes.length >= 5) {
                best = Array.from(nodes).map(a => {
                    // 计算嵌套层级
                    let level = 1;
                    let el = a.closest('li');
                    if (el) {
                        // 统计祖先 li 的数量
                        let p = el.parentElement;
                        while (p) {
                            if (p.tagName === 'LI') level++;
                            p = p.parentElement;
                        }
                    }
                    return {
                        text: (a.textContent || '').trim().replace(/\\s+/g, ' '),
                        href: a.href || a.getAttribute('href') || '',
                        level: Math.min(level, 4)
                    };
                }).filter(e => e.text && e.href);
                break;
            }
        }
        return best;
    }''')

    base_parsed = urlparse(base_url)
    base_domain = base_parsed.netloc
    base_path = base_parsed.path.rsplit('/', 1)[0] if '/' in base_parsed.path else ''

    seen_hrefs = set()
    result = []

    for entry in entries:
        href = entry['href']
        text = entry['text']
        if not href or not text:
            continue
        # 过滤锚点、javascript、mailto
        if href.startswith('#') or href.startswith('javascript:') or href.startswith('mailto:'):
            continue

        # 转为绝对 URL
        parsed = urlparse(href)
        if not parsed.netloc:
            # 相对路径
            if href.startswith('/'):
                href = f"{base_parsed.scheme}://{base_domain}{href}"
            else:
                href = f"{base_parsed.scheme}://{base_domain}{base_path}/{href}"

        parsed = urlparse(href)
        # 仅保留同域名
        if parsed.netloc != base_domain:
            continue
        # 去重（同一 URL 只保留第一次出现的标题）
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        result.append((text, href, entry.get('level', 1)))

    return result


def _extract_main_content(html):
    """从页面 HTML 中提取主内容区域，去除页眉/侧边栏/页脚。"""
    soup = BeautifulSoup(html, 'html.parser')

    # 移除不需要的元素
    remove_selectors = [
        'nav', 'footer',
        '#sidenav', '#sideaffix', '#sidetoc', '#sidefilter',
        '#navbar', '#breadcrumb', '#toc',
        '.sidenav', '.sideaffix', '.sidetoc', '.sidefilter',
        '.navbar', '.breadcrumb', '.toc', '.sidebar',
        '[role="navigation"]',
        'script', 'style', 'noscript',
    ]
    for sel in remove_selectors:
        for el in soup.select(sel):
            el.decompose()

    # 尝试提取主内容区域
    content_selectors = [
        'article', 'main',
        '.markdown-body', '.content', '#content',
        '.article-content', '.post-content', '.page-content',
        '.main-content', '#main-content',
        '.doc-content', '#doc-content',
        '.body-content', '#body-content',
    ]
    for sel in content_selectors:
        content = soup.select_one(sel)
        if content:
            return str(content)

    # 回退：直接返回 body 内容
    body = soup.find('body')
    return str(body) if body else html


def _crawl_multi_page(start_url, log=None, nav_entries=None,
                      site_title='', existing_images=None):
    """整站爬取：从起始页提取导航，逐一抓取所有子页面，合并内容。

    参数:
        start_url: 起始 URL
        log: 日志回调
        nav_entries: 可选的已有导航 [(text, href, level), ...]，
                    若提供则跳过导航提取步骤
        site_title: 可选，若提供且 nav_entries 也提供则跳过首页加载
        existing_images: 可选，首轮已下载的图片，传入则增量追加

    返回 (site_title, page_data_list, combined_images, site_structure)
    page_data_list: [(chapter_num, section_title, section_url, main_html), ...]
    site_structure: [(section_title, page_url, [(h_level, h_text), ...]), ...]
    """
    def _log(msg, tag='info'):
        if log:
            log(msg, tag)

    base_parsed = urlparse(start_url)
    base_domain = base_parsed.netloc

    _log("=" * 50, 'info')
    _log("整站爬取模式", 'info')
    _log(f"起始 URL: {start_url}", 'info')

    combined_images = existing_images if existing_images else {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )

        try:
            # ── Step 1: 抓取首页 + 提取导航（或使用传入的已有导航） ──
            if nav_entries:
                _log(f"使用已提取的导航: {len(nav_entries)} 个链接", 'info')
            else:
                _log("正在加载首页...", 'info')
                page0 = context.new_page()
                try:
                    page0.goto(start_url, wait_until='networkidle', timeout=60000)
                    page0.wait_for_timeout(2000)
                    nav_entries = _extract_sidebar_links(page0, start_url)
                finally:
                    page0.close()

            if nav_entries:
                _log(f"导航共 {len(nav_entries)} 个链接", 'info')
            else:
                _log("未检测到多页导航结构，将以单页模式处理", 'info')
                page0 = context.new_page()
                try:
                    page0.goto(start_url, wait_until='networkidle', timeout=60000)
                    page0.wait_for_timeout(2000)
                    first_html = page0.content()
                    site_title = page0.title() or base_domain
                    first_images = _download_page_images(page0, context, combined_images)
                    _log(f"首页图片: {first_images} 张", 'info')
                finally:
                    page0.close()
                browser.close()
                headings = _extract_headings(first_html)
                page_data = [('1', site_title, start_url, first_html)]
                return site_title, page_data, combined_images, [(site_title, start_url, headings)]

            # ── Step 2: 获取站点标题及首页图片 ──
            if site_title and existing_images is not None:
                # 调用方已提供标题和图片，跳过首页加载
                _log(f"站点标题: {site_title}（重用首轮抓取结果）", 'info')
            else:
                site_title = base_domain  # 默认值
                page = context.new_page()
                try:
                    page.goto(start_url, wait_until='networkidle', timeout=60000)
                    page.wait_for_timeout(2000)
                    site_title = page.title() or base_domain
                    _log(f"站点标题: {site_title}", 'info')
                    # 下载首页图片
                    first_images = _download_page_images(page, context, combined_images)
                    if first_images:
                        _log(f"首页图片: {first_images} 张", 'info')
                finally:
                    page.close()

            # ── Step 3: 逐页抓取 ──
            total = len(nav_entries)
            chapter_nums = _generate_chapter_numbers(nav_entries)
            page_data_list = []  # [(chapter_num, section_text, section_url, main_html), ...]
            site_structure = []  # [(section_text, page_url, [(h_level, h_text)]), ...]

            for idx, (section_text, section_url, level) in enumerate(nav_entries):
                chapter_num = chapter_nums[idx]
                pct = int((idx + 1) / total * 100) if total else 100
                _log(f"[{idx + 1}/{total}] {chapter_num} {section_text} ({pct}%)", 'info')

                sp = None
                try:
                    sp = context.new_page()
                    sp.goto(section_url, wait_until='networkidle', timeout=45000)
                    sp.wait_for_timeout(1500)

                    page_html = sp.content()
                    page_headings = _extract_headings(page_html)
                    page_images = _download_page_images(sp, context, combined_images)
                    if page_images:
                        _log(f"  └ 图片: {page_images} 张", 'debug')

                    # 提取主内容
                    main_html = _extract_main_content(page_html)
                    page_data_list.append((chapter_num, section_text, section_url, main_html))
                    site_structure.append((section_text, section_url, page_headings))

                except Exception as e:
                    _log(f"  └ 抓取失败: {e}", 'warn')
                    # 失败页面也占位，保持编号对应
                    page_data_list.append((chapter_num, section_text, section_url, ''))
                    site_structure.append((section_text, section_url, []))
                finally:
                    if sp is not None:
                        try:
                            sp.close()
                        except Exception:
                            pass

            browser.close()

            # ── Step 4: 汇总 ──
            success_count = sum(1 for _, _, _, html in page_data_list if html)
            _log(f"爬取完成: {success_count}/{total} 页成功, {len(combined_images)} 张图片", 'success')

            return site_title, page_data_list, combined_images, site_structure

        except Exception as e:
            try:
                browser.close()
            except Exception:
                pass
            raise


def _escape_html(text):
    """HTML 转义文本中的特殊字符"""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _generate_chapter_numbers(nav_entries):
    """根据侧边栏层级生成章节编号列表。

    nav_entries: [(text, url, level), ...]   level 从 1 开始（顶层）
    返回: ["1", "1.1", "1.2", "2", "2.1", ...]

    示例:
        level=1 → counters=[1] → "1"
        level=2 → counters=[1,1] → "1.1"
        level=2 → counters=[1,2] → "1.2"
        level=1 → counters=[2] → "2"
    """
    counters = []  # 各层级当前计数
    result = []
    for _, _, level in nav_entries:
        lv = max(1, level)
        # 扩展或裁剪 counters 到当前层级
        while len(counters) < lv:
            counters.append(1)
        counters = counters[:lv]
        # 递增当前层计数器
        counters[-1] += 1
        # 生成编号字符串
        result.append('.'.join(str(c) for c in counters))
    return result


def _derive_site_folder_name(title, nav_entries, url=''):
    """从页面标题和导航条目推导站点文件夹名。

    优先级：
    1. nav_entries 中第一个 level=1 条目文本（通常是章节根标题，最可靠）
    2. 清理 page.title() 中 "| SiteName"、" - SiteName" 等污染后缀
    3. URL 域名兜底

    典型场景：page.title()=" | 客户端软件文档" 无页面名，
    但侧边栏第一条是 "客户端软件操作指南" → 返回后者。
    """
    # 1. 侧边栏第一级条目（最可靠）
    if nav_entries:
        for text, _, level in nav_entries:
            if level == 1 and text:
                return text.strip()

    # 2. 清理 title 污染后缀（"页面名 | 网站名" → "页面名"）
    if title:
        for sep in (' | ', ' - ', ' — '):
            if sep in title:
                first_part = title.split(sep)[0].strip()
                if first_part:
                    return first_part
        # 无分隔符，去除首尾分隔符残留后返回
        clean = title.strip('| -— \t')
        if clean:
            return clean

    # 3. 域名兜底
    if url:
        from urllib.parse import urlparse
        return urlparse(url).netloc or 'site'
    return 'site'


def _download_page_images(page, context, images_dict):
    """从 Playwright 页面下载所有图片到 images_dict（原地修改），返回下载数量。"""
    img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg', '.ico', '.webp'}
    img_count = 0

    img_elements = page.evaluate('''() => {
        const imgs = document.querySelectorAll('img');
        return Array.from(imgs).map(img => ({
            src: img.src || img.getAttribute('src') || '',
            currentSrc: img.currentSrc || '',
            datasetSrc: img.dataset?.src || ''
        }));
    }''')

    for entry in img_elements:
        src = entry['currentSrc'] or entry['src'] or entry['datasetSrc']
        if not src or src.startswith('data:'):
            continue
        try:
            parsed = urlparse(src)
            path_part = unquote(parsed.path).lstrip('/')
            if not path_part:
                continue
            fname = path_part.rsplit('/', 1)[-1] if '/' in path_part else path_part
            ext = Path(fname).suffix.lower()
            if ext not in img_exts:
                continue
        except Exception:
            continue

        if fname in images_dict:
            continue

        try:
            img_resp = context.request.get(src, timeout=15000)
            if img_resp and img_resp.ok:
                data = img_resp.body()
                if data:
                    images_dict[fname] = data
                    if path_part != fname:
                        images_dict[path_part] = data
                    img_count += 1
        except Exception:
            pass

    return img_count


def _extract_headings(html):
    """从 HTML 提取标题结构，返回 [(level, text), ...]

    level 为 1-6（对应 h1-h6），已过滤空白标题和侧边栏内的标题。
    用于 URL 模式转换前预览页面结构。
    """
    soup = BeautifulSoup(html, 'html.parser')

    # 移除已知侧边栏/导航容器（避免导航链接标题混入结构预览）
    sidebar_ids = {'sidenav', 'sideaffix', 'sidetoc', 'sidefilter',
                   'toc-toggle', 'navbar', 'breadcrumb', 'toc'}
    for sid in sidebar_ids:
        for el in soup.find_all(id=sid):
            el.decompose()
        for el in soup.find_all(class_=lambda c: c and sid in ' '.join(c) if isinstance(c, list) else sid in str(c)):
            el.decompose()
    # 也移除 nav/footer 内的标题
    for tag_name in ['nav', 'footer']:
        for el in soup.find_all(tag_name):
            el.decompose()

    headings = []
    for tag in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        text = tag.get_text(strip=True)
        if not text:
            continue
        level = int(tag.name[1])
        headings.append((level, text))

    return headings


class URLPreviewDialog:
    """URL 抓取内容预览对话框 — 支持单页和多页整站结构预览"""

    def __init__(self, parent, url, title, headings_or_site, img_count, html_len):
        """
        参数:
            headings_or_site:
                - 单页模式: [(level, text), ...]  = 旧格式
                - 整站模式: [(section_title, section_url, [(level, text), ...]), ...]
        """
        self.result = False
        self.site_structure = None  # 整站模式
        self.headings = None        # 单页模式

        # 判断模式
        if headings_or_site and isinstance(headings_or_site[0], (list, tuple)):
            first = headings_or_site[0]
            if len(first) >= 3 and isinstance(first[0], str) and isinstance(first[1], str):
                # 三元组 (title, url, headings) → 整站模式
                self.site_structure = headings_or_site
            else:
                # 二元组 (level, text) → 单页模式
                self.headings = headings_or_site
        else:
            self.headings = headings_or_site if headings_or_site else []

        is_multi = self.site_structure is not None
        total_pages = len(self.site_structure) if is_multi else 1
        total_headings = (
            sum(len(entry[2]) for entry in self.site_structure)
            if is_multi else (len(self.headings) if self.headings else 0)
        )

        # ── 窗口 ──
        self.top = tk.Toplevel(parent)
        mode_label = "整站爬取预览" if is_multi else "网页内容预览"
        self.top.title(mode_label)
        self.top.geometry("780x560" if is_multi else "680x520")
        self.top.resizable(True, True)
        self.top.transient(parent)
        self.top.grab_set()

        # ── 顶部信息栏 ──
        info_frame = ttk.Frame(self.top, padding=(15, 10))
        info_frame.pack(fill='x')

        ttk.Label(info_frame, text=f"站点: {title[:80]}",
                  font=('Microsoft YaHei', 11, 'bold')).pack(anchor='w')
        ttk.Label(info_frame, text=f"URL: {url[:100]}",
                  font=('Microsoft YaHei', 8), foreground='gray').pack(anchor='w', pady=(2, 0))

        stats_frame = ttk.Frame(info_frame)
        stats_frame.pack(fill='x', pady=(8, 0))

        if is_multi:
            stats_text = (
                f"章节: {total_pages} 页  ·  "
                f"标题节点: {total_headings} 个  ·  "
                f"图片: {img_count} 张  ·  "
                f"HTML: {html_len / 1024:.1f} KB"
            )
        else:
            stats_text = (
                f"标题节点: {total_headings} 个  ·  "
                f"图片: {img_count} 张  ·  "
                f"HTML 大小: {html_len / 1024:.1f} KB"
            )
        ttk.Label(stats_frame, text=stats_text,
                  font=('Microsoft YaHei', 9)).pack(side='left')

        ttk.Separator(self.top, orient='horizontal').pack(fill='x', padx=15)

        # ── 底部按钮（先创建，确保始终可见） ──
        btn_frame = ttk.Frame(self.top, padding=(15, 10))
        btn_frame.pack(side='bottom', fill='x')

        confirm_text = "确认整站转换 ->" if (is_multi and total_pages > 1) else "确认转换 ->"
        warning = (
            f"将依次抓取 {total_pages} 个页面，分章节保存为独立 Markdown 文件，"
            f"并生成 _目录.md 索引。请确认结构无误。"
            if is_multi and total_pages > 1
            else "请检查以上内容结构是否完整正确，再决定是否转换。"
        )
        ttk.Label(btn_frame, text=warning,
                  font=('Microsoft YaHei', 8), foreground='#666').pack(side='left')

        self.btn_cancel = ttk.Button(btn_frame, text="取消", command=self._on_cancel)
        self.btn_cancel.pack(side='right', padx=(5, 0))

        self.btn_ok = ttk.Button(btn_frame, text=confirm_text, command=self._on_confirm)
        self.btn_ok.pack(side='right')

        # ── 树形预览区 ──
        tree_frame = ttk.Frame(self.top, padding=(15, 5))
        tree_frame.pack(side='top', fill='both', expand=True)

        hint = "整站结构预览（侧边栏导航 -> 各页标题）：" if is_multi else "页面结构预览（抓取自渲染后 DOM）："
        ttk.Label(tree_frame, text=hint,
                  font=('Microsoft YaHei', 9)).pack(anchor='w')

        tree_container = ttk.Frame(tree_frame)
        tree_container.pack(fill='both', expand=True, pady=(5, 0))

        tree_height = 20 if is_multi else 18
        self.tree = ttk.Treeview(tree_container, columns=('text',), show='tree',
                                  selectmode='none', height=tree_height)
        self.tree.pack(side='left', fill='both', expand=True)

        vsb = ttk.Scrollbar(tree_container, orient='vertical', command=self.tree.yview)
        vsb.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=vsb.set)

        # 填充树节点（异常不影响按钮功能）
        try:
            if is_multi:
                self._populate_site_tree()
            else:
                self._populate_tree(self.headings)
        except Exception:
            import traceback
            self.tree.insert('', 'end', text=f'(预览构建失败: {traceback.format_exc()[:200]})', 
                           values=('错误',))

        # 绑定关闭事件
        self.top.protocol('WM_DELETE_WINDOW', self._on_cancel)

        # 居中
        self.top.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.top.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.top.winfo_height()) // 2
        self.top.geometry(f'+{max(0,x)}+{max(0,y)}')

    def _populate_site_tree(self):
        """整站模式：显示侧边栏导航 -> 每页内部标题"""
        if not self.site_structure:
            self.tree.insert('', 'end', text='(无章节)', values=('未检测到导航结构',))
            return

        for idx, (sec_title, sec_url, page_headings) in enumerate(self.site_structure):
            display_title = sec_title[:80] + '...' if len(sec_title) > 80 else sec_title
            sec_iid = self.tree.insert(
                '', 'end',
                text=f'[{idx+1}] {display_title}',
                values=(f'{idx + 1}. {sec_title}',),
                open=False
            )

            if page_headings:
                for level, h_text in page_headings:
                    display_text = h_text[:90] + '...' if len(h_text) > 90 else h_text
                    tag = f'H{level}'
                    indent = '  ' * (level - 1)
                    self.tree.insert(
                        sec_iid, 'end',
                        text=f'{indent}[{tag}] {display_text}',
                        values=(h_text,)
                    )
            else:
                self.tree.insert(
                    sec_iid, 'end',
                    text='  (正文无标题)',
                    values=('该页未检测到 h1-h6 标题',)
                )

    def _populate_tree(self, headings):
        """单页模式：将标题列表填充为树形结构"""
        if not headings:
            self.tree.insert('', 'end', text='(无标题)', values=('(页面没有检测到 h1-h6 标题)',))
            return

        stack = [('',)]  # stack[0] = 根

        for level, text in headings:
            display_text = text[:100] + '…' if len(text) > 100 else text
            tag = f'H{level}'

            while len(stack) <= level:
                stack.append(('',))

            stack = stack[:level + 1]

            parent = ''
            if level > 1 and len(stack) >= level:
                parent = stack[level - 1][0] if stack[level - 1][0] else ''

            iid = self.tree.insert(parent, 'end', text=f'[{tag}] {display_text}',
                                    values=(display_text,))

            if parent:
                self.tree.item(parent, open=True)

            stack[level] = (iid,)

    def _on_confirm(self):
        self.result = True
        self.top.destroy()

    def _on_cancel(self):
        self.result = False
        self.top.destroy()

    def wait(self):
        """阻塞等待用户选择（在主线程中运行）"""
        self.top.wait_window()
        return self.result


def _html_to_md(html, title, out_dir, log=None, files_dict=None, basename=None):
    """将 HTML 内容转换为 Markdown 文件，保存到 out_dir

    返回生成的 .md 文件路径。
    复用现有的 HTMLCleaner + MDConverter + fix_markdown_links 引擎。

    files_dict: 可选，{文件名/路径: bytes} 的图片字典。
                传入后会调用 _embed_images() 将图片以 base64 内嵌到输出 MD。
    basename:    可选，自定义输出文件名（不含扩展名），
                传入时优先使用此名而非从 title 推导。
    """
    def _log(msg, tag='info'):
        if log: log(msg, tag)

    _log(f"处理: {title}", 'info')

    # 如果提供了图片字典，先嵌入图片（base64），再清理/转换
    # 注意：必须先 embed 再 clean，否则 clean 可能移除了 img 标签
    if files_dict:
        html = _embed_images(html, files_dict)
        _log("图片已内嵌为 base64", 'info')

    # 清理 HTML
    cleaner = HTMLCleaner()
    cleaned = cleaner.clean(html)

    # 转换
    conv = MDConverter()
    md = conv.convert(cleaned)

    # 修复链接（已内嵌的 data: URI 会被 fix_markdown_links 跳过）
    md = fix_markdown_links(md)

    # 写入文件
    if basename:
        fn = _clean_path(basename)
    else:
        fn = _clean_path(title) or 'index'
    if not fn.endswith('.md'):
        fn += '.md'
    out_path = Path(out_dir) / fn
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding='utf-8')
    _log(f"已保存: {out_path.name} ({len(md)} 字符)", 'success')
    return str(out_path)


# ═══════════════════ CHM Extraction ═══════════════════

_7Z_PATHS = [
    '7z',                                                          # PATH
    r'C:\Program Files\7-Zip\7z.exe',
    r'C:\Program Files (x86)\7-Zip\7z.exe',
]


def _find_7z():
    """返回 7z.exe 完整路径，找不到返回 None"""
    for p in _7Z_PATHS:
        try:
            # 先检查文件是否存在（绝对路径情况）
            if os.path.isabs(p) and not os.path.isfile(p):
                continue
            # 用 --help 验证可执行性（避免某些版本无参数返回非0）
            shell_flag = True if os.name == 'nt' else False
            if subprocess.run(
                [p] if not shell_flag else f'"{p}"',
                capture_output=True, timeout=5,
                shell=shell_flag
            ).returncode == 0 or os.path.isfile(p):
                return p
        except Exception:
            continue
    # where 兜底
    try:
        r = subprocess.run(
            'where 7z' if os.name == 'nt' else 'which 7z',
            capture_output=True, timeout=5, shell=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            found = r.stdout.strip().splitlines()
            if found and os.path.isfile(found[0].strip()):
                return found[0].strip()
    except Exception:
        pass
    return None


def _extract_via_7z(chm_path, log):
    """使用7-Zip提取CHM (优先方案)"""
    exe = _find_7z()
    if not exe:
        log("  [7-Zip] 未找到，尝试 hh.exe...", 'info')
        return None
    tmp = Path(tempfile.mkdtemp(prefix='chm7z_'))
    _register_temp_dir(tmp)
    try:
        result = subprocess.run(
            [exe, 'x', str(Path(chm_path).resolve()), f'-o{tmp}', '-y'],
            capture_output=True, text=True, encoding='utf-8', errors='ignore',
            timeout=120
        )
        if result.returncode == 0:
            entries = {}
            for f in tmp.rglob('*'):
                if f.is_file():
                    entries[str(f.relative_to(tmp)).replace('\\', '/')] = f.read_bytes()
            if entries:
                n = sum(1 for k in entries if k.lower().endswith(('.html', '.htm')))
                log(f"  [7-Zip] {len(entries)} 个文件 ({n} HTML)", 'success')
                return entries
        log(f"  [7-Zip] 失败 (返回码={result.returncode})", 'info')
        return None
    except FileNotFoundError:
        log("  [7-Zip] 未找到，尝试 hh.exe...", 'info')
        return None
    except Exception as e:
        log(f"  [7-Zip] 错误: {e}", 'info')
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_via_hh(chm_path, log):
    """使用Windows hh.exe提取CHM"""
    tmp = Path(tempfile.mkdtemp(prefix='chmhh_'))
    _register_temp_dir(tmp)
    out = tmp / 'out'; out.mkdir()
    try:
        tmp_chm = tmp / '_in.chm'
        shutil.copy2(chm_path, tmp_chm)
        log("  [hh.exe] 正在解压...", 'info')
        subprocess.run(
            ['hh.exe', '-decompile', str(out), str(tmp_chm)],
            capture_output=True, timeout=60
        )
        entries = {}
        for f in out.rglob('*'):
            if f.is_file():
                entries[str(f.relative_to(out)).replace('\\', '/')] = f.read_bytes()
        if entries:
            n = sum(1 for k in entries if k.lower().endswith(('.html', '.htm')))
            log(f"  [hh.exe] {len(entries)} 个文件 ({n} HTML)", 'success')
            return entries
        log("  [hh.exe] 无输出", 'info')
        return None
    except Exception as e:
        log(f"  [hh.exe] 错误: {e}", 'info')
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_via_itsf(chm_path, log):
    """ITSF格式直接解析 + 原始扫描回退"""
    log("  [ITSF] 正在解析 CHM 二进制...", 'info')
    with open(chm_path, 'rb') as f:
        data = f.read()

    if data[:4] != b'ITSF':
        log("  [ITSF] 非 ITSF 格式，回退到原始扫描", 'info')
        return _extract_via_scan_raw(data)

    entries = {}
    try:
        import struct
        dir_off = struct.unpack_from('<I', data, 72)[0]
        cont_off = struct.unpack_from('<I', data, 88)[0]
        pmgl_pos = data.find(b'PMGL', dir_off)
        if pmgl_pos >= 0:
            chunk = data[pmgl_pos:pmgl_pos + 4096]
            free_space = struct.unpack_from('<I', chunk, 4)[0]
            end_data = max(20, min(4096 - free_space, 4096))
            pos = 20
            while pos < end_data:
                name_len = chunk[pos]; pos += 1
                if name_len == 0 or name_len > 200 or pos + name_len > len(chunk):
                    break
                name = chunk[pos:pos + name_len].decode('latin-1', errors='replace')
                pos += name_len
                section = chunk[pos]; pos += 1
                off = 0; shift = 0
                while pos < len(chunk):
                    b = chunk[pos]; pos += 1
                    off |= (b & 0x7F) << shift; shift += 7
                    if not (b & 0x80): break
                length = 0; shift = 0
                while pos < len(chunk):
                    b = chunk[pos]; pos += 1
                    length |= (b & 0x7F) << shift; shift += 7
                    if not (b & 0x80): break
                if section == 0 and 0 < length < 50_000_000:
                    abs_pos = cont_off + off
                    if abs_pos + length <= len(data):
                        entries[name] = data[abs_pos:abs_pos + length]
        log(f"  [ITSF] {len(entries)} 个 section-0 文件", 'info')
    except Exception as e:
        log(f"  [ITSF] 警告: {e}", 'info')

    # 检测无扩展名的HTML文件
    if sum(1 for k in entries if k.lower().endswith(('.html', '.htm'))) == 0:
        for name, raw in list(entries.items()):
            try:
                d = decode_html(raw)
                if d.lstrip().lower().startswith(('<html', '<!doctype', '<?xml')):
                    new_name = name + ('' if name.lower().endswith('.html') else '.html')
                    entries[new_name] = raw
            except Exception:
                continue

    total_html = sum(1 for k in entries if k.lower().endswith(('.html', '.htm')))
    if total_html == 0:
        entries.update(_extract_via_scan_raw(data))
        total_html = sum(1 for k in entries if k.lower().endswith(('.html', '.htm')))
    log(f"  [ITSF] 总计: {len(entries)} 个文件 ({total_html} HTML)", 'info')
    return entries


def _extract_via_scan_raw(data):
    """原始二进制扫描查找HTML内容"""
    entries = {}; idx = 0
    while idx < len(data):
        p = data.find(b'<html', idx)
        if p < 0: p = data.find(b'<HTML', idx)
        if p < 0: p = data.find(b'<?xml', idx)
        if p < 0: break
        e1 = data.find(b'</html>', p + 5)
        if e1 < 0: e1 = data.find(b'</HTML>', p + 5)
        if e1 < 0: idx = p + 1; continue
        e1 += len(b'</html>')
        chunk = data[p:e1]
        try:
            d = decode_html(chunk)
            if '<body' in d.lower() or '<title>' in d.lower() or '<h1>' in d.lower():
                entries[f'page_{len(entries)}.html'] = chunk
        except Exception:
            pass
        idx = e1
        if len(entries) > 500: break
    return entries


# ═══════════════════ TOC Flat & Index ═══════════════════

def _assign_toc_numbers(node, numbers=None):
    """递归为TOCNode分配层级数字编号，如 1, 1.2, 1.2.7.5"""
    if numbers is None:
        numbers = []
    for i, child in enumerate(node.children):
        child_numbers = numbers + [str(i + 1)]
        child.number = '.'.join(child_numbers)
        if child.children:
            _assign_toc_numbers(child, child_numbers)


def _prepend_book_number(node, book_num):
    """在TOC树的所有编号前加上书序号，如 1.2 → 3.1.2（文件夹模式用）"""
    for child in node.children:
        if child.number:
            child.number = f"{book_num}.{child.number}"
        if child.children:
            _prepend_book_number(child, book_num)


def _toc_to_flat(node, prefix=''):
    """递归展开TOCNode树为扁平列表"""
    result = []
    for child in node.children:
        title = child.title.strip()
        local = (child.file_path or '').replace('\\', '/')
        # merge节点视作有子节点（子项来自合并的CHM）
        has_children = len(child.children) > 0 or child.merge_ref is not None
        clean = _clean_path(title) if title else 'untitled'
        number = child.number or ''

        # 将数字编号加入目录/文件名，如 "1.1.2 VBScript基础"
        if number:
            clean = f"{number} {clean}"

        if has_children:
            child_prefix = f"{prefix}/{clean}" if prefix else clean
            # 无local的纯分组节点也收录（如"杂项"），toc_map构建时会自动跳过
            result.append((prefix, clean, title, local or '', True, number))
            result.extend(_toc_to_flat(child, child_prefix))
        else:
            if local:
                result.append((prefix, clean, title, local, False, number))
    return result


def _remap_flat(flat_sub, parent_prefix):
    """将子CHM的扁平列表重映射到父前缀下"""
    result = []
    for p_parent, fn, title, local, has_children, number in flat_sub:
        new_parent = f"{parent_prefix}/{p_parent}" if p_parent else parent_prefix
        result.append((new_parent, fn, title, local, has_children, number))
    return result


def _write_index(out_dir, title, entries, log):
    lines = [f"# {title}", ""]
    for parent, fn, etitle, local, has_children, number in entries:
        num_prefix = f"{number}  " if number else ""
        if has_children:
            lines.append(f"- **{num_prefix}{etitle}**")
            if local:
                lines.append(f"  - [概述]({fn}/{fn}.md)")
            else:
                lines.append(f"  - 📁 `{fn}/`")
        else:
            lines.append(f"- [{num_prefix}{etitle}]({fn}.md)")
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / '_index.md'
    fp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    log(f"  📑 {fp.parent.name}/_index.md", 'info')


# ═══════════════════ Main Converter ═══════════════════

def convert_chm(chm_path, out_root, log, book_num=None):
    """Convert CHM -> structured Markdown with TOC, metadata, etc.
    book_num: 文件夹模式下的书序号，会加到所有编号前(如 1.2→3.1.2)和目录名前"""
    p = Path(chm_path)
    log(f"处理: {p.name}", 'info')
    log("-" * 50, 'info')

    md_converter = MDConverter()

    # ── 初始化调试临时目录 (使用系统临时目录，30分钟自动清理) ──
    dbg = TempFileManager.get()
    dbg.init(Path(tempfile.gettempdir()) / 'chm2md_debug', ttl=1800)
    dbg.write('_source_chm.txt', f"{p.name}\n{p.resolve()}\n")

    # ── Extract ──
    files = _extract_via_7z(str(p), log)
    if not files:
        files = _extract_via_hh(str(p), log)
    if not files:
        files = _extract_via_itsf(str(p), log)

    # 调试: 记录提取到的文件清单
    dbg.write_json('_extracted_files.json', {
        'chm': p.name, 'total': len(files),
        'html_count': sum(1 for k in files if k.lower().endswith(('.html','.htm'))),
        'file_list': sorted(files.keys())
    })

    html_map = {}
    for k, v in files.items():
        if k.lower().endswith(('.html', '.htm')):
            html_map[k.replace('\\', '/').lower()] = v

    if not html_map:
        log("  中止: 未找到 HTML 文件", 'error')
        return 0, Path(out_root) / _safe_fn(p.stem)

    # ── Parse TOC (主 + 合并子CHM) ──
    hhc_data = next((v for k, v in files.items() if k.lower().endswith('.hhc')), None)
    toc_parser = TOCParser()
    toc_root = None
    flat = []
    if hhc_data:
        # 保存原始hhc到调试目录
        try:
            dbg.write_bytes('_main.hhc', hhc_data)
        except Exception:
            pass
        toc_root = toc_parser.parse(hhc_data)
        _assign_toc_numbers(toc_root)
        if book_num is not None:
            _prepend_book_number(toc_root, book_num)
        flat = _toc_to_flat(toc_root)

    # ── 处理合并引用 (merge) ──
    unmerged = []  # 未找到子CHM的合并节点
    for title, merge_ref, parent_title in toc_parser.merge_refs:
        # 解析 merge_ref: "opc.chm::/opc.hhc" → chm_file="opc.chm"
        chm_file = merge_ref.split('::')[0]
        chm_path = Path(p.parent) / chm_file  # 在同一个目录下查找
        if not chm_path.exists():
            # 尝试在 CHM 同级目录查找
            log(f"  ⚠ 合并子CHM未找到: {chm_file} (引用自 {parent_title} → {title})", 'info')
            unmerged.append((title, merge_ref))
            continue

        log(f"  📦 处理合并子CHM: {chm_file} → {title}", 'info')
        try:
            sub_files = _extract_via_7z(str(chm_path), log)
            if not sub_files:
                sub_files = _extract_via_hh(str(chm_path), log)
            if not sub_files:
                sub_files = _extract_via_itsf(str(chm_path), log)

            if sub_files:
                # 合并HTML到主html_map
                sub_cnt = 0
                for k, v in sub_files.items():
                    if k.lower().endswith(('.html', '.htm')):
                        html_map[k.replace('\\', '/').lower()] = v
                        sub_cnt += 1
                    # 合并所有文件（图片等）
                    if k not in files:
                        files[k] = v

                # 解析子CHM的TOC
                sub_hhc = next((v for k, v in sub_files.items() if k.lower().endswith('.hhc')), None)
                if sub_hhc:
                    try:
                        dbg.write_bytes(f'_sub_{_clean_path(title)}.hhc', sub_hhc)
                    except Exception:
                        pass
                    sub_parser = TOCParser()
                    sub_root = sub_parser.parse(sub_hhc)
                    _assign_toc_numbers(sub_root)
                    if book_num is not None:
                        _prepend_book_number(sub_root, book_num)
                    sub_flat = _toc_to_flat(sub_root)
                    # 重映射到 merge 节点下
                    clean_title = _clean_path(title)
                    remapped = _remap_flat(sub_flat, clean_title)
                    flat.extend(remapped)
                    log(f"  ✓ 合并了 {sub_cnt} 个HTML, {len(remapped)} 个TOC条目", 'success')

                    # 递归处理子CHM的 merge 引用
                    for stitle, smerge, sparent in sub_parser.merge_refs:
                        schm_file = smerge.split('::')[0]
                        schm_path = Path(chm_path.parent) / schm_file
                        if schm_path.exists():
                            log(f"    ↳ 递归合并: {schm_file} → {stitle}", 'info')
                            # 简化处理：只记录，不做多层递归（避免无限循环）
                            unmerged.append((f"{title}/{stitle}", smerge))
        except Exception as e:
            log(f"  ✗ 处理子CHM失败 {chm_file}: {e}", 'error')

    # 调试: 记录TOC扁平列表和合并引用
    dbg.write_json('_toc_flat.json', {
        'total_entries': len(flat),
        'merge_refs': [{'title': t, 'ref': r, 'parent': pt} for t, r, pt in toc_parser.merge_refs],
        'unmerged': unmerged,
        'entries': [
            {'parent': p, 'fn': f, 'title': t, 'local': l, 'has_children': h, 'number': n}
            for p, f, t, l, h, n in flat
        ]
    })

    # ── 折叠根级目录 (三层→两层): 循环递归折叠，直到根级无has_children ──
    if book_num is not None and toc_root and toc_root.children:
        first_child_title = _clean_path(toc_root.children[0].title)
        base_dir = Path(out_root) / f"{book_num:02d}_{first_child_title}"
        # 递归折叠：每次消去一层根级目录节点，直到根级只有叶子
        collapse_rounds = 0
        while collapse_rounds < 50:  # 防护上限
            _root_dir_names = {e[1] for e in flat if e[0] == "" and e[4]}
            if not _root_dir_names:
                break
            new_flat = []
            for entry in flat:
                parent, fn, title, local, has_children, number = entry
                if parent == "" and has_children:
                    # 根级目录条目 → 变为叶子文件
                    new_flat.append(("", fn, title, local, False, number))
                elif parent in _root_dir_names:
                    # 被折叠条目的直接子节点 → 提升到根级
                    new_flat.append(("", fn, title, local, has_children, number))
                elif "/" in parent:
                    # 检查是否更深层后代需要减少一级前缀
                    top = parent.split("/", 1)[0]
                    if top in _root_dir_names:
                        new_parent = parent[len(top) + 1:]
                        new_flat.append((new_parent, fn, title, local, has_children, number))
                    else:
                        new_flat.append(entry)
                else:
                    new_flat.append(entry)
            if new_flat == flat:
                break  # 无变化，防止死循环
            flat = new_flat
            collapse_rounds += 1
        log(f"  📐 折叠根级目录 ({collapse_rounds}轮) → 输出到 {base_dir.name}/", 'info')
    else:
        base_dir = Path(out_root) / _safe_fn(f"{book_num:02d}_{p.stem}" if book_num else p.stem)

    # ── Build TOC map (full-path + basename fallback) ──
    toc_map = {}       # 完整路径匹配 (去碎片后)
    toc_basename = {}   # basename 回退 (去碎片后)
    for parent, fn, title, local, has_children, number in flat:
        loc = local.replace('\\', '/').lower()
        # 去除URL碎片 (#section)
        if '#' in loc:
            loc = loc.split('#')[0]
        if loc:
            toc_map[loc] = (parent, fn, title, has_children, number)
            fname = os.path.basename(loc)
            if fname:
                toc_basename[fname] = (parent, fn, title, has_children, number)

    # 调试: 记录路径映射
    dbg.write_json('_toc_map.json', {
        'full_path_keys': len(toc_map),
        'basename_keys': len(toc_basename),
        'duplicate_basenames': [
            b for b in toc_basename if sum(1 for l in toc_map if os.path.basename(l) == b) > 1
        ][:200]
    })

    # ── Convert HTML -> MD ──
    groups = {}
    orphans = {}  # 记录无TOC文件的归类情况
    total = len(html_map)
    success = 0; failed = 0; skipped = 0

    for idx, (hp, raw) in enumerate(sorted(html_map.items()), 1):
        hp_key = hp.replace('\\', '/').lower()
        hp_fname = os.path.basename(hp_key)
        # 优先完整路径匹配，回退到 basename
        entry = toc_map.get(hp_key) or toc_basename.get(hp_fname)

        try:
            html = decode_html(raw)

            # 清理HTML
            cleaned = HTMLCleaner.clean(html)

            # 提取标题
            if entry:
                title = entry[2]
            else:
                title = HTMLCleaner.extract_title(cleaned) or hp_fname.replace('.htm', '').replace('.html', '')

            # 嵌入图片
            cleaned = _embed_images(cleaned, files)

            # 转Markdown
            md = md_converter.convert(cleaned)

            if not md.strip():
                log(f"  [{idx}/{total}] ⏭ 跳过空白: {hp_fname}", 'info')
                skipped += 1
                continue

            # 修复链接
            md = fix_markdown_links(md)

            # 添加标题（有TOC条目时加上层级编号前缀）
            if title and not md.lstrip().startswith('#'):
                if entry:
                    number = entry[4] if len(entry) > 4 else ''
                    prefix = f"{number}  " if number else ""
                    md = f"# {prefix}{title}\n\n{md}"
                else:
                    md = f"# {title}\n\n{md}"

            # 确定输出路径
            if entry:
                parent, fn, _, has_children, number = entry if len(entry) >= 5 else (*entry, '')
                out_dir = base_dir / parent if parent else base_dir
                out_dir.mkdir(parents=True, exist_ok=True)
                if has_children:
                    # 文件夹自身的内容页用纯标题命名（编号已在目录名中体现）
                    page_dir = out_dir / fn
                    page_dir.mkdir(parents=True, exist_ok=True)
                    _write_page_md(md, title, page_dir, _clean_path(title))
                else:
                    _write_page_md(md, title, out_dir, fn)
                key = parent or '__root__'
                groups.setdefault(key, []).append((parent, fn, title, hp_fname, has_children, number))
            else:
                # 无TOC条目：跳过，用户只需要TOC中的内容
                continue

            log(f"  [{idx}/{total}] ✓ {fn}.md", 'success')
            success += 1

        except Exception as e:
            log(f"  [{idx}/{total}] ✗ {hp_fname}: {e}", 'error')
            failed += 1

    # ── Final stats (图片已base64内嵌于md中，无需单独复制) ──
    stats = {"total": total, "success": success, "failed": failed, "skipped": skipped,
             "images": 0, "has_toc": toc_root is not None}

    # 调试: 记录转换摘要
    orphan_summary = {}
    for cat, items in orphans.items():
        orphan_summary[cat] = {'count': len(items), 'files': sorted(items)[:50]}
    dbg.write_json('_convert_summary.json', {
        'chm': p.name, 'stats': stats,
        'toc_entries': len(flat),
        'html_map_size': len(html_map),
        'output_dir': str(base_dir.resolve()),
        'orphans': {'total': sum(len(v) for v in orphans.values()), 'by_category': orphan_summary},
    })

    log(f"  完成: {p.name} → {success} 个 .md | {failed} 失败 | {skipped} 跳过", 'success')
    return success, base_dir


def _get_orphan_output_dir(base_dir: Path, chm_internal_path: str) -> Path:
    """为非TOC文件确定输出目录，镜像CHM内部文件夹结构"""
    # 提取CHM内部路径中的目录部分，如 html/some.htm → html
    internal_dir = ''
    path_normalized = chm_internal_path.replace('\\', '/')
    if '/' in path_normalized:
        internal_dir = path_normalized.rsplit('/', 1)[0]
    if not internal_dir:
        return base_dir / '_root'
    # 清理目录名
    safe = re.sub(r'[\\/:*?"<>|]', '_', internal_dir)
    # 去掉可能重复的层级前缀
    return base_dir / f'_{safe}'


def _dump_tree_snapshot(dbg, root_dir, filename, max_depth=5):
    """将目录树快照写入调试文件"""
    lines = [f"目录树快照: {root_dir.name}", "=" * 60]
    try:
        for d in sorted(root_dir.rglob('*')):
            if d.is_dir():
                rel = d.relative_to(root_dir)
                depth = len(rel.parts)
                if depth > max_depth:
                    continue
                prefix = "  " * depth
                lines.append(f"{prefix}[D] {rel.parts[-1] if depth > 0 else d.name}")
        dbg.write(filename, '\n'.join(lines))
    except Exception:
        pass


def _write_page_md(md, title, out_dir, fn):
    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / f"{fn}.md"
    _fn = fn
    c = 1
    while fp.exists() and c < 100:
        _fn = f"{fn}_{c}"; c += 1
        fp = out_dir / f"{_fn}.md"
    fp.write_text(md, encoding='utf-8')





# ═══════════════════ Tkinter GUI ═══════════════════

class Tooltip:
    """鼠标悬停提示"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        self._show_id = None
        self._hide_id = None
        widget.bind('<Enter>', self._enter)
        widget.bind('<Leave>', self._leave)

    def _enter(self, e=None):
        if self._hide_id:
            self.widget.after_cancel(self._hide_id)
            self._hide_id = None
        if not self._show_id and not self.tip:
            self._show_id = self.widget.after(400, self._show)

    def _leave(self, e=None):
        if self._show_id:
            self.widget.after_cancel(self._show_id)
            self._show_id = None
        if self.tip and not self._hide_id:
            self._hide_id = self.widget.after(200, self._hide)

    def _show(self):
        self._show_id = None
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 5
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        lbl = tk.Label(self.tip, text=self.text, background='#ffffcc',
                       foreground='#333333', font=('Microsoft YaHei', 9),
                       relief='solid', borderwidth=1, padx=6, pady=3)
        lbl.pack()

    def _hide(self):
        self._hide_id = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _split_large_md(filepath, max_mb=10, log=None):
    """将过大的 .md 文件按 H2 标题边界分割，每个分块 < max_mb MB。
    返回: (是否分割, 生成文件数)"""
    import re as _re
    fp = Path(filepath)
    if not fp.exists():
        return False, 0
    raw = fp.read_bytes()
    if len(raw) <= max_mb * 1024 * 1024:
        return False, 0
    text = raw.decode('utf-8', errors='replace')
    lines = text.split('\n')
    # 找 H2 边界
    h2_idx = [0]
    for i, line in enumerate(lines):
        if _re.match(r'^##\s', line):
            h2_idx.append(i)
    h2_idx.append(len(lines))
    # 构建 section 列表 (start, end, text)
    sections = []
    for j in range(len(h2_idx) - 1):
        s, e = h2_idx[j], h2_idx[j + 1]
        sections.append((s, e, '\n'.join(lines[s:e])))
    max_bytes = max_mb * 1024 * 1024
    chunks = []
    cur_text = ''
    cur_start = 0
    for s, e, sec_text in sections:
        sec_b = len(sec_text.encode('utf-8'))
        if sec_b > max_bytes:
            # 大 section：按行级分割
            if cur_text:
                chunks.append((cur_start, cur_text))
                cur_text = ''
            chunk_lines = []
            cstart = s
            for k in range(s, e):
                lb = len((lines[k] + '\n').encode('utf-8'))
                if chunk_lines and len('\n'.join(chunk_lines + [lines[k]]).encode('utf-8')) > max_bytes:
                    chunks.append((cstart, '\n'.join(chunk_lines)))
                    chunk_lines = [lines[k]]
                    cstart = k
                else:
                    chunk_lines.append(lines[k])
            if chunk_lines:
                chunks.append((cstart, '\n'.join(chunk_lines)))
            cur_start = e
            continue
        # 正常累积
        test = cur_text + '\n' + sec_text if cur_text else sec_text
        if len(test.encode('utf-8')) > max_bytes and cur_text:
            chunks.append((cur_start, cur_text))
            cur_text = sec_text
            cur_start = s
        else:
            cur_text = test
            if cur_start == 0:
                cur_start = s
    if cur_text:
        chunks.append((cur_start, cur_text))
    if len(chunks) <= 1:
        return False, 0
    # 写入分块
    stem, ext = fp.stem, fp.suffix
    for ci, (_, chunk_text) in enumerate(chunks, 1):
        new_path = fp.parent / f'{stem}({ci}){ext}'
        new_path.write_text(chunk_text + '\n' if not chunk_text.endswith('\n') else chunk_text, encoding='utf-8')
        if log:
            cmb = len(chunk_text.encode('utf-8')) / (1024 * 1024)
            log(f"    → {new_path.name} ({cmb:.1f} MB)", 'info')
    fp.unlink()
    # 🔁 递归分割：检查输出文件是否仍超限，是则再次分割（防止尾块/超大单行穿透）
    total_parts = len(chunks)
    for ci in range(1, len(chunks) + 1):
        sub_path = fp.parent / f'{stem}({ci}){ext}'
        if sub_path.exists() and sub_path.stat().st_size > max_bytes:
            sub_ok, sub_cnt = _split_large_md(sub_path, max_mb, log)
            if sub_ok:
                total_parts += sub_cnt - 1  # 原文件被替换为 sub_cnt 个文件
    if log:
        log(f"  ✂ 分割完成: {fp.name} → {total_parts} 个文件", 'info')
    return True, total_parts


class App:
    def __init__(self, r):
        self.r = r
        r.title("CHM/HTML/MHTML/URL → Markdown v3")
        r.geometry("760x760"); r.minsize(600, 580)
        self.run = False
        s = ttk.Style(); s.theme_use('clam')
        s.configure('TButton', font=('Microsoft YaHei', 10))
        s.configure('TLabel', font=('Microsoft YaHei', 10))
        s.configure('Big.TButton', font=('Microsoft YaHei', 12, 'bold'))
        self._files = []
        self._ui()

    def _check_tools(self):
        """检测可用的提取工具"""
        tools = {}
        # 7-Zip
        tools['7z'] = _find_7z() is not None
        # hh.exe (Windows内置，C:\Windows\hh.exe)
        try:
            r = subprocess.run(
                ['where', 'hh.exe'], capture_output=True, timeout=5,
                shell=True)
            tools['hh'] = r.returncode == 0
        except Exception:
            tools['hh'] = False
        # ITSF (内置，始终可用)
        tools['itsf'] = True
        return tools

    def _ui(self):
        # ── 模式选择 ──
        top = ttk.Frame(self.r, padding=(15, 15, 15, 5)); top.pack(fill='x')
        mode_lbl = ttk.Label(top, text="模式：")
        mode_lbl.pack(side='left')
        Tooltip(mode_lbl, "选择输入方式：CHM/HTML/MHTML/URL")
        self.m = tk.StringVar(value='single')

        # 第一行: CHM 三种模式
        row1 = ttk.Frame(self.r, padding=(15, 0, 15, 0)); row1.pack(fill='x')
        ttk.Label(row1, text="  CHM:", font=('Microsoft YaHei', 9, 'bold')).pack(side='left')
        for val, txt, tip in [
            ('single', '单个文件', '选择一个 .chm 文件进行转换'),
            ('multi', '多个文件', '一次选择多个 .chm 文件批量转换'),
            ('folder', '整个文件夹', '选择文件夹，自动扫描其中所有 .chm 文件'),
        ]:
            rb = ttk.Radiobutton(row1, text=txt, variable=self.m,
                                 value=val, command=self._fm)
            rb.pack(side='left', padx=(8, 2))
            Tooltip(rb, tip)

        # 第二行: HTML / MHTML / URL
        row2 = ttk.Frame(self.r, padding=(15, 2, 15, 0)); row2.pack(fill='x')
        ttk.Label(row2, text="  其他:", font=('Microsoft YaHei', 9, 'bold')).pack(side='left')
        for val, txt, tip in [
            ('html', 'HTML 文件', '选择一个 .html/.htm 网页文件转换'),
            ('mhtml', 'MHTML 文件', '选择浏览器保存的 .mhtml 单文件'),
            ('url', '网页 URL', '输入在线网页地址，自动抓取并转换'),
        ]:
            rb = ttk.Radiobutton(row2, text=txt, variable=self.m,
                                 value=val, command=self._fm)
            rb.pack(side='left', padx=(8, 2))
            Tooltip(rb, tip)

        # ── 工具状态 ──
        tools = self._check_tools()
        sf = ttk.LabelFrame(self.r, text="提取工具", padding=(12, 8))
        sf.pack(fill='x', padx=15, pady=(8, 0))
        status_row = ttk.Frame(sf); status_row.pack(fill='x')

        items = [
            ("7-Zip（首选）", tools['7z'],
             "最可靠的提取方式，速度快，兼容性好\n如不可用请安装 7-Zip 并加入 PATH"),
            ("hh.exe（备选）", tools['hh'],
             "Windows 内置的 HTML Help 解压工具\n无需额外安装"),
            ("ITSF 解析器（内置）", tools['itsf'],
             "纯 Python 实现的 CHM 解析器\n无需任何外部依赖，始终可用"),
        ]
        for name, ok, tip_text in items:
            icon = "✔" if ok else "✘"
            color = "#4ec9b0" if ok else "#f44747"
            lbl = tk.Label(status_row, text=f"{icon} {name}", fg=color,
                           font=('Microsoft YaHei', 9), cursor='hand2')
            lbl.pack(side='left', padx=(0, 18))
            Tooltip(lbl, tip_text)

        # ── 输入 ──
        f1 = ttk.LabelFrame(self.r, text="输入", padding=(15, 10))
        f1.pack(fill='x', padx=15, pady=(5, 0))
        self.il = ttk.Label(f1, text="CHM 文件："); self.il.pack(anchor='w', pady=(0, 5))
        r1 = ttk.Frame(f1); r1.pack(fill='x')
        self.iv = tk.StringVar()
        self.ent = ttk.Entry(r1, textvariable=self.iv)
        self.ent.pack(side='left', fill='x', expand=True)
        Tooltip(self.ent, "所选文件的路径\nCHM/HTML/MHTML 模式可直接粘贴路径\nURL 模式请输入网址")
        self.btn_browse = ttk.Button(r1, text="浏览...", command=self._bi)
        self.btn_browse.pack(side='left', padx=(8, 0))
        Tooltip(self.btn_browse, "打开文件对话框选择文件或文件夹")

        # ── 输出 ──
        f2 = ttk.LabelFrame(self.r, text="输出", padding=(15, 10))
        f2.pack(fill='x', padx=15, pady=(10, 0))
        out_lbl = ttk.Label(f2, text="输出目录（默认: ./md_output）：")
        out_lbl.pack(anchor='w', pady=(0, 5))
        Tooltip(out_lbl, "转换后的 Markdown 文件将保存到此目录\n留空则使用程序目录下的 md_output 文件夹")
        r2 = ttk.Frame(f2); r2.pack(fill='x')
        self.ov = tk.StringVar()
        ent2 = ttk.Entry(r2, textvariable=self.ov)
        ent2.pack(side='left', fill='x', expand=True)
        Tooltip(ent2, "可直接输入或粘贴输出路径\n留空使用默认路径")
        btn_out = ttk.Button(r2, text="浏览...", command=self._bo)
        btn_out.pack(side='left', padx=(8, 0))
        Tooltip(btn_out, "选择输出文件夹")

        # ── 开始按钮 ──
        bf = ttk.Frame(self.r, padding=(15, 12)); bf.pack(fill='x')
        self.btn = ttk.Button(bf, text="开始转换", command=self._start, style='Big.TButton')
        self.btn.pack(fill='x', ipady=4)
        Tooltip(self.btn, "点击开始将 CHM 文件转换为 Markdown\n转换过程中可随时取消")

        # ── 进度条 ──
        self.pv = tk.DoubleVar()
        pb = ttk.Progressbar(self.r, variable=self.pv, maximum=100)
        pb.pack(fill='x', padx=15, pady=(5, 0))
        Tooltip(pb, "转换进度，0% ~ 100%")
        self.pl = ttk.Label(self.r, text=""); self.pl.pack(anchor='center', padx=15)

        # ── 日志 ──
        lf = ttk.LabelFrame(self.r, text="日志", padding=(10, 8))
        lf.pack(fill='both', expand=True, padx=15, pady=(10, 15))
        self.log = scrolledtext.ScrolledText(
            lf, height=14, font=('Consolas', 10),
            bg='#1e1e1e', fg='#d4d4d4', insertbackground='white',
            relief='flat', borderwidth=0)
        self.log.pack(fill='both', expand=True)
        for tg, cl in [('success', '#4ec9b0'), ('error', '#f44747'), ('info', '#569cd6'), ('warn', '#e5b73c')]:
            self.log.tag_configure(tg, foreground=cl)

    def _fm(self):
        mode = self.m.get()
        labels = {
            'single': "CHM 文件：", 'multi': "CHM 文件：", 'folder': "CHM 文件夹：",
            'html': "HTML 文件：", 'mhtml': "MHTML 文件：", 'url': "网页 URL：",
        }
        browse_texts = {
            'single': "浏览...", 'multi': "浏览...", 'folder': "浏览...",
            'html': "选择 HTML...", 'mhtml': "选择 MHTML...", 'url': "抓取",
        }
        self.il.config(text=labels.get(mode, "选择："))
        self.btn_browse.config(text=browse_texts.get(mode, "浏览..."))
        self._files = []; self.iv.set('')

    def _bi(self):
        mode = self.m.get()
        if mode == 'single':
            p = filedialog.askopenfilename(title="选择 CHM 文件", filetypes=[("CHM 文件", "*.chm")])
            if p: self.iv.set(p); self._files = [p]
        elif mode == 'multi':
            paths = filedialog.askopenfilenames(
                title="选择多个 CHM 文件", filetypes=[("CHM 文件", "*.chm")])
            if paths:
                self._files = list(paths)
                self.iv.set(f"已选择 {len(paths)} 个文件")
        elif mode == 'folder':
            p = filedialog.askdirectory(title="选择包含 CHM 文件的文件夹")
            if p: self.iv.set(p); self._files = sorted(str(f) for f in Path(p).glob("*.chm"))
        elif mode == 'html':
            p = filedialog.askopenfilename(
                title="选择 HTML 文件",
                filetypes=[("HTML 文件", "*.html *.htm"), ("所有文件", "*.*")])
            if p: self.iv.set(p); self._files = [p]
        elif mode == 'mhtml':
            p = filedialog.askopenfilename(
                title="选择 MHTML 文件",
                filetypes=[("MHTML 文件", "*.mhtml *.mht"), ("所有文件", "*.*")])
            if p: self.iv.set(p); self._files = [p]
        elif mode == 'url':
            # URL 模式不需要浏览文件，输入框即可
            pass

    def _bo(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p: self.ov.set(p)

    def _log(self, t, tag='info'):
        """线程安全的日志输出。"""
        def _update():
            try:
                self.log.insert('end', t + '\n', tag)
                self.log.see('end')
            except Exception:
                pass  # 窗口已销毁时静默忽略
        # 在主线程执行 UI 更新（线程安全）
        try:
            self.r.after(0, _update)
        except Exception:
            pass  # root 已销毁时静默忽略

    def _start(self):
        op = self.ov.get().strip()
        if not op:
            op = str(Path(__file__).parent / 'md_output')

        mode = self.m.get()
        tasks = []
        input_type = 'chm'  # chm | html | mhtml | url

        if mode in ('single', 'multi', 'folder'):
            # ── CHM 模式 ──
            if mode == 'single':
                ip = self.iv.get().strip()
                if not ip: messagebox.showwarning("提示", "请选择一个文件！"); return
                if not os.path.exists(ip): messagebox.showerror("错误", "文件不存在"); return
                tasks = [ip]
            elif mode == 'multi':
                if not self._files: messagebox.showwarning("提示", "请先选择文件！"); return
                tasks = sorted(self._files)
            else:
                ip = self.iv.get().strip()
                if not ip: messagebox.showwarning("提示", "请选择一个文件夹！"); return
                tasks = sorted(str(f) for f in Path(ip).glob("*.chm"))
                if not tasks:
                    messagebox.showwarning("提示", "文件夹中没有 .chm 文件"); return
            input_type = 'chm'
        elif mode == 'html':
            ip = self.iv.get().strip()
            if not ip: messagebox.showwarning("提示", "请选择 HTML 文件！"); return
            if not os.path.exists(ip): messagebox.showerror("错误", "文件不存在"); return
            tasks = [ip]
            input_type = 'html'
        elif mode == 'mhtml':
            ip = self.iv.get().strip()
            if not ip: messagebox.showwarning("提示", "请选择 MHTML 文件！"); return
            if not os.path.exists(ip): messagebox.showerror("错误", "文件不存在"); return
            tasks = [ip]
            input_type = 'mhtml'
        elif mode == 'url':
            url = self.iv.get().strip()
            if not url: messagebox.showwarning("提示", "请输入网页 URL！"); return
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url
            tasks = [url]
            input_type = 'url'

        # ── CHM 多文件时询问是否整体统一编号 ──
        unified = False
        if input_type == 'chm' and len(tasks) > 1:
            unified = messagebox.askyesno(
                "编号方式",
                f"检测到 {len(tasks)} 个 CHM 文件。\n\n"
                "是否整体统一编号？\n\n"
                "  [是] — 所有文件按顺序统一编号\n"
                "         (如第3本书的1.2节 → 3.1.2)\n"
                "  [否] — 每个文件独立编号\n"
                "         (每个文件都从1开始)\n\n"
                "建议选择 [是]，方便跨文件检索。"
            )

        self.run = True
        self.btn.config(text="转换中...", state='disabled')
        self.pv.set(0); self.pl.config(text=""); self.log.delete('1.0', 'end')

        def work():
            try:
                self._log("CHM/HTML/MHTML/URL → Markdown v3", 'info')
                self._log("=" * 50, 'info')

                if input_type == 'chm':
                    count = 0; total = len(tasks)
                    self._log(f"待处理 CHM 文件数: {total}", 'info')
                    if unified:
                        self._log("编号方式: 整体统一编号", 'info')
                    else:
                        self._log("编号方式: 各文件独立编号", 'info')
                    for i, cf in enumerate(tasks):
                        pct = 10 + 85 * i // total if total else 100
                        self.r.after(0, lambda p=pct, t=f"{i+1}/{total}": (
                            self.pv.set(p), self.pl.config(text=t)))
                        try:
                            bnum = (i + 1) if unified else None
                            n, out_dir = convert_chm(
                                cf, op, lambda m, t='info': self._log(m, t), bnum)
                            count += n
                        except Exception as e:
                            self._log(f"失败: {Path(cf).name}: {e}", 'error')
                    self.r.after(0, lambda: (self.pv.set(100), self.pl.config(text="完成！")))
                    self._log(f"\n总计: {count} 个 .md 文件（来自 {total} 个 CHM）", 'success')

                elif input_type == 'html':
                    self._log(f"输入: HTML 文件 ({Path(tasks[0]).name})", 'info')
                    self.r.after(0, lambda: self.pv.set(20))
                    html, title = _read_html_file(tasks[0], lambda m, t='info': self._log(m, t))
                    # 检测浏览器保存的配套文件夹，加载图片
                    files_dict = {}
                    companion = _find_companion_folder(tasks[0])
                    if companion:
                        self._log(f"检测到配套文件夹: {companion.name}", 'info')
                        files_dict = _load_companion_images(companion,
                            lambda m, t='info': self._log(m, t))
                    self.r.after(0, lambda: self.pv.set(50))
                    _html_to_md(html, title, op, lambda m, t='info': self._log(m, t),
                              files_dict=files_dict if files_dict else None)
                    self.r.after(0, lambda: (self.pv.set(100), self.pl.config(text="完成！")))
                    self._log(f"\nHTML → Markdown 转换完成", 'success')

                elif input_type == 'mhtml':
                    self._log(f"输入: MHTML 文件 ({Path(tasks[0]).name})", 'info')
                    self.r.after(0, lambda: self.pv.set(20))
                    html, title, images_dict = _parse_mhtml(tasks[0], lambda m, t='info': self._log(m, t))
                    self.r.after(0, lambda: self.pv.set(50))
                    _html_to_md(html, title, op, lambda m, t='info': self._log(m, t),
                              files_dict=images_dict if images_dict else None)
                    self.r.after(0, lambda: (self.pv.set(100), self.pl.config(text="完成！")))
                    self._log(f"\nMHTML → Markdown 转换完成", 'success')

                elif input_type == 'url':
                    url = tasks[0]
                    self._log(f"输入: URL ({url})", 'info')
                    self.r.after(0, lambda: (self.pv.set(5), self.pl.config(text="正在抓取网页...")))

                    # ── Step 1: 首轮抓取（首页 + 侧边栏导航提取） ──
                    html, title, images_dict, nav_entries = _fetch_url_first_pass(
                        url, lambda m, t='info': self._log(m, t)
                    )

                    # 推导站点文件夹名（侧边栏根条目优先，比 page.title() 可靠）
                    site_folder_name = _derive_site_folder_name(title, nav_entries, url)

                    is_multi = len(nav_entries) >= 3  # 3+ 链接视为多页站点

                    # ── Step 2: 预览 ──
                    if is_multi:
                        # 整站预览：显示侧边栏导航结构（不含每页内部标题，需爬取后才有）
                        preview_site = [(text, href, []) for (text, href, _) in nav_entries]
                        self._log(f"检测到整站结构: {len(nav_entries)} 个章节", 'info')
                    else:
                        # 单页预览
                        headings = _extract_headings(html)

                    preview_event = threading.Event()
                    preview_result = [False]
                    preview_error = [None]

                    def show_preview():
                        try:
                            preview_data = preview_site if is_multi else headings
                            dlg = URLPreviewDialog(
                                self.r, url, title, preview_data,
                                len(images_dict), len(html)
                            )
                            preview_result[0] = dlg.wait()
                        except Exception as e:
                            preview_error[0] = e
                            import traceback
                            preview_error[0] = f"{e}\n{traceback.format_exc()}"
                        finally:
                            preview_event.set()

                    self.r.after(0, show_preview)
                    # 超时 120 秒，防止因 tk 嵌套事件循环异常导致永久阻塞
                    if not preview_event.wait(timeout=120):
                        self._log("预览窗口超时（120s），跳过预览继续转换", 'warn')
                    if preview_error[0]:
                        self._log(f"预览窗口创建失败: {preview_error[0]}", 'error')
                        # 预览失败不阻止转换，继续执行
                        preview_result[0] = True

                    if not preview_result[0]:
                        self._log("用户取消了转换", 'info')
                        self.r.after(0, lambda: (self.pv.set(0), self.pl.config(text="已取消")))
                        return

                    # ── Step 3: 整站爬取 或 单页转换 ──
                    if is_multi:
                        self._log(f"开始整站爬取 ({len(nav_entries)} 个页面)...", 'info')
                        self.r.after(0, lambda: (self.pv.set(10), self.pl.config(text="正在爬取所有页面...")))

                        site_title_new, page_data_list, combined_images, site_structure = \
                            _crawl_multi_page(url, lambda m, t='info': self._log(m, t),
                                            nav_entries=nav_entries,
                                            site_title=site_folder_name,
                                            existing_images=images_dict)

                        # 创建站点子目录
                        site_dir = Path(op) / _clean_path(site_title_new or 'site')
                        site_dir.mkdir(parents=True, exist_ok=True)

                        # 逐页转换保存
                        total_pages = len(page_data_list)
                        self.r.after(0, lambda: (self.pv.set(50),
                            self.pl.config(text=f"正在转换 {total_pages} 个页面...")))

                        toc_lines = [f"# {site_title_new}\n"]
                        saved_count = 0
                        for i, (chapter_num, section_text, section_url, page_html) in enumerate(page_data_list):
                            sub_pct = 50 + 45 * i // total_pages if total_pages else 95
                            self.r.after(0, lambda p=sub_pct: self.pv.set(p))

                            if not page_html:
                                self._log(f"  ! 跳过空内容: {chapter_num} {section_text}", 'warn')
                                toc_lines.append(
                                    f"- {chapter_num} {section_text} *(抓取失败)*")
                                continue

                            filename = f"{chapter_num} {section_text}"
                            _html_to_md(page_html, section_text, str(site_dir),
                                       lambda m, t='info': self._log(m, t),
                                       files_dict=combined_images if combined_images else None,
                                       basename=filename)
                            toc_lines.append(
                                f"- [{chapter_num} {section_text}]({filename}.md)")
                            saved_count += 1

                        # 生成目录索引文件
                        if saved_count:
                            self._log(f"已保存 {saved_count} 个章节", 'success')
                            index_content = '\n'.join(toc_lines) + '\n'
                            (site_dir / '_目录.md').write_text(index_content, encoding='utf-8')
                            self._log(f"目录索引: _目录.md", 'info')
                            self._log(f"输出目录: {site_dir.resolve()}", 'info')
                    else:
                        self._log("单页模式，开始转换...", 'info')
                        self.r.after(0, lambda: self.pv.set(50))
                        _html_to_md(html, title, op, lambda m, t='info': self._log(m, t),
                                  files_dict=images_dict if images_dict else None)

                    self.r.after(0, lambda: (self.pv.set(100), self.pl.config(text="完成！")))
                    self._log(f"\nURL → Markdown 转换完成", 'success')
                    if not is_multi:
                        self._log(f"输出目录: {Path(op).resolve()}", 'info')

                # ── 检测过大文件（所有模式） ──
                # rglob 递归搜索，整站分章节文件在子目录中也会被找到
                out_path = Path(op)
                big_files = []
                for mf in sorted(out_path.rglob('*.md')):
                    if not mf.is_file():
                        continue
                    sz = mf.stat().st_size
                    if sz > 10 * 1024 * 1024:
                        big_files.append((str(mf), sz))
                if big_files:
                    self._log(f"\n⚠ 检测到 {len(big_files)} 个文件超过 10 MB:", 'warn')
                    for bf, sz in big_files:
                        self._log(f"  {Path(bf).name} ({sz / 1024 / 1024:.1f} MB)", 'warn')
                    self.r.after(0, lambda bf=big_files: self._ask_split(bf))
            except Exception as e:
                self._log(f"致命错误: {e}", 'error')
                import traceback; self._log(traceback.format_exc(), 'error')
            finally:
                self.run = False
                self.r.after(0, lambda: self.btn.config(text="开始转换", state='normal'))

        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        if self.run and not messagebox.askokcancel("退出", "转换正在进行中，确定退出吗？"):
            return
        TempFileManager.get().shutdown()
        self.r.destroy()

    def _ask_split(self, big_files):
        """弹窗询问是否分割过大 .md 文件"""
        if not big_files:
            return
        names = '\n'.join(f'  · {Path(f).name} ({s / 1024 / 1024:.1f} MB)' for f, s in big_files[:20])
        if len(big_files) > 20:
            names += f'\n  ... 共 {len(big_files)} 个'
        ok = messagebox.askyesno(
            "分割大文件",
            f"检测到 {len(big_files)} 个 .md 文件超过 10 MB：\n\n"
            f"{names}\n\n"
            f"GitHub 单文件限制 100 MB，但大文件影响浏览和检索。\n"
            f"是否按 H2 标题自动分割为 <10 MB 的小文件？\n\n"
            f"分割后编号保留，仅加 (1)(2)(3) 后缀。"
        )
        if ok:
            self._log("\n✂ 开始分割大文件...", 'info')
            threading.Thread(target=self._do_split, args=(big_files,), daemon=True).start()

    def _do_split(self, big_files):
        """在后台线程执行文件分割"""
        count = 0
        for fp, sz in big_files:
            try:
                did, n = _split_large_md(fp, 10, lambda m, t='info': self._log(m, t))
                if did:
                    count += 1
            except Exception as e:
                self._log(f"  ✗ 分割失败 {Path(fp).name}: {e}", 'error')
        self._log(f"✂ 分割完成: {len(big_files)} 个文件 → {count} 个已分割", 'success')


if __name__ == '__main__':
    try:
        a = App(tk.Tk())
        a.r.protocol("WM_DELETE_WINDOW", a.on_close)
        a.r.mainloop()
    except Exception as e:
        import traceback
        msg = f"程序启动失败:\n\n{traceback.format_exc()}"
        try:
            messagebox.showerror("启动错误", msg)
        except Exception:
            # 连 messagebox 都弹不了时，写日志文件
            log_path = Path(__file__).parent / 'error.log'
            log_path.write_text(msg, encoding='utf-8')
        raise
