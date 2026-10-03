# -*- coding: utf-8 -*-
"""通用工具：时间戳、文件摘要、体积格式化。

这里只放与程序状态无关的纯函数，任何模块都可以安全导入。
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

#: 备份 / 冲突文件后缀里使用的时间戳格式
STAMP_FMT = "%Y%m%d-%H%M%S"


def ts() -> str:
    """本地时间戳，形如 20261003-181942。"""
    return datetime.now().strftime(STAMP_FMT)


def now_iso() -> str:
    """带时区的 ISO 时间，写进包的 manifest，供对端判断新旧。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    """文件内容摘要，分块读取以免大文件吃内存。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def human(n: int) -> str:
    """把字节数格式化成人读的形式。"""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{value:.1f}GB"
