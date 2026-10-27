#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""清理 workspaces 表里的「跨机路径」与「已删目录」记录。

背景
----
v2.2.x 及更早的引擎在合并远端库时不做路径改写，别机的
`C:\\Users\\<别人>\\WorkBuddy\\<空间>` 会原样并进本机 workspaces 表。
这些记录在本机上点开是空目录 —— 条目在、内容不在，属于「看起来同步成功、
体验已经受损」。v2.3.0 起 merge_db 会改写路径，但**只对之后新合并的行生效**，
历史脏行不会自动修复，需要跑一次本脚本。

判定与处置
----------
对每一条「目录并不存在」的记录：

  改写后目标存在、且表内已有同一目标  -> 删脏行，打开时间取两者较大值
  改写后目标存在、表内没有            -> 原地改写成目标路径
  改写后目标仍不存在                  -> 删（死链）
  无法识别归属（本机路径但目录已删）    -> 只在 --purge-orphans 时删

映射来源
--------
1. `state/from-*.zip` 里各机 manifest 的 `home` / `workspace_root`
   （v2.1.1 及更早的包可能没有 workspace_root，则只按 home 推）；
2. `--map "对方根=本机根"`，可重复，用于 manifest 没记录的历史根
   （例如工作区根从 D 盘搬到 C 盘的那一段）。

用法
----
    python tools/repair_paths.py                       # 干跑，只打印计划
    python tools/repair_paths.py --apply               # 执行（自动备份）
    python tools/repair_paths.py --apply --purge-orphans
    python tools/repair_paths.py --apply --map "D:/WorkBuddy/workbuddy=C:/Users/me/WorkBuddy"

安全
----
- 默认只读；`--apply` 前用 `VACUUM INTO` 落一份备份，路径打印在输出里
- 全部改动在一个事务里，失败整体回滚
- 结束后跑 `PRAGMA integrity_check`
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


def norm(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def remap(value: str, mapping: list) -> str | None:
    """按映射表改写路径；长前缀优先，避免短根抢先匹配。"""
    if not value:
        return None
    v = str(value).replace("\\", "/")
    for peer, local in sorted(mapping, key=lambda kv: len(kv[0]), reverse=True):
        p = peer.replace("\\", "/").rstrip("/")
        if not p:
            continue
        if v.lower() == p.lower():
            return local.replace("\\", "/")
        if v.lower().startswith(p.lower() + "/"):
            tail = v[len(p) + 1:]
            return (Path(local) / Path(tail)).as_posix()
    return None


def collect_mapping(state_dir: Path, local_home: Path, local_ws: Path,
                    extra: list) -> list:
    """从各机 manifest 推映射，再把 extra 追加进去（去重）。"""
    mapping = []
    for z in sorted(state_dir.glob("from-*.zip")):
        try:
            with zipfile.ZipFile(z) as zf:
                names = [n for n in zf.namelist()
                         if n.endswith("manifest.json") and n.count("/") <= 1]
                if not names:
                    continue
                meta = json.loads(zf.read(names[0]).decode("utf-8"))
        except Exception:
            continue
        host = meta.get("host") or z.stem
        for key, target in (("workspace_root", local_ws), ("home", local_home)):
            peer = meta.get(key)
            if not peer:
                continue
            if norm(peer) in (norm(local_home), norm(local_ws)):
                continue                      # 就是本机，跳过
            mapping.append((peer, str(target)))
            print(f"  映射 {host:14s} {key:14s} {peer} -> {target}")
    for item in extra:
        if "=" not in item:
            raise SystemExit(f"--map 需要 对方根=本机根 形式，收到：{item}")
        peer, local = item.split("=", 1)
        mapping.append((peer.strip(), local.strip()))
        print(f"  映射 {'(手动)':14s} {'--map':14s} {peer} -> {local}")

    # 去重：同一对方根保留最后一个（手动优先）
    dedup = {}
    for peer, local in mapping:
        dedup[norm(peer)] = (peer, local)
    return list(dedup.values())


def scan(db: Path, mapping: list, local_home: Path, local_ws: Path,
         purge_orphans: bool) -> dict:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        rows = [(p, t) for p, t in
                con.execute("SELECT path, last_opened_at FROM workspaces")
                if p]
    finally:
        con.close()

    def is_local_path(v: str) -> bool:
        return norm(v).startswith(norm(local_home) + "/") or norm(v).startswith(norm(local_ws) + "/")

    plan = {"keep": 0, "rewrite": [], "drop_dup": [], "drop_dead": [],
            "orphan_local": [], "unmapped": [], "total": len(rows)}
    target_ts = {}
    for p, t in rows:
        target_ts.setdefault(norm(p), t)

    for p, t in rows:
        if os.path.isdir(p.replace("/", "\\")):
            plan["keep"] += 1
            continue
        new = remap(p, mapping)
        if new is None:
            (plan["orphan_local"] if is_local_path(p) else plan["unmapped"]).append((p, t))
            continue
        if os.path.isdir(new.replace("/", "\\")):
            if norm(new) in target_ts:
                plan["drop_dup"].append((p, new, t, target_ts[norm(new)]))
            else:
                plan["rewrite"].append((p, new, t))
                target_ts[norm(new)] = t
        else:
            plan["drop_dead"].append((p, new, t))
    plan["_purge"] = purge_orphans
    return plan


def apply_plan(db: Path, plan: dict, purge_orphans: bool) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db.with_name(f"workbuddy.db.bak-{stamp}")
    con = sqlite3.connect(str(db), timeout=30)
    try:
        con.execute("VACUUM INTO ?", (str(backup),))
        print(f"\n备份：{backup}  ({backup.stat().st_size/1048576:.1f} MB)")

        con.execute("BEGIN IMMEDIATE")
        n_rew = n_dup = n_dead = n_orph = 0
        for p, new, t in plan["rewrite"]:
            con.execute("UPDATE workspaces SET path=? WHERE path=?", (new, p))
            n_rew += 1
        for p, new, t, keep_ts in plan["drop_dup"]:
            con.execute("DELETE FROM workspaces WHERE path=?", (p,))
            if keep_ts is not None and t is not None:
                try:
                    newer = t > keep_ts if type(t) is type(keep_ts) else str(t) > str(keep_ts)
                except TypeError:
                    newer = False
                if newer:
                    con.execute("UPDATE workspaces SET last_opened_at=? WHERE path=?",
                                (t, new))
            n_dup += 1
        for p, new, t in plan["drop_dead"]:
            con.execute("DELETE FROM workspaces WHERE path=?", (p,))
            n_dead += 1
        if purge_orphans:
            for p, t in plan["orphan_local"]:
                con.execute("DELETE FROM workspaces WHERE path=?", (p,))
                n_orph += 1
        con.commit()
        print(f"改写 {n_rew} · 删重复 {n_dup} · 删死链 {n_dead} · 删本机孤儿 {n_orph}")
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        print("integrity_check:", con.execute("PRAGMA integrity_check").fetchone()[0])
        left = con.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
        bad = sum(1 for (p,) in con.execute("SELECT path FROM workspaces") if p
                  and not os.path.isdir(p.replace("/", "\\")))
        print(f"workspaces 现存 {left} 条，仍不可达 {bad} 条")
    finally:
        con.close()


def main() -> int:
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    ap = argparse.ArgumentParser(description="清理 workspaces 表的跨机路径与死链")
    ap.add_argument("--db", default=str(home / ".workbuddy" / "workbuddy.db"))
    ap.add_argument("--state", default=None, help="同步目录下的 state/，默认读 wb-sync 配置")
    ap.add_argument("--local-home", default=str(home))
    ap.add_argument("--local-ws", default=str(home / "WorkBuddy"))
    ap.add_argument("--map", action="append", default=[], metavar="对方根=本机根")
    ap.add_argument("--purge-orphans", action="store_true",
                    help="同时删除「本机路径但目录已删」的记录")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认干跑）")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.is_file():
        print(f"数据库不存在：{db}")
        return 1

    state_dir = args.state
    if not state_dir:
        cfg = home / "AppData" / "Local" / "wb-sync" / "config.json"
        try:
            root = json.loads(cfg.read_text(encoding="utf-8"))["sync_root"]
            state_dir = str(Path(root) / "state")
        except Exception:
            state_dir = None
    if not state_dir or not Path(state_dir).is_dir():
        print("找不到 state 目录（里面应有各机的 from-*.zip）。用 --state 指定。")
        return 1

    local_home, local_ws = Path(args.local_home), Path(args.local_ws)
    print(f"本机 home = {local_home}")
    print(f"本机 空间根 = {local_ws}")
    print(f"state 目录 = {state_dir}\n推导映射：")
    mapping = collect_mapping(Path(state_dir), local_home, local_ws, args.map)
    if not mapping:
        print("  （无）—— 那就只能识别「本机路径但目录已删」这一类")

    plan = scan(db, mapping, local_home, local_ws, args.purge_orphans)
    print(f"\nworkspaces 共 {plan['total']} 条")
    print(f"  可达，保留            : {plan['keep']}")
    print(f"  改写为目标路径        : {len(plan['rewrite'])}")
    print(f"  与已有记录重复，删    : {len(plan['drop_dup'])}")
    print(f"  映射后仍不存在，删    : {len(plan['drop_dead'])}")
    print(f"  本机路径已删目录      : {len(plan['orphan_local'])}"
          f"  {'（将删除）' if args.purge_orphans else '（保留，加 --purge-orphans 才删）'}")
    if plan["unmapped"]:
        print(f"  无法识别归属          : {len(plan['unmapped'])}")
        for p, _ in plan["unmapped"][:5]:
            print(f"      {p}")

    for label, key in (("改写", "rewrite"), ("删重复", "drop_dup"), ("删死链", "drop_dead")):
        if plan[key]:
            print(f"\n{label} 样例：")
            for item in plan[key][:4]:
                print(f"  {item[0][:60]}\n    -> {item[1][:60]}")

    if not args.apply:
        print("\n[dry-run] 未写入。确认无误后加 --apply。")
        return 0
    apply_plan(db, plan, args.purge_orphans)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
