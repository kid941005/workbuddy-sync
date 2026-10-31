# -*- coding: utf-8 -*-
"""通用工具：时间戳、文件摘要、体积格式化。

这里只放与程序状态无关的纯函数，任何模块都可以安全导入。
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

#: 备份 / 冲突文件后缀里使用的时间戳格式
STAMP_FMT = "%Y%m%d-%H%M%S"

# ─────────────────── Windows 超长路径 ───────────────────
# NTFS 支持 32767 字符，卡住的只是 Win32 API 的 260。没开 LongPathsEnabled 的
# 机器上，超过 259 的路径在 open()/copy() 里会报 ENOENT —— 看起来像「文件不存在」。
# 这个能力被 core 与 utils 同时需要，放在 utils 以免 core↔utils 循环导入。

_IS_WIN = sys.platform == "win32"
_EXT_PREFIX = "\\\\?\\"
MAX_PATH = 259

#: 短于此长度不必加前缀（加前缀本身有开销，且部分 API 不认）
_LP_THRESHOLD = 200


def long_path(p) -> str:
    """把接近上限的绝对路径转成 \\\\?\\ 扩展长度形式（非 Windows 原样返回）。"""
    s = os.fspath(p)
    if not _IS_WIN or s.startswith(_EXT_PREFIX):
        return s
    if len(s) < _LP_THRESHOLD:
        return s
    return _EXT_PREFIX + os.path.abspath(s)


def ts() -> str:
    """本地时间戳，形如 20261003-181942。"""
    return datetime.now().strftime(STAMP_FMT)


def now_iso() -> str:
    """带时区的 ISO 时间，写进包的 manifest，供对端判断新旧。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    """文件内容摘要，分块读取以免大文件吃内存。

    走 long_path()：深路径（工作区里 esp-idf / node_modules 那种）不这样做
    会在 open() 处 ENOENT，而 merge_tree 会把它当成"读不了就跳过"，
    结果是这些文件永远不参与「谁更新」的判断 —— 静默漏同步。
    """
    h = hashlib.sha256()
    with open(long_path(path), "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def human(n: int) -> str:
    """把字节数格式化成人读的形式。"""
    value = float(n)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}GB"
