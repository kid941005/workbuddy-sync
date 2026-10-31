#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查集群内各机的数据 / 资产 / 环境是否一致。

判定的核心事实
--------------
同步是**并集**语义，不是镜像：每台机器把别人的文件并进来，自己独有的保留。
所以「一致」不能定义成「完全相同」，否则永远不可能达成。可判定的定义是：

    C1 覆盖一致   每台机器的文件集合 = 全集群并集（无遗漏）
    C2 版本一致   所有机器的引擎版本相同
    C3 消费到位   各机已应用到最新增量序列
    C4 关键资产   身份 / 会话 / 空间 / 自动化 / 技能 / 凭据六层都在
    C5 路径可达   各机 workspaces 中指向本机不存在目录的条数为 0

本脚本读 `state/` 下的 `from-*.zip` 与 `from-*.index.json` 做前 4 项；
C5 需要在本机直接查库（`--db` 指定），不在本机的那几台只能读包、无法验。

用法
----
    python tools/consistency.py
    python tools/consistency.py --state "D:/sync/state" --json report.json
    python tools/consistency.py --local m-04a698d0 --db "%USERPROFILE%/.workbuddy/workbuddy.db"

退出码：0 全部通过；1 有不一致项。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

#: 六层关键资产：层名 -> 判定用的路径前缀或文件名
KEY_ASSETS = {
    "L1 身份": ("files/SOUL.md", "files/IDENTITY.md", "files/USER.md"),
    "L2 会话": ("db/workbuddy.db",),
    "L3 空间": ("workspaces/",),
    "L4 自动化": ("db/workbuddy.db",),
    "L5 技能": ("files/skills/",),
    "L6 凭据": ("files/cli-env/",),
}


def load_index(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def scan_machine(zpath: Path) -> dict:
    """从 zip 中央目录拿文件清单（不解压），并尽量读 manifest 与索引。"""
    host = zpath.stem.replace("from-", "")
    info = {"host": host, "zip": str(zpath), "bytes": zpath.stat().st_size,
            "mtime": datetime.fromtimestamp(zpath.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            "files": {}, "engine": None, "manifest_error": None, "index": None}
    try:
        with zipfile.ZipFile(zpath) as zf:
            for zi in zf.infolist():
                if not zi.is_dir():
                    info["files"][zi.filename] = zi.file_size
            names = [n for n in zf.namelist()
                     if n.endswith("manifest.json") and n.count("/") <= 1]
            if names:
                meta = json.loads(zf.read(names[0]).decode("utf-8"))
                info["engine"] = meta.get("engine_version")
                info["host_meta"] = meta.get("host")
                info["home"] = meta.get("home")
                info["workspace_root"] = meta.get("workspace_root")
                info["exported_at"] = meta.get("exported_at")
                info["partial"] = meta.get("partial")
    except Exception as e:
        info["manifest_error"] = str(e)

    hp = zpath.with_name(zpath.stem + ".health.json")
    if hp.is_file():
        try:
            info["health"] = json.loads(hp.read_text(encoding="utf-8"))
        except Exception:
            info["health"] = None

    idx_path = zpath.with_name(zpath.stem + ".index.json")
    if idx_path.is_file():
        idx = load_index(idx_path)
        info["index"] = {"seq": idx.get("seq"), "base_seq": idx.get("base_seq"),
                         "engine": idx.get("engine"), "entries": len(idx.get("entries") or {}),
                         "deltas": len(idx.get("deltas") or [])}
    return info


def local_host() -> str:
    p = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "wb-sync" / "machine-id"
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def check_db(db: Path) -> dict:
    out = {"path": str(db), "ok": False}
    if not db.is_file():
        out["error"] = "不存在"
        return out
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
        rows = {}
        for t in ("sessions", "workspaces", "automations"):
            try:
                rows[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                rows[t] = None
        bad = 0
        try:
            for (p,) in c.execute("SELECT path FROM workspaces"):
                if p and not os.path.isdir(str(p).replace("/", "\\")):
                    bad += 1
        except sqlite3.Error:
            pass
        out.update({"ok": True, "rows": rows, "unreachable": bad,
                    "integrity": c.execute("PRAGMA integrity_check").fetchone()[0]})
        c.close()
    except Exception as e:
        out["error"] = str(e)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="检查多机同步的一致性")
    ap.add_argument("--state", default=None, help="共享目录下的 state/，默认读 wb-sync 配置")
    ap.add_argument("--local", default=None, help="本机 host 标识，默认读 machine-id")
    ap.add_argument("--db", default=None, help="本机数据库，用于 C5 路径可达")
    ap.add_argument("--json", default=None, metavar="文件名", help="把完整结果写成 JSON")
    args = ap.parse_args()

    home = Path(os.environ.get("USERPROFILE") or Path.home())
    state = args.state
    if not state:
        cfg = home / "AppData" / "Local" / "wb-sync" / "config.json"
        try:
            state = str(Path(json.loads(cfg.read_text(encoding="utf-8"))["sync_root"]) / "state")
        except Exception:
            state = None
    if not state or not Path(state).is_dir():
        print("找不到 state 目录，用 --state 指定")
        return 1

    zips = sorted(Path(state).glob("from-*.zip"))
    if not zips:
        print("state 目录里没有 from-*.zip")
        return 1

    me = (args.local or local_host() or "").strip()
    machines = [scan_machine(z) for z in zips]
    union = set()
    for m in machines:
        union |= set(m["files"])
    total_bytes = max((sum(m["files"].values()) for m in machines), default=0)

    print("=" * 72)
    print(f"集群一致性检查    state = {state}")
    print(f"本机 = {me or '(未识别)'}    机器数 = {len(machines)}    并集文件 = {len(union)}")
    print("=" * 72)

    problems = []

    print("\n【各机摘要】")
    print(f"{'host':16s} {'引擎':8s} {'文件数':>8s} {'包大小':>10s} {'包时间':>17s} {'增量':>8s}  缺失")
    for m in machines:
        idx = m["index"] or {}
        seq = f"seq{idx.get('seq')}" if idx else "无索引"
        miss = len(union - set(m["files"]))
        tag = " ←本机" if m["host"] == me else ""
        print(f"{m['host']:16s} {str(m['engine']):8s} {len(m['files']):>8d} "
              f"{m['bytes']/1048576:>9.1f}M {m['mtime']:>17s} {seq:>8s} {miss:>5d}{tag}")

    # C2 版本一致
    print("\n【C2 引擎版本】")
    vers = {m["host"]: m["engine"] for m in machines}
    uniq = set(v for v in vers.values() if v)
    if len(uniq) == 1:
        print(f"  ✓ 全部 {list(uniq)[0]}")
    else:
        print(f"  ✗ 存在 {len(uniq)} 种版本：{sorted(uniq)}")
        for h, v in vers.items():
            mark = "" if v == max(uniq) else "  ← 落后"
            print(f"      {h:16s} {v}{mark}")
        problems.append(f"C2 引擎版本不一致：{sorted(uniq)}")

    # C1 覆盖一致
    print("\n【C1 覆盖一致（相对并集）】")
    for m in machines:
        miss = sorted(union - set(m["files"]))
        if not miss:
            print(f"  ✓ {m['host']:16s} 无缺失")
            continue
        # 分支目录缺失可能只是该机没有这类数据，按层判断
        layers = {}
        for x in miss:
            key = x.split("/")[0] if "/" in x else "(根)"
            layers[key] = layers.get(key, 0) + 1
        detail = "、".join(f"{k} {v}" for k, v in sorted(layers.items(), key=lambda kv: -kv[1])[:4])
        print(f"  · {m['host']:16s} 缺 {len(miss):>6d} 个：{detail}")
        if m["host"] == me:
            problems.append(f"C1 本机缺 {len(miss)} 个文件")
            for x in miss[:5]:
                print(f"        ! {x}")

    # C3 增量消费
    print("\n【C3 增量消费】")
    ddir = Path(state) / "deltas"
    have = sorted(ddir.glob("*.zip")) if ddir.is_dir() else []
    if not have:
        print("  · 无增量包，全部走基线")
    for m in machines:
        idx = m["index"]
        if not idx:
            print(f"  ! {m['host']:16s} 无 index.json —— 旧引擎，不参与增量")
            problems.append(f"C3 {m['host']} 无索引，不支持增量")
            continue
        latest = max([int(x) for x in (idx.get("seq"),) if x is not None] or [0])
        pending = [z.name for z in have if z.name.split("-")[-1].startswith(f"{latest+1:03d}")
                   or z.name.split("-")[-1].startswith(str(latest + 1))]
        state_txt = "已到最新" if not pending else f"待消费 {len(pending)} 个"
        print(f"  {'✓' if not pending else '!'} {m['host']:16s} seq={latest} {state_txt}")

    # C4 关键资产
    print("\n【C4 关键资产矩阵】")
    header = "  " + "层".ljust(12) + "".join(f"{m['host'][-6:]:>10s}" for m in machines)
    print(header)
    for layer, keys in KEY_ASSETS.items():
        line = "  " + layer.ljust(12)
        for m in machines:
            hit = any(k.endswith("/") and any(f.startswith(k) for f in m["files"])
                      or k in m["files"] for k in keys)
            line += f"{'✓' if hit else '—':>10s}"
            if not hit:
                problems.append(f"C4 {m['host']} 缺 {layer}")
        print(line)

    # C6 自报健康：只有在 push 时写过 health.json 的机器才有
    print("\n【C6 自报健康】（各机 push 时留下的快照）")
    _any_health = False
    for m in machines:
        h = m.get("health")
        if not h:
            continue
        _any_health = True
        rows = (h.get("db_rows") or {}).get("sessions")
        ws = f"{h.get('workspaces', 0)}({h.get('workspaces_unreachable', 0)})"
        tag = " ←本机" if m["host"] == me else ""
        print(f"  {m['host']:16s} 引擎{str(h.get('engine')):7s} 文件{h.get('files', 0):>7d} "
              f"会话{str(rows if rows is not None else '?'):>5s} 空间{ws:>9s} "
              f"未知目录{h.get('unknown_dirs', 0):>3d} 上次pull "
              f"{str(h.get('last_pull') or '从未')[:16]}{tag}")
        if h.get("workspaces_unreachable"):
            problems.append(
                f"C6 {m['host']} 有 {h['workspaces_unreachable']} 个空间不可达")
        if h.get("unknown_dirs"):
            print(f"      ▸ 该机有 {h['unknown_dirs']} 个目录不在同步白名单里")
    if not _any_health:
        print("  · 各机都还没有 health.json（旧引擎不会写）—— 升级后 push 一次即可")

    # C5 路径可达（只在有 --db 时）
    print("\n【C5 路径可达】")
    if args.db:
        dbi = check_db(Path(args.db))
        if dbi.get("ok"):
            print(f"  workspaces {dbi['rows']['workspaces']} 条，不可达 {dbi['unreachable']} 条，"
                  f"integrity={dbi['integrity']}")
            if dbi["unreachable"]:
                problems.append(f"C5 本机 {dbi['unreachable']} 条空间不可达")
        else:
            print(f"  ! 无法检查：{dbi.get('error')}")
    else:
        print("  · 未提供 --db，跳过（其他机器的可达性只能在该机本地验）")

    # 汇总
    print("\n" + "=" * 72)
    if problems:
        print(f"结论：{len(problems)} 项不一致")
        for p in problems:
            print(f"  - {p}")
    else:
        print("结论：全部一致 ✓")
    print("=" * 72)

    if args.json:
        Path(args.json).write_text(json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "state": state, "local": me, "union_files": len(union),
            "machines": [{k: v for k, v in m.items() if k != "files"} for m in machines],
            "files_per_machine": {m["host"]: len(m["files"]) for m in machines},
            "problems": problems,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.json}")

    return 0 if not problems else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
