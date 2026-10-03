#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 全资产双向同步

在多台机器之间，通过一个共享的「同步目录」双向同步 WorkBuddy 的全部个人资产：
配置、技能、记忆、对话、任务、空间（工作目录）。

同步目录可以是任何「双向同步型」的网盘或共享目录 —— OneDrive、坚果云、
百度网盘、局域网共享都行，由 config.json 指定，或用 --sync-root 临时覆盖。
不需要 VPN，也不依赖 GitHub。

架构：per-machine 包
    每台机器只写自己的包  state/from-<机器标识>.zip，
    永远不会覆盖对方的文件 —— 从根上避开同步盘的冲突副本问题。

    push : 本机状态  ->  <同步目录>/state/from-<本机>.zip
    pull : 同步目录上「其他机器」的包  ->  合并进本机

    数据库合并用 INSERT OR IGNORE：同 id 保留本机，对方独有的记录追加，
    因此重复执行是幂等的，多台机器最终收敛到同一份数据。

机器标识
    首次运行生成一个稳定的短 ID 并持久化到 <配置目录>\\machine-id。
    不使用主机名 —— 多台机器重名时会互相覆盖对方的包。
    若同步目录里已存在 from-<主机名>.zip（老版本留下的），会沿用主机名以保持兼容。

配置（都在 <配置目录> 下，Windows 默认 %LOCALAPPDATA%\\wb-sync）
    machine-id      本机标识，首次运行自动生成
    config.json     同步目录等设置，例如 {"sync_root": "D:\\\\Cloud\\\\wb-sync"}
    manifest.json   追加/覆盖同步白名单（可选）

白名单（改清单不用改代码）
    同目录的 default-manifest.json 是默认清单，用户 manifest.json 可追加条目。
    客户端升级新增资产时，只需更新 default-manifest.json，不必动 Python。

用法：
    python wb_sync.py doctor      # 环境自检（先跑这个）
    python wb_sync.py where       # 看当前生效的配置与清单来源
    python wb_sync.py status      # 看两侧差异，不动文件
    python wb_sync.py push        # 本机 -> 同步目录
    python wb_sync.py pull        # 同步目录 -> 本机
    python wb_sync.py push --dry-run

⚠️ 铁律
    1. push 可以在 WorkBuddy 运行时执行（只读快照）
    2. pull 会写数据库，执行前必须完全退出 WorkBuddy
    3. 同一时间只让一台电脑 push

合并语义（为什么不再丢数据）
    · 追加型文件（projects 下的 jsonl/ndjson、memory 下的 md）两边各自续写也安全，
      走「行级并集」，对方的行会补进来，不再整文件覆盖。
    · 其它文件按 mtime 取较新的一方；若本机在对方出快照之后也改过（真冲突），
      取较新的对方版本，并把本机版本另存为 <文件名>.conflict-<机器标识>-<时间戳>。
    · pull 开头会自动「自愈」：把历史上被覆盖丢掉的日志行从 .bak 里并回来（幂等）。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ─────────────────────────── 配置区 ───────────────────────────

ENGINE_VERSION = "2.0.0-dev"

WB_HOME = Path.home() / ".workbuddy"
WORKSPACE_ROOT = Path.home() / "WorkBuddy"

# 本机配置目录：机器标识 / config.json / 用户 manifest.json
# 可用环境变量 WB_SYNC_CONFIG 直接指定（便于测试与便携部署）
CONFIG_DIR = Path(os.environ["WB_SYNC_CONFIG"]) if os.environ.get("WB_SYNC_CONFIG") else Path(
    os.environ.get("LOCALAPPDATA")
    or os.environ.get("XDG_CONFIG_HOME")
    or (Path.home() / ".config")
) / "wb-sync"

# ── 内置默认清单 ──
# 这是「兜底 + 参考基准」。正式清单来自同目录的 default-manifest.json，
# 用户可用 <CONFIG_DIR>\manifest.json 追加自己的条目（列表项取并集）。
# ★ 改清单就能改同步范围，不用改代码 —— 客户端升级后只需更新这一个 JSON。
DEFAULT_MANIFEST = {
    "manifest_version": 1,

    # 单文件资产（支持 glob）
    "sync_files": [
        "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md",
        "settings.json", "models.json", "mcp.json",
        "plugins/installed_plugins.json",
        "plugins/known_marketplaces.json",
        "*/sidebar-list-snapshot.json",   # 侧边栏对话列表；目录名是账号 ID，故用通配
    ],

    # 目录资产（递归；支持 glob）
    "sync_dirs": [
        "skills",            # 技能
        "memory",            # 记忆
        "projects",          # 对话记录
        "tasks",             # 任务
        "storage",           # 用户级个性化数据
        "artifact-index",    # 产物索引
        "experts",           # 自定义专家
        "plugins/marketplaces/my-experts",
        "assistant-display",
        "todos", "plans", "deliverables", "brain",
        "project-resources",     # 项目级 MCP 等资源
        "plugin-account-state",  # 插件启用状态
        "appearance-resources",  # 皮肤等外观资源
    ],

    # 数据库里需要合并的表（session_usage 是运行时统计，不迁）
    "db_tables": [
        "sessions", "workspaces", "automations",
        "automation_runs", "automation_runtime_state", "buddy_snapshots",
    ],

    # 路径中任一段命中即跳过
    "never_segments": [
        "workbuddy.db", "workbuddy.db-shm", "workbuddy.db-wal",
        "connectors", "binaries", "app", "logs", "sessions", "traces",
        "cache", "credentials", ".credentials.json", ".master.key", "security",
        "local-models", "blobs", "shell-snapshots", "vendor", "wbipc",
        "connectors-marketplace", "plugin-marketplace-state-new", "pending-telemetry",
        "node_modules", ".git", "__pycache__", ".venv", "venv", ".idea", ".vscode",
        "file-tree-manifests", "skill-cloud-sync", "editor-sdk-sandbox-ws",
        # 以下为「确认不需要同步」的本机专属目录。列出来不是为了排除（它们本就
        # 不同步），而是为了让 doctor 的未知资产检测不再反复提示。
        "workspace",          # 仅含 sessions/ 编辑器修改备份，可达 GB 级
        "audit-log",          # 审计日志，本机专属
        "file-history",       # 文件历史版本，会持续增长
        "clipboard-images",   # 剪贴板图片缓存
        "local_storage",      # 本地 UI 状态缓存
    ],

    "never_suffix": [
        ".db", ".db-shm", ".db-wal", ".key", ".pem", ".pfx",
        ".tmp", ".log", ".swp", ".env",
    ],

    # 路径中出现即视为敏感、不参与同步（宁可少同步，不能泄密）
    "sensitive_words": [
        "secret", "credential", "password", "passwd",
        "apikey", "api-key", "api_key", "token",
        "private-key", "privatekey",
    ],

    # 额外排除的相对路径 glob（正斜杠）。
    # ★ 默认空 —— 这类条目是「个人环境专属」的，属于用户配置，不该进默认清单。
    "extra_globs": [],

    # 单个文件超过这个大小就跳过（防视频/模型混进来）
    "max_file_mb": 200,
}

# 运行时生效值（main() 里由 apply_manifest() 填充）
SYNC_FILES = list(DEFAULT_MANIFEST["sync_files"])
SYNC_DIRS = list(DEFAULT_MANIFEST["sync_dirs"])
DB_TABLES = list(DEFAULT_MANIFEST["db_tables"])
NEVER_SEGMENTS = set(DEFAULT_MANIFEST["never_segments"])
NEVER_SUFFIX = tuple(DEFAULT_MANIFEST["never_suffix"])
SENSITIVE_WORDS = tuple(DEFAULT_MANIFEST["sensitive_words"])
EXTRA_GLOBS = list(DEFAULT_MANIFEST["extra_globs"])
MAX_WORKSPACE_FILE_MB = DEFAULT_MANIFEST["max_file_mb"]

# 追加型日志（对话记录 jsonl/ndjson、memory 下的 md）：两边各自往后追加，
# 整文件覆盖必然丢行 → 这类文件改走「行级并集」。
UNION_SUFFIX = (".jsonl", ".ndjson")

# 冲突判定容差（秒）：本机文件 mtime 比对方快照时间还新这么多，就认为两边都改过。
CLOCK_GRACE = 120

# 机器标识：main() 里由 resolve_machine_id() 填充。
# HOST 用于构成文件名（包的标识）；MACHINE_NAME 仅用于展示，便于人读。
HOST = ""
MACHINE_NAME = ""

STAMP_FMT = "%Y%m%d-%H%M%S"


# ─────────────────────────── 工具 ───────────────────────────

def ts() -> str:
    return datetime.now().strftime(STAMP_FMT)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


# ─────────────────── 配置 / 清单加载 ───────────────────

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _merge_manifest(base: dict, over: dict) -> dict:
    """把用户清单叠加到默认清单上：列表取并集（去重、默认项在前），标量覆盖。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, list) and isinstance(out.get(k), list):
            merged = list(out[k])
            for item in v:
                if item not in merged:
                    merged.append(item)
            out[k] = merged
        else:
            out[k] = v
    return out


def load_manifest(script_dir: Path) -> tuple:
    """返回 (生效清单, 来源列表)。

    优先级：内置默认 < default-manifest.json（同目录） < <CONFIG_DIR>/manifest.json
    """
    man = dict(DEFAULT_MANIFEST)
    sources = ["内置默认"]

    shipped = script_dir / "default-manifest.json"
    over = _read_json(shipped, None)
    if isinstance(over, dict):
        # 同目录的 default-manifest.json 是「完整清单」：它给出的键整体替换内置值。
        # 这样才真正做到「改清单不用改代码」，也才能删减内置的默认项。
        for key, val in over.items():
            man[key] = val
        sources = [str(shipped)]

    user = CONFIG_DIR / "manifest.json"
    over = _read_json(user, None)
    if isinstance(over, dict):
        man = _merge_manifest(man, over)
        sources.append(str(user))

    return man, sources


def apply_manifest(man: dict) -> None:
    """把生效清单写进模块级变量。"""
    global SYNC_FILES, SYNC_DIRS, DB_TABLES
    global NEVER_SEGMENTS, NEVER_SUFFIX, SENSITIVE_WORDS, EXTRA_GLOBS
    global MAX_WORKSPACE_FILE_MB
    SYNC_FILES = list(man.get("sync_files") or [])
    SYNC_DIRS = list(man.get("sync_dirs") or [])
    DB_TABLES = list(man.get("db_tables") or [])
    NEVER_SEGMENTS = set(man.get("never_segments") or [])
    NEVER_SUFFIX = tuple(man.get("never_suffix") or [])
    SENSITIVE_WORDS = tuple(man.get("sensitive_words") or [])
    EXTRA_GLOBS = list(man.get("extra_globs") or [])
    MAX_WORKSPACE_FILE_MB = int(man.get("max_file_mb") or 200)


def load_config() -> dict:
    cfg = _read_json(CONFIG_DIR / "config.json", {})
    return cfg if isinstance(cfg, dict) else {}


def resolve_machine_id(cfg: dict, sync_root: Path) -> tuple:
    """确定本机标识，返回 (标识, 展示名)，并写入模块级 HOST / MACHINE_NAME。

    稳定的标识是整套方案的前提：直接用主机名时，多台机器重名会互相覆盖对方的包。
        1) config.json / 命令行显式指定  → 用它
        2) <CONFIG_DIR>/machine-id       → 用它（正常情况走这里）
        3) 首次运行、且同步目录里已存在 from-<主机名>.zip → 沿用主机名（兼容老数据）
        4) 其余                          → 生成 m-xxxxxxxx
    """
    global HOST, MACHINE_NAME

    name = (cfg.get("machine_name") or "").strip() or socket.gethostname()

    cid = (cfg.get("machine_id") or "").strip()
    if not cid:
        id_file = CONFIG_DIR / "machine-id"
        if id_file.is_file():
            cid = id_file.read_text(encoding="utf-8").strip()

    if not cid:
        legacy = sync_root / "state" / f"from-{name}.zip"
        cid = name if legacy.is_file() else "m-" + uuid.uuid4().hex[:8]
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            (CONFIG_DIR / "machine-id").write_text(cid, encoding="utf-8")
        except OSError:
            pass

    HOST, MACHINE_NAME = cid, name
    return HOST, MACHINE_NAME


def describe_scope() -> None:
    """首次运行时明确告知：要同步什么、往哪传。"""
    print("  本次同步范围：")
    print(f"    单文件 {len(SYNC_FILES)} 项 · 目录 {len(SYNC_DIRS)} 个 · "
          f"数据库表 {len(DB_TABLES)} 张")
    print(f"    排除规则 {len(NEVER_SEGMENTS)} 条 · "
          f"敏感词 {', '.join(SENSITIVE_WORDS)}")


def find_onedrive() -> Path | None:
    """自动探测 OneDrive 根目录 —— 仅作为首次运行的默认值。

    这里不写入任何硬编码路径：用户可用 config.json 的 sync_root 指向
    任意「双向同步型」目录（坚果云 / 百度网盘 / 局域网共享等）。
    """
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(key)
        if v and Path(v).is_dir():
            return Path(v)
    for cand in (Path.home() / "OneDrive",):
        if cand.is_dir():
            return cand
    return None


def is_excluded(rel: str) -> bool:
    norm = rel.replace("\\", "/")
    parts = norm.split("/")
    if any(p in NEVER_SEGMENTS for p in parts):
        return True
    low = norm.lower()
    if low.endswith(NEVER_SUFFIX):
        return True
    if ".bak" in low or ".conflict-" in low or low.endswith("~"):
        return True
    if any(fnmatch.fnmatch(norm, g) for g in EXTRA_GLOBS):
        return True
    return any(w in low for w in SENSITIVE_WORDS)


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def _process_names_via_powershell() -> list:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Select-Object -ExpandProperty ProcessName"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    return [ln.strip().lower() for ln in out.splitlines() if ln.strip()]


def workbuddy_running() -> bool:
    """检测 WorkBuddy / CodeBuddy 进程是否在运行（决定能不能写库）。

    先用 tasklist；拿不到结果就退回 PowerShell。两种方式都失败时返回 True
    —— 宁可拒绝，也不要在「无法判断」的情况下写数据库。
    （沙箱/受限环境里两个命令都可能被拦；确认 WorkBuddy 已关闭时可加 --force）
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        out = ""

    if out and out.strip():
        names = [line.split(",")[0].strip('"').lower()
                 for line in out.splitlines() if line.strip()]
    else:
        names = _process_names_via_powershell()

    if not names:
        return True  # 无法判断 → 保守当作「正在运行」
    return any("workbuddy" in n or "codebuddy" in n for n in names)


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
                        if p.stat().st_size > MAX_WORKSPACE_FILE_MB * 1024 * 1024:
                            continue
                    except OSError:
                        continue
                    yield rel, p


def asset_summary() -> dict:
    n = 0
    size = 0
    for _, p in iter_assets(WB_HOME, SYNC_DIRS, SYNC_FILES):
        n += 1
        try:
            size += p.stat().st_size
        except OSError:
            pass
    ws_n = ws_size = 0
    if WORKSPACE_ROOT.is_dir():
        for _, p in iter_assets(WORKSPACE_ROOT, ["."], None):
            ws_n += 1
            try:
                ws_size += p.stat().st_size
            except OSError:
                pass
    db = WB_HOME / "workbuddy.db"
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


def merge_db(target: Path, incoming: Path) -> dict:
    """把 incoming 数据库合并进 target：INSERT OR IGNORE，同 id 保留本机。"""
    if not incoming.is_file():
        return {}
    stats = {}
    conn = sqlite3.connect(str(target), timeout=30)
    try:
        # 本机 uid，用于把对方的 uid 映射过来
        local_uids = set()
        try:
            local_uids = {r[0] for r in conn.execute(
                "SELECT DISTINCT user_id FROM sessions").fetchall()}
        except sqlite3.Error:
            pass
        local_uid = sorted(local_uids)[0] if local_uids else None

        conn.execute("ATTACH DATABASE ? AS inc", (str(incoming),))
        for t in DB_TABLES:
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

                # user_id 自动映射到本机 uid
                select_list = cl
                if "user_id" in cols and local_uid:
                    src_uids = {r[0] for r in conn.execute(
                        f"SELECT DISTINCT user_id FROM inc.{t}").fetchall()}
                    if src_uids and not src_uids.issubset(local_uids):
                        safe = lambda v: "'" + str(v).replace("'", "''") + "'"
                        case = "CASE user_id"
                        for u in src_uids:
                            case += f" WHEN {safe(u)} THEN {safe(local_uid)}"
                        case += " ELSE user_id END AS user_id"
                        select_list = ", ".join(
                            case if c == "user_id" else f'"{c}"' for c in cols)

                before = conn.execute(f'SELECT COUNT(*) FROM main."{t}"').fetchone()[0]
                conn.execute(
                    f'INSERT OR IGNORE INTO main."{t}" ({cl}) '
                    f'SELECT {select_list} FROM inc."{t}"'
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


def db_row_counts(path: Path) -> dict:
    if not path.is_file():
        return {}
    out = {}
    try:
        c = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        for t in DB_TABLES:
            try:
                out[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                pass
        c.close()
    except sqlite3.Error:
        pass
    return out


# ─────────────────────────── 打包 / 解包 ───────────────────────────

def build_package(sync_root: Path, dry: bool) -> dict:
    """把本机全量资产打包成 state/from-<host>.zip。"""
    state = sync_root / "state"
    zip_path = state / f"from-{HOST}.zip"
    staging = Path(tempfile.mkdtemp(prefix="wb-pack-"))
    info = {"host": HOST, "zip": str(zip_path), "files": 0, "bytes": 0}

    try:
        files_root = staging / "files"
        for rel, p in iter_assets(WB_HOME, SYNC_DIRS, SYNC_FILES):
            dst = files_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            info["files"] += 1

        ws_root = staging / "workspaces"
        if WORKSPACE_ROOT.is_dir():
            for rel, p in iter_assets(WORKSPACE_ROOT, ["."], None):
                dst = ws_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                info["files"] += 1

        db_src = WB_HOME / "workbuddy.db"
        if db_src.is_file():
            db_snapshot(db_src, staging / "db" / "workbuddy.db")

        manifest = {
            "schema": 2,
            "host": HOST,
            "os": sys.platform,
            "home": str(Path.home()),
            "exported_at": now_iso(),
            "db_rows": db_row_counts(staging / "db" / "workbuddy.db"),
            "counts": {t: 1 for t in DB_TABLES},
        }
        import json
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

        state.mkdir(parents=True, exist_ok=True)
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
    if rel.endswith(UNION_SUFFIX):
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
               pkg_time: float = 0.0) -> list:
    """合并目录树。追加型日志走行级并集；其余按 mtime：缺失则补，本机旧则更新，本机新则保留。
    非日志文件若在对方出快照之后本机也改过（两边都动过），标记为冲突而不是默默覆盖。"""
    if not src_root.is_dir():
        return []
    actions = []
    for p in src_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src_root).as_posix()
        if is_excluded(rel):
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
                both = bool(pkg_time) and dp.stat().st_mtime > pkg_time + CLOCK_GRACE
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
        suffix = f".conflict-{HOST}-{stamp}" if kind == "冲突·更新" else f".bak-{stamp}"
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
        root = WB_HOME / sub
        if root.is_dir():
            targets += [(WB_HOME, p) for p in root.rglob("*.bak-*")]
    if WORKSPACE_ROOT.is_dir():
        for ws in WORKSPACE_ROOT.iterdir():
            mem = ws / ".workbuddy" / "memory"
            if mem.is_dir():
                targets += [(WORKSPACE_ROOT, p) for p in mem.iterdir()
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
    return sync_root / "state" / f".last-pull-{HOST}.json"


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


def do_pull(sync_root: Path, dry: bool, force: bool = False) -> int:
    state = sync_root / "state"
    if not state.is_dir():
        print(f"[!] 同步目录不存在：{state}")
        print("    先在本机执行一次 push，或检查 OneDrive 是否已就绪")
        return 1

    others = sorted(p for p in state.glob("from-*.zip")
                    if p.stem != f"from-{HOST}")
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
            import json
            mf = tmp / "manifest.json"
            pkg_time = 0.0
            if mf.is_file():
                meta = json.loads(mf.read_text(encoding="utf-8"))
                print(f"   来源 {meta.get('host')} · {meta.get('exported_at')}")
                try:
                    pkg_time = datetime.fromisoformat(meta["exported_at"]).timestamp()
                except (KeyError, ValueError, TypeError):
                    pkg_time = 0.0

            # 1) 文件层
            acts = merge_tree(tmp / "files", WB_HOME, dry, "文件", pkg_time)
            ws_acts = merge_tree(tmp / "workspaces", WORKSPACE_ROOT, dry, "空间", pkg_time)
            acts += ws_acts
            if acts:
                w, k, m, ml, cf = apply_actions(acts, dry, stamp)
                total_updated += w
                total_kept += k
                total_merged += m
                total_merged_lines += ml
                total_conflicts += cf

            # 2) 数据库层
            src_db = tmp / "db" / "workbuddy.db"
            target_db = WB_HOME / "workbuddy.db"
            if src_db.is_file() and target_db.is_file():
                if dry:
                    before = db_row_counts(target_db)
                    after = db_row_counts(src_db)
                    for t in DB_TABLES:
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
    if skipped:
        print(f"      另有 {skipped} 个包未变化，已跳过")
    if total_merged:
        print(f"      追加型日志行合并 {total_merged} 个，补齐 {total_merged_lines} 行")
    if heal_files:
        print(f"      从历史 .bak 自愈 {heal_files} 个文件，找回 {heal_lines} 行")
    if total_conflicts:
        print(f"      ⚠ {total_conflicts} 个文件两边都改过，本机版本已存为 "
              f".conflict-{HOST}-{stamp}")
    if not dry and (total_updated or total_merged or heal_files):
        print(f"      被覆盖的原文件均已备份为 .bak-{stamp}")
    return 0


# ─────────────────────────── 状态 / 自检 ───────────────────────────

def do_status(sync_root: Path, root_src: str = "") -> int:
    s = asset_summary()
    print(f"本机：{MACHINE_NAME}    标识 {HOST}    引擎 {ENGINE_VERSION}")
    print(f"  WorkBuddy 目录   {WB_HOME}")
    print(f"  空间目录         {WORKSPACE_ROOT}")
    print(f"  可同步文件       {s['files']} 个 / {human(s['size'])}")
    print(f"  空间文件         {s['ws_files']} 个 / {human(s['ws_size'])}")
    print(f"  数据库           {human(s['db_size'])}")
    print(f"  数据库行数       {db_row_counts(WB_HOME / 'workbuddy.db')}")
    print(f"\n同步目录：{sync_root}" + (f"   ← {root_src}" if root_src else ""))
    state = sync_root / "state"
    if not state.is_dir():
        print("  （尚未创建）")
        return 0
    marker = load_pull_marker(sync_root)
    found = False
    for zp in sorted(state.glob("from-*.zip")):
        if zp.stem == f"from-{HOST}":
            tag = "[本机]"
        elif marker.get(zp.name) == pkg_sig(zp):
            tag = "[远端·已同步]"
        else:
            tag = "[远端·待拉取]"
        print(f"  {tag:<14} {zp.name:<28} "
              f"{human(zp.stat().st_size):>9}  "
              f"{datetime.fromtimestamp(zp.stat().st_mtime):%Y-%m-%d %H:%M}")
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
    for rel, _p in iter_assets(WB_HOME, SYNC_DIRS, SYNC_FILES):
        covered.add(rel.split("/")[0])
    covered |= NEVER_SEGMENTS

    unknown = []
    if not WB_HOME.is_dir():
        return unknown
    try:
        entries = sorted(WB_HOME.iterdir())
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
                rel = f.relative_to(WB_HOME).as_posix()
                if is_excluded(rel):
                    continue
                try:
                    if f.stat().st_size > MAX_WORKSPACE_FILE_MB * 1024 * 1024:
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
    print(f"  本机           {MACHINE_NAME}   标识 {HOST}")
    print(f"  引擎版本       {ENGINE_VERSION}")
    print(f"  Python         {sys.version.split()[0]}")
    print(f"  配置目录       {CONFIG_DIR}")
    if man_sources:
        shown = " < ".join(
            "内置默认" if s == "内置默认" else Path(s).name for s in man_sources)
        print(f"  清单来源       {shown}")

    if WB_HOME.is_dir():
        print(f"  WorkBuddy      {WB_HOME}  ✓")
    else:
        problems.append(f"WorkBuddy 目录不存在：{WB_HOME}")

    db = WB_HOME / "workbuddy.db"
    if db.is_file():
        try:
            c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            c.execute("SELECT count(*) FROM sessions").fetchone()
            c.close()
            print(f"  数据库可读     ✓")
        except sqlite3.Error as e:
            problems.append(f"数据库读取失败：{e}")
    else:
        problems.append("找不到 workbuddy.db")

    if workbuddy_running():
        print("  WorkBuddy 进程  正在运行 —— pull 前需要关闭")
    else:
        print("  WorkBuddy 进程  未运行  ✓")

    od = find_onedrive()
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
        print(f"  把它们加进 {CONFIG_DIR / 'manifest.json'} 的 \"sync_dirs\" 即可。")

    if problems:
        print("\n发现以下问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("\n全部检查通过 ✓")
    return 0


# ─────────────────────────── 入口 ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="WorkBuddy 全资产双向同步")
    ap.add_argument("action", choices=["doctor", "status", "push", "pull", "where"],
                    help="doctor 自检 / where 看配置 / status 看差异 / push 上传 / pull 拉取")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    ap.add_argument("--force", action="store_true", help="WorkBuddy 运行时仍强制写库")
    ap.add_argument("--sync-root", help="覆盖同步根目录（默认取 config.json）")
    ap.add_argument("--name", help="覆盖本机显示名")
    ap.add_argument("--wb-home", help="覆盖 WorkBuddy 目录（测试用）")
    ap.add_argument("--workspace-root", help="覆盖空间目录（测试用）")
    ap.add_argument("--version", action="version", version=f"wb-sync {ENGINE_VERSION}")
    args = ap.parse_args()

    global WB_HOME, WORKSPACE_ROOT
    if args.wb_home:
        WB_HOME = Path(args.wb_home)
    if args.workspace_root:
        WORKSPACE_ROOT = Path(args.workspace_root)

    # 1) 清单 —— 决定「同步什么」
    script_dir = Path(__file__).resolve().parent
    man, man_sources = load_manifest(script_dir)
    apply_manifest(man)

    # 2) 配置 —— 决定「同步到哪」
    cfg = load_config()
    if args.name:
        cfg = dict(cfg, machine_name=args.name)

    if args.sync_root:
        sync_root, root_src = Path(args.sync_root), "命令行 --sync-root"
    elif cfg.get("sync_root"):
        sync_root, root_src = Path(cfg["sync_root"]), f"配置 {CONFIG_DIR / 'config.json'}"
    else:
        od = find_onedrive()
        if not od:
            print("[!] 未能确定同步目录")
            print(f"    请创建 {CONFIG_DIR / 'config.json'}，内容形如：")
            print('      { "sync_root": "D:\\\\Cloud\\\\wb-sync" }')
            print("    或用 --sync-root 临时指定。")
            print("    支持任意「双向同步型」目录：OneDrive / 坚果云 / 百度网盘 / 局域网共享。")
            return 1
        sync_root, root_src = od / "wb-sync", f"自动探测 {od}"

    # 3) 机器标识 —— 决定「以谁的身份写包」
    first_run = not (CONFIG_DIR / "machine-id").is_file()
    resolve_machine_id(cfg, sync_root)

    if first_run and args.action in ("push", "pull"):
        print("首次运行，已为本机分配标识")
        describe_scope()
        print(f"  同步目录：{sync_root}   ← {root_src}")
        print()

    if args.action == "where":
        print(f"引擎版本   {ENGINE_VERSION}")
        print(f"本机标识   {HOST}    （展示名 {MACHINE_NAME}）")
        print(f"配置目录   {CONFIG_DIR}")
        print(f"同步目录   {sync_root}   ← {root_src}")
        print(f"清单来源   {' < '.join(man_sources)}")
        print(f"同步范围   {len(SYNC_FILES)} 个文件 · {len(SYNC_DIRS)} 个目录 · "
              f"{len(DB_TABLES)} 张表")
        print(f"排除规则   {len(NEVER_SEGMENTS)} 段 · {len(NEVER_SUFFIX)} 后缀 · "
              f"{len(EXTRA_GLOBS)} glob · 敏感词 {len(SENSITIVE_WORDS)}")
        return 0

    if args.action == "doctor":
        return do_doctor(sync_root, root_src, man_sources)
    if args.action == "status":
        return do_status(sync_root, root_src)

    if args.action == "push":
        print(f"方向：{HOST} -> {sync_root}")
        info = build_package(sync_root, args.dry_run)
        if args.dry_run:
            return 0
        print(f"  包已生成：{info['zip']}")
        print(f"  {info['files']} 个文件，压缩后 {human(info['bytes'])}")
        print("  下一步：等同步盘同步完成，再到另一台电脑执行 pull")
        return 0

    if args.action == "pull":
        if workbuddy_running() and not args.force:
            print("[!] 请先完全退出 WorkBuddy 再执行 pull")
            print("    （数据库需要独占写入；确认风险可加 --force）")
            return 1
        print(f"方向：{sync_root} -> {HOST}")
        return do_pull(sync_root, args.dry_run, args.force)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
