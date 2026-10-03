# -*- coding: utf-8 -*-
"""新旧版本等价性对照。

用途：拆包 / 重构 / 改清单之后，证明「同步范围零变化」——
      在同一进程内、紧邻时刻分别用两个版本采集白名单结果集，然后 diff。

为什么要同进程紧邻采集：
    两次独立运行之间，正在运行的 WorkBuddy 会写出新文件，于是出现
    「3683 vs 3684」这类假差异。同进程紧邻采集能把这种环境抖动压到最低。

用法：
    python tools/parity_check.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

DEV = Path(__file__).resolve().parent.parent
LEGACY = DEV / "tools" / "legacy-singlefile-v1.py"
SRC = DEV / "src"
MANIFEST_DIR = DEV              # 两版都用 dev 根的 default-manifest.json

sys.path.insert(0, str(SRC))


def load_legacy():
    spec = importlib.util.spec_from_file_location("legacy_single", LEGACY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def collect(iter_assets, base, dirs, files):
    """{相对路径: 大小}；stat 失败的记 -1（并发写导致的瞬时不可读）。"""
    out = {}
    for rel, p in iter_assets(base, dirs, files):
        try:
            out[rel] = p.stat().st_size
        except OSError:
            out[rel] = -1
    return out


def main() -> int:
    print(f"对照基准（旧单文件版）：{LEGACY.name}")
    print(f"待验证（新包结构）：    {SRC / 'wb_sync'}")
    print(f"清单目录：              {MANIFEST_DIR}")
    print()

    # ── 旧版 ──
    legacy = load_legacy()
    lman, lsrc = legacy.load_manifest(MANIFEST_DIR)
    legacy.apply_manifest(lman)
    l_wb, l_ws = legacy.WB_HOME, legacy.WORKSPACE_ROOT

    old_files = collect(legacy.iter_assets, l_wb, legacy.SYNC_DIRS, legacy.SYNC_FILES)
    old_ws = collect(legacy.iter_assets, l_ws, ["."], None)

    # ── 新版（紧邻采集）──
    from wb_sync import config, core, state

    nman, nsrc = config.load_manifest(MANIFEST_DIR)
    config.apply_manifest(nman)
    n_wb, n_ws = state.WB_HOME, state.WORKSPACE_ROOT

    new_files = collect(core.iter_assets, n_wb, state.SYNC_DIRS, state.SYNC_FILES)
    new_ws = collect(core.iter_assets, n_ws, ["."], None)

    # ── 清单本身也要一致 ──
    print("── 生效清单 ──")
    keys = sorted(set(lman) | set(nman))
    man_ok = True
    for k in keys:
        lv, nv = lman.get(k), nman.get(k)
        if isinstance(lv, list) and isinstance(nv, list):
            same = sorted(map(str, lv)) == sorted(map(str, nv))
        else:
            same = lv == nv
        if not same:
            man_ok = False
            print(f"  ✗ {k}: 旧 {len(lv) if hasattr(lv,'__len__') else lv} "
                  f"/ 新 {len(nv) if hasattr(nv,'__len__') else nv}")
    print("  清单完全一致 ✓" if man_ok else "  清单存在差异 ✗")
    print(f"  （来源 旧：{' < '.join(lsrc)}）")
    print(f"  （来源 新：{' < '.join(nsrc)}）")
    print()

    # ── 文件集 ──
    ok = man_ok
    for label, old, new in (("WorkBuddy 资产", old_files, new_files),
                            ("空间文件", old_ws, new_ws)):
        only_old = sorted(set(old) - set(new))
        only_new = sorted(set(new) - set(old))
        size_diff = [k for k in set(old) & set(new) if old[k] != new[k]]
        old_bytes = sum(v for v in old.values() if v > 0)
        new_bytes = sum(v for v in new.values() if v > 0)

        print(f"── {label} ──")
        print(f"  旧：{len(old):>5} 个 / {old_bytes:>13,} B")
        print(f"  新：{len(new):>5} 个 / {new_bytes:>13,} B")
        if only_old:
            ok = False
            print(f"  ✗ 仅旧版有 {len(only_old)} 个：")
            for r in only_old[:10]:
                print(f"      {r}")
        if only_new:
            ok = False
            print(f"  ✗ 仅新版有 {len(only_new)} 个：")
            for r in only_new[:10]:
                print(f"      {r}")
        if size_diff:
            print(f"  ⚠ {len(size_diff)} 个文件大小不同（可能采集中文件正在写）：")
            for r in size_diff[:5]:
                print(f"      {r}  旧 {old[r]} → 新 {new[r]}")
        if not (only_old or only_new or size_diff):
            print("  完全一致 ✓")
        print()

    print("══ 结论 ══")
    print("  等价，拆包未改变同步范围 ✓" if ok else "  存在差异，需排查 ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
