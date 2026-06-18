# CHM to Markdown Converter (GUI)

> 一键将 CHM 帮助文档转换为结构化 Markdown 文件，保留完整目录树和层级编号。

[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](#)

---

## ✨ 功能特点

- **图形界面** — 拖拽式操作，支持单文件/多文件/文件夹批量转换
- **目录树还原** — 解析 CHM 的 `.hhc` 目录文件，生成完整嵌套目录结构
- **层级编号** — 自动分配 `1.2.7.5` 格式的数字编号，便于快速定位
- **合并 CHM 支持** — 自动处理 `merge` 引用，合并主子 CHM 到统一目录树
- **图片内嵌** — 将图片转为 base64 内嵌，单文件即开即用
- **多种提取策略** — 7-Zip → hh.exe → ITSF 纯 Python 解析，三重兜底
- **智能表格对齐** — 自动计算中文字符宽度，对齐 Markdown 表格
- **编码检测** — 自动识别 GBK/UTF-8 等编码，正确处理中文内容
- **进度日志** — 实时显示转换进度和彩色日志

## 📸 界面预览

```
┌─────────────────────────────────────────────────┐
│  CHM → Markdown 转换器                           │
│─────────────────────────────────────────────────│
│  📁 输入文件:  [浏览] [文件夹]  ✓ 7z  ✓ hh.exe  │
│  📂 输出目录:  [浏览]                            │
│─────────────────────────────────────────────────│
│  [▶ 开始转换]                                    │
│─────────────────────────────────────────────────│
│  处理: VBScript.chm                              │
│  [7-Zip] 正在提取...                    ✓ 完成   │
│  📦 处理合并子CHM: opc.chm → Scripting 运行时库  │
│  完成: VBScript.chm → 318 个 .md                 │
│  📄 toc.json  📄 TOC.md  📄 metadata.json         │
└─────────────────────────────────────────────────┘
```

## 🚀 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动程序

```bash
python chm_to_md_gui.py
```

或直接双击 `启动.bat`

### 基本使用

1. 点击 **浏览** 选择一个或多个 `.chm` 文件（或选择文件夹批量转换）
2. 选择输出目录
3. 点击 **开始转换**
4. 等待完成，在输出目录查看结果

## 📁 输出结构

```
输出目录/
└── 文档名/
    ├── README.md              ← 转换说明
    ├── TOC.md                 ← 带编号的完整目录树
    ├── toc.json               ← 结构化目录数据
    ├── metadata.json          ← 转换元信息
    ├── file_mapping.json      ← 文件名→标题映射
    ├── _index.md              ← 带编号的目录索引
    ├── assets/images/         ← 图片资源
    ├── 欢迎使用VBScript/
    │   ├── _index.md
    │   ├── VBScript 用户手册/
    │   │   ├── _index.md
    │   │   ├── 什么是 VBScript？.md    ← # 1.1.1  什么是 VBScript？
    │   │   ├── VBScript基础/
    │   │   │   ├── VBScript 数据类型.md  ← # 1.1.2.1  VBScript 数据类型
    │   │   │   └── ...
    │   │   └── ...
    │   └── VBScript 语言参考/
    │       ├── VBScript 函数/
    │       │   ├── Abs 函数.md           ← # 1.2.7.1  Abs 函数
    │       │   └── ...
    │       └── ...
    └── ...
```

## 📋 目录编号规则

每个 `.md` 文件标题带有层级数字编号，便于快速识别：

| 层级 | 格式 | 示例 | 说明 |
|---|---|---|---|
| 第1级 | `N` | `1  欢迎使用VBScript` | 顶层章节 |
| 第2级 | `N.N` | `1.1  VBScript 用户手册` | 二级目录 |
| 第3级 | `N.N.N` | `1.2.7  VBScript 函数` | 三级目录 |
| 第4级 | `N.N.N.N` | `1.2.7.5  CBool 函数` | 具体条目 |

## ⚙️ 提取策略

程序按以下优先级尝试提取 CHM 内容：

1. **7-Zip** (推荐) — 速度最快，支持最好
2. **hh.exe** — Windows 自带，无需额外安装
3. **ITSF 纯 Python** — 内置兜底，无任何外部依赖

## 📦 依赖

| 包 | 用途 |
|---|---|
| `beautifulsoup4>=4.12` | HTML 解析与清理 |
| `html2text>=2024.2` | HTML → Markdown 转换 |
| `pypinyin>=0.51` | 中文文件名转拼音 |
| `chardet>=5.2` | 编码自动检测 |
| `lxml>=5.1` | 快速 XML/HTML 解析（可选） |

> 可选：安装 [7-Zip](https://www.7-zip.org/) 获得最佳提取体验。

## 🛠️ 开发相关

### 调试模式

转换过程中会在 `<输出目录>/.debug/` 下保存调试文件（30分钟自动清理）：

- `_toc_flat.json` — 扁平化目录数据
- `_toc_map.json` — 路径映射统计
- `_convert_summary.json` — 转换摘要
- `_final_tree.txt` — 输出目录树快照

### 灵感来源

本项目基于 [chy5301/chm-to-markdown-converter](https://github.com/chy5301/chm-to-markdown-converter) 重写，新增 GUI 界面、合并 CHM 支持、层级编号、图片内嵌等功能。

## 📄 许可证

MIT License — 详见 [LICENSE](LICENSE)

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！如有问题或建议，请在 GitHub 上提出。
