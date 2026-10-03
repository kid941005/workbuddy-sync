# -*- coding: utf-8 -*-
"""Windows 平台相关能力。

本项目目前**只支持 Windows**（见项目 README 的说明）：进程检测依赖
tasklist / PowerShell，自启接管依赖注册表 Run 项。macOS / Linux 上
这些能力都需要另写一套实现。

设计原则：**判断不了就当作「WorkBuddy 正在运行」**。
    写数据库是有风险的操作（并发写会损坏 workbuddy.db），所以检测失败时
    宁可拒绝也不放行 —— 用户确认已退出时可以显式加 --force。
    早期版本这里是 fail-open（检测失败返回 False），意味着受限环境下会
    静默地边跑边写库，已修正。
"""
from __future__ import annotations

import subprocess
import sys

#: 是否运行在 Windows 上（其他平台进程检测不可用，会退化为「保守拒绝」）
IS_WINDOWS = sys.platform == "win32"

#: 进程名里出现这些关键字即认为 WorkBuddy 在运行
_PROCESS_HINTS = ("workbuddy", "codebuddy")


def _names_via_tasklist() -> list:
    """tasklist /FO CSV /NH → ['chrome.exe', 'python.exe', ...]（小写）"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return []
    if not out or not out.strip():
        return []
    return [line.split(",")[0].strip('"').lower()
            for line in out.splitlines() if line.strip()]


def _names_via_powershell() -> list:
    """tasklist 被拦时的退路。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Select-Object -ExpandProperty ProcessName"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    return [ln.strip().lower() for ln in out.splitlines() if ln.strip()]


def list_process_names() -> tuple:
    """返回 (进程名列表, 取得方式)。两种方式都失败时列表为空。"""
    names = _names_via_tasklist()
    if names:
        return names, "tasklist"
    names = _names_via_powershell()
    if names:
        return names, "powershell"
    return [], ""


def workbuddy_running() -> bool:
    """WorkBuddy / CodeBuddy 是否在运行（决定能不能写数据库）。

    取不到进程列表时返回 True —— 保守拒绝，详见模块开头说明。
    """
    names, _how = list_process_names()
    if not names:
        return True
    return any(hint in n for n in names for hint in _PROCESS_HINTS)
