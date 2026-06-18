#!/usr/bin/env python3
"""
CHM → Markdown Converter (GUI)
将 CHM 帮助文档一键转换为带层级编号的结构化 Markdown 文件。

灵感来源: chy5301/chm-to-markdown-converter
依赖安装: pip install -r requirements.txt
"""

import os, re, json, base64, subprocess, tempfile, shutil, threading, time, atexit
from pathlib import Path
from urllib.parse import unquote
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

    # 2. 修复图片路径指向 assets/images/
    def fix_image_path(m):
        alt_text = m.group(1) or ""
        img_path = m.group(2)
        # 跳过 data URI 和外部链接，不修改
        if img_path.startswith(('data:', 'http://', 'https://', 'assets/')):
            return m.group(0)
        img_filename = Path(img_path).name
        return f'![{alt_text}](assets/images/{img_filename})'

    markdown = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', fix_image_path, markdown)

    # 3. 移除HTML锚点链接
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

    # ── 折叠根级目录 (三层→两层): 根级 has_children 条目提升一级 ──
    if book_num is not None and toc_root and toc_root.children:
        first_child_title = _clean_path(toc_root.children[0].title)
        base_dir = Path(out_root) / f"{book_num:02d}_{first_child_title}"
        # 收集所有根级目录条目 (parent="", has_children=True)
        _root_dir_names = {e[1] for e in flat if e[0] == "" and e[4]}
        if _root_dir_names:
            new_flat = []
            for entry in flat:
                parent, fn, title, local, has_children, number = entry
                if parent == "" and has_children:
                    # 根级目录条目 → 变成文件直接放在 base_dir 下
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
            log(f"  📐 折叠根级目录: {len(_root_dir_names)} 个 → 输出到 {base_dir.name}/", 'info')
            flat = new_flat
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


def _copy_images(files_dict, assets_dir, log):
    """复制图片文件到assets/images/"""
    assets_dir.mkdir(parents=True, exist_ok=True)
    img_exts = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.svg'}
    count = 0
    for k, v in files_dict.items():
        ext = Path(k).suffix.lower()
        if ext in img_exts:
            try:
                target = assets_dir / Path(k).name
                if not target.exists():
                    target.write_bytes(v)
                    count += 1
            except Exception:
                pass
    if count > 0:
        log(f"  🖼  复制了 {count} 张图片到 assets/images/", 'info')
    return count


def _save_toc_outputs(toc_root, toc_parser, base_dir, log):
    """保存toc.json / TOC.md / file_mapping.json"""
    try:
        # toc.json
        toc_json = {"version": "1.0.0", "tree": toc_root.to_dict()}
        (base_dir / 'toc.json').write_text(
            json.dumps(toc_json, ensure_ascii=False, indent=2), encoding='utf-8')
        log("  📄 toc.json", 'info')

        # TOC.md
        toc_md_content = "# 文档目录\n\n本目录由CHM文件的目录树自动生成。\n\n"
        toc_md_content += _toc_to_markdown(toc_root)
        (base_dir / 'TOC.md').write_text(toc_md_content, encoding='utf-8')
        log("  📄 TOC.md", 'info')

        # file_mapping.json
        md_mapping = {}
        for html_path, title in toc_parser.file_mapping.items():
            md_path = re.sub(r'\.html?$', '.md', html_path)
            md_mapping[md_path] = title
        mapping_data = {"version": "1.0.0", "count": len(md_mapping), "mapping": md_mapping}
        (base_dir / 'file_mapping.json').write_text(
            json.dumps(mapping_data, ensure_ascii=False, indent=2), encoding='utf-8')
        log("  📄 file_mapping.json", 'info')
    except Exception as e:
        log(f"  ⚠ 目录输出错误: {e}", 'info')


def _toc_to_markdown(node, indent=0):
    """递归转换TOC树为Markdown（带层级数字编号）"""
    lines = []
    prefix = "    " * indent
    for child in node.children:
        num_prefix = f"{child.number}  " if child.number else ""
        if child.file_path:
            md_path = re.sub(r'\.html?$', '.md', child.file_path)
            lines.append(f"{prefix}- [{num_prefix}{child.title}]({md_path})")
        else:
            lines.append(f"{prefix}- {num_prefix}{child.title}")
        if child.children:
            lines.append(_toc_to_markdown(child, indent + 1))
    return '\n'.join(lines)


def _create_metadata(chm_path, base_dir, stats, log):
    """创建metadata.json"""
    metadata = {
        "version": "1.0.0",
        "source": {
            "file": chm_path.name,
            "size": chm_path.stat().st_size,
            "date": datetime.fromtimestamp(chm_path.stat().st_mtime).isoformat(),
        },
        "converted": {
            "date": datetime.now().isoformat(),
            "tool": "chm-to-markdown-converter",
            "version": "2.0.0",
        },
        "statistics": stats,
    }
    (base_dir / 'metadata.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    log("  📄 metadata.json", 'info')


def _create_readme(base_dir, name, stats, log):
    """创建README.md"""
    content = f"""# {name.upper()} 文档

本文档由CHM文件自动转换而成。

## 转换信息
- 转换时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
- 总文件数: {stats['total']}
- 转换成功: {stats['success']}
- 转换失败: {stats['failed']}
- 跳过文件: {stats['skipped']}
- 图片数量: {stats['images']}

## 文档结构
- `toc.json` — 结构化目录树
- `TOC.md` — 可读目录树
- `file_mapping.json` — 文件名→标题映射
- `metadata.json` — 转换统计
- `assets/images/` — 图片资源
- `*.md` — 转换后的Markdown文件

## 使用说明
1. 使用IDE的搜索功能可以直接搜索和查询文档内容
2. 所有图片资源保存在 `assets/images/` 目录下
3. 内部链接已转换为 `.md` 格式

## 注意事项
- 部分复杂的HTML格式可能在转换过程中丢失
- 建议查看原始CHM文件以确认关键信息
"""
    (base_dir / 'README.md').write_text(content, encoding='utf-8')
    log("  📄 README.md", 'info')


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


class App:
    def __init__(self, r):
        self.r = r
        r.title("CHM → Markdown Converter v2")
        r.geometry("760x720"); r.minsize(600, 540)
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
        Tooltip(mode_lbl, "选择输入方式：单文件、多文件或整个文件夹")
        self.m = tk.StringVar(value='single')
        rb1 = ttk.Radiobutton(top, text="单个文件", variable=self.m,
                              value='single', command=self._fm)
        rb1.pack(side='left', padx=(10, 5))
        Tooltip(rb1, "选择一个 .chm 文件进行转换")
        rb2 = ttk.Radiobutton(top, text="多个文件", variable=self.m,
                              value='multi', command=self._fm)
        rb2.pack(side='left', padx=5)
        Tooltip(rb2, "一次选择多个 .chm 文件批量转换")
        rb3 = ttk.Radiobutton(top, text="整个文件夹", variable=self.m,
                              value='folder', command=self._fm)
        rb3.pack(side='left')
        Tooltip(rb3, "选择文件夹，自动扫描其中所有 .chm 文件")

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
        ent = ttk.Entry(r1, textvariable=self.iv)
        ent.pack(side='left', fill='x', expand=True)
        Tooltip(ent, "所选 CHM 文件的路径\n也可直接粘贴路径到此处")
        btn_browse = ttk.Button(r1, text="浏览...", command=self._bi)
        btn_browse.pack(side='left', padx=(8, 0))
        Tooltip(btn_browse, "打开文件对话框选择 CHM 文件或文件夹")

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
        for tg, cl in [('success', '#4ec9b0'), ('error', '#f44747'), ('info', '#569cd6')]:
            self.log.tag_configure(tg, foreground=cl)

    def _fm(self):
        mode = self.m.get()
        labels = {'single': "CHM 文件：", 'multi': "CHM 文件：", 'folder': "CHM 文件夹："}
        self.il.config(text=labels.get(mode, "选择："))
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
        else:
            p = filedialog.askdirectory(title="选择包含 CHM 文件的文件夹")
            if p: self.iv.set(p); self._files = sorted(str(f) for f in Path(p).glob("*.chm"))

    def _bo(self):
        p = filedialog.askdirectory(title="选择输出目录")
        if p: self.ov.set(p)

    def _log(self, t, tag='info'):
        self.log.insert('end', t + '\n', tag); self.log.see('end')
        self.r.update_idletasks()

    def _start(self):
        op = self.ov.get().strip()
        if not op:
            op = str(Path(__file__).parent / 'md_output')

        tasks = []
        mode = self.m.get()
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

        # ── 多文件时询问是否整体统一编号 ──
        unified = False
        if len(tasks) > 1:
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
                self._log("CHM → Markdown 转换器 v2", 'info')
                self._log("基于 chm-to-markdown-converter (github.com/chy5301)", 'info')
                self._log("=" * 50, 'info')
                count = 0; total = len(tasks)
                self._log(f"待处理文件数: {total}", 'info')
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
                self._log(f"输出目录: {Path(op).resolve()}", 'info')
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
