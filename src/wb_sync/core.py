# -*- coding: utf-8 -*-
"""同步内核：资产收集 / 数据库合并 / 打包 / 合并远端 / 状态与自检。

这一层与「机器是谁」「用户是谁」无关，是整个方案的护城河：

· 数据库合并用 INSERT OR IGNORE + 列交集 → 幂等、可重复执行、源端加列不炸
· 追加型日志（对话 jsonl、memory 的 md）走**行级并集**，不再整文件覆盖丢行
· 真冲突时对方版本胜出，本机版本另存 .conflict-<标识>-<时间戳>
· per-machine 包：每台机只写自己的 state/from-<标识>.zip，
  从根上避开同步盘的冲突副本问题

★ 所有可变状态都通过 state 模块读写（见 state.py 的说明）。
"""
from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import ENGINE_VERSION, MANIFEST_SCHEMA
from . import config
from . import state
from .platform_win import workbuddy_running
from .utils import human, now_iso, sha256, ts


# ─────────────────────────── 资产收集 ───────────────────────────

def _expand(base: Path, pattern: str) -> list:
    """把带通配符的条目展开成实际路径；无通配符时按原样返回（保持原有行为）。

    支持 glob 是为了覆盖「目录名随账号变化」的资源 —— 例如
    <账号ID>/sidebar-list-snapshot.json，写死 ID 没法通用。
    """
    if "*" in pattern or "?" in pattern:
        try:
            return sorted(base.glob(pattern))
        except (OSError, ValueError):
            return []
    return [base / pattern]


def is_excluded(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    parts = norm.split("/")
    if any(p in state.NEVER_SEGMENTS for p in parts):
        return True
    low = norm.lower()
    if low.endswith(state.NEVER_SUFFIX):
        return True
    if ".bak" in low or ".conflict-" in low or low.endswith("~"):
        return True
    if any(fnmatch.fnmatch(norm, g) for g in state.EXTRA_GLOBS):
        return True
    return any(w in low for w in state.SENSITIVE_WORDS)


def iter_assets(base: Path, dirs=None, files=None):
    """产出 (相对路径, 绝对路径)，按白名单收集。

    sync_files / sync_dirs 均支持 glob，例如 "*/sidebar-list-snapshot.json"。
    """
    if files:
        for rel in files:
            for p in _expand(base, rel):
                if p.is_file():
                    r = p.relative_to(base).as_posix()
                    if not is_excluded(r):
                        yield r, p
    if dirs:
        for top in dirs:
            for d in _expand(base, top):
                if not d.is_dir():
                    continue
                for p in d.rglob("*"):
                    if not p.is_file():
                        continue
                    rel = p.relative_to(base).as_posix()
                    if is_excluded(rel):
                        continue
                    try:
                        if p.stat().st_size > state.MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    yield rel, p


def asset_summary() -> dict:
    n = 0
    size = 0
    for _, p in iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES):
        n += 1
        try:
            size += p.stat().st_size
        except OSError:
            pass
    ws_n = ws_size = 0
    if state.WORKSPACE_ROOT.is_dir():
        for _, p in iter_assets(state.WORKSPACE_ROOT, ["."], None):
            ws_n += 1
            try:
                ws_size += p.stat().st_size
            except OSError:
                pass
    db = state.WB_HOME / "workbuddy.db"
    return {
        "files": n, "size": size,
        "ws_files": ws_n, "ws_size": ws_size,
        "db_size": db.stat().st_size if db.is_file() else 0,
    }


# ─────────────────────────── 数据库 ───────────────────────────

def db_snapshot(src: Path, dst: Path) -> None:
    """用 SQLite backup API 取一致性快照 —— 直接 copy 会拷到 WAL 未合并的脏状态。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    # 快照若残留 WAL，合并回主库
    for suf in ("-wal", "-shm"):
        leftover = Path(str(dst) + suf)
        if leftover.exists():
            leftover.unlink()


def table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list:
    try:
        cur = conn.execute(f"PRAGMA {schema}.table_info('{table}')")
        return [r[1] for r in cur.fetchall()]
    except sqlite3.Error:
        return []


# ─────────────────────────── 跨账号会话复制（B 方案）───────────────────────────

#: 副本 id 派生命名空间：语义固定为「workbuddy-sync 会话复制」。
#: 同一 (源uid, 源cid) 永远派生出同一新 id —— 幂等的关键，也是阶段 2
#: 「关联 + 增量同步」能从副本 id 反推源会话的前提。
COPY_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def derive_copy_id(src_uid: str, src_cid: str) -> str:
    """确定性派生副本 id：把对方账号的会话复制到本账号时使用。"""
    return str(uuid.uuid5(COPY_NAMESPACE, f"{src_uid}:{src_cid}"))


def _safe_sql(v) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def merge_db(target: Path, incoming: Path) -> dict:
    """把 incoming 数据库合并进 target。

    - sessions 表：只合并「本机 uid」的会话（同 uid 幂等、同 id 保留本机）。
      对方 uid 的会话由 copy_peer_sessions 复制成新 id，不在这里处理。
    - 其余表（workspaces / automations / …）：INSERT OR IGNORE 幂等合并。
    """
    if not incoming.is_file():
        return {}
    stats = {}
    conn = sqlite3.connect(str(target), timeout=30)
    try:
        local_uids = set()
        try:
            local_uids = {r[0] for r in conn.execute(
                "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL").fetchall()}
        except sqlite3.Error:
            pass

        conn.execute("ATTACH DATABASE ? AS inc", (str(incoming),))
        for t in state.DB_TABLES:
            try:
                if not conn.execute(
                    "SELECT name FROM inc.sqlite_master WHERE type='table' AND name=?",
                    (t,),
                ).fetchone():
                    continue
                inc_cols = table_columns(conn, "inc", t)
                main_cols = table_columns(conn, "main", t)
                if not main_cols:
                    continue
                cols = [c for c in main_cols if c in inc_cols]
                if not cols:
                    continue
                cl = ", ".join(f'"{c}"' for c in cols)

                where = ""
                if t == "sessions":
                    # 跨账号：只合并本机 uid 的会话；本机还没有会话时跳过
                    #（对方全是 peer，交给 copy_peer_sessions 复制）
                    if not local_uids:
                        continue
                    where = " WHERE user_id IN (" + ",".join(
                        _safe_sql(u) for u in sorted(local_uids)) + ")"

                before = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                conn.execute(
                    f'INSERT OR IGNORE INTO main."{t}" ({cl}) '
                    f'SELECT {cl} FROM inc."{t}"{where}'
                )
                after = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                added = after - before
                if added:
                    stats[t] = added
            except sqlite3.Error as e:
                stats[t] = f"skip({e})"
        conn.commit()
        conn.execute("DETACH DATABASE inc")
    finally:
        conn.close()
    return stats


def copy_peer_sessions(target_db: Path, src_db: Path, src_files: Path,
                       dst_home: Path, dry: bool) -> tuple:
    """B 方案：把对方账号的会话复制成本机账号的新会话（新 id）。

    - 只复制「对方 uid 的非软删会话」；本机 uid 的会话由 merge_db 同 uid 幂等合并。
    - 新 id = derive_copy_id(源uid, 源cid)：确定性、幂等、可从 id 反推源。
    - 数据库 INSERT OR REPLACE（id → 新 id、user_id → 本机 uid、deleted_at 清空，
      其余列保留源）；正文 jsonl 同步 replace(源cid → 新cid) 写成新文件名。

    返回 (对方 uid 会话的源 cid 集合【含软删】, 复制数量)。调用方据此在文件层
    合并时跳过这些源正文，避免把「对方账号的正文」当普通文件并进本机、留下孤儿 jsonl。
    """
    if not (target_db.is_file() and src_db.is_file()):
        return set(), 0

    conn = sqlite3.connect(str(target_db), timeout=30)
    try:
        local_uids = {r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL").fetchall()}
        tgt_cols = table_columns(conn, "main", "sessions")
    except sqlite3.Error:
        local_uids, tgt_cols = set(), []
    conn.close()

    if not local_uids:
        # 本机还没有任何会话（或尚未登录）—— 无法确定本机 uid，跳过复制
        return set(), 0
    local_uid = sorted(local_uids)[0]

    sconn = sqlite3.connect(str(src_db), timeout=30)
    try:
        src_cols = table_columns(sconn, "main", "sessions")
        src_uids = {r[0] for r in sconn.execute(
            "SELECT DISTINCT user_id FROM sessions WHERE user_id IS NOT NULL").fetchall()}
    except sqlite3.Error:
        src_uids, src_cols = set(), []
    peer_uids = src_uids - local_uids
    if not peer_uids:
        sconn.close()
        return set(), 0

    cols = [c for c in tgt_cols if c in src_cols]
    # 所有对方 uid 会话的 cid（含软删）：供文件层跳过正文，避免软删会话的
    # 正文被 merge_tree 当普通文件并进本机、留下没有会话记录的孤儿 jsonl。
    all_peer_cids: set = set()
    peers = []  # 只含非软删，用于复制
    for uid in peer_uids:
        try:
            for row in sconn.execute("SELECT * FROM sessions WHERE user_id=?", (uid,)):
                d = dict(zip(src_cols, row))
                cid = d.get("id")
                if cid:
                    all_peer_cids.add(cid)
                if d.get("deleted_at") is None:
                    peers.append((uid, d))
        except sqlite3.Error:
            pass
    sconn.close()
    if not peers:
        return all_peer_cids, 0

    # 正文索引：{cid: 源正文绝对路径}
    body_index = {}
    projects_root = src_files / "projects"
    if projects_root.is_dir():
        for p in projects_root.rglob("*.jsonl"):
            body_index[p.stem] = p

    copied = 0
    placeholders = ",".join("?" * len(cols))
    quoted_cols = ", ".join(f'"{c}"' for c in cols)
    ins_sql = (f"INSERT OR REPLACE INTO sessions "
               f"({quoted_cols}) VALUES ({placeholders})")

    tconn = sqlite3.connect(str(target_db), timeout=30)
    try:
        for uid, d in peers:
            src_cid = d.get("id")
            if not src_cid:
                continue
            new_cid = derive_copy_id(uid, src_cid)
            if dry:
                print(f"  [dry-run] 会话复制 {src_cid[:8]}… → {new_cid[:8]}…  "
                      f"（{d.get('title') or '(无标题)'}）")
                copied += 1
                continue
            vals = []
            for c in cols:
                if c == "id":
                    vals.append(new_cid)
                elif c == "user_id":
                    vals.append(local_uid)
                elif c == "deleted_at":
                    vals.append(None)
                else:
                    vals.append(d.get(c))
            tconn.execute(ins_sql, vals)
            # 正文：同一相对路径（projects/{ws}/…），只把文件名换成新 cid
            sp = body_index.get(src_cid)
            if sp is not None:
                rel = sp.relative_to(src_files)
                dp = dst_home / rel.parent / f"{new_cid}.jsonl"
                try:
                    text = sp.read_text(encoding="utf-8", errors="replace")
                    new_text = text.replace(src_cid, new_cid)
                    dp.parent.mkdir(parents=True, exist_ok=True)
                    dp.write_text(new_text, encoding="utf-8")
                except OSError as e:
                    print(f"  [warn] 正文复制失败 {rel}: {e}")
            copied += 1
        tconn.commit()
    finally:
        tconn.close()
    return all_peer_cids, copied


def db_row_counts(path: Path) -> dict:
    if not path.is_file():
        return {}
    out = {}
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        for t in state.DB_TABLES:
            try:
                out[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                pass
        c.close()
    except sqlite3.Error:
        pass
    return out


# ─────────────────────────── 打包 / 解包 ───────────────────────────

def _task_title(task_dir: Path) -> str:
    """读任务目录里 updatedAt 最新那条任务的 subject 作为可读标题。"""
    best_ts = -1
    best_title = ""
    try:
        for f in task_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                ts = d.get("updatedAt") or d.get("createdAt") or 0
                if ts > best_ts:
                    best_ts = ts
                    best_title = d.get("subject") or d.get("activeForm") or ""
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return best_title


def list_selectable() -> dict:
    """列出可勾选的同步条目：空间（~/WorkBuddy 下的目录）+ 任务（tasks 下的任务列表）。"""
    workspaces = []
    if state.WORKSPACE_ROOT.is_dir():
        for p in sorted(state.WORKSPACE_ROOT.iterdir()):
            if p.is_dir():
                workspaces.append({"name": p.name, "path": p.name})

    tasks = []
    tasks_dir = state.WB_HOME / "tasks"
    if tasks_dir.is_dir():
        for p in sorted(tasks_dir.iterdir()):
            if not p.is_dir():
                continue
            n = 0
            try:
                n = sum(1 for _ in p.glob("*.json"))
            except OSError:
                pass
            title = _task_title(p)
            tasks.append({"name": title or p.name, "path": p.name, "count": n})

    return {"workspaces": workspaces, "tasks": tasks}


def build_package(sync_root: Path, dry: bool, selection: dict | None = None) -> dict:
    """把本机资产打包成 state/from-<标识>.zip。

    selection = {"workspaces": [...], "tasks": [...]}：非空时只打包勾选的
    空间 / 任务；None 或空 = 全量（向后兼容）。
    """
    sel_ws = set((selection or {}).get("workspaces") or [])
    sel_tasks = set((selection or {}).get("tasks") or [])
    state_dir = sync_root / "state"
    zip_path = state_dir / f"from-{state.HOST}.zip"
    staging = Path(tempfile.mkdtemp(prefix="wb-pack-"))
    info = {"host": state.HOST, "zip": str(zip_path), "files": 0, "bytes": 0}

    try:
        files_root = staging / "files"
        # 1) 白名单资产（任务若被选择，则从目录级同步里摘出、单独处理）
        sync_dirs = [d for d in state.SYNC_DIRS if not (sel_tasks and d == "tasks")]
        for rel, p in iter_assets(state.WB_HOME, sync_dirs, state.SYNC_FILES):
            dst = files_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            info["files"] += 1

        # 2) 任务选择性：只打包勾选的任务列表目录
        tasks_dir = state.WB_HOME / "tasks"
        if sel_tasks and tasks_dir.is_dir():
            for tid in sel_tasks:
                tp = tasks_dir / tid
                if not tp.is_dir():
                    continue
                for rel, p in iter_assets(tp, ["."], None):
                    dst = files_root / "tasks" / tid / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    info["files"] += 1

        # 3) 空间选择性：只打包勾选的空间目录
        ws_root = staging / "workspaces"
        if state.WORKSPACE_ROOT.is_dir():
            if sel_ws:
                for ws in sel_ws:
                    wp = state.WORKSPACE_ROOT / ws
                    if not wp.is_dir():
                        continue
                    for rel, p in iter_assets(wp, ["."], None):
                        dst = ws_root / ws / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, dst)
                        info["files"] += 1
            else:
                for rel, p in iter_assets(state.WORKSPACE_ROOT, ["."], None):
                    dst = ws_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dst)
                    info["files"] += 1

        db_src = state.WB_HOME / "workbuddy.db"
        if db_src.is_file():
            db_snapshot(db_src, staging / "db" / "workbuddy.db")

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "host": state.HOST,
            "machine_name": state.MACHINE_NAME,
            "os": sys.platform,
            "home": str(Path.home()),
            "exported_at": now_iso(),
            "db_rows": db_row_counts(staging / "db" / "workbuddy.db"),
            "counts": {t: 1 for t in state.DB_TABLES},
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # db 快照会被上面的 db_row_counts() 重新打开，从而再生成 -shm / -wal
        # （运行时索引、不属于快照内容）。必须在打包前统一清掉，否则会混进包里。
        for junk in (staging / "db").glob("workbuddy.db-*"):
            try:
                junk.unlink()
            except OSError:
                pass

        if dry:
            total = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())
            info["bytes"] = total
            print(f"  [dry-run] 将打包 {info['files']} 个文件，未压缩约 {human(total)}")
            return info

        state_dir.mkdir(parents=True, exist_ok=True)
        tmp_zip = zip_path.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in staging.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(staging).as_posix())
        tmp_zip.replace(zip_path)
        info["bytes"] = zip_path.stat().st_size
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return info


def unpack(zip_path: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="wb-unpack-"))
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
        # zipfile 不还原 mtime（解压出来全是"现在"），会让「谁更新」的判断彻底失效、
        # 退化成"对方无条件覆盖本机" → 必须按包内记录的时间手动补回。
        for zi in z.infolist():
            if zi.is_dir():
                continue
            f = tmp / zi.filename
            if f.is_file():
                t = datetime(*zi.date_time).timestamp()
                os.utime(f, (t, t))
    return tmp


# ─────────────────────────── 合并远端 ───────────────────────────

def is_append_log(rel: str) -> bool:
    """追加型文本：对话记录（jsonl/ndjson）与记忆日志（*.md 且父目录里有一层叫 memory）。
    这类文件两边会各自往后追加，整文件覆盖必然丢行，只能做行级并集。"""
    if rel.endswith(config.UNION_SUFFIX):
        return True
    return rel.endswith(".md") and "memory" in rel.replace("\\", "/").split("/")[:-1]


def union_lines(remote_text: str, local_text: str) -> str | None:
    """行级并集：本机内容原样保留，只把对方独有的行补到末尾。
    用计数而不是集合做去重，同一文件内的重复行（空行、分隔线）不会被折叠。
    对方没有本机缺的行时返回 None，表示无需写入。"""
    local = local_text.splitlines()
    rest = Counter(local)
    merged = list(local)
    added = False
    for ln in remote_text.splitlines():
        if rest.get(ln, 0) > 0:
            rest[ln] -= 1          # 视为已存在（多数是公共前缀里的同一行）
            continue
        merged.append(ln)          # 本机没有 → 补进来
        added = True
    if not added:
        return None
    return "\n".join(merged) + "\n"


def merge_tree(src_root: Path, dst_root: Path, dry: bool, tag: str,
               pkg_time: float = 0.0, skip_cids: set | None = None) -> list:
    """合并目录树。追加型日志走行级并集；其余按 mtime：缺失则补，本机旧则更新，本机新则保留。
    非日志文件若在对方出快照之后本机也改过（两边都动过），标记为冲突而不是默默覆盖。

    skip_cids：跨账号会话复制时，对方账号的会话正文（projects/**/<cid>.jsonl）
    由 copy_peer_sessions 复制成新 id，这里跳过，避免并进本机留下孤儿 jsonl。"""
    if not src_root.is_dir():
        return []
    skip_cids = skip_cids or set()
    actions = []
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if is_excluded(rel):
            continue
        if skip_cids and rel.startswith("projects/") and p.stem in skip_cids:
            continue
        dp = dst_root / rel
        try:
            if not dp.exists():
                actions.append(("新增", tag, rel, p, dp))
                continue
            if sha256(p) == sha256(dp):
                continue
            if is_append_log(rel):
                actions.append(("行合并", tag, rel, p, dp))
            elif p.stat().st_mtime > dp.stat().st_mtime:
                both = bool(pkg_time) and dp.stat().st_mtime > pkg_time + config.CLOCK_GRACE
                actions.append(("冲突·更新" if both else "更新", tag, rel, p, dp))
            else:
                actions.append(("本机较新·保留", tag, rel, p, dp))
        except OSError:
            continue
    return actions


def apply_actions(actions: list, dry: bool, stamp: str) -> tuple:
    """返回 (写入, 本机较新保留, 行合并文件数, 补齐行数, 冲突数)。"""
    written = kept = merged = merged_lines = conflicts = 0
    for kind, _tag, rel, sp, dp in actions:
        if kind == "本机较新·保留":
            kept += 1
            continue

        if kind == "行合并":
            try:
                remote_text = sp.read_text(encoding="utf-8", errors="replace")
                local_text = dp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            merged_text = union_lines(remote_text, local_text)
            if merged_text is None:
                kept += 1
                continue
            gained = max(merged_text.count("\n") - local_text.count("\n"), 0)
            merged += 1
            merged_lines += gained
            if dry:
                print(f"  [dry-run] 行合并  {rel}  (+{gained} 行)")
                continue
            dp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dp, dp.with_name(dp.name + f".bak-{stamp}"))
            dp.write_text(merged_text, encoding="utf-8")
            continue

        if dry:
            print(f"  [dry-run] {kind:<14} {rel}")
            continue
        dp.parent.mkdir(parents=True, exist_ok=True)
        suffix = (f".conflict-{state.HOST}-{stamp}" if kind == "冲突·更新"
                  else f".bak-{stamp}")
        if dp.exists():
            shutil.copy2(dp, dp.with_name(dp.name + suffix))
        shutil.copy2(sp, dp)
        written += 1
        if kind == "冲突·更新":
            conflicts += 1
            print(f"  ⚠ 冲突：{rel}\n"
                  f"     两边都改过 → 取 mtime 较新的对方版本，"
                  f"本机版本存为 {dp.name}{suffix}")
    return written, kept, merged, merged_lines, conflicts


def heal_append_logs(dry: bool, stamp: str) -> tuple:
    """自愈：历史上被整文件覆盖过的追加型日志，用同目录的 .bak-* 把丢掉的行并回来。
    只扫真正会出现追加日志的三处（projects / memory / 各空间下的 .workbuddy/memory），
    不去遍历 binaries、logs 这类大目录。并集幂等，重复执行不会产生新内容。
    返回 (文件数, 找回行数)。"""
    targets = []
    for sub in ("projects", "memory"):
        root = state.WB_HOME / sub
        if root.is_dir():
            targets += [(state.WB_HOME, p) for p in root.rglob("*.bak-*")]
    if state.WORKSPACE_ROOT.is_dir():
        for ws in state.WORKSPACE_ROOT.iterdir():
            mem = ws / ".workbuddy" / "memory"
            if mem.is_dir():
                targets += [(state.WORKSPACE_ROOT, p) for p in mem.iterdir()
                            if ".bak-" in p.name]

    files = lines = 0
    for base, bak in targets:
        if not bak.is_file():
            continue
        orig = bak.with_name(bak.name.split(".bak-")[0])
        if not orig.is_file():
            continue
        if not is_append_log(orig.relative_to(base).as_posix()):
            continue
        try:
            old_text = bak.read_text(encoding="utf-8", errors="replace")
            cur_text = orig.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        merged_text = union_lines(old_text, cur_text)
        if merged_text is None:
            continue
        gained = max(merged_text.count("\n") - cur_text.count("\n"), 0)
        files += 1
        lines += gained
        print(f"  自愈 {orig.relative_to(base).as_posix()}: 从 .bak 找回 {gained} 行")
        if dry:
            continue
        shutil.copy2(orig, orig.with_name(orig.name + f".bak-{stamp}"))
        orig.write_text(merged_text, encoding="utf-8")
    return files, lines


def pkg_sig(zp: Path) -> str:
    """包的「身份指纹」：大小 + 修改时间。用来判断这个包上次是否已经拉过。"""
    st = zp.stat()
    return f"{st.st_size}-{int(st.st_mtime)}"


def pull_marker_path(sync_root: Path) -> Path:
    return sync_root / "state" / f".last-pull-{state.HOST}.json"


def load_pull_marker(sync_root: Path) -> dict:
    f = pull_marker_path(sync_root)
    if f.is_file():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def save_pull_marker(sync_root: Path, marker: dict) -> None:
    try:
        pull_marker_path(sync_root).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _check_peer_version(meta: dict) -> None:
    """两端引擎版本不一致时提示 —— 跨版本合并的语义可能有差异。"""
    peer = str(meta.get("engine_version") or "").strip()
    if peer and peer != ENGINE_VERSION:
        print(f"   ⚠ 对方引擎 {peer} · 本机 {ENGINE_VERSION} —— 版本不同，"
              f"建议两端升级到同一版本后再持续同步")

    schema = meta.get("schema")
    if schema is not None and schema != MANIFEST_SCHEMA:
        print(f"   ⚠ 对方包结构 v{schema} · 本机 v{MANIFEST_SCHEMA} —— "
              f"包格式不同，请联系维护者确认兼容性")


def do_pull(sync_root: Path, dry: bool, force: bool = False) -> int:
    sync_state = sync_root / "state"
    if not sync_state.is_dir():
        print(f"[!] 同步目录不存在：{sync_state}")
        print("    先在本机执行一次 push，或检查同步盘是否已就绪")
        return 1

    others = sorted(p for p in sync_state.glob("from-*.zip")
                    if p.stem != f"from-{state.HOST}")
    if not others:
        print("  没有发现其他机器的包（本机是第一个）")
        return 0

    running = workbuddy_running()
    allow_db = (not running) or force
    if running and force:
        print("[!] WorkBuddy 仍在运行，--force 已生效，即将写入数据库（有损坏风险）")

    stamp = ts()
    total_added = total_updated = total_kept = 0
    total_merged = total_merged_lines = 0
    total_conflicts = 0
    total_copied = 0
    skipped = 0

    marker = load_pull_marker(sync_root)
    marker_dirty = False
    pending_seen = False

    # 0) 自愈：先把历史上被整文件覆盖丢掉的日志行，从 .bak 里并回来（幂等）
    heal_files, heal_lines = heal_append_logs(dry, stamp)

    for zp in others:
        sig = pkg_sig(zp)
        if not dry and not force and marker.get(zp.name) == sig:
            print(f"\n── {zp.name}  与上次拉取时相同，跳过 ──")
            skipped += 1
            continue
        pending_seen = True
        print(f"\n── 处理 {zp.name}  ({human(zp.stat().st_size)}) ──")
        tmp = unpack(zp)
        try:
            mf = tmp / "manifest.json"
            pkg_time = 0.0
            if mf.is_file():
                meta = json.loads(mf.read_text(encoding="utf-8"))
                print(f"   来源 {meta.get('host')} · {meta.get('exported_at')}")
                _check_peer_version(meta)
                try:
                    pkg_time = datetime.fromisoformat(meta["exported_at"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    pkg_time = 0.0

            # 0) 跨账号会话复制（B 方案）：先算 peer_cids，供文件层跳过对方正文
            src_db = tmp / "db" / "workbuddy.db"
            target_db = state.WB_HOME / "workbuddy.db"
            peer_cids: set = set()
            if src_db.is_file() and target_db.is_file():
                if dry:
                    peer_cids, _ = copy_peer_sessions(
                        target_db, src_db, tmp / "files", state.WB_HOME, True)
                elif allow_db:
                    peer_cids, copied = copy_peer_sessions(
                        target_db, src_db, tmp / "files", state.WB_HOME, False)
                    if copied:
                        print(f"  会话复制 {copied} 个（跨账号 → 新 id）")
                        total_copied += copied

            # 1) 文件层（跳过对方账号的会话正文，避免留下孤儿 jsonl）
            acts = merge_tree(tmp / "files", state.WB_HOME, dry, "文件", pkg_time,
                              skip_cids=peer_cids)
            ws_acts = merge_tree(tmp / "workspaces", state.WORKSPACE_ROOT,
                                 dry, "空间", pkg_time)
            acts += ws_acts
            if acts:
                w, k, m, ml, cf = apply_actions(acts, dry, stamp)
                total_updated += w
                total_kept += k
                total_merged += m
                total_merged_lines += ml
                total_conflicts += cf

            # 2) 数据库层（本机 uid 会话 + 其他表；对方 uid 会话已在第 0 步复制）
            if src_db.is_file() and target_db.is_file():
                if dry:
                    before = db_row_counts(target_db)
                    after = db_row_counts(src_db)
                    for t in state.DB_TABLES:
                        if t in after and after[t] != before.get(t, 0):
                            print(f"  [dry-run] 数据库 {t}: 本机 {before.get(t,0)} 行 / 对方 {after[t]} 行")
                elif not allow_db:
                    print("  [跳过] 未关闭 WorkBuddy，数据库合并已跳过")
                else:
                    stats = merge_db(target_db, src_db)
                    for t, v in stats.items():
                        print(f"  数据库 {t}: +{v} 行")
                        total_added += v
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        if not dry:
            marker[zp.name] = sig
            marker_dirty = True

    if not dry and marker_dirty:
        save_pull_marker(sync_root, marker)

    if not dry and not pending_seen:
        print(f"\n合计：{skipped} 个远端包均与上次拉取时相同，无需处理"
              f"（要强制重跑加 --force）")
        return 0

    print(f"\n合计：数据库新增 {total_added} 行，文件写入 {total_updated} 个，"
          f"本机较新保留 {total_kept} 个")
    if total_copied:
        print(f"      跨账号会话复制 {total_copied} 个（新 id，云端归属分离）")
    if skipped:
        print(f"      另有 {skipped} 个包未变化，已跳过")
    if total_merged:
        print(f"      追加型日志行合并 {total_merged} 个，补齐 {total_merged_lines} 行")
    if heal_files:
        print(f"      从历史 .bak 自愈 {heal_files} 个文件，找回 {heal_lines} 行")
    if total_conflicts:
        print(f"      ⚠ {total_conflicts} 个文件两边都改过，本机版本已存为 "
              f".conflict-{state.HOST}-{stamp}")
    if not dry and (total_updated or total_merged or heal_files):
        print(f"      被覆盖的原文件均已备份为 .bak-{stamp}")
    return 0


# ─────────────────────────── 状态 / 自检 ───────────────────────────

def do_status(sync_root: Path, root_src: str = "") -> int:
    s = asset_summary()
    print(f"本机：{state.MACHINE_NAME}    标识 {state.HOST}    引擎 {ENGINE_VERSION}")
    print(f"  WorkBuddy 目录   {state.WB_HOME}")
    print(f"  空间目录         {state.WORKSPACE_ROOT}")
    print(f"  可同步文件       {s['files']} 个 / {human(s['size'])}")
    print(f"  空间文件         {s['ws_files']} 个 / {human(s['ws_size'])}")
    print(f"  数据库           {human(s['db_size'])}")
    print(f"  数据库行数       {db_row_counts(state.WB_HOME / 'workbuddy.db')}")
    print(f"\n同步目录：{sync_root}" + (f"   ← {root_src}" if root_src else ""))
    sync_state = sync_root / "state"
    if not sync_state.is_dir():
        print("  （尚未创建）")
        return 0
    marker = load_pull_marker(sync_root)
    found = False
    for zp in sorted(sync_state.glob("from-*.zip")):
        if zp.stem == f"from-{state.HOST}":
            tag = "[本机]"
        elif marker.get(zp.name) == pkg_sig(zp):
            tag = "[远端·已同步]"
        else:
            tag = "[远端·待拉取]"
        peer = ""
        print(f"  {tag:<14} {zp.name:<28} "
              f"{human(zp.stat().st_size):>9}  "
              f"{datetime.fromtimestamp(zp.stat().st_mtime):%Y-%m-%d %H:%M}{peer}")
        found = True
    if not found:
        print("  （还没有任何机器的包）")
    return 0


def detect_unknown_assets() -> list:
    """扫描 ~/.workbuddy 顶层，找出「真正会被漏掉」的目录。

    判定标准不是「目录名在白名单里吗」，而是「这里面到底有没有文件会被同步」——
    否则像 workspace/（只含 sessions/ 修改备份，而 sessions 在排除表里）
    这类目录会被误报成漏同步资产。

    这些大概率是客户端升级后新增的资产，也就是「静默漏同步」的根源。
    不报错，只提示，让用户自己决定要不要纳入。
    """
    # 判断依据是「白名单实际覆盖到了哪些顶层目录」（含 glob 命中的），
    # 而不是「目录名有没有写在清单里」—— 后者会把 glob 覆盖到的目录误报。
    covered = set()
    for rel, _p in iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES):
        covered.add(rel.split("/")[0])
    covered |= state.NEVER_SEGMENTS

    unknown = []
    if not state.WB_HOME.is_dir():
        return unknown
    try:
        entries = sorted(state.WB_HOME.iterdir())
    except OSError:
        return unknown

    for p in entries:
        try:
            if not p.is_dir() or p.name.startswith(".") or p.name in covered:
                continue
            # 只有存在「确实会被同步」的文件才算资产
            has_syncable = False
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                rel = f.relative_to(state.WB_HOME).as_posix()
                if is_excluded(rel):
                    continue
                try:
                    if f.stat().st_size > state.MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
                has_syncable = True
                break
        except OSError:
            continue
        if has_syncable:
            unknown.append(p.name)
    return unknown


def do_doctor(sync_root: Path, root_src: str = "", man_sources=None) -> int:
    problems = []
    print("── 环境自检 ──")
    print(f"  本机           {state.MACHINE_NAME}   标识 {state.HOST}")
    print(f"  引擎版本       {ENGINE_VERSION}")
    print(f"  Python         {sys.version.split()[0]}")
    print(f"  配置目录       {config.CONFIG_DIR}")
    if man_sources:
        shown = " < ".join(
            "内置默认" if s == "内置默认" else Path(s).name for s in man_sources)
        print(f"  清单来源       {shown}")

    if state.WB_HOME.is_dir():
        print(f"  WorkBuddy      {state.WB_HOME}  ✓")
    else:
        problems.append(f"WorkBuddy 目录不存在：{state.WB_HOME}")

    db = state.WB_HOME / "workbuddy.db"
    if db.is_file():
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            c.execute("SELECT count(*) FROM sessions").fetchone()
            c.close()
            print("  数据库可读     ✓")
        except sqlite3.Error as e:
            problems.append(f"数据库读取失败：{e}")
    else:
        problems.append("找不到 workbuddy.db")

    if workbuddy_running():
        print("  WorkBuddy 进程  正在运行 —— pull 前需要关闭")
    else:
        print("  WorkBuddy 进程  未运行  ✓")

    od = config.find_onedrive()
    print(f"  同步盘探测     {od if od else '未自动发现（已用 config.json 指定则无妨）'}")

    try:
        sync_root.mkdir(parents=True, exist_ok=True)
        probe = sync_root / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"  同步目录可写   {sync_root}  ✓"
              + (f"   ← {root_src}" if root_src else ""))
    except OSError as e:
        problems.append(f"同步目录不可写：{e}")

    unknown = detect_unknown_assets()
    if unknown:
        print(f"\n⚠ 有 {len(unknown)} 个目录不在同步白名单里：")
        for u in unknown:
            print(f"      {u}")
        print("  可能是客户端升级后新增的资产。需要一起同步的话，")
        print(f"  把它们加进 {config.CONFIG_DIR / 'manifest.json'} 的 \"sync_dirs\" 即可。")

    if problems:
        print("\n发现以下问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n全部检查通过 ✓")
    return 0
