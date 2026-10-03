# -*- coding: utf-8 -*-
"""便携模式的面板启动薄垫片。

真实实现在 src/wb_sync/ui.py；这里只把 src 挂到 sys.path 再转交，
好让双击 wb-ui.cmd / 开机自启 vbs 继续用同一个文件路径启动面板。
"""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wb_sync.ui import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
