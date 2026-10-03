#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""源码 / 便携模式的入口垫片。

真正的实现在同目录的 src/wb_sync/ 包里；这里只负责把 src 挂到 sys.path，
然后把参数转交过去。这样两种部署方式都能用：

    · 便携模式（推荐给双机同步场景）
        目录里放 src/ + default-manifest.json，直接
            python wb_sync.py <action>
        好处：代码集中在一处（同步盘），改一处两台机器同时生效，不必每台都装。

    · pip 安装模式
        pip install workbuddy-sync
            wb-sync <action>

两者等价，选一个即可。
"""
import os
import sys
from pathlib import Path

# 不要在同步盘里生成 __pycache__：这些 .pyc 会在每台机器之间来回同步，
# 纯属噪音（而且平台无关的 .pyc 对端还会判为"有更新"白白处理）。
sys.dont_write_bytecode = True

_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

# 指明清单位置：本目录若有 default-manifest.json 就用它（可直接编辑），
# 否则包内那份兜底。用 setdefault 以便外部显式指定时优先生效。
if (_HERE / "default-manifest.json").is_file():
    os.environ.setdefault("WB_SYNC_MANIFEST_DIR", str(_HERE))

try:
    from wb_sync.cli import main
except ImportError as exc:  # pragma: no cover - 仅在目录结构损坏时触发
    print(f"[!] 找不到 wb_sync 包：{exc}")
    print(f"    期望位置：{_SRC / 'wb_sync'}")
    print("    可改用 pip 安装：pip install workbuddy-sync，然后执行 wb-sync")
    raise SystemExit(1)

if __name__ == "__main__":
    raise SystemExit(main())
