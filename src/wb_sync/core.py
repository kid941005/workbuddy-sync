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
import hashlib
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
from . import packs
from . import paths
from . import secret_scan
from . import state
from . import utils
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
                if not p.is_file():
                    continue
                r = p.relative_to(base).as_posix()
                if is_excluded(r):
                    continue
                # 与 dirs 分支用同一个「单文件上限」。max_file_mb 在文档里的说法是
                # 「单文件超过 200MB 跳过」，不该因为文件来自白名单列表就网开一面 ——
                # 否则那条承诺对 ~/.workbuddy 下的命中文件是不成立的。
                try:
                    if p.stat().st_size > state.MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                        continue
                except OSError:
                    continue
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
    _makedirs(dst.parent)
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


def session_snapshot(src: Path, dst: Path, cids: set) -> None:
    """生成只含勾选会话的「精简数据库快照」。

    选择性同步勾选了会话时使用：包里的 db 只保留 sessions 表 + 勾选的那几行，
    其余表（workspaces / automations / …）一律不携带 —— 语义与「勾选什么就同步
    什么」一致，也让选择性包的体积最小。pull 端的 merge_db 只对存在且同列的表
    做 INSERT OR IGNORE，精简快照里没有的表会自动跳过。
    """
    _makedirs(dst.parent)
    if dst.exists():
        dst.unlink()
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)  # 先整体复制（数据库通常几 MB），保证 sessions 表结构与索引完整
        d.execute("PRAGMA foreign_keys=OFF")
        if cids:
            ph = ",".join("?" for _ in cids)
            d.execute(f"DELETE FROM sessions WHERE id NOT IN ({ph})", list(cids))
        # 丢弃 sessions 以外的所有表（含 sqlite_sequence 等内部表）
        for (t,) in d.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sessions'"
        ).fetchall():
            d.execute(f'DROP TABLE IF EXISTS "{t}"')
        d.commit()
        d.execute("VACUUM")
    finally:
        d.close()
        s.close()


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


# ─────────────────── 跨设备路径重写（多设备一致性）───────────────────
#
# 问题：WorkBuddy 把绝对路径直接存进数据库 —— workspaces.path、sessions.cwd。
# 两台机器的用户名/盘符/空间根不同，合并时这些行因主键不同而"两份并存"：
# 同一逻辑空间变成两条记录，会话打开后找不到对应目录。
#
# 而对话正文的落位目录名由 cwd 编码而来（projects/<编码>/<cid>.jsonl），
# 编码规则在不同客户端版本间还不统一，于是同一会话出现两份正文。
# 这里统一在 pull 侧做重映射：以对方 manifest 记录的 home / workspace_root
# 为前缀基准，改写成本机口径，再按本机校准出的编码把正文搬到正确目录。

#: 带绝对路径、需要重写的表与列
_PATH_COLUMNS = (("sessions", "cwd"), ("workspaces", "path"))


def _newer(a, b) -> bool:
    """时间列比较：类型一致就直比，否则降级成字符串比（ISO 串可字典序比较）。"""
    if a is None:
        return False
    if b is None:
        return True
    try:
        return a > b if type(a) is type(b) else str(a) > str(b)
    except TypeError:
        return False


def _rewrite_attached_paths(conn: sqlite3.Connection, path_map: list) -> None:
    """把 inc 库里的绝对路径列改写成本机口径（逐行 Python 侧处理）。

    逐行而不是用 SQL 前缀替换：路径里可能含 CJK（用户名/空间名），
    SQLite 的 lower()/LIKE 对非 ASCII 大小写不可靠，这里用 normcase 比较。

    ★ 两类表要分开处理
      有 id 主键（sessions）：直接 UPDATE，改的列不参与主键，不会冲突。
      无 id、路径列自己就是主键（workspaces）时不能 UPDATE —— 改写后的值
      可能与 inc 里已有的「本机路径」行撞主键，SQLite 抛错被吞掉，脏行原样
      留下，随后 INSERT OR IGNORE 把它并进 main。结果就是同一个空间出现两条
      记录，其中一条指向别机，点开是空目录。改为「读整行 → 删旧 → 插新」。
    """
    for t, col in _PATH_COLUMNS:
        try:
            if not conn.execute(
                    "SELECT name FROM inc.sqlite_master WHERE type='table' AND name=?",
                    (t,)).fetchone():
                continue
            inc_cols = table_columns(conn, "inc", t)
            if col not in inc_cols:
                continue

            if "id" in inc_cols:
                rows = conn.execute(
                    f'SELECT "id", "{col}" FROM inc."{t}" WHERE "{col}" IS NOT NULL'
                ).fetchall()
                for k, v in rows:
                    new = paths.remap_with_map(v, path_map)
                    if new and new != v:
                        conn.execute(
                            f'UPDATE inc."{t}" SET "{col}"=? WHERE "id"=?', (new, k))
                continue

            cols = list(inc_cols)
            cl = ", ".join(f'"{c}"' for c in cols)
            holders = ", ".join("?" * len(cols))
            has_ts = "last_opened_at" in cols
            rows = conn.execute(
                f'SELECT {cl} FROM inc."{t}" WHERE "{col}" IS NOT NULL').fetchall()
            for row in rows:
                rec = dict(zip(cols, row))
                old = rec[col]
                new = paths.remap_with_map(old, path_map)
                if not new or new == old:
                    continue
                rec[col] = new
                conn.execute(f'DELETE FROM inc."{t}" WHERE "{col}"=?', (old,))
                cur = conn.execute(
                    f'INSERT OR IGNORE INTO inc."{t}" ({cl}) VALUES ({holders})',
                    tuple(rec[c] for c in cols))
                if cur.rowcount == 0 and has_ts:
                    # 目标行已存在：保留较新的打开时间，否则「最近打开」会倒退
                    got = conn.execute(
                        f'SELECT "last_opened_at" FROM inc."{t}" WHERE "{col}"=?',
                        (new,)).fetchone()
                    if got and _newer(rec.get("last_opened_at"), got[0]):
                        conn.execute(
                            f'UPDATE inc."{t}" SET "last_opened_at"=? WHERE "{col}"=?',
                            (rec.get("last_opened_at"), new))
        except sqlite3.Error:
            continue


def _peer_path_map(meta: dict) -> list:
    """由对方包 manifest 推出「对方根 -> 本机根」映射。

    空间根优先于用户主目录：前者更长更精确，能覆盖后者。
    """
    pairs = []
    peer_ws = meta.get("workspace_root")
    if peer_ws:
        pairs.append((peer_ws, state.WORKSPACE_ROOT))
    peer_home = meta.get("home")
    if peer_home:
        pairs.append((peer_home, Path.home()))
    return pairs


def _local_cwd_encoder() -> int:
    """用本机会话校准「cwd -> 目录名」编码变体。

    不假设客户端用哪种大小写规则：拿本机 sessions.cwd 与真实目录名对照，
    取命中率最高的变体。本机没有历史会话时退回默认变体（盘符小写）。
    """
    db = state.WB_HOME / "workbuddy.db"
    cwd_by_cid: dict = {}
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
            try:
                for cid, cwd in con.execute("SELECT id, cwd FROM sessions"):
                    if cid and cwd:
                        cwd_by_cid[cid] = cwd
            finally:
                con.close()
        except sqlite3.Error:
            pass
    variant, _hit, _total = paths.calibrate_encoder(
        state.WB_HOME / "projects", cwd_by_cid)
    return variant


def relocate_project_bodies(tmp: Path, mapping: list, dry: bool) -> tuple:
    """把包内 projects/<对方编码>/<cid>.jsonl 搬到本机正确的编码目录。

    返回 (搬迁数, 未能识别归属数)。未能识别（cwd 不在对方任何已知根下）的
    保持原样，交给常规合并流程，避免误搬。
    """
    src_db = tmp / "db" / "workbuddy.db"
    proj = tmp / "files" / "projects"
    if not src_db.is_file() or not proj.is_dir() or not mapping:
        return 0, 0

    body: dict = {}
    for p in proj.rglob("*.jsonl"):
        body.setdefault(p.stem, p)
    if not body:
        return 0, 0

    try:
        con = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            rows = list(con.execute("SELECT id, cwd FROM sessions"))
        finally:
            con.close()
    except sqlite3.Error:
        return 0, 0

    variant = _local_cwd_encoder()
    moved = unrouted = 0
    for cid, cwd in rows:
        if not cid or not cwd:
            continue
        src = body.get(cid)
        if src is None:
            continue
        local_cwd = paths.remap_with_map(cwd, mapping)
        if not local_cwd:
            unrouted += 1
            continue
        want = proj / paths.encode_cwd(local_cwd, variant) / src.name
        if os.path.normcase(str(want)) == os.path.normcase(str(src)):
            continue
        moved += 1
        if dry:
            continue
        _makedirs(want.parent)
        if want.exists():
            # 目标已有同名正文：按行级并集合并，保持「一个会话一份正文」，
            # 避免同 cid 在两个目录下各存一份、后续越并越乱。
            if _union_too_large(src, want):
                # 两份都太大，不合并也**不能删 src** —— 删了就真丢一份正文了。
                print(f"  ⚠ 会话正文超过 {UNION_MAX_MB} MB，未合并：{src.name}"
                      f"（文件已保留，请手工处理）")
                continue
            try:
                rtxt = src.read_text(encoding="utf-8", errors="replace")
                ltxt = want.read_text(encoding="utf-8", errors="replace")
                merged = union_lines(rtxt, ltxt)
                if merged is not None:
                    _write_text(want, merged)
            except OSError:
                pass
            try:
                src.unlink()
            except OSError:
                pass
            continue
        try:
            shutil.move(_lp(src), _lp(want))
        except OSError:
            pass
    return moved, unrouted


def merge_db(target: Path, incoming: Path, path_map: list | None = None) -> dict:
    """把 incoming 数据库合并进 target。

    - sessions 表：只合并「本机 uid」的会话（同 uid 幂等、同 id 保留本机）。
      对方 uid 的会话由 copy_peer_sessions 复制成新 id，不在这里处理。
    - 其余表（workspaces / automations / …）：INSERT OR IGNORE 幂等合并。
    - path_map：[(对方根, 本机根), …]，用于把对方库里的绝对路径改写成
      本机口径。不做这一步时，同一逻辑空间会因绝对路径不同而变成两条记录。
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
        if path_map:
            _rewrite_attached_paths(conn, path_map)
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
                d = dict(zip(src_cols, row, strict=True))
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
                    _makedirs(dp.parent)
                    _write_text(dp, new_text)
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
    c = None
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        for t in state.DB_TABLES:
            try:
                out[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                pass
    except sqlite3.Error:
        pass
    finally:
        if c is not None:
            c.close()
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
                # 时间戳可能是数字，也可能是 ISO 字符串；不转换会抛 TypeError
                #（原实现只捕获 OSError/ValueError，一个异常文件就能让面板的
                #  /api/items 整体失败）。
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    ts = 0.0
                if ts > best_ts:
                    best_ts = ts
                    best_title = d.get("subject") or d.get("activeForm") or ""
            except (OSError, ValueError, AttributeError):
                continue
    except OSError:
        pass
    return best_title


def list_selectable() -> dict:
    """列出可勾选的同步条目：空间 + 任务 + 会话（对话）。

    会话直接读数据库 sessions 表（非软删），拿 id / 标题 / 更新时间 ——
    这是会话列表的权威来源，比扫 projects 目录更准（标题、软删状态都在库里）。
    """
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

    sessions = []
    db = state.WB_HOME / "workbuddy.db"
    if db.is_file():
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
            try:
                for cid, title, custom, created, upd, cwd in conn.execute(
                    "SELECT id, title, custom_title, created_at, updated_at, cwd "
                    "FROM sessions WHERE deleted_at IS NULL ORDER BY updated_at DESC"
                ):
                    name = (custom or title or "").strip() or "(未命名会话)"
                    # 所属空间 = cwd 的最后一段（~/WorkBuddy/<目录名>）
                    ws_name = ""
                    if cwd:
                        ws_name = cwd.replace("\\", "/").rstrip("/").split("/")[-1]
                    # 关联任务目录 = tasks/<会话id>/（目录名就是会话 id）
                    has_tasks = (tasks_dir / cid).is_dir()
                    sessions.append({
                        "id": cid, "name": name,
                        "created": created, "updated": upd,
                        "workspace": ws_name, "has_tasks": has_tasks,
                    })
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    return {"workspaces": workspaces, "tasks": tasks, "sessions": sessions}


def _pkg_meta(zip_path: Path) -> dict:
    """读上次包的 manifest，只解一个条目。

    用来判断「这次内容有没有变」，代价与包体积无关。
    """
    if not zip_path.is_file():
        return {}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if "manifest.json" not in zf.namelist():
                return {}
            return json.loads(zf.read("manifest.json").decode("utf-8")) or {}
    except (OSError, zipfile.BadZipFile, ValueError):
        return {}


def asset_fingerprint(entries: dict | None = None) -> str:
    """资产集合的轻量指纹：相对路径 + 大小 + mtime，**不读文件内容**。

    用途是回答「这次 push 会不会和上次产出一模一样的包」。
    用 sha256 读内容要多读几百 MB，而 size+mtime 已覆盖绝大多数写入
    （编辑器保存、SQLite 提交都会更新 mtime）；真出现「内容变但 mtime 不变」
    的极端情况，由 pull 侧的 zip CRC 兜底，不会丢数据。

    数据库必须计入：会话/任务都在 workbuddy.db 里，文件没变不等于没变化。

    传入 ``entries``（_collect_entries 的结果）时直接复用，省掉一次全量遍历。
    entries 的插入顺序就是「先 files/ 后 workspaces/」，与直接遍历一致，
    因此两种走法算出的指纹逐字节相同 —— 否则老包会被误判成「内容变了」。
    """
    h = hashlib.sha256()
    db = state.WB_HOME / "workbuddy.db"
    if db.is_file():
        try:
            st = db.stat()
            h.update(f"db|{st.st_size}|{st.st_mtime_ns}\n".encode())
        except OSError:
            pass

    # 先把 (包内路径, 大小, mtime) 收集齐，**排序后**再喂给哈希。
    # rglob/os.scandir 的返回顺序取决于文件系统，同一份内容在两次遍历里可能不同序 ——
    # 不排序的话指纹会随机抖动，把"完全没变"误判成"变了"，白白重传近 1 GB。
    # 两个分支（复用 entries / 直接遍历）也由此保证逐字节一致。
    rows = []
    if entries is not None:
        rows = [(k, v[1], v[2]) for k, v in entries.items()]
    else:
        for rel, p in iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES):
            try:
                st = p.stat()
            except OSError:
                continue
            rows.append(("files/" + rel, st.st_size, st.st_mtime_ns))
        if state.WORKSPACE_ROOT.is_dir():
            for rel, p in iter_assets(state.WORKSPACE_ROOT, ["."], None):
                try:
                    st = p.stat()
                except OSError:
                    continue
                rows.append(("workspaces/" + rel, st.st_size, st.st_mtime_ns))

    for key, size, mtime in sorted(rows):
        if key.startswith("workspaces/"):
            h.update(f"ws/{key[11:]}|{size}|{mtime}\n".encode())
        else:
            h.update(f"{key}|{size}|{mtime}\n".encode())
    return h.hexdigest()


def _collect_entries() -> dict:
    """当前同步范围的全部文件：{包内路径: (本机路径, 大小, mtime_ns)}。

    包内路径与 zip 条目完全一致（POSIX 分隔符、files/ 与 workspaces/ 两个前缀），
    这样索引里的 key 可以直接对上解包后的相对路径。
    """
    out: dict = {}
    for rel, p in iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES):
        try:
            st = p.stat()
        except OSError:
            continue
        out["files/" + rel.replace("\\", "/")] = (p, st.st_size, st.st_mtime_ns)
    if state.WORKSPACE_ROOT.is_dir():
        for rel, p in iter_assets(state.WORKSPACE_ROOT, ["."], None):
            try:
                st = p.stat()
            except OSError:
                continue
            out["workspaces/" + rel.replace("\\", "/")] = (p, st.st_size, st.st_mtime_ns)
    return out


def _incremental_blockers(state_dir: Path) -> list:
    """列出「引擎过旧、读不了增量」的对端。

    这是增量方案的安全阀：只发增量的前提是对端能把 base + delta 拼起来。
    版本低的对端只会读基线，看到的将是上次重建时的状态 —— 属于**静默的过期**，
    比慢一点严重得多，所以宁可整体退回全量。
    只读各包内的小成员 manifest.json，不解压正文。
    """
    bad = []
    for zp in state_dir.glob("from-*.zip"):
        if zp.stem == f"from-{state.HOST}":
            continue
        meta = _pkg_meta(zp)
        if not packs.engine_supports(meta.get("engine_version")):
            ver = meta.get("engine_version") or "未知"
            bad.append(f"{meta.get('machine_name') or zp.stem} · {ver}")
    return bad


def _write_index(state_dir: Path, entries: dict, base_seq: int, seq: int,
                 deltas: list, fingerprint: str, base_sig: str) -> None:
    """写索引：每个文件只记「大小 + mtime」，不读内容。

    完整性不靠这里 —— zip 每个条目自带 CRC32，解包时 zipfile 会校验，
    传输损坏在解包阶段就会抛 BadZipFile。再存一份 sha 属于重复劳动，
    而且首次建基线要把 1.3 GB 全读一遍，白白拖慢几分钟。

    fingerprint / base_sig 是这次补上的两个字段，各自堵一个坑：
    - fingerprint：增量模式下基线包不会被重写，基线 manifest 里的
      content_fingerprint 会一直停在建基线那一刻。只看那里的指纹，
      「内容没变」永远判定不成立 → 每次空 push 都全量重建。
    - base_sig：基线的大小+mtime。pull 时若对不上，说明上次重建基线后
      索引没写成，此时的增量是相对旧基线算的，叠加会把旧内容盖回去。
    """
    data = {
        "schema": packs.INDEX_SCHEMA,
        "host": state.HOST,
        "engine": ENGINE_VERSION,
        "base_seq": int(base_seq or 0),
        "seq": int(seq or 0),
        "fingerprint": fingerprint,
        "base_sig": base_sig,
        "deltas": deltas,
        "entries": {k: [v[1], v[2]] for k, v in entries.items()},
    }
    packs.save(state_dir, state.HOST, data)


def _drop_incremental_state(state_dir: Path) -> int:
    """作废索引与增量包，返回删除数量。

    选择性 push 会把 from-<host>.zip 换成「只有勾选内容」的不完整包。
    此时任何增量都是相对旧基线算出来的，必须一并清掉 —— 否则下一次全量
    push 会以为基线完整、只补差值，结果是一批文件永远传不过去。
    """
    n = 0
    try:
        packs.index_path(state_dir, state.HOST).unlink()
        n += 1
    except OSError:
        pass
    return n + _cleanup_deltas(state_dir)


def _cleanup_deltas(state_dir: Path) -> int:
    """基线重建后，旧增量就没有意义了 —— 只清本机的，别碰对端的。"""
    d = packs.delta_dir(state_dir)
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob(f"d-{state.HOST}-*.zip"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def _secret_gate(res: dict, fail_on_secret: bool, label: str = "") -> dict:
    """打印扫描结果，并判断是否应当中止本次 push。

    返回值就是给 manifest 用的精简结构，外加一个 blocked 标记。
    """
    summary = secret_scan.manifest_summary(res)
    lines = secret_scan.report_lines(res)
    if not lines:
        return {"summary": summary, "blocked": False}

    print(f"  ⚠ 敏感内容体检{('（' + label + '）') if label else ''}")
    for ln in lines:
        print("    " + ln)
    if fail_on_secret:
        print("    --fail-on-secret 已开启：本次不生成包。")
        print("    处理方式二选一：")
        print("      · 把凭据移出同步范围（清单里去掉对应 sync_files / sync_dirs）")
        print("      · 确需同步就在清单加 \"secret_allow\": [\"相对路径 glob\"]")
        return {"summary": summary, "blocked": True}
    print("    仅告警，包照常生成（严格模式加 --fail-on-secret）")
    return {"summary": summary, "blocked": False}


def _scan_staged(staging: Path, fail_on_secret: bool) -> dict:
    """对 staged 目录做内容级敏感扫描。

    ★ 为什么在 staging 上扫
        staging 里的字节就是「即将进包的那批」，全量 / 选择性 / 增量三条路径
        都先落到 staging 再打包，因此这一段代码天然覆盖全部入口，不会出现
        「全量能报、增量漏报」这种只在某条分支上成立的假承诺。

    ★ 为什么不读源文件
        源文件可能正被 WorkBuddy 改写（db、jsonl），读到的内容与入包内容
        未必一致；扫描必须针对「真正会被传出去的那份」。
    """
    if not state.SECRET_SCAN:
        return {"summary": {"count": 0, "files": [], "rules": []}, "blocked": False}
    return _secret_gate(secret_scan.scan_tree(staging, state.SECRET_ALLOW),
                        fail_on_secret)


def _build_delta(state_dir: Path, entries: dict, changed: list, idx: dict,
                 dry: bool, fingerprint: str, base_sig: str,
                 fail_on_secret: bool = False) -> dict:
    """只把「相对上次变化」的文件打成一个增量包。

    条目路径与基线包完全一致，pull 时直接叠加到解开的基线上即可 ——
    这样合并逻辑（merge_tree / 行级并集 / 冲突备份）一行都不用改。
    """
    seq = int((idx or {}).get("seq") or 0) + 1
    ddir = packs.delta_dir(state_dir)
    dz = ddir / packs.delta_name(state.HOST, seq)
    raw = sum(entries[k][1] for k in changed if k in entries)
    info = {"host": state.HOST, "zip": str(dz), "files": len(changed),
            "bytes": raw, "delta": True, "seq": seq}
    print(f"  增量模式：{len(changed)} 个文件有变化（未压缩 {human(raw)}）")
    if dry:
        # dry-run 不落 staging，但变化文件是明确的，直接按源文件扫 ——
        # 「预演」阶段就该看到密钥，而不是等真跑一次才发现。
        sec = {"count": 0, "files": [], "rules": []}
        if state.SECRET_SCAN:
            res = secret_scan.scan_items(
                [(k, entries[k][0]) for k in changed if k in entries],
                state.SECRET_ALLOW)
            gate = _secret_gate(res, fail_on_secret, "增量")
            sec, blocked = gate["summary"], gate["blocked"]
            if blocked:
                info.update(blocked=True, secrets=sec)
                return info
        info["secrets"] = sec
        print(f"  [dry-run] 将写入 state/deltas/{dz.name}，不重建基线")
        return info

    staging = Path(tempfile.mkdtemp(prefix="wb-delta-"))
    staged = 0
    try:
        for key in changed:
            rec = entries.get(key)
            if not rec:
                continue
            src = rec[0]
            dst = staging / key
            try:
                _makedirs(dst.parent)
                _copy_file(src, dst)
                staged += 1
            except OSError as e:
                print(f"  ⚠ 跳过 {key}：{e}")

        db_src = state.WB_HOME / "workbuddy.db"
        db_path = staging / "db" / "workbuddy.db"
        if db_src.is_file():
            db_snapshot(db_src, db_path)

        gate = _scan_staged(staging, fail_on_secret)
        if gate["blocked"]:
            info.update(blocked=True, secrets=gate["summary"])
            return info

        manifest = {
            "schema": MANIFEST_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "host": state.HOST,
            "machine_name": state.MACHINE_NAME,
            "os": sys.platform,
            "home": str(Path.home()),
            "workspace_root": str(state.WORKSPACE_ROOT),
            "wb_home": str(state.WB_HOME),
            "exported_at": now_iso(),
            # 增量包：只含变化文件。pull 侧据此判断「这是叠加层而非完整包」
            "incremental": True,
            "seq": seq,
            "packaged_files": staged,
            # 本增量里含多少处疑似凭据（只记规则名与路径，不含明文）
            "secrets": gate["summary"],
        }
        if db_path.is_file():
            manifest["db_rows"] = db_row_counts(db_path)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for junk in (staging / "db").glob("workbuddy.db-*"):
            try:
                junk.unlink()
            except OSError:
                pass

        ddir.mkdir(parents=True, exist_ok=True)
        tmp_zip = dz.with_suffix(".zip.part")
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in staging.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(staging).as_posix())
        tmp_zip.replace(dz)
        size = dz.stat().st_size
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    info.update(files=staged, bytes=size)
    deltas = list((idx or {}).get("deltas") or [])
    deltas.append({"seq": seq, "zip": dz.name, "files": staged, "bytes": int(size)})
    try:
        _write_index(state_dir, entries, int((idx or {}).get("base_seq") or 0),
                     seq, deltas, fingerprint, base_sig)
    except OSError as e:
        # 索引没写成不影响本次数据正确性：下次 push 会因为索引与基线对不上
        # 而重建基线。但不该静默 —— 用户得知道下次会多传一次整包。
        print(f"  ⚠ 索引写入失败（{e}）；下次 push 将重建全量基线")
    print(f"  已写入增量 state/deltas/{dz.name}（{human(size)}）")
    return info


def build_package(sync_root: Path, dry: bool, selection: dict | None = None,
                  force: bool = False, fail_on_secret: bool = False) -> dict:
    """把本机资产打包成 state/from-<标识>.zip。

    selection = {"workspaces": [...], "tasks": [...], "sessions": [...]}：非空时进入
    「选择性模式」，只打包勾选的空间/任务/会话。勾选会话时会同时携带会话正文
    （projects/**/<cid>.jsonl）与精简数据库快照（仅该会话的 sessions 行），
    其余白名单资产与数据库不打包（包小、传输快）；None 或空 = 全量（向后兼容）。
    """
    def _clean(vals) -> set:
        """过滤 selection 里的非法项。

        这些值来自面板或 `--selection` 的 JSON，属于外部输入。空串在
        `WORKSPACE_ROOT / ""` 下会退化成空间根本身 —— 一个空值就能把整棵
        空间树打包；带分隔符或 `..` 的项则会指向根之外。一律丢掉。
        """
        out = set()
        for x in vals or []:
            if not isinstance(x, str):
                continue
            s = x.strip()
            if not s or s in (".", "..") or "/" in s or "\\" in s:
                continue
            out.add(s)
        return out

    sel_ws = _clean((selection or {}).get("workspaces"))
    sel_tasks = _clean((selection or {}).get("tasks"))
    sel_sessions = _clean((selection or {}).get("sessions"))
    has_sel = bool(sel_ws or sel_tasks or sel_sessions)
    state_dir = sync_root / "state"
    zip_path = state_dir / f"from-{state.HOST}.zip"
    info = {"host": state.HOST, "zip": str(zip_path), "files": 0, "bytes": 0}

    # 内容没变就别重写：包是全量 zip，每天定时 push 若每次都重建，
    # 同步盘会把整个包（数百 MB）重新上传一遍 —— 空闲一天也是白传几次。
    # 指纹只比 size+mtime（不读内容），代价极低；--force 可强制重建。
    entries = idx = None
    fp = ""
    base_sig = pkg_sig(zip_path) if zip_path.is_file() else ""
    if not has_sel:
        entries = _collect_entries()
        fp = asset_fingerprint(entries)
        idx = packs.load(state_dir, state.HOST)
        base_meta = _pkg_meta(zip_path)

        # 指纹优先取索引里的。增量模式下基线包不会被重写，基线 manifest 里的
        # content_fingerprint 会一直停在「建基线那一刻」——只比那里的话，
        # 第一次增量之后每次空 push 都会被判成「有变化」而全量重建。
        prev_fp = (idx or {}).get("fingerprint") or base_meta.get("content_fingerprint")
        if not force and prev_fp and prev_fp == fp:
            size = zip_path.stat().st_size if zip_path.is_file() else 0
            info.update(files=len(entries), bytes=size, fingerprint=fp, skipped=True)
            if dry:
                print(f"  [dry-run] 内容与上次相同（指纹 {fp[:12]}），无需重写包")
            else:
                print(f"  内容与上次相同（指纹 {fp[:12]}），跳过重写，沿用现有包")
            return info

        base_size = zip_path.stat().st_size if zip_path.is_file() else 0
        blockers = _incremental_blockers(state_dir)
        rebuild, why = packs.decide_rebuild(idx, base_size, force, blockers)
        if base_meta.get("partial"):
            # 上一次是选择性包，基线里只有勾选的内容 —— 必须重建，不能拿它当全量
            rebuild, why = True, "当前基线是选择性包（内容不完整），需重建全量"
        changed = packs.diff(idx, {k: (v[1], v[2]) for k, v in entries.items()})
        if not rebuild and changed:
            return _build_delta(state_dir, entries, changed, idx, dry, fp, base_sig,
                                fail_on_secret=fail_on_secret)
        if rebuild:
            print(f"  重建全量基线：{why}")
            info["rebuild_reason"] = why
        elif not changed:
            # 指纹已经拦掉了「完全没变」，走到这里说明 db 之类的统计有抖动，
            # 保险起见仍按全量重建，不要产出空增量。
            print("  内容比对未发现变化，按全量重建")

    staging = Path(tempfile.mkdtemp(prefix="wb-pack-"))
    try:
        files_root = staging / "files"
        ws_root = staging / "workspaces"

        if has_sel:
            # ── 选择性模式：只打包勾选的空间/任务/会话，不含白名单资产（数据库仅会话行）──
            # 联动：勾选会话时，自动带上它所属的空间（sessions.cwd）与任务目录（tasks/<cid>/）。
            # 引擎层兜底，即使前端没联动勾选，也不会漏掉关联产出。
            if sel_sessions:
                db_src = state.WB_HOME / "workbuddy.db"
                if db_src.is_file():
                    try:
                        conn = sqlite3.connect(f"file:{db_src.as_posix()}?mode=ro", uri=True, timeout=30)
                        try:
                            for cid, cwd in conn.execute("SELECT id, cwd FROM sessions"):
                                if cid in sel_sessions and cwd:
                                    name = cwd.replace("\\", "/").rstrip("/").split("/")[-1]
                                    if name:
                                        sel_ws.add(name)
                        finally:
                            conn.close()
                    except sqlite3.Error:
                        pass
                tdir = state.WB_HOME / "tasks"
                for cid in sel_sessions:
                    if (tdir / cid).is_dir():
                        sel_tasks.add(cid)

            tasks_dir = state.WB_HOME / "tasks"
            if sel_tasks and tasks_dir.is_dir():
                for tid in sel_tasks:
                    tp = tasks_dir / tid
                    if not tp.is_dir():
                        continue
                    for rel, p in iter_assets(tp, ["."], None):
                        dst = files_root / "tasks" / tid / rel
                        _makedirs(dst.parent)
                        _copy_file(p, dst)
                        info["files"] += 1
            if sel_ws:
                for ws in sel_ws:
                    wp = state.WORKSPACE_ROOT / ws
                    if not wp.is_dir():
                        continue
                    for rel, p in iter_assets(wp, ["."], None):
                        dst = ws_root / ws / rel
                        _makedirs(dst.parent)
                        _copy_file(p, dst)
                        info["files"] += 1
            if sel_sessions:
                # 会话正文：projects/{ws编码}/{cid}.jsonl → files/projects/…（与全量同路径）
                proj_root = state.WB_HOME / "projects"
                if proj_root.is_dir():
                    body_index: dict = {}
                    for p in proj_root.rglob("*.jsonl"):
                        body_index.setdefault(p.stem, p)
                    for cid in sel_sessions:
                        sp = body_index.get(cid)
                        if sp is None:
                            continue
                        rel = sp.relative_to(proj_root)
                        dst = files_root / "projects" / rel
                        _makedirs(dst.parent)
                        _copy_file(sp, dst)
                        info["files"] += 1
                # 会话元数据：精简 db 快照（只含勾选会话的 sessions 行，其他表不携带）
                db_src = state.WB_HOME / "workbuddy.db"
                if db_src.is_file():
                    session_snapshot(db_src, staging / "db" / "workbuddy.db", sel_sessions)
        else:
            # ── 全量模式：白名单资产 + 全部空间 + 数据库 ──
            # 直接复用 entries：key 就是包内相对路径（files/… 与 workspaces/…），
            # 不必再遍历一次 iter_assets —— 那样等于把 22k 个目录项统计三遍。
            for key, (src, _size, _mt) in entries.items():
                dst = staging / key
                _makedirs(dst.parent)
                _copy_file(src, dst)
                info["files"] += 1
            db_src = state.WB_HOME / "workbuddy.db"
            if db_src.is_file():
                db_snapshot(db_src, staging / "db" / "workbuddy.db")

        # 全量 / 选择性两条路径都先落到 staging，扫描放在这里覆盖两者。
        gate = _scan_staged(staging, fail_on_secret)
        if gate["blocked"]:
            info.update(blocked=True, secrets=gate["summary"])
            return info

        db_path = staging / "db" / "workbuddy.db"
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "engine_version": ENGINE_VERSION,
            "host": state.HOST,
            "machine_name": state.MACHINE_NAME,
            "os": sys.platform,
            # 这两个根是 pull 侧重写对方绝对路径的唯一依据。
            # 旧版只记录 home，空间根换过盘符的机器无法正确归位。
            "home": str(Path.home()),
            "workspace_root": str(state.WORKSPACE_ROOT),
            "wb_home": str(state.WB_HOME),
            "exported_at": now_iso(),
            "db_rows": db_row_counts(db_path),
            "selective": has_sel,
            # 选择性包只含勾选内容，不是完整快照。打上标记，避免下一次
            # （或另一台机器）把它当成全量基线来算增量。
            "partial": has_sel,
            # 供下次 push 判断「内容是否变化」，避免无谓重写整包
            "content_fingerprint": fp,
            "packaged_files": info["files"],
            # 本包含多少处疑似凭据（只有规则名与路径，值已在扫描阶段丢弃）
            "secrets": gate["summary"],
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
        if entries is not None:
            # 基线重建完成：索引重置为「当前状态 = 基线」，旧增量随之作废。
            # 顺序很重要 —— 必须在 zip 落地之后再写索引，否则索引可能指向
            # 一个还不存在的基线。
            seq = int((idx or {}).get("seq") or 0) + 1
            try:
                _write_index(state_dir, entries, seq, seq, [], fp,
                             pkg_sig(zip_path))
            except OSError as e:
                print(f"  ⚠ 索引写入失败（{e}）；下次 push 将再次重建基线")
            dropped = _cleanup_deltas(state_dir)
            if dropped:
                print(f"  已清理 {dropped} 个过期增量包")
            info["seq"] = seq
        else:
            # 选择性包：基线内容不完整，索引与增量全部作废，
            # 下次全量 push 会重建一份完整基线。
            dropped = _drop_incremental_state(state_dir)
            if dropped:
                print(f"  ▸ 选择性包已作废增量状态（{dropped} 项），"
                      f"下次全量 push 会重建基线")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return info


# ─────────────────── 超长路径（Windows MAX_PATH） ───────────────────
# NTFS 本身支持 32767 字符，卡住的只是 Win32 API 的 260。
# 没开 LongPathsEnabled 的机器上，只要「根目录 + 包内相对路径」超过 259，
# 解包就会在 open() 时报 ENOENT —— 看起来像"文件不存在"，其实是路径太长。

_WIN = sys.platform == "win32"
_MAX_PATH = utils.MAX_PATH
#: 实现在 utils（core 与 utils 都需要，放在这里会循环导入）
_lp = utils.long_path


def _makedirs(p) -> None:
    """建目录；普通调用失败时用扩展长度路径再试一次。"""
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
    except OSError:
        if not _WIN:
            raise
        os.makedirs(_lp(p), exist_ok=True)


def _write_text(p, text: str) -> None:
    """写文本文件，与 _lp / _makedirs 一致地处理超长路径。

    直接 Path.write_text() 在接近 MAX_PATH 时同样会 ENOENT —— 与解包那个坑
    是同一个成因，所以文件写入必须一并走扩展长度前缀，否则「解包修好了、
    合并写回仍然失败」。
    """
    with open(_lp(p), "w", encoding="utf-8") as f:
        f.write(text)


def _copy_file(src, dst) -> None:
    """复制文件并保留元数据，超长路径安全。"""
    shutil.copy2(_lp(src), _lp(dst))


def _unpack_root(longest: int) -> Path:
    """挑一个足够短的临时根目录来解包。

    %TEMP% 本身就要占掉 50+ 字符（C:\\Users\\<用户>\\AppData\\Local\\Temp），
    碰上工作区里 esp-idf / node_modules 那种 200+ 字符的深路径，
    父目录还没建完就撞上 MAX_PATH 了。所以按包内最长相对路径反推根目录上限，
    优先用盘根下的短目录。"""
    if not _WIN:
        return Path(tempfile.gettempdir())

    drive = os.environ.get("SystemDrive") or "C:"
    roots = [Path(f"{drive}\\wbx"), Path(f"{drive}\\wbx-t"),
             Path(f"{drive}\\wbx-tmp"),
             Path(tempfile.gettempdir()) / "wbx"]
    fallback = None
    for root in roots:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if fallback is None:
            fallback = root
        # +1 是分隔符，+12 给 mkdtemp 的随机后缀留余量
        if len(str(root)) + 1 + longest + 12 <= _MAX_PATH:
            return root
    return fallback or Path(tempfile.gettempdir())


def _safe_target(base: Path, name: str) -> Path | None:
    """把包内条目名解析成 base 之下的绝对路径；越界的返回 None。

    条目名是**外部输入**：包放在共享目录里，任何能写同步盘的进程都能伪造一个
    `../../startup/x.bat` 或 `C:/Windows/x.dll`。不校验就等于把解包目录之外
    的位置开放给包的生产者（zip slip）。`zipfile.extractall()` 自带这层校验，
    我们为了长路径和 mtime 手写了解包，就必须自己补上。
    """
    if not name or name.startswith(("/", "\\")):
        return None
    if len(name) > 1 and name[1] == ":":          # C:xxx —— 驱动器相对路径
        return None
    try:
        dst = (base / name).resolve()
    except (OSError, RuntimeError):
        return None
    try:
        return dst if dst.is_relative_to(base) else None
    except ValueError:
        return None


def _extract_into(z: zipfile.ZipFile, dest: Path) -> None:
    """把已打开的 zip 逐条目写进 dest（已存在的同名文件直接覆盖）。

    不用 extractall()：它无法逐文件做长路径兜底，也不还原 mtime。
    """
    base = dest.resolve()
    skipped = 0
    for zi in z.infolist():
        if zi.is_dir():
            continue
        dst = _safe_target(base, zi.filename)
        if dst is None:
            skipped += 1
            continue
        _makedirs(dst.parent)
        with z.open(zi) as src, open(_lp(dst), "wb") as out:
            shutil.copyfileobj(src, out, 1024 * 512)
        # zipfile 不还原 mtime（解压出来全是"现在"），会让「谁更新」的判断彻底失效、
        # 退化成"对方无条件覆盖本机" → 必须按包内记录的时间手动补回。
        try:
            t = datetime(*zi.date_time).timestamp()
            os.utime(_lp(dst), (t, t))
        except (OSError, ValueError):
            pass
    if skipped:
        print(f"  ⚠ 已跳过 {skipped} 个越界条目（包内路径指向解包目录之外，"
              f"可能被篡改；建议核对来源）")


def unpack(zip_path: Path) -> Path:
    """解包到临时目录。
    不用 extractall()：它无法在解包前挑短根目录，也没有逐文件的长路径兜底。
    这里先按包内最长路径选根，再逐个文件写入（必要时走 \\\\?\\ 前缀）。
    解包中途失败（坏包、磁盘满）时删掉半成品目录，不留几百 MB 垃圾。"""
    with zipfile.ZipFile(zip_path) as z:
        longest = max((len(i.filename) for i in z.infolist() if not i.is_dir()),
                      default=0)
        tmp = Path(tempfile.mkdtemp(prefix="wb-unpack-", dir=str(_unpack_root(longest))))
        try:
            _extract_into(z, tmp)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return tmp


def overlay_zip(zip_path: Path, dest: Path) -> None:
    """把增量包叠加到已解开的目录上：同路径覆盖，新路径新增。

    增量包只含变化过的文件，"覆盖"就是正确语义 —— 没出现的文件意味着
    对方那边没变，应当保留基线里的版本。
    """
    with zipfile.ZipFile(zip_path) as z:
        _extract_into(z, dest)


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


#: 行级并集要把「对方全文 + 本机全文」同时读进内存。会话正文与记忆日志正常在
#: 几 MB 以内，但没有上限保护时，一个被误放大的文件（例如把整段日志追加进同一个
#: jsonl）就能让 pull 在 workbuddy 目录里 OOM。超过这个体量就不再并，改走降级。
UNION_MAX_MB = 64


def _union_too_large(sp: Path, dp: Path) -> bool:
    """判断行级并集是否已超出安全体积（任一方的文件大小）。"""
    try:
        limit = UNION_MAX_MB * 1024 * 1024
        return max(sp.stat().st_size, dp.stat().st_size) > limit
    except OSError:
        return False


def merge_tree(src_root: Path, dst_root: Path, tag: str,
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
            # 先比大小再算哈希：同步范围里动辄有几百 MB 的产物文件，
            # 无条件对两边各读一遍做 sha256，会把 pull 的 IO 直接翻倍。
            # 大小不同的文件必然不同，能挡掉绝大多数比较。
            if p.stat().st_size == dp.stat().st_size and sha256(p) == sha256(dp):
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


# ─────────────────── 冲突台账 ───────────────────
# 冲突会把本机版本另存为 <name>.conflict-<host>-<stamp>，散落在各自的目录深处，
# 事后基本找不到。这里在本机配置目录记一份索引（**不进同步包**，避免误导其他机器），
# 供 status 汇总。备份文件被删除后该条自动不再计数，无需手工清理。

CONFLICT_LEDGER = "conflicts.json"
LEDGER_MAX = 200


def _ledger_path() -> Path:
    return config.CONFIG_DIR / CONFLICT_LEDGER


def record_conflicts(rows: list) -> None:
    """把本次冲突追加进本机台账。rows 为 dict 列表：{rel, backup, stamp}。"""
    if not rows:
        return
    p = _ledger_path()
    data = {}
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        data = {}
    items = list(data.get("items") or []) if isinstance(data, dict) else []
    items.extend(rows)
    payload = {"updated_at": now_iso(), "items": items[-LEDGER_MAX:]}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def conflict_summary(limit: int = 3) -> tuple:
    """返回 (仍待处理的冲突数, 最近若干条)。只统计备份文件还在的。"""
    p = _ledger_path()
    if not p.is_file():
        return 0, []
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return 0, []
    items = [r for r in (data.get("items") or []) if isinstance(r, dict)]
    alive = []
    for r in items:
        bak = r.get("backup")
        try:
            if bak and Path(bak).exists():
                alive.append(r)
        except OSError:
            continue
    return len(alive), alive[-limit:]


def apply_actions(actions: list, dry: bool, stamp: str) -> tuple:
    """返回 (写入, 本机较新保留, 行合并文件数, 补齐行数, 冲突数)。"""
    written = kept = merged = merged_lines = conflicts = 0
    ledger = []
    for kind, _tag, rel, sp, dp in actions:
        if kind == "本机较新·保留":
            kept += 1
            continue

        if kind == "行合并":
            if _union_too_large(sp, dp):
                # 超大追加日志：行级并集要把两份全文读进内存，有 OOM 风险。
                # 降级为「按 mtime 取整份 + 旧版本留 .bak」—— 数据不丢，
                # 只是可能少并进对方独有的几行，且必须让人知道。
                if sp.stat().st_mtime <= dp.stat().st_mtime:
                    kept += 1
                    continue
                kind = "更新"
                print(f"  ⚠ {rel} 超过 {UNION_MAX_MB} MB，跳过行级并集，"
                      f"改为整文件替换（本机旧版已留 .bak）")
            else:
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
                _makedirs(dp.parent)
                _copy_file(dp, dp.with_name(dp.name + f".bak-{stamp}"))
                _write_text(dp, merged_text)
                continue

        if dry:
            print(f"  [dry-run] {kind:<14} {rel}")
            continue
        _makedirs(dp.parent)
        suffix = (f".conflict-{state.HOST}-{stamp}" if kind == "冲突·更新"
                  else f".bak-{stamp}")
        # 单个文件写不进去（被占用 / 权限）不应让整次 pull 崩掉：
        # 报出来继续，其余文件照常合并。
        try:
            if dp.exists():
                _copy_file(dp, dp.with_name(dp.name + suffix))
            _copy_file(sp, dp)
        except OSError as e:
            print(f"  [warn] 写入失败 {rel}：{e}")
            continue
        written += 1
        if kind == "冲突·更新":
            conflicts += 1
            bak = dp.with_name(dp.name + suffix)
            ledger.append({"rel": rel, "backup": str(bak), "stamp": stamp})
            print(f"  ⚠ 冲突：{rel}\n"
                  f"     两边都改过 → 取 mtime 较新的对方版本，"
                  f"本机版本存为 {dp.name}{suffix}")
    record_conflicts(ledger)
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
        if _union_too_large(bak, orig):
            print(f"  [skip] {orig.name} 超过 {UNION_MAX_MB} MB，跳过自愈；"
                  f"需要的话请手工比对 {bak.name}")
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
        try:
            _copy_file(orig, orig.with_name(orig.name + f".bak-{stamp}"))
            _write_text(orig, merged_text)
        except OSError as e:
            print(f"  [warn] 自愈写入失败 {orig.name}: {e}")
            files -= 1
            lines -= gained
    return files, lines


def pkg_sig(zp: Path) -> str:
    """包的「身份指纹」：大小 + 修改时间。用来判断这个包上次是否已经拉过。

    包可能被同步盘在读取途中撤走（改名、回收站、断网盘），stat 失败时返回
    一个不可能的签名而不是抛异常 —— 调用方遍布 status/pull/doctor，
    不该因为一个消失的文件把整条命令打断。
    """
    try:
        st = zp.stat()
    except OSError:
        return "0-0"
    return f"{st.st_size}-{int(st.st_mtime)}"


class PeerPackageIncomplete(RuntimeError):
    """对端包还拼不完整（增量未同步到本机 / 压缩包损坏）。

    这种情况必须**跳过并让下次重试**，绝不能把基线当成全量数据合并进去 ——
    那样看起来成功、实际少了一批变化文件。
    """


def _peer_host(zp: Path) -> str:
    """``from-<host>.zip`` → ``<host>``"""
    name = zp.stem
    return name[5:] if name.startswith("from-") else name


def _effective_index(sync_state: Path, zp: Path) -> dict | None:
    """取对端索引，并确认它和基线包是「同一代」。

    基线重建完、索引却没写成（进程被杀／磁盘满）时，索引描述的仍是旧基线；
    此时叠加增量会把过期内容盖回新基线上。对不上就当作没有索引 ——
    只用基线（它本身是完整快照），宁可少传一次增量，不可把旧内容写回去。
    """
    idx = packs.load(sync_state, _peer_host(zp))
    if not idx:
        return None
    recorded = str(idx.get("base_sig") or "")
    if recorded and recorded != pkg_sig(zp):
        print(f"   ⚠ {zp.name} 的索引与基线代次不一致，本次只解基线、不叠加增量")
        return None
    return idx


def _peer_sig(sync_state: Path, zp: Path) -> str:
    """包身份 = 基线的大小-mtime + 索引里的 seq。

    只比基线是不够的：只产生增量时基线并不会变，签名不变就会被误判成
    「已拉取」而永远拉不到新数据。
    """
    idx = _effective_index(sync_state, zp)
    base = pkg_sig(zp)
    return f"{base}|{idx.get('seq')}" if idx else base


def open_peer_package(sync_state: Path, zp: Path) -> tuple:
    """解开对端包：基线 + 全部增量按索引顺序叠加。

    返回 (临时根, 索引, 已叠加的增量名单)。增量包或索引对不上、包本身读不出来
    （损坏 / 同步盘刚撤走 / 磁盘满）时统一抛 PeerPackageIncomplete，由调用方
    跳过本对端 —— 一个坏包不该让整次 pull 中断，后续对端还得处理。
    """
    try:
        idx = _effective_index(sync_state, zp)
        tmp = unpack(zp)
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        raise PeerPackageIncomplete(f"对端包无法解开：{e}") from e
    applied = []
    try:
        for rec in (idx or {}).get("deltas") or []:
            name = str((rec or {}).get("zip") or "")
            if not name:
                continue
            dz = packs.delta_dir(sync_state) / name
            if not dz.is_file():
                raise PeerPackageIncomplete(
                    f"增量包 {name} 尚未同步到本机，无法还原到第 {idx.get('seq')} 版")
            overlay_zip(dz, tmp)
            applied.append(name)
    except PeerPackageIncomplete:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise PeerPackageIncomplete(f"对端包无法解开：{e}") from e
    return tmp, idx, applied


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
    incomplete = 0

    marker = load_pull_marker(sync_root)
    marker_dirty = False
    pending_seen = False

    # 0) 自愈：先把历史上被整文件覆盖丢掉的日志行，从 .bak 里并回来（幂等）
    heal_files, heal_lines = heal_append_logs(dry, stamp)

    for zp in others:
        sig = _peer_sig(sync_state, zp)
        if not dry and not force and marker.get(zp.name) == sig:
            print(f"\n── {zp.name}  与上次拉取时相同，跳过 ──")
            skipped += 1
            continue
        print(f"\n── 处理 {zp.name}  ({human(zp.stat().st_size)}) ──")
        try:
            tmp, pidx, applied = open_peer_package(sync_state, zp)
        except PeerPackageIncomplete as e:
            print(f"   ⚠ {e}")
            print("    已跳过本对端，且**不标记为已拉取** —— 等同步盘传完再跑一次 pull")
            skipped += 1
            incomplete += 1
            continue
        pending_seen = True
        if applied:
            print(f"   叠加增量 {len(applied)} 个（{applied[0]} … {applied[-1]}）"
                  f" → 第 {pidx.get('seq')} 版")
        db_pending = False
        try:
            mf = tmp / "manifest.json"
            pkg_time = 0.0
            meta = {}
            if mf.is_file():
                meta = json.loads(mf.read_text(encoding="utf-8"))
                print(f"   来源 {meta.get('host')} · {meta.get('exported_at')}")
                _check_peer_version(meta)
                try:
                    pkg_time = datetime.fromisoformat(meta["exported_at"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    pkg_time = 0.0

            # 跨设备路径归一：对方所有绝对路径先改写成本机口径，再做任何合并。
            # 顺序很重要 —— 必须早于 copy_peer_sessions / merge_tree / merge_db，
            # 否则正文会落到对方的编码目录、数据库会插入对方路径的行。
            pmap = _peer_path_map(meta)
            if pmap:
                moved, unrouted = relocate_project_bodies(tmp, pmap, dry)
                if moved:
                    print(f"   会话正文归位 {moved} 个（对齐本机空间路径编码）")
                if unrouted:
                    print(f"   其中 {unrouted} 个会话的 cwd 不在对方已知根下，保持原位")

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
            acts = merge_tree(tmp / "files", state.WB_HOME, "文件", pkg_time,
                              skip_cids=peer_cids)
            ws_acts = merge_tree(tmp / "workspaces", state.WORKSPACE_ROOT,
                                 "空间", pkg_time)
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
                    db_pending = True
                    print("  [跳过] 未关闭 WorkBuddy，数据库合并已跳过")
                else:
                    stats = merge_db(target_db, src_db, path_map=pmap)
                    for t, v in stats.items():
                        # merge_db 对单表失败会返回 "skip(<原因>)" 而不是行数。
                        # 直接 += 会抛 TypeError，把整次 pull 打断在"数据库已提交"
                        # 之后 —— 数据没坏，但后面的对端全不处理了。
                        if isinstance(v, int):
                            print(f"  数据库 {t}: +{v} 行")
                            total_added += v
                        else:
                            print(f"  数据库 {t}: {v}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                # 短根目录（如 C:\wbx）用完就删，不留空壳；非空时 rmdir 会失败，忽略
                tmp.parent.rmdir()
            except OSError:
                pass

        if not dry and db_pending:
            # 数据库没合并就标记"已拉取"，下次会因签名相同直接跳过 → 数据库永远合不上。
            # 宁可下次重跑一遍（文件层合并是幂等的），也不留下这个静默的坑。
            print("  ↳ 数据库尚未合并，本次不标记为已拉取；"
                  "完全退出 WorkBuddy 后再跑一次 pull 即可补齐")
        elif not dry:
            marker[zp.name] = sig
            marker_dirty = True

    if not dry and marker_dirty:
        save_pull_marker(sync_root, marker)

    if not dry and not pending_seen:
        if incomplete:
            print(f"\n合计：{incomplete} 个对端包不完整（增量未同步到本机），本次未处理；"
                  f"另有 {skipped - incomplete} 个无需处理")
        else:
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
    wb_src, ws_src = state.SOURCE_SRC
    print(f"  WorkBuddy 目录   {state.WB_HOME}" + (f"   ← {wb_src}" if wb_src else ""))
    print(f"  空间目录         {state.WORKSPACE_ROOT}"
          + (f"   ← {ws_src}" if ws_src else "")
          + ("" if state.WORKSPACE_ROOT.is_dir() else "   ⚠ 不存在"))
    print(f"  可同步文件       {s['files']} 个 / {human(s['size'])}")
    print(f"  空间文件         {s['ws_files']} 个 / {human(s['ws_size'])}")
    print(f"  数据库           {human(s['db_size'])}")
    print(f"  数据库行数       {db_row_counts(state.WB_HOME / 'workbuddy.db')}")
    n_conf, latest = conflict_summary()
    if n_conf:
        print(f"  待处理冲突       {n_conf} 个")
        for r in latest:
            print(f"      {r.get('rel', '')[:70]}")
        print("     （本机版本已另存为同名 .conflict-* 文件，处理完删掉即可）")
    else:
        print("  待处理冲突       无")
    print(f"\n同步目录：{sync_root}" + (f"   ← {root_src}" if root_src else ""))
    sync_state = sync_root / "state"
    if not sync_state.is_dir():
        print("  （尚未创建）")
        return 0
    marker = load_pull_marker(sync_root)
    found = False
    for zp in sorted(sync_state.glob("from-*.zip")):
        idx = packs.load(sync_state, _peer_host(zp))
        if zp.stem == f"from-{state.HOST}":
            tag = "[本机]"
        elif marker.get(zp.name) == _peer_sig(sync_state, zp):
            tag = "[远端·已同步]"
        else:
            tag = "[远端·待拉取]"
        extra = ""
        if idx:
            extra = f"  +{len(idx.get('deltas') or [])} 增量 · seq {idx.get('seq')}"
        print(f"  {tag:<14} {zp.name:<28} "
              f"{human(zp.stat().st_size):>9}  "
              f"{datetime.fromtimestamp(zp.stat().st_mtime):%Y-%m-%d %H:%M}{extra}")
        found = True
    if not found:
        print("  （还没有任何机器的包）")
    else:
        print(f"  增量状态         {packs.describe(packs.load(sync_state, state.HOST))}")
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

    # 空间根是「源目录」的另一半，且可被客户端改到任意盘符。
    # 不存在时不是小问题：push 会打包一个空目录却报告成功。
    if state.WORKSPACE_ROOT.is_dir():
        print(f"  空间根         {state.WORKSPACE_ROOT}  ✓")
    else:
        problems.append(
            f"空间根不存在：{state.WORKSPACE_ROOT}"
            "（检查 app-config.json 的 defaultWorkspacePath，或 config.json 的 workspace_root）")

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

    # 注意：这里**不创建**同步目录。
    # 旧实现 doctor 会 mkdir，于是路径写错（盘符不存在 / 网盘改名）时自检也"通过"，
    # 掩盖了「两台机器其实没指向同一个目录」这一最致命的分叉原因。
    if sync_root.is_dir():
        try:
            # 探针文件名带随机后缀：固定名 .write-probe 若被上一次崩溃留下，
            # 这里会先覆盖再删掉 —— 等于替用户删了一个同名文件。
            probe = sync_root / f".write-probe-{uuid.uuid4().hex[:8]}"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(f"  同步目录可写   {sync_root}  ✓"
                  + (f"   ← {root_src}" if root_src else ""))
        except OSError as e:
            problems.append(f"同步目录不可写：{e}")
    else:
        problems.append(
            f"同步目录不存在：{sync_root}"
            + (f"（{root_src}）" if root_src else ""))

    if sync_root.is_dir():
        ok, cinfo = config.check_cluster(sync_root, create=False)
        if ok:
            print(f"  共享目录ID     {cinfo}")
        else:
            problems.append(cinfo)

    # 增量状态。索引和基线对不上本身会自愈（下次 push 重建基线），只提示；
    # 但索引引用的增量包真丢了，对端 pull 会一直报「尚未同步」，算问题。
    if sync_root.is_dir():
        sdir = sync_root / "state"
        idx = packs.load(sdir, state.HOST)
        if idx:
            print(f"  增量状态       {packs.describe(idx)}")
            base = sdir / f"from-{state.HOST}.zip"
            recorded = str(idx.get("base_sig") or "")
            if recorded and base.is_file() and recorded != pkg_sig(base):
                print("                 ▸ 索引与基线代次不一致，下次 push 会重建基线")
            lost = [str(d.get("zip")) for d in (idx.get("deltas") or [])
                    if not (packs.delta_dir(sdir) / str(d.get("zip"))).is_file()]
            if lost:
                problems.append(
                    f"缺少 {len(lost)} 个增量包（{', '.join(lost[:3])}）—— "
                    "对端会一直提示尚未同步；执行一次 push --force 重建基线即可")
        else:
            print("  增量状态       未启用（下次 push 会建立索引）")

    # 内容级敏感体检：路径过滤只看名字，看不进文件里 —— 而凭据恰恰常待在
    # 名字完全正常的文件里。这里只提示，不算问题（用户可能就是要同步它）。
    if state.SECRET_SCAN:
        pairs = [("files/" + rel, p) for rel, p in
                 iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES)]
        if state.WORKSPACE_ROOT.is_dir():
            pairs += [("workspaces/" + rel, p) for rel, p in
                      iter_assets(state.WORKSPACE_ROOT, ["."], None)]
        res = secret_scan.scan_items(pairs, state.SECRET_ALLOW)
        if res["count"]:
            print(f"  敏感内容体检   命中 {res['count']} 处 / {len(res['files'])} 个文件"
                  " —— 这些会随包同步到所有机器")
            for ln in secret_scan.report_lines(res, limit=5):
                print("      " + ln)
            print("      确需同步：在清单加 \"secret_allow\" 豁免该文件")
            print("      不该同步：从 manifest 的 sync_files / sync_dirs 里移除")
        else:
            print("  敏感内容体检   未发现凭据  ✓")

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
