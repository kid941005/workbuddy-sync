# -*- coding: utf-8 -*-
"""同步内核：资产收集 / 数据库合并 / 打包 / 合并远端 / 状态与自检。

这一层与「机器是谁」「用户是谁」无关，是整个方案的护城河：

· 数据库合并用 INSERT OR IGNORE + 列交集 → 幂等、可重复执行、源端加列不炸
· 追加型日志（对话 jsonl、memory 的 md）走**行级并集**，不再整文件覆盖丢行
· 真冲突时对方版本胜出，本机版本另存 .conflict-<标识>-<时间戳>
· per-machine 包：每台机只写自己的 state/from-<标识>.zip，
  从根上避开同步盘的冲突副本问题

★ 所有可变状态都通过 state 模块读写（见 state.py 的说明）。

─────────────────────────── 分区导览 ───────────────────────────

文件按九个分区自上而下排列，箭头表示调用方向：

    1 资产收集        iter_assets → _collect_entries → asset_fingerprint
    2 数据库          db_snapshot / session_snapshot / merge_db
    3 跨账号会话复制   copy_peer_sessions（新 id，确定性派生，幂等）
    4 跨设备路径重写   _peer_path_map → _rewrite_attached_paths
                      → relocate_project_bodies（**必须早于任何合并**）
                      portable.localize_tree 也要在这里完成（见 5 的说明）
    5 打包 / 解包      build_package → _neutralize_staging → _build_delta
                      → _write_index
                      unpack / overlay_zip → _extract_into（含 zip slip 校验）
    6 超长路径         _lp / _unpack_root（Windows MAX_PATH 兜底）
    7 合并远端         merge_tree → apply_actions → union_lines
    8 冲突台账         record_conflicts / conflict_summary
    9 状态 / 自检      do_status / do_verify / do_doctor / do_guard

命令走哪条链：

    push            → 1 → 5
    pull            → 5(解包) → 4(路径本地化 + 归一) → 3(会话复制) → 7(文件) → 2(库)
    status / doctor → 1 + 2 + 只读索引
    verify          → 1 + 只读索引

改一处之前先确认自己在哪个分区：4 必须早于 2/7 的顺序是硬约束，
打乱会让正文落到对方的编码目录、库里写进对方路径。

路径有两层，别混淆：

    · 打包侧（5）把本机根换成 {{WB:WS}} 占位符 —— 治的是「包里的路径」
    · 拉取侧（4）把占位符换回本机根 —— 治的是「落到本机的路径」
  前者不依赖对方是谁，后者必须在任何合并之前完成。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from . import ENGINE_VERSION, MANIFEST_SCHEMA
from . import config
from . import packs
from . import paths
from . import portable
from . import secret_scan
from . import state
from . import utils
from .platform_win import workbuddy_running
from .utils import human, now_iso, parse_timestamp, sha256, ts


# ─────────────────────────── 资产收集 ───────────────────────────

#: 进度回调：(百分比 0–100, 阶段文案)。
#: CLI 下保持 None（零开销）；Web 面板执行时由 ui 注入，用来驱动进度条。
#: 做成模块级钩子而不是给 build_package 加参数，是因为面板走的是
#: cli.main(argv) 这条既有入口，加参数得一路穿透三层。
PROGRESS_HOOK = None
_PROGRESS_HOOK_LOCK = threading.Lock()


def _emit(pct: int, label: str = "") -> None:
    """上报进度。回调本身出错绝不能影响同步主流程。"""
    with _PROGRESS_HOOK_LOCK:
        hook = PROGRESS_HOOK
    if hook is None:
        return
    try:
        hook(max(0, min(100, int(pct))), label)
    except Exception:  # noqa: BLE001
        pass


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


def iter_assets(base: Path, dirs=None, files=None, failures: list | None = None):
    """产出 (相对路径, 绝对路径)，按白名单收集。

    sync_files / sync_dirs 均支持 glob，例如 "*/sidebar-list-snapshot.json"。
    传入 failures 时，枚举或 stat 失败会记录为 (路径, 原因)，调用方据此拒绝
    生成不完整基线；不传时保持旧的跳过不可读项行为。
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
                except OSError as e:
                    if failures is not None:
                        failures.append((p.as_posix(), str(e)))
                    continue
                yield r, p
    if dirs:
        for top in dirs:
            for d in _expand(base, top):
                if not d.is_dir():
                    continue
                try:
                    children = d.rglob("*")
                    iterator = iter(children)
                except OSError as e:
                    if failures is not None:
                        failures.append((d.as_posix(), str(e)))
                    continue
                while True:
                    try:
                        p = next(iterator)
                    except StopIteration:
                        break
                    except OSError as e:
                        if failures is not None:
                            failures.append((d.as_posix(), str(e)))
                        break
                    if not p.is_file():
                        continue
                    rel = p.relative_to(base).as_posix()
                    if is_excluded(rel):
                        continue
                    try:
                        if p.stat().st_size > state.MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                            continue
                    except OSError as e:
                        if failures is not None:
                            failures.append((p.as_posix(), str(e)))
                        continue
                    yield rel, p


def asset_summary() -> dict:
    n = 0
    size = 0
    scan_failures: list = []
    for _, p in iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES,
                            scan_failures):
        n += 1
        try:
            size += p.stat().st_size
        except OSError:
            pass
    ws_n = ws_size = 0
    if state.WORKSPACE_ROOT.is_dir():
        for _, p in iter_assets(state.WORKSPACE_ROOT, ["."], None,
                                scan_failures):
            ws_n += 1
            try:
                ws_size += p.stat().st_size
            except OSError:
                pass
    db = state.WB_HOME / "workbuddy.db"
    if scan_failures:
        print(f"  [warn] {len(scan_failures)} 个同步条目无法枚举或读取；"
              f"首个失败：{scan_failures[0][0]}")
    return {
        "files": n, "size": size,
        "ws_files": ws_n, "ws_size": ws_size,
        "db_size": db.stat().st_size if db.is_file() else 0,
    }


# ─────────────────────────── 数据库 ───────────────────────────

#: 写库前快照保留份数。一份 1.5 MB 左右，留 5 份足够回滚到任意一次合并之前。
DB_BACKUP_KEEP = 5


def backup_db_before_merge(db: Path) -> Path | None:
    """写库前留一份整库快照，返回快照路径；失败返回 None。

    为什么必须做：merge_db 是行级并集、正常幂等，但一旦碰上没预料到的表结构
    差异、磁盘异常或进程被杀，没有整库快照就只能靠散落的文件级 .bak 拼回来。
    快照失败时调用方应当**放弃本次 db 合并**（下次 pull 会重跑），
    而不是在没有退路的情况下改用户的库。
    """
    if not db.is_file():
        return None
    dst = db.with_name(f"{db.name}.bak-{ts()}")
    try:
        db_snapshot(db, dst)
    except (OSError, sqlite3.Error) as e:
        print(f"  ⚠ 写库前快照失败：{e}")
        try:
            dst.unlink()
        except OSError:
            pass
        return None
    _prune_db_backups(db, DB_BACKUP_KEEP)
    return dst


def _prune_db_backups(db: Path, keep: int) -> None:
    """只保留最近 keep 份 <db>.bak-*，避免每台机器攒出几十份快照。"""
    try:
        baks = sorted(db.parent.glob(db.name + ".bak-*"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in baks[max(keep, 1):]:
        try:
            old.unlink()
        except OSError:
            pass


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

#: 值「就是一个绝对路径」的列
_PATH_COLUMNS = (("sessions", "cwd"), ("workspaces", "path"),
                 ("automation_runs", "source_cwd"))

#: 值「是含路径的 JSON 文本」的列。路径埋在字符串中间，按前缀做文本替换，
#: 不解析 JSON —— 结构随客户端版本变，解析反而脆弱。
#: 这两列此前完全没被处理过，跨机后自动化记录与技能选择全部指向对方机器。
_PATH_JSON_COLUMNS = (("automation_runs", "runs_json"),
                      ("sessions", "addon_selection"))


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
                # strict=True：cl 与 cols 现在必然等长（同一份列表拼的），
                # 但将来若有人只改一处，静默截断会让整行字段错位 —— 宁可当场抛错。
                rec = dict(zip(cols, row, strict=True))
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

    # JSON 文本列：路径在字符串中间，按前缀替换即可，不碰结构
    for t, col in _PATH_JSON_COLUMNS:
        try:
            if not conn.execute(
                    "SELECT name FROM inc.sqlite_master WHERE type='table' AND name=?",
                    (t,)).fetchone():
                continue
            if col not in table_columns(conn, "inc", t):
                continue
            rows = conn.execute(
                f'SELECT rowid, "{col}" FROM inc."{t}" WHERE "{col}" IS NOT NULL'
            ).fetchall()
            for rid, v in rows:
                if not isinstance(v, str):
                    continue
                new = paths.remap_text(v, path_map)
                if new != v:
                    conn.execute(
                        f'UPDATE inc."{t}" SET "{col}"=? WHERE rowid=?', (new, rid))
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

    返回 (搬迁数, 未能识别归属数, 失败数)。未能识别（cwd 不在对方任何已知根下）的
    保持原样，交给常规合并流程，避免误搬。任何搬运或合并失败都计数，供 pull
    决定是否写成功标记。
    """
    src_db = tmp / "db" / "workbuddy.db"
    proj = tmp / "files" / "projects"
    if not src_db.is_file() or not proj.is_dir() or not mapping:
        return 0, 0, 0

    body: dict = {}
    for p in proj.rglob("*.jsonl"):
        body.setdefault(p.stem, p)
    if not body:
        return 0, 0, 0

    try:
        con = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            rows = list(con.execute("SELECT id, cwd FROM sessions"))
        finally:
            con.close()
    except sqlite3.Error as e:
        print(f"  [warn] 无法读取包内会话索引：{e}")
        return 0, 0, 1

    variant = _local_cwd_encoder()
    moved = unrouted = failures = 0
    for cid, cwd in rows:
        if not cid or not cwd:
            continue
        src = body.get(cid)
        if src is None:
            continue
        local_cwd = paths.remap_with_map(cwd, mapping)
        if not local_cwd:
            # portable 包解开时已把 cwd 换成本机口径，这里没有可映射的对方前缀。
            # 直接认作已归位 —— 否则会被判成「无法识别归属」，正文留在对方编码目录。
            if paths.is_under_any(cwd, [lr for _pr, lr in mapping if lr]):
                local_cwd = cwd
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
                failures += 1
                continue
            try:
                rtxt = src.read_text(encoding="utf-8", errors="replace")
                ltxt = want.read_text(encoding="utf-8", errors="replace")
                merged = union_lines(rtxt, ltxt)
                if merged is not None:
                    _write_text(want, merged)
            except OSError as e:
                print(f"  [warn] 会话正文归位合并失败 {src.name}：{e}")
                failures += 1
                continue
            try:
                src.unlink()
            except OSError as e:
                print(f"  [warn] 会话正文归位后清理失败 {src.name}：{e}")
                failures += 1
            continue
        try:
            shutil.move(_lp(src), _lp(want))
        except OSError as e:
            print(f"  [warn] 会话正文归位失败 {src.name}：{e}")
            failures += 1
    return moved, unrouted, failures


def merge_db(target: Path, incoming: Path, path_map: list | None = None) -> dict:
    """把 incoming 数据库合并进 target。

    - sessions 表：只合并「本机 uid」的会话（同 uid 幂等、同 id 保留本机）。
      对方 uid 的会话由 copy_peer_sessions 复制成新 id，不在这里处理。
    - 其余表（workspaces / automations / …）：INSERT OR IGNORE 幂等合并。
    - path_map：[(对方根, 本机根), …]，用于把对方库里的绝对路径改写成
      本机口径。不做这一步时，同一逻辑空间会因绝对路径不同而变成两条记录。
    """
    if not incoming.is_file():
        return {"_merge_failed": "对端数据库缺失"}
    stats = {}
    try:
        conn = sqlite3.connect(str(target), timeout=30)
    except sqlite3.Error as e:
        return {"_merge_failed": f"打开本机数据库失败：{e}"}
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
    except sqlite3.Error as e:
        # 单表错误已经在循环里收敛为 skip(...)；这里兜住 ATTACH / 路径改写 /
        # 提交 / DETACH 等事务级失败。调用方必须知道本次没有完整合并，
        # 不能把它当成普通空统计后写成功标记。
        stats["_merge_failed"] = str(e)
    finally:
        conn.close()
    return stats


def copy_peer_sessions(target_db: Path, src_db: Path, src_files: Path,
                       dst_home: Path, dry: bool) -> tuple:
    """B 方案：把对方账号的会话复制成本机账号的新会话（新 id）。

    - 只复制「对方 uid 的非软删会话」；本机 uid 的会话由 merge_db 同 uid 幂等合并。
    - 新 id = derive_copy_id(源uid, 源cid)：确定性、幂等、可从 id 反推源。
    - 数据库 INSERT OR IGNORE（id → 新 id、user_id → 本机 uid、deleted_at 清空，
      其余列保留源）；只有数据库行首次插入时才写正文，避免重复 pull 覆盖
      本机已经继续使用或编辑过的副本正文。

    返回 (对方 uid 会话的源 cid 集合【含软删】, 复制数量, 正文复制失败数)。调用方据此在文件层
    合并时跳过这些源正文，避免把「对方账号的正文」当普通文件并进本机、留下孤儿 jsonl。
    """
    if not (target_db.is_file() and src_db.is_file()):
        return set(), 0, 0

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
        return set(), 0, 0
    if len(local_uids) != 1:
        # 多账号时无法判断对方会话应归属哪个本机账号。随机挑一个会泄露到
        # 另一个账号的工作区，因此宁可明确跳过，交由用户先确定目标账号。
        print("  [warn] 本机数据库含多个账号，无法确定跨账号会话复制目标；本次跳过复制")
        return set(), 0, 0
    local_uid = next(iter(local_uids))

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
        return set(), 0, 0

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
        return all_peer_cids, 0, 0

    # 正文索引：{cid: 源正文绝对路径}
    body_index = {}
    projects_root = src_files / "projects"
    if projects_root.is_dir():
        for p in projects_root.rglob("*.jsonl"):
            body_index[p.stem] = p

    copied = failures = 0
    placeholders = ",".join("?" * len(cols))
    quoted_cols = ", ".join(f'"{c}"' for c in cols)
    ins_sql = (f"INSERT OR IGNORE INTO sessions "
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
            inserted = tconn.execute(ins_sql, vals).rowcount
            sp = body_index.get(src_cid)
            dp = None
            if sp is not None:
                rel = sp.relative_to(src_files)
                dp = dst_home / rel.parent / f"{new_cid}.jsonl"
            # 行已存在时只修复「正文缺失」：上次可能写库成功、复制正文失败；
            # 但正文已存在时绝不覆盖，本机可能已继续编辑过副本。
            need_body = inserted == 1 or (dp is not None and not dp.exists())
            if inserted != 1 and not need_body:
                continue
            if sp is not None and need_body:
                try:
                    text = sp.read_text(encoding="utf-8", errors="replace")
                    new_text = text.replace(src_cid, new_cid)
                    _makedirs(dp.parent)
                    _write_text(dp, new_text)
                except OSError as e:
                    print(f"  [warn] 正文复制失败 {rel}: {e}")
                    failures += 1
            copied += 1
        tconn.commit()
    finally:
        tconn.close()
    return all_peer_cids, copied, failures


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
                # 时间戳可能是数字，也可能是 ISO 字符串；统一走 parse_timestamp，
                # 让任务标题、会话排序与快照时间使用同一套语义。
                ts = parse_timestamp(ts)
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

    用来判断「这次内容有没有变」，代价与包体积无关。manifest 是外部/旧版本
    产出，先限制成员大小再解析；损坏或超限都视为没有可用基线。
    """
    if not zip_path.is_file():
        return {}
    try:
        with zipfile.ZipFile(zip_path) as zf:
            info = zf.getinfo("manifest.json")
            if info.file_size > 4 * 1024 * 1024:
                return {}
            data = json.loads(zf.read(info).decode("utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, KeyError, zipfile.BadZipFile, ValueError):
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
    failures: list = []
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
        for rel, p in iter_assets(state.WB_HOME, state.SYNC_DIRS,
                                  state.SYNC_FILES, failures):
            try:
                st = p.stat()
            except OSError as e:
                failures.append(("db/workbuddy.db", str(e)))
                continue
            rows.append(("files/" + rel, st.st_size, st.st_mtime_ns))
        if state.WORKSPACE_ROOT.is_dir():
            for rel, p in iter_assets(state.WORKSPACE_ROOT, ["."], None,
                                      failures):
                try:
                    st = p.stat()
                except OSError as e:
                    failures.append(("workspaces/" + rel, str(e)))
                    continue
                rows.append(("workspaces/" + rel, st.st_size, st.st_mtime_ns))
        if failures:
            raise OSError(
                f"{len(failures)} 个同步条目无法枚举或读取；"
                f"首个失败：{failures[0][0]}")

    for key, size, mtime in sorted(rows):
        if key.startswith("workspaces/"):
            h.update(f"ws/{key[11:]}|{size}|{mtime}\n".encode())
        else:
            h.update(f"{key}|{size}|{mtime}\n".encode())
    return h.hexdigest()


def _collect_entries(failures: list | None = None) -> dict:
    """当前同步范围的全部文件：{包内路径: (本机路径, 大小, mtime_ns)}。

    包内路径与 zip 条目完全一致（POSIX 分隔符、files/ 与 workspaces/ 两个前缀），
    这样索引里的 key 可以直接对上解包后的相对路径。

    传入 ``failures`` 时，扫描期间无法 stat 的条目会记录为 ``(包内路径, 原因)``；
    调用方据此拒绝生成不完整基线，而不是把读取失败静默当成文件不存在。
    """
    out: dict = {}
    scan_failures: list = []
    for rel, p in iter_assets(state.WB_HOME, state.SYNC_DIRS, state.SYNC_FILES,
                              scan_failures):
        try:
            st = p.stat()
        except OSError as e:
            if failures is not None:
                failures.append(("files/" + rel.replace("\\", "/"), str(e)))
            continue
        out["files/" + rel.replace("\\", "/")] = (p, st.st_size, st.st_mtime_ns)
    if state.WORKSPACE_ROOT.is_dir():
        for rel, p in iter_assets(state.WORKSPACE_ROOT, ["."], None,
                                  scan_failures):
            try:
                st = p.stat()
            except OSError as e:
                if failures is not None:
                    failures.append(("workspaces/" + rel.replace("\\", "/"), str(e)))
                continue
            out["workspaces/" + rel.replace("\\", "/")] = (p, st.st_size, st.st_mtime_ns)
    if failures is not None and scan_failures:
        failures.extend(scan_failures)
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


def _workspace_stats() -> tuple:
    """返回 (空间总数, 指向本机不存在目录的条数)。

    三个地方要用：health 快照、pull 收尾体检、doctor 自检。
    抽出来是为了三者口径完全一致 —— 口径不一致会让"面板说 0、doctor 说 3"。
    """
    db = state.WB_HOME / "workbuddy.db"
    if not db.is_file():
        return 0, 0
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=10)
        try:
            total = c.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0]
            bad = 0
            for (p,) in c.execute("SELECT path FROM workspaces"):
                if p and not os.path.isdir(str(p).replace("/", "\\")):
                    bad += 1
            return total, bad
        finally:
            c.close()
    except sqlite3.Error as e:
        print(f"  [warn] 无法读取空间统计：{e}")
        return 0, 0


def _write_health(state_dir: Path, files: int, bytes_: int, selective: bool) -> None:
    """把本机健康快照写进 `state/from-<host>.health.json`。

    为什么需要它：跨机一致性检查只能读包，读不到对端的数据库行数、空间可达数、
    未知资产数 —— 而这些恰恰是「体验有没有坏」的关键指标。每台机器 push 时留
    一份自述，任何一台拉下来就能看到整集群的健康状况，不必登录到对方机器上查。

    只读本地库、只写占空间的几百字节；失败不影响 push 主流程。
    """
    db = state.WB_HOME / "workbuddy.db"
    rows: dict = {}
    if db.is_file():
        rows = db_row_counts(db)
    ws_total, ws_bad = _workspace_stats()

    marker = state_dir / f".last-pull-{state.HOST}.json"
    try:
        last_pull = (datetime.fromtimestamp(marker.stat().st_mtime)
                     .isoformat(timespec="seconds")) if marker.is_file() else None
    except OSError:
        last_pull = None

    payload = {
        "schema": 1,
        "host": state.HOST,
        "machine_name": state.MACHINE_NAME,
        "engine": ENGINE_VERSION,
        "generated_at": now_iso(),
        "selective": bool(selective),
        "files": int(files),
        "bytes": int(bytes_),
        "db_rows": rows,
        "workspaces": ws_total,
        "workspaces_unreachable": ws_bad,
        "unknown_dirs": len(detect_unknown_assets()),
        "last_pull": last_pull,
        "os": sys.platform,
    }
    p = state_dir / f"from-{state.HOST}.health.json"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        print(f"  [warn] 健康快照写入失败：{e}")


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
    db = state.WB_HOME / "workbuddy.db"
    try:
        db_state = [db.stat().st_size, db.stat().st_mtime_ns] if db.is_file() else None
    except OSError:
        db_state = None
    data = {
        "schema": packs.INDEX_SCHEMA,
        "host": state.HOST,
        "engine": ENGINE_VERSION,
        "base_seq": int(base_seq or 0),
        "seq": int(seq or 0),
        "fingerprint": fingerprint,
        "db_state": db_state,
        "base_sig": base_sig,
        # 本索引描述的基线是用哪种路径格式打的（0=老格式，1=占位符）。
        # 与基线包 manifest 里的 portable 互为印证：两者不一致说明索引过期。
        "portable": portable.PORTABLE_VERSION if state.PORTABLE_PATHS else 0,
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
    # 只有「确认命中」才可能在严格模式下中止。没扫到（文件太大 / 预算用尽）
    # 是「未知」，不是「有罪」—— 若也阻断，开启 --fail-on-secret 后每次 push
    # 都会因为一两个大文件而失败，用户只能把这个开关关掉，等于废掉它。
    if not res.get("count"):
        print("    以上是未覆盖到的范围，严格模式不会因此中止")
        return {"summary": summary, "blocked": False}
    if fail_on_secret:
        print("    --fail-on-secret 已开启：本次不生成包。")
        print("    处理方式二选一：")
        print("      · 把凭据移出同步范围（清单里去掉对应 sync_files / sync_dirs）")
        print("      · 确需同步就在清单加 \"secret_allow\": [\"相对路径 glob\"]")
        return {"summary": summary, "blocked": True}
    print("    仅告警，包照常生成（严格模式加 --fail-on-secret）")
    return {"summary": summary, "blocked": False}


def _neutralize_staging(staging: Path) -> None:
    """把 staging 里的本机绝对路径统一换成占位符（portable 包）。

    ★ 为什么在 staging 上做
        与敏感扫描同理：staging 里的字节就是「即将进包的那批」，全量 /
        选择性 / 增量三条路径都先落到 staging，一段代码覆盖全部入口。

    ★ 为什么不再枚举列
        之前是「发现一个带路径的字段就补一条」：sessions.cwd、workspaces.path、
        automation_runs.source_cwd、runs_json…… 实测库里 6 个列、配置文件里
        十几个都含绝对路径，枚举必然漏。这里改成对**内容**做替换，
        新载体天然覆盖，不需要维护清单。
    """
    if not state.PORTABLE_PATHS:
        return
    stats = portable.neutralize_tree(staging)
    if stats.get("failed"):
        # 路径未完全中立化的包跨设备后会把本机绝对路径留在对端；宁可整批
        # 失败也不要把「看似成功、实际不 portable」的包写进共享目录。
        raise OSError(
            f"路径中立化失败 {stats['failed']} 处；本次未生成包，可修正权限后重试")
    if stats["files"] or stats["rows"]:
        print(f"  路径中立化：{stats['files']} 个文件 / {stats['rows']} 个库字段"
              f"（对端拉取时自动指向本机对应目录）")


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
    return _secret_gate(secret_scan.scan_tree(staging, state.SECRET_ALLOW,
                                             state.SECRET_SCAN_MB),
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
                state.SECRET_ALLOW, state.SECRET_SCAN_MB)
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
                raise OSError(f"增量索引引用了已消失的条目：{key}")
            src = rec[0]
            dst = staging / key
            try:
                _makedirs(dst.parent)
                _copy_file(src, dst)
                staged += 1
            except OSError as e:
                # 增量绝不能跳过失败文件后继续推进 seq：索引一旦记录 seq，对端
                # 会认为已拿到这一版，缺失文件就永久补不上。整批失败可安全重试。
                raise OSError(f"增量复制失败 {key}：{e}") from e

        db_src = state.WB_HOME / "workbuddy.db"
        db_path = staging / "db" / "workbuddy.db"
        if db_src.is_file():
            db_snapshot(db_src, db_path)

        gate = _scan_staged(staging, fail_on_secret)
        if gate["blocked"]:
            info.update(blocked=True, secrets=gate["summary"])
            return info

        _neutralize_staging(staging)

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
            "portable": portable.PORTABLE_VERSION if state.PORTABLE_PATHS else 0,
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
        _zfiles = [f for f in staging.rglob("*") if f.is_file()]
        _zn = len(_zfiles) or 1
        try:
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED,
                                 compresslevel=6) as z:
                for _i, f in enumerate(_zfiles, 1):
                    z.write(f, f.relative_to(staging).as_posix())
                    if _i % 500 == 0 or _i == _zn:
                        _emit(70 + int(25 * _i / _zn), f"压缩 {_i}/{_zn}")
            tmp_zip.replace(dz)
        finally:
            # 压缩或 rename 失败时不能把 *.zip.part 留在共享目录；part 不是
            # 合法包，但会占用大量空间，也会让运维误判正在传输。
            try:
                tmp_zip.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  ⚠ 无法清理增量临时包 {tmp_zip}：{e}")
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
    _write_health(state_dir, len(entries), int(size), False)
    return info


def normalize_selection(selection: dict | None) -> dict:
    """严格校验选择性同步输入，返回去重后的集合。

    面板、HTTP 和 CLI 都经过这里。未知字段、非对象、非字符串列表或路径式
    条目都明确报错，避免静默回退成“全量同步”。
    """
    if selection is None or selection == {}:
        return {"workspaces": set(), "tasks": set(), "sessions": set()}
    if not isinstance(selection, dict):
        raise ValueError("selection 必须是 JSON 对象")
    allowed = {"workspaces", "tasks", "sessions"}
    unknown = set(selection) - allowed
    if unknown:
        raise ValueError("selection 含未知字段：" + ", ".join(sorted(map(str, unknown))))
    result = {}
    for key in allowed:
        values = selection.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"selection.{key} 必须是字符串数组")
        out = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"selection.{key} 只能包含字符串")
            item = value.strip()
            if (not item or item in (".", "..") or "/" in item or "\\" in item
                    or "\x00" in item):
                raise ValueError(f"selection.{key} 含非法条目：{value!r}")
            out.add(item)
        result[key] = out
    return result


def build_package(sync_root: Path, dry: bool, selection: dict | None = None,
                  force: bool = False, fail_on_secret: bool = False) -> dict:
    """把本机资产打包成 state/from-<标识>.zip。

    selection = {"workspaces": [...], "tasks": [...], "sessions": [...]}：非空时进入
    「选择性模式」，只打包勾选的空间/任务/会话。勾选会话时会同时携带会话正文
    （projects/**/<cid>.jsonl）与精简数据库快照（仅该会话的 sessions 行），
    其余白名单资产与数据库不打包（包小、传输快）；None 或空 = 全量（向后兼容）。
    """
    sel = normalize_selection(selection)
    sel_ws, sel_tasks, sel_sessions = (
        sel["workspaces"], sel["tasks"], sel["sessions"])
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
        _emit(3, "扫描同步范围")
        scan_errors: list = []
        entries = _collect_entries(scan_errors)
        if scan_errors:
            raise OSError(
                f"{len(scan_errors)} 个同步文件无法读取元数据；为避免生成不完整基线，"
                f"本次未生成包（首个失败：{scan_errors[0][0]}）")
        _emit(8, f"扫描完成：{len(entries)} 个文件")
        fp = asset_fingerprint(entries)
        idx = packs.load(state_dir, state.HOST)
        base_meta = _pkg_meta(zip_path)

        # 指纹优先取索引里的。增量模式下基线包不会被重写，基线 manifest 里的
        # content_fingerprint 会一直停在「建基线那一刻」——只比那里的话，
        # 第一次增量之后每次空 push 都会被判成「有变化」而全量重建。
        prev_fp = (idx or {}).get("fingerprint") or base_meta.get("content_fingerprint")
        # 指纹相同也不能跳过缺失/损坏的基线：索引里仍可能记着旧 seq，而对端
        # 连完整包都没有。这里先要求 zip 可读且含 manifest，才允许沿用。
        if not force and zip_path.is_file() and base_meta and prev_fp and prev_fp == fp:
            size = zip_path.stat().st_size if zip_path.is_file() else 0
            info.update(files=len(entries), bytes=size, fingerprint=fp, skipped=True)
            _emit(100, "内容未变，无需重写")
            if dry:
                print(f"  [dry-run] 内容与上次相同（指纹 {fp[:12]}），无需重写包")
            else:
                print(f"  内容与上次相同（指纹 {fp[:12]}），跳过重写，沿用现有包")
                # 包没变但健康快照可能还没生成过（从旧版本升级上来的第一台）。
                # 缺就补一份，否则 consistency 永远读不到这台机器的情况。
                if not (state_dir / f"from-{state.HOST}.health.json").is_file():
                    _write_health(state_dir, len(entries), size, False)
                    print("  ▸ 已补写健康快照")
            return info

        base_size = zip_path.stat().st_size if zip_path.is_file() else 0
        blockers = _incremental_blockers(state_dir)
        # 基线包的路径格式（0 = 2.5.0 之前的老格式，1 = 占位符）。
        # 与当前引擎的输出不一致时必须重建 —— 否则增量会把新格式叠到老基线上，
        # 包里一半绝对路径一半占位符，对端只认得后者。
        base_portable = int(base_meta.get("portable") or 0)
        want_portable = portable.PORTABLE_VERSION if state.PORTABLE_PATHS else 0
        rebuild, why = packs.decide_rebuild(idx, base_size, force, blockers,
                                           base_portable=base_portable,
                                           want_portable=want_portable)
        if base_meta.get("partial"):
            # 上一次是选择性包，基线里只有勾选的内容 —— 必须重建，不能拿它当全量
            rebuild, why = True, "当前基线是选择性包（内容不完整），需重建全量"
        changed = packs.diff(idx, {k: (v[1], v[2]) for k, v in entries.items()})
        if not rebuild:
            # 删除不会出现在当前 entries 中，只比较现有文件会让它们永远留在
            # 增量索引里。检测到旧索引条目消失时重建完整基线；重建的清单不含
            # 该文件，pull 侧也明确采用并集语义（不会误删本机文件）。
            old_keys = set((idx or {}).get("entries") or {})
            if old_keys - set(entries):
                rebuild, why = True, "检测到已删除文件，需重建基线以更新文件清单"
        if rebuild:
            print(f"  重建全量基线：{why}")
            info["rebuild_reason"] = why
        if not rebuild:
            # 数据库变化不会体现在文件 diff 中；指纹已经证明内容变化，
            # 因而即使 changed 为空也要生成只含 db 快照的增量。
            return _build_delta(state_dir, entries, changed, idx, dry, fp, base_sig,
                                fail_on_secret=fail_on_secret)

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
            selection_failures: list = []
            selected_names: list = []
            for tid in sorted(sel_tasks):
                tp = tasks_dir / tid
                if not tp.is_dir():
                    selection_failures.append((f"tasks/{tid}", "不存在或不是目录"))
                    continue
                selected_names.append(f"任务 {tid}")
                for rel, p in iter_assets(tp, ["."], None, selection_failures):
                    dst = files_root / "tasks" / tid / rel
                    _makedirs(dst.parent)
                    _copy_file(p, dst)
                    info["files"] += 1
            for ws in sorted(sel_ws):
                wp = state.WORKSPACE_ROOT / ws
                if not wp.is_dir():
                    selection_failures.append((f"workspaces/{ws}", "不存在或不是目录"))
                    continue
                selected_names.append(f"空间 {ws}")
                for rel, p in iter_assets(wp, ["."], None, selection_failures):
                    dst = ws_root / ws / rel
                    _makedirs(dst.parent)
                    _copy_file(p, dst)
                    info["files"] += 1
            if sel_sessions:
                proj_root = state.WB_HOME / "projects"
                body_index: dict = {}
                if proj_root.is_dir():
                    body_iter = proj_root.rglob("*.jsonl")
                    try:
                        for p in body_iter:
                            body_index.setdefault(p.stem, p)
                    except OSError as e:
                        selection_failures.append((proj_root.as_posix(), str(e)))
                for cid in sorted(sel_sessions):
                    sp = body_index.get(cid)
                    if sp is None:
                        selection_failures.append(
                            (f"sessions/{cid}", "不存在或缺少会话正文"))
                        continue
                    rel = sp.relative_to(proj_root)
                    dst = files_root / "projects" / rel
                    _makedirs(dst.parent)
                    _copy_file(sp, dst)
                    info["files"] += 1
                    selected_names.append(f"会话 {cid}")
                db_src = state.WB_HOME / "workbuddy.db"
                if db_src.is_file():
                    session_snapshot(db_src, staging / "db" / "workbuddy.db",
                                     sel_sessions)
            if selection_failures:
                preview = "; ".join(f"{p}: {why}" for p, why in selection_failures[:3])
                raise OSError(
                    f"选择性同步有 {len(selection_failures)} 项不可用（{preview}）；"
                    "为避免漏同步，本次未生成包")
        else:
            # ── 全量模式：白名单资产 + 全部空间 + 数据库 ──
            # 直接复用 entries：key 就是包内相对路径（files/… 与 workspaces/…），
            # 不必再遍历一次 iter_assets —— 那样等于把 22k 个目录项统计三遍。
            _total_n = len(entries) or 1
            for _i, (key, (src, _size, _mt)) in enumerate(entries.items(), 1):
                dst = staging / key
                _makedirs(dst.parent)
                _copy_file(src, dst)
                info["files"] += 1
                if _i % 500 == 0 or _i == _total_n:
                    _emit(10 + int(60 * _i / _total_n),
                          f"打包文件 {_i}/{_total_n}")
            db_src = state.WB_HOME / "workbuddy.db"
            if db_src.is_file():
                db_snapshot(db_src, staging / "db" / "workbuddy.db")

        # 全量 / 选择性两条路径都先落到 staging，扫描放在这里覆盖两者。
        gate = _scan_staged(staging, fail_on_secret)
        if gate["blocked"]:
            info.update(blocked=True, secrets=gate["summary"])
            return info

        _neutralize_staging(staging)

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
            # 包内路径编码版本。0 = 未启用（老式「把本机绝对路径原样塞进包」）。
            # 读包方看到比自己支持的值更高时必须拒绝 —— 见 _check_peer_version。
            "portable": portable.PORTABLE_VERSION if state.PORTABLE_PATHS else 0,
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
        _emit(97, "写入索引与健康快照")
        _write_health(state_dir, info["files"], info["bytes"], has_sel)
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
    path = Path(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        data = text.encode("utf-8")
        with open(_lp(tmp), "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(_lp(tmp), _lp(path))
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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


MAX_ZIP_MEMBERS = 100_000
MAX_ZIP_BYTES = 8 * 1024 * 1024 * 1024
MAX_ZIP_FILE_BYTES = 2 * 1024 * 1024 * 1024


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
    infos = z.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError(f"压缩包成员过多：{len(infos)} > {MAX_ZIP_MEMBERS}")
    total = sum(max(0, int(zi.file_size)) for zi in infos)
    if total > MAX_ZIP_BYTES:
        raise ValueError(
            f"压缩包解压后体积过大：{human(total)} > {human(MAX_ZIP_BYTES)}")
    for zi in infos:
        if zi.is_dir():
            continue
        if zi.flag_bits & 0x1:
            raise ValueError(f"不支持加密压缩包条目：{zi.filename}")
        dst = _safe_target(base, zi.filename)
        if dst is None:
            # 越界条目说明整个包不可信，不能只跳过它后继续使用其余内容。
            raise ValueError(f"压缩包含越界路径：{zi.filename!r}")
        if zi.file_size > MAX_ZIP_FILE_BYTES:
            raise ValueError(f"压缩包单个文件过大：{zi.filename}")
    for zi in infos:
        if zi.is_dir():
            continue
        dst = _safe_target(base, zi.filename)
        assert dst is not None
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
               pkg_time: float = 0.0, skip_cids: set | None = None,
               failures: list | None = None) -> list:
    """合并目录树。追加型日志走行级并集；其余按 mtime：缺失则补，本机旧则更新，本机新则保留。
    非日志文件若在对方出快照之后本机也改过（两边都动过），标记为冲突而不是默默覆盖。

    skip_cids：跨账号会话复制时，对方账号的会话正文（projects/**/<cid>.jsonl）
    由 copy_peer_sessions 复制成新 id，这里跳过，避免并进本机留下孤儿 jsonl。
    failures：可选列表；扫描期间 stat 失败的条目会追加进去，供 pull 阻止成功标记。
    """
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
        except OSError as e:
            # 文件在扫描期间被占用 / 删除时继续处理其余文件，但不能静默吞掉：
            # 用户需要知道本次少比较了一个路径。
            print(f"  [warn] 无法比较 {rel}：{e}")
            if failures is not None:
                failures.append(rel)
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


def record_conflicts(rows: list) -> bool:
    """把本次冲突追加进本机台账。rows 为 dict 列表：{rel, backup, stamp}。

    返回是否成功落盘；失败时由调用方计入“未完整完成”，否则冲突备份已经
    生成、台账却缺失，用户排查时看不到对应记录。
    """
    if not rows:
        return True
    p = _ledger_path()
    data = {}
    try:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as e:
        print(f"  [warn] 冲突台账损坏，将重建：{e}")
        data = {}
    items = list(data.get("items") or []) if isinstance(data, dict) else []
    items.extend(rows)
    payload = {"updated_at": now_iso(), "items": items[-LEDGER_MAX:]}
    try:
        _write_text(p, json.dumps(payload, ensure_ascii=False, indent=2))
    except OSError as e:
        print(f"  [warn] 冲突台账写入失败：{e}")
        return False
    return True


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
    """返回 (写入, 本机较新保留, 行合并文件数, 补齐行数, 冲突数, 失败数)。"""
    written = kept = merged = merged_lines = conflicts = failures = 0
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
                try:
                    remote_mtime, local_mtime = sp.stat().st_mtime, dp.stat().st_mtime
                except OSError as e:
                    failures += 1
                    print(f"  [warn] 读取行合并文件时间失败 {rel}：{e}")
                    continue
                if remote_mtime <= local_mtime:
                    kept += 1
                    continue
                kind = "更新"
                print(f"  ⚠ {rel} 超过 {UNION_MAX_MB} MB，跳过行级并集，"
                      f"改为整文件替换（本机旧版已留 .bak）")
            else:
                try:
                    remote_text = sp.read_text(encoding="utf-8", errors="replace")
                    local_text = dp.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    failures += 1
                    print(f"  [warn] 读取行合并文件失败 {rel}：{e}")
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
                try:
                    _makedirs(dp.parent)
                    _copy_file(dp, dp.with_name(dp.name + f".bak-{stamp}"))
                    _write_text(dp, merged_text)
                except OSError as e:
                    failures += 1
                    merged -= 1
                    merged_lines -= gained
                    print(f"  [warn] 行合并写入失败 {rel}：{e}")
                continue

        if dry:
            print(f"  [dry-run] {kind:<14} {rel}")
            continue
        try:
            _makedirs(dp.parent)
            suffix = (f".conflict-{state.HOST}-{stamp}" if kind == "冲突·更新"
                      else f".bak-{stamp}")
            # 单个文件写不进去（被占用 / 权限）不应让整次 pull 崩掉：
            # 报出来继续，其余文件照常合并。
            if dp.exists():
                _copy_file(dp, dp.with_name(dp.name + suffix))
            _copy_file(sp, dp)
        except OSError as e:
            failures += 1
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
    if not record_conflicts(ledger):
        failures += 1
    return written, kept, merged, merged_lines, conflicts, failures


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
        except OSError as e:
            print(f"  [warn] 自愈读取失败 {orig.name}：{e}")
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


def peer_sig(sync_state: Path, zp: Path) -> str:
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
        host = _peer_host(zp)
        for rec in (idx or {}).get("deltas") or []:
            name = str((rec or {}).get("zip") or "")
            if not re.fullmatch(rf"d-{re.escape(host)}-\d{{4}}\.zip", name):
                raise PeerPackageIncomplete(f"增量索引含非法文件名：{name!r}")
            dz = packs.delta_dir(sync_state) / name
            if not dz.is_file():
                raise PeerPackageIncomplete(
                    f"增量包 {name} 尚未同步到本机，无法还原到第 {idx.get('seq')} 版")
            overlay_zip(dz, tmp)
            applied.append(name)
    except PeerPackageIncomplete:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except (OSError, zipfile.BadZipFile, ValueError) as e:
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
    path = pull_marker_path(sync_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


#: 机器标识的合法形态：包名是 `from-<host>.zip`，host 里出现路径分隔符或 `..`
#: 就可能被拼到 state 目录之外 —— 面板是 HTTP 入口，必须当成不可信输入校验。
_HOST_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def remove_peer(sync_root: Path, host: str, purge: bool = False) -> dict:
    """从同步目录移除某台机器的全部痕迹（包 / 索引 / 健康快照 / 增量 / 拉取标记）。

    为什么需要：退役机器不删，它就会一直留在成员表里；更麻烦的是引擎过旧的对端
    会让增量安全阀长期退回全量（每次 push 重传数百 MB），删掉才能恢复增量。

    ⚠ 三处硬保护：
    1. 只动 `state/` 下**名字明确属于该 host** 的文件，绝不碰本机与其他机器
    2. host 必须匹配 `[A-Za-z0-9._-]`，防止 `..`、盘符、分隔符把删除引到 state 之外
    3. 默认**不真删**：移到**配置目录**下的 `_removed/<host>-<时间戳>/`，后悔还能捞回来；
       确认不要了再传 ``purge=True`` 直删。
       注意「移到同步目录内的子目录」不算删除 —— 同步盘会把它当新文件再传一遍。
    """
    host = str(host or "").strip()
    if not host:
        return {"ok": False, "error": "缺少机器标识"}
    if host == state.HOST:
        return {"ok": False, "error": "不能移除本机"}
    if not _HOST_RE.fullmatch(host):
        return {"ok": False, "error": "机器标识不合法（只允许字母、数字、点、下划线、连字符）"}

    state_dir = Path(sync_root) / "state"
    if not state_dir.is_dir():
        return {"ok": False, "error": f"同步目录不存在：{state_dir}"}

    targets = [
        state_dir / f"from-{host}.zip",
        state_dir / f"from-{host}.zip.part",
        state_dir / f"from-{host}.index.json",
        state_dir / f"from-{host}.index.json.tmp",
        state_dir / f"from-{host}.health.json",
        state_dir / f".last-pull-{host}.json",
    ]
    ddir = state_dir / "deltas"
    if ddir.is_dir():
        targets += sorted(ddir.glob(f"d-{host}-*.zip"))

    existing = [p for p in targets if p.is_file()]
    if not existing:
        return {"ok": False, "error": "没有找到该机器的文件（可能已被移除）"}

    total = 0
    for p in existing:
        try:
            total += p.stat().st_size
        except OSError:
            pass

    if purge:
        for p in existing:
            try:
                p.unlink()
            except OSError as e:
                return {"ok": False, "error": f"删除失败：{p.name}：{e}"}
        moved_to = ""
    else:
        # 刻意放到**同步目录之外**（配置目录下）。留在 state/_removed/ 的话，
        # 同步盘会把这份副本当成新文件再上传一遍 —— 退役机器的 500 MB
        # 反而多传一次，而且永远躺在共享目录里被其他机器看到。
        moved_to = str(config.CONFIG_DIR / "_removed" / f"{host}-{ts()}")
        try:
            Path(moved_to).mkdir(parents=True, exist_ok=True)
            for p in existing:
                shutil.move(str(p), str(Path(moved_to) / p.name))
        except OSError as e:
            return {"ok": False, "error": f"移动失败：{e}"}

    names = [p.name for p in existing]
    print(f"已移除机器 {host}：{len(existing)} 个文件 / {human(total)}"
          + ("（彻底删除）" if purge else f"（已移到 {moved_to}）"))
    return {"ok": True, "host": host, "files": len(existing), "bytes": total,
            "names": names, "purged": bool(purge), "moved_to": moved_to}


class PeerTooNew(RuntimeError):
    """对端包的格式或引擎版本超出本机能安全处理的范围。

    与「包损坏」不同：内容本身没问题，是本机读不懂。这种情况必须**跳过而不合并**，
    因为错读新格式的后果是静默写错（列对不上、语义变了），比报错难查得多。
    """


def _check_peer_version(meta: dict, force: bool = False) -> None:
    """校验对端包的版本与结构；不安全时抛 PeerTooNew。

    规则是「高版本保护、低版本兼容」：

    · 包结构 schema 高于本机 → 拒绝。本机不认识新格式，硬合并可能写错列。
    · 结构低于本机 → 放行并提示（本项目 schema 一直是 2，属理论分支）。
    · 引擎高于本机 → 拒绝，除非 --force 或清单打开 allow_newer_peer。
      新引擎可能写入了本机读不懂的语义（例如新增的表、新的路径归一规则）。
    · 引擎低于本机 → 放行并提示。增量闸门另有兜底（对端 <2.3.0 自动退全量）。
    """
    peer = str(meta.get("engine_version") or "").strip()
    schema = meta.get("schema")

    if not isinstance(schema, int) or isinstance(schema, bool):
        raise PeerTooNew(f"对方包结构 schema 非法：{schema!r}")
    if schema > MANIFEST_SCHEMA:
        raise PeerTooNew(
            f"对方包结构 v{schema} 高于本机 v{MANIFEST_SCHEMA}，"
            f"先升级本机引擎再拉取")

    # 路径编码版本：包里可能全是 {{WB:WS}} 这类占位符。不认识的引擎会把它
    # 当成真实路径原样写进用户的库 —— 那是静默写错，--force 也不能放行。
    pv = meta.get("portable", 0)       # 旧包没有该字段时按未启用处理
    if not isinstance(pv, int) or isinstance(pv, bool):
        raise PeerTooNew(f"对方包路径编码 portable 非法：{pv!r}")
    if pv > portable.PORTABLE_VERSION:
        raise PeerTooNew(
            f"对方包的路径编码 v{pv} 高于本机支持 v{portable.PORTABLE_VERSION}，"
            f"先升级本机引擎再拉取（该格式不能被强制读取）")

    if peer and packs.compare_versions(peer, ENGINE_VERSION) > 0:
        if not (force or state.ALLOW_NEWER_PEER):
            raise PeerTooNew(
                f"对方引擎 {peer} 高于本机 {ENGINE_VERSION}，"
                f"先升级本机再拉取（确认风险可加 --force）")
        print(f"   ⚠ 对方引擎 {peer} 高于本机 {ENGINE_VERSION}，"
              f"已按允许继续（可能读到本机不认识的语义）")

    if schema < MANIFEST_SCHEMA:
        print(f"   ⚠ 对方包结构 v{schema} 低于本机 v{MANIFEST_SCHEMA}，按兼容模式读取")
    # 独立判断，不能写成 elif：结构低 + 引擎低是常见组合（旧包），
    # 两者都要提示，否则「版本不同」这句会被结构那句话吞掉。
    if (peer and peer != ENGINE_VERSION
            and packs.compare_versions(peer, ENGINE_VERSION) < 0):
        print(f"   ⚠ 对方引擎 {peer} · 本机 {ENGINE_VERSION} —— 版本不同，"
              f"建议两端升级到同一版本后再持续同步")


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
        _emit(100, "无需拉取")
        return 0
    _emit(3, f"发现 {len(others)} 个对端包")

    running = workbuddy_running()
    allow_db = (not running) or force
    if running and force:
        print("[!] WorkBuddy 仍在运行，--force 已生效，即将写入数据库（有损坏风险）")

    stamp = ts()
    total_added = total_updated = total_kept = 0
    total_merged = total_merged_lines = 0
    total_conflicts = 0
    total_copied = 0
    total_failures = 0
    skipped = 0
    incomplete = 0
    blocked = 0

    marker = load_pull_marker(sync_root)
    marker_dirty = False
    pending_seen = False

    # 0) 自愈：先把历史上被整文件覆盖丢掉的日志行，从 .bak 里并回来（幂等）
    heal_files, heal_lines = heal_append_logs(dry, stamp)

    _n_peers = len(others) or 1
    for _pi, zp in enumerate(others, 1):
        _base_pct = 5 + int(80 * (_pi - 1) / _n_peers)
        _emit(_base_pct, f"处理 {zp.name}（{_pi}/{_n_peers}）")
        sig = peer_sig(sync_state, zp)
        if not dry and not force and marker.get(zp.name) == sig:
            print(f"\n── {zp.name}  与上次拉取时相同，跳过 ──")
            skipped += 1
            continue
        try:
            size_label = human(zp.stat().st_size)
        except OSError as e:
            print(f"   ⚠ 无法读取 {zp.name}：{e}")
            incomplete += 1
            continue
        print(f"\n── 处理 {zp.name}  ({size_label}) ──")
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
        peer_failed = False
        copied = 0
        try:
            mf = tmp / "manifest.json"
            pkg_time = 0.0
            if not mf.is_file():
                print("   ⚠ 对端包缺少 manifest.json，无法校验版本与来源")
                print("     已跳过本对端，且不标记为已拉取 —— 等同步盘传完再跑一次 pull")
                incomplete += 1
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                print(f"   ⚠ 无法读取 manifest：{e}")
                incomplete += 1
                continue
            if not isinstance(meta, dict):
                print("   ⚠ manifest 必须是 JSON 对象")
                incomplete += 1
                continue
            print(f"   来源 {meta.get('host')} · {meta.get('exported_at')}")
            try:
                _check_peer_version(meta, force=force)
            except PeerTooNew as e:
                print(f"   ✗ {e}")
                print("     已跳过本对端，且不标记为已拉取 —— 升级后重跑 pull 即可")
                blocked += 1
                continue
            try:
                pkg_time = parse_timestamp(meta["exported_at"])
            except (KeyError, TypeError):
                pkg_time = 0.0

            # 包内路径解码：portable 包里的 {{WB:WS}} 等占位符换成本机根。
            # 必须早于下面所有步骤 —— 后面的 path_map 只认识「对方那一台的
            # 绝对路径」，认不出占位符；晚一步就会把占位符当正文写进本机。
            if meta.get("portable"):
                loc = portable.localize_tree(tmp)
                if loc["files"] or loc["rows"]:
                    print(f"   路径本地化 {loc['files']} 文件 / {loc['rows']} 个库字段"
                          f"（包内占位符 → 本机根）")
                if loc.get("failed"):
                    total_failures += loc["failed"]
                    peer_failed = True
                    print(f"   ⚠ 路径本地化失败 {loc['failed']} 处；"
                          "为避免把占位符写入本机，本对端本次未应用")
                    continue

            # 跨设备路径归一：对方所有绝对路径先改写成本机口径，再做任何合并。
            # 顺序很重要 —— 必须早于 copy_peer_sessions / merge_tree / merge_db，
            # 否则正文会落到对方的编码目录、数据库会插入对方路径的行。
            pmap = _peer_path_map(meta)
            if pmap:
                moved, unrouted, reloc_failed = relocate_project_bodies(tmp, pmap, dry)
                if moved:
                    print(f"   会话正文归位 {moved} 个（对齐本机空间路径编码）")
                if unrouted:
                    print(f"   其中 {unrouted} 个会话的 cwd 不在对方已知根下，保持原位")
                if reloc_failed:
                    total_failures += reloc_failed
                    peer_failed = True
                    print(f"   ⚠ {reloc_failed} 个会话正文未能归位，本次未完整应用")

            # 0) 跨账号会话复制（B 方案）：先算 peer_cids，供文件层跳过对方正文
            src_db = tmp / "db" / "workbuddy.db"
            target_db = state.WB_HOME / "workbuddy.db"
            peer_cids: set = set()
            if src_db.is_file() and target_db.is_file():
                if dry:
                    peer_cids, _, copy_failed = copy_peer_sessions(
                        target_db, src_db, tmp / "files", state.WB_HOME, True)
                    if copy_failed:
                        total_failures += copy_failed
                        peer_failed = True
                elif allow_db:
                    # 任何数据库写入之前先留快照。会话复制也在写库，
                    # 所以快照必须发生在它之前，而不是仅放在 merge_db 之前。
                    snap = backup_db_before_merge(target_db)
                    if snap is None:
                        db_pending = True
                        print("  [跳过] 无法生成写库前快照，本次不写数据库")
                        continue
                    peer_cids, copied, copy_failed = copy_peer_sessions(
                        target_db, src_db, tmp / "files", state.WB_HOME, False)
                    if copied:
                        print(f"  会话复制 {copied} 个（跨账号 → 新 id）")
                        total_copied += copied
                    if copy_failed:
                        total_failures += copy_failed
                        peer_failed = True
                        print(f"   ⚠ {copy_failed} 个会话正文复制失败，本次未完整应用")

            # 1) 文件层（跳过对方账号的会话正文，避免留下孤儿 jsonl）
            scan_failures: list = []
            acts = merge_tree(tmp / "files", state.WB_HOME, "文件", pkg_time,
                              skip_cids=peer_cids, failures=scan_failures)
            ws_acts = merge_tree(tmp / "workspaces", state.WORKSPACE_ROOT,
                                 "空间", pkg_time, failures=scan_failures)
            if scan_failures:
                total_failures += len(scan_failures)
                peer_failed = True
                print(f"   ⚠ {len(scan_failures)} 个文件无法比较，"
                      "本次未完整应用")
            acts += ws_acts
            _emit(_base_pct + int(50 / _n_peers), f"合并文件 {len(acts)} 项")
            if acts:
                w, k, m, ml, cf, failed = apply_actions(acts, dry, stamp)
                total_updated += w
                total_kept += k
                total_merged += m
                total_merged_lines += ml
                total_conflicts += cf
                total_failures += failed
                peer_failed = peer_failed or failed > 0

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
                    _emit(_base_pct + int(70 / _n_peers), "合并数据库")
                    stats = merge_db(target_db, src_db, path_map=pmap)
                    merge_error = stats.get("_merge_failed")
                    if merge_error:
                        db_pending = True
                        peer_failed = True
                        print(f"  [warn] 数据库合并失败：{merge_error}")
                    else:
                        print(f"  写库前快照：{snap.name}")
                    for t, v in stats.items():
                        if t.startswith("_"):
                            continue
                        # merge_db 对单表失败会返回 "skip(<原因>)" 而不是行数。
                        # 直接 += 会抛 TypeError，把整次 pull 打断在"数据库已提交"
                        # 之后 —— 数据没坏，但后面的对端全不处理了。
                        if isinstance(v, int):
                            print(f"  数据库 {t}: +{v} 行")
                            total_added += v
                        else:
                            print(f"  数据库 {t}: {v}")
                            db_pending = True
                            peer_failed = True
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                # 短根目录（如 C:\wbx）用完就删，不留空壳；非空时 rmdir 会失败，忽略
                tmp.parent.rmdir()
            except OSError:
                pass

        if not dry and (db_pending or peer_failed):
            # 数据库没合并就标记"已拉取"，下次会因签名相同直接跳过 → 数据库永远合不上。
            # 宁可下次重跑一遍（文件层合并是幂等的），也不留下这个静默的坑。
            print("  ↳ 本次未完整应用，未标记为已拉取；修正错误后重跑 pull 即可补齐")
        elif not dry:
            marker[zp.name] = sig
            marker_dirty = True

    _emit(95, "保存拉取标记")
    if not dry and marker_dirty:
        try:
            save_pull_marker(sync_root, marker)
        except OSError as e:
            print(f"   ⚠ 保存拉取标记失败：{e}（下次会重新检查并幂等重跑）")
            total_failures += 1

    if not dry and not pending_seen:
        if incomplete:
            print(f"\n合计：{incomplete} 个对端包不完整（增量未同步到本机），本次未处理；"
                  f"另有 {max(0, skipped - incomplete)} 个无需处理")
        elif blocked:
            print(f"\n合计：{blocked} 个对端包因版本不兼容被跳过，本次未处理")
        else:
            print(f"\n合计：{skipped} 个远端包均与上次拉取时相同，无需处理"
                  f"（要强制重跑加 --force）")
        if incomplete or blocked:
            print("  ✗ 本次拉取未完整完成，退出码 2；修正后重跑 pull 即可补齐")
            return 2
        return 0

    print(f"\n合计：数据库新增 {total_added} 行，文件写入 {total_updated} 个，"
          f"本机较新保留 {total_kept} 个")
    if total_copied:
        print(f"      跨账号会话复制 {total_copied} 个（新 id，云端归属分离）")
    if skipped:
        print(f"      另有 {skipped} 个包未变化，已跳过")
    if blocked:
        print(f"      ⚠ {blocked} 个对端因版本不兼容被跳过（先升级本机再 pull）")
    if total_merged:
        print(f"      追加型日志行合并 {total_merged} 个，补齐 {total_merged_lines} 行")
    if heal_files:
        print(f"      从历史 .bak 自愈 {heal_files} 个文件，找回 {heal_lines} 行")
    if total_conflicts:
        print(f"      ⚠ {total_conflicts} 个文件两边都改过，本机版本已存为 "
              f".conflict-{state.HOST}-{stamp}")
    if not dry and (total_updated or total_merged or heal_files):
        print(f"      被覆盖的原文件均已备份为 .bak-{stamp}")
    if not dry:
        _wt, _wb = _workspace_stats()
        if _wb:
            print(f"  ⚠ 拉取后仍有 {_wb}/{_wt} 个空间指向不存在的目录")
            print("     运行 tools/repair_paths.py 可清理（默认只读，--apply 才写）")
    _emit(100, "拉取完成")
    if total_failures or incomplete or blocked or db_pending:
        print("  ✗ 本次拉取未完整完成，退出码 2；未成功项会在下次 pull 重试")
        return 2
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
    stat_errors = 0
    for zp in sorted(sync_state.glob("from-*.zip")):
        try:
            st = zp.stat()
        except OSError as e:
            print(f"  [warn] 无法读取 {zp.name}：{e}")
            stat_errors += 1
            continue
        idx = packs.load(sync_state, _peer_host(zp))
        if zp.stem == f"from-{state.HOST}":
            tag = "[本机]"
        elif marker.get(zp.name) == peer_sig(sync_state, zp):
            tag = "[远端·已同步]"
        else:
            tag = "[远端·待拉取]"
        extra = ""
        if idx:
            extra = f"  +{len(idx.get('deltas') or [])} 增量 · seq {idx.get('seq')}"
        print(f"  {tag:<14} {zp.name:<28} "
              f"{human(st.st_size):>9}  "
              f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}{extra}")
        found = True
    if not found:
        print("  （还没有任何机器的包）")
    else:
        print(f"  增量状态         {packs.describe(packs.load(sync_state, state.HOST))}")
        # 阻塞只在 push 时才生效，但用户往往先看 status 再疑惑"为什么天天全量"。
        # 退役机器的包会躺在这里好几个月，把它显式列出来 + 给出移除办法。
        blockers = _incremental_blockers(sync_state)
        if blockers:
            print(f"  增量受阻         {len(blockers)} 个旧引擎对端："
                  + "、".join(sorted(blockers)))
            print("                   → 它们读不了增量，push 只能退回全量；")
            print("                     确认已退役就在面板「集群成员」里移除该机器。")
    return 1 if stat_errors else 0


def do_verify(sync_root: Path, strict: bool = False) -> int:
    """本机内部一致性：索引（上次 push 的快照）与磁盘实况逐条比对。

    跨机一致性看 `tools/consistency.py` 的 C1–C6；这里回答另一半问题：
    本机自上次 push 之后还有多少没同步出去、有没有索引指向的文件已经不在了。

    退出码：默认 0（"有未推送的改动"是正常状态，不是错误）；
    加 --strict 且漂移率超阈值时返回 1，供脚本判断"该 push 了"。
    """
    state_dir = sync_root / "state"
    idx = packs.load(state_dir, state.HOST)
    scan_errors: list = []
    entries = _collect_entries(scan_errors)
    db = state.WB_HOME / "workbuddy.db"

    print(f"本机内部一致性：{state.MACHINE_NAME}   标识 {state.HOST}   引擎 {ENGINE_VERSION}")
    print(f"  同步范围      {len(entries)} 个文件")
    if scan_errors:
        print(f"  ⚠ 扫描失败      {len(scan_errors)} 个文件（结果不完整）")
    db_state = None
    if db.is_file():
        try:
            st = db.stat()
            db_state = [st.st_size, st.st_mtime_ns]
            print(f"  数据库        {human(st.st_size)}")
        except OSError:
            pass

    if not idx:
        print("  索引          无（还没 push 过，或上次重建基线未完成）")
        print("\n结论：无索引可比对，执行一次 push 建立基线后再看。")
        return 0

    old = {}
    for k, v in (idx.get("entries") or {}).items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            old[k] = (v[0], v[1])

    base = state_dir / f"from-{state.HOST}.zip"
    sig = pkg_sig(base) if base.is_file() else "缺失"
    recorded = str(idx.get("base_sig") or "")
    print(f"  索引          seq{idx.get('seq')} · {len(old)} 条 · "
          f"基线 {human(base.stat().st_size) if base.is_file() else '缺失'}")
    if recorded and recorded != sig:
        print("                ▸ 索引与基线代次不一致，下次 push 会重建基线")

    changed, added, gone = [], [], []
    for k, (_p, size, mtime) in entries.items():
        rec = old.get(k)
        if rec is None:
            added.append(k)
        elif rec[0] != size or rec[1] != mtime:
            changed.append(k)
    for k in old:
        if k not in entries:
            gone.append(k)

    db_changed = db_state != idx.get("db_state")
    total = max(len(entries), len(old), 1) + 1
    diff_n = len(changed) + len(added) + len(gone) + int(db_changed)
    rate = diff_n / total
    print("\n  ── 与磁盘实况的差异 ──")
    print(f"  数据库未推    {'是' if db_changed else '否'}")
    print(f"  已改未推      {len(changed)}")
    print(f"  新增未推      {len(added)}")
    print(f"  已删未推      {len(gone)}")
    print(f"  漂移率        {rate * 100:.2f}%（阈值 1.00%）")

    for label, rows in (("已改", changed), ("新增", added), ("已删", gone)):
        if rows:
            print(f"\n  {label}样例：")
            for k in rows[:5]:
                print(f"    {k}")
            if len(rows) > 5:
                print(f"    … 另有 {len(rows) - 5} 个")

    if gone:
        print("\n  注意：已删未推的文件在 push 后会从新包与新索引里消失，"
              "但仍留在对端的旧包里 —— 对端 pull 不会删除本地文件（同步是并集语义）。")

    if rate > 0.01:
        print(f"\n结论：漂移 {rate * 100:.2f}%（{diff_n} 处）超过 1% 阈值，"
              "建议执行一次 push。")
        return 1 if strict else 0
    if diff_n:
        # 有差异但没到阈值。原实现这里直接打印「一致 ✓」—— 与上面刚列出的
        # 78 处差异自相矛盾，容易让人以为「什么都没有」而跳过 push。
        print(f"\n结论：有 {diff_n} 处未推送的改动（漂移 {rate * 100:.2f}%，在 1% 阈值内）。")
        print("      内容没丢，只是还没推出去；下次 push 会带上。")
        return 0
    print("\n结论：引擎视图与磁盘实况完全一致 ✓")
    return 0


#: guard 的执行台账文件名（放在配置目录，不进同步包）
GUARD_LOG = "guard.log"


def _guard_log(msg: str) -> None:
    """guard 每次真正干活时记一行，便于事后回答「上次收敛是什么时候」。"""
    try:
        p = config.CONFIG_DIR / GUARD_LOG
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{now_iso()}  {msg}\n")
    except OSError:
        pass


def do_guard(sync_root: Path) -> int:
    """无人值守的收敛：WorkBuddy 没在跑就 pull 一次，在跑就静默退出。

    为什么要它：pull 要求 WorkBuddy 完全退出，而人几乎不会为了同步去关它。
    结果是每台机器各 push 各的、谁都不 pull，一致性停在某个时刻不再前进。
    guard 由计划任务每 15 分钟触发一次，专挑「WorkBuddy 恰好没运行」的窗口，
    不需要用户记得任何事 —— 关得越频繁，收敛越及时。
    """
    if workbuddy_running():
        print("WorkBuddy 正在运行，跳过本次 guard（rc=0，不写日志）")
        return 0

    ok, cinfo = config.check_cluster(sync_root, create=False)
    if not ok:
        print(cinfo)
        _guard_log(f"跳过：{cinfo}")
        return 1

    print(f"guard：WorkBuddy 未运行，开始 pull（{sync_root} -> {state.HOST}）")
    rc = do_pull(sync_root, dry=False, force=False)
    _guard_log(f"pull 完成 rc={rc}")
    return rc


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
        if not p.is_dir() or p.name.startswith(".") or p.name in covered:
            continue
        # 只有存在「确实会被同步」的文件才算资产。使用有界 BFS，而不是
        # Path.rglob：客户端若新增一个含数十万文件的缓存目录，doctor 会在这里
        # 长时间扫描，而目的只需知道“是否存在一个可同步文件”。
        has_syncable = False
        truncated = False
        queue = [p]
        visited = 0
        while queue and not has_syncable and visited < 10_000:
            d = queue.pop()
            try:
                with os.scandir(d) as it:
                    children = list(it)
            except OSError:
                continue
            for child in children:
                visited += 1
                if visited >= 10_000:
                    truncated = True
                    break
                try:
                    if child.is_dir(follow_symlinks=False):
                        rel = Path(child.path).relative_to(state.WB_HOME).as_posix()
                        if not is_excluded(rel):
                            queue.append(Path(child.path))
                        continue
                    if not child.is_file(follow_symlinks=False):
                        continue
                    rel = Path(child.path).relative_to(state.WB_HOME).as_posix()
                    if is_excluded(rel):
                        continue
                    if child.stat(follow_symlinks=False).st_size > (
                            state.MAX_WORKSPACE_FILE_MB * 1024 * 1024):
                        continue
                    has_syncable = True
                    break
                except (OSError, ValueError):
                    continue
        if has_syncable or truncated:
            unknown.append(p.name)
    return unknown


def do_doctor(sync_root: Path, root_src: str = "", man_sources=None,
              strict: bool = False) -> int:
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
    db_ok = db.is_file()          # 只 stat 一次，下面两段共用
    if db_ok:
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            c.execute("SELECT count(*) FROM sessions").fetchone()
            c.close()
            print("  数据库可读     ✓")
        except sqlite3.Error as e:
            problems.append(f"数据库读取失败：{e}")
    else:
        problems.append("找不到 workbuddy.db")

    if db_ok:
        _wt, _wb = _workspace_stats()
        if _wb:
            print(f"  空间可达性     {_wb}/{_wt} 条指向不存在的目录")
            if strict:
                problems.append(f"{_wb} 个空间指向不存在的目录"
                                f"（跑 tools/repair_paths.py 清理）")
            else:
                print("                  （加 --strict 会让这种情况返回非零）")
        else:
            print(f"  空间可达性     {_wt} 条全部可达  ✓")

        _ph = portable.count_in_db(db) if state.PORTABLE_PATHS else 0
        if _ph:
            print(f"  路径编码       {_ph} 个字段仍是包内占位符（本机库被写脏了）")
            problems.append(f"{_ph} 个库字段残留路径占位符（需要重新 pull 或手工修复）")
        else:
            print("  路径编码       本机库无占位符残留  ✓")

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
            # 路径格式不一致同样会触发重建；不提前说的话，用户会以为"增量又坏了"
            if base.is_file():
                _bp = int(_pkg_meta(base).get("portable") or 0)
                _wp = portable.PORTABLE_VERSION if state.PORTABLE_PATHS else 0
                if _bp != _wp:
                    print(f"                 ▸ 基线路径格式 v{_bp} 与当前输出 v{_wp} 不一致，"
                          "下次 push 会重建基线")
            _blk = _incremental_blockers(sdir)
            if _blk:
                print(f"                 ▸ 增量被 {len(_blk)} 个旧引擎对端阻塞："
                      + "、".join(sorted(_blk)))
                print("                   （它们读不了增量；确认退役可在面板里移除）")
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
        res = secret_scan.scan_items(pairs, state.SECRET_ALLOW, state.SECRET_SCAN_MB)
        if res["count"]:
            print(f"  敏感内容体检   命中 {res['count']} 处 / {len(res['files'])} 个文件"
                  " —— 这些会随包同步到所有机器")
            for ln in secret_scan.report_lines(res, limit=5):
                print("      " + ln)
            print("      确需同步：在清单加 \"secret_allow\" 豁免该文件")
            print("      不该同步：从 manifest 的 sync_files / sync_dirs 里移除")
        else:
            print("  敏感内容体检   未发现凭据  ✓")
            # 没命中不代表扫全了：大文件被跳过这件事必须说出来
            for ln in secret_scan.blind_spots(res):
                print("      " + ln)

    unknown = detect_unknown_assets()
    if unknown:
        print(f"\n⚠ 有 {len(unknown)} 个目录不在同步白名单里：")
        for u in unknown:
            print(f"      {u}")
        print("  可能是客户端升级后新增的资产。需要一起同步的话，")
        print(f"  把它们加进 {config.CONFIG_DIR / 'manifest.json'} 的 \"sync_dirs\" 即可。")
        print("  ⚠ 加之前先看内容：*-store / registry 这类缓存加进来只会让包变大；")
        print("    含凭据的目录（例如 designkit/credentials.json）则会随包扩散到所有机器。")
        if strict:
            # 严格模式下视为问题：客户端升级新增资产目录是「静默漏同步」的
            # 唯一可发现途径，自动化里需要一个非零退出码来兜住它。
            problems.append(f"{len(unknown)} 个目录不在同步白名单（可能漏同步）")
        else:
            print("  （加 --strict 可让这种情况返回非零退出码，便于自动化检查）")

    if problems:
        print("\n发现以下问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n全部检查通过 ✓")
    return 0
