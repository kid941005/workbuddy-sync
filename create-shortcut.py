# -*- coding: utf-8 -*-
"""在桌面创建「WorkBuddy 同步助手」快捷方式（指向同目录 wb-ui.cmd）。

纯标准库（ctypes 调 ShellLink COM）。双击或 python 运行即可。
"""
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

HRESULT = wintypes.LONG  # HRESULT 本质是 32 位有符号整数

HERE = Path(__file__).resolve().parent
TARGET = HERE / "wb-ui.cmd"


def _guid(d1, d2, d3, b4):
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]
    return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*b4))


CLSID_ShellLink = _guid(0x00021401, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IShellLinkW = _guid(0x000214F9, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
IID_IPersistFile = _guid(0x0000010B, 0, 0, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))


def create_shortcut(lnk_path: Path, target: Path):
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)

    shell_link = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(
        ctypes.byref(CLSID_ShellLink), None, 1,
        ctypes.byref(IID_IShellLinkW), ctypes.byref(shell_link))
    if hr != 0 or not shell_link:
        raise RuntimeError(f"CoCreateInstance 失败 HRESULT={hr}")

    vtable = ctypes.cast(shell_link, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents

    # IShellLinkW vtable：SetPath=20, SetWorkingDirectory=9, SetDescription=7
    def call(index, *args):
        return ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *(
            [type(a) if not isinstance(a, str) else wintypes.LPCWSTR for a in args]
        ))(vtable[index])(shell_link, *args)

    SetPath = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[20])
    SetWorkingDirectory = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[9])
    SetDescription = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.LPCWSTR)(vtable[7])

    SetPath(shell_link, str(target))
    SetWorkingDirectory(shell_link, str(HERE))
    SetDescription(shell_link, "WorkBuddy 同步助手")

    # 取 IPersistFile 接口
    persist = ctypes.c_void_p()
    ole32.CoCreateInstance  # noqa
    # QueryInterface 拿到 IPersistFile（vtable index 0）
    QueryInterface = ctypes.WINFUNCTYPE(
        HRESULT, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(vtable[0])
    qi_hr = QueryInterface(shell_link, ctypes.byref(IID_IPersistFile), ctypes.byref(persist))
    if qi_hr != 0 or not persist:
        raise RuntimeError(f"QueryInterface(IPersistFile) 失败 HRESULT={qi_hr}")

    # IPersistFile vtable：Save=6
    pvtable = ctypes.cast(persist, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    Save = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.LPCWSTR, wintypes.BOOL)(pvtable[6])
    save_hr = Save(persist, str(lnk_path), True)
    if save_hr != 0:
        raise RuntimeError(f"Save 失败 HRESULT={save_hr}")

    ole32.CoUninitialize()
    return lnk_path


def desktop_dir() -> Path:
    # 优先用已知文件夹 API，退回 USERPROFILE\Desktop
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf)
    p = Path(buf.value)
    return p if p.is_dir() else Path.home() / "Desktop"


if __name__ == "__main__":
    if not TARGET.is_file():
        print(f"[!] 找不到 {TARGET}")
        raise SystemExit(1)
    desk = desktop_dir()
    lnk = desk / "WorkBuddy 同步助手.lnk"
    try:
        create_shortcut(lnk, TARGET)
        print(f"✓ 已创建桌面快捷方式：{lnk}")
        print(f"  指向：{TARGET}")
    except Exception as e:  # noqa: BLE001
        print(f"[!] 创建失败：{e}")
        raise SystemExit(1)
