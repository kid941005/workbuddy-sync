# -*- coding: utf-8 -*-
"""在启动文件夹创建开机自启快捷方式，指向同目录 wb-ui-autostart.vbs。

运行一次即可。此后开机登录会自动在后台无窗口启动同步面板。
"""
import importlib.util
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

# create-shortcut.py 文件名含连字符，无法用 import 语句，改用按路径加载
_spec = importlib.util.spec_from_file_location(
    "create_shortcut", HERE / "create-shortcut.py")
_cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cs)
create_shortcut = _cs.create_shortcut

TARGET = HERE / "wb-ui-autostart.vbs"

STARTUP = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
           / "Start Menu" / "Programs" / "Startup")
LNK = STARTUP / "WorkBuddy 同步助手.lnk"

if __name__ == "__main__":
    if not TARGET.is_file():
        print(f"[!] 找不到 {TARGET}")
        raise SystemExit(1)
    STARTUP.mkdir(parents=True, exist_ok=True)
    try:
        create_shortcut(LNK, TARGET)
        print(f"✓ 已创建开机自启快捷方式：{LNK}")
        print(f"  指向：{TARGET}")
        print(f"  效果：开机登录后自动在后台（无窗口）启动同步面板")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 创建失败：{e}")
        raise SystemExit(1)
