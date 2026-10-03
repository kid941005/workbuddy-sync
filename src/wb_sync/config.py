# -*- coding: utf-8 -*-
"""常量、清单加载、配置加载、机器标识。

设计要点
    1. **白名单是数据，不是代码** —— 同步范围写在 default-manifest.json 里，
       用户还能用 <配置目录>/manifest.json 追加。客户端升级新增资产时，
       只改 JSON 就行，不必动 Python。
    2. 可变的运行时状态一律写进 state 模块（见 state.py 的说明），
       本模块不持有任何需要跨模块共享的可变状态。
"""
from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path

from . import ENGINE_VERSION
from . import state

# ─────────────────────────── 常量 ───────────────────────────

#: 本机配置目录：机器标识 / config.json / 用户 manifest.json
#: 可用环境变量 WB_SYNC_CONFIG 直接指定（便于测试与便携部署）
CONFIG_DIR = (
    Path(os.environ["WB_SYNC_CONFIG"])
    if os.environ.get("WB_SYNC_CONFIG")
    else Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("XDG_CONFIG_HOME")
        or (Path.home() / ".config")
    )
    / "wb-sync"
)

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
        # 登录态目录：里面的 state.json / cookie 是凭证，绝不能上云
        "auth",
        # 以下为「确认不需要同步」的本机专属目录。列出来不是为了排除（它们本就
        # 不同步），而是为了让 doctor 的未知资产检测不再反复提示。
        "workspace",          # 仅含 sessions/ 编辑器修改备份，可达 GB 级
        "audit-log",          # 审计日志，本机专属
        "file-history",       # 文件历史版本，会持续增长
        "clipboard-images",   # 剪贴板图片缓存
        "local_storage",      # 本地 UI 状态缓存
        ".temp",              # 各类中间产物（Excel 修复过程文件等）
        "changes-detail",     # AI 编辑快照：内含本机绝对路径，跨机无意义
        "changes-index",
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
        "cookie", "auth-state",
    ],

    # 额外排除的相对路径 glob（正斜杠）。
    # ★ 默认空 —— 这类条目是「个人环境专属」的，属于用户配置，不该进默认清单。
    "extra_globs": [],

    # 单个文件超过这个大小就跳过（防视频/模型混进来）
    "max_file_mb": 200,
}

#: 追加型日志（对话记录 jsonl/ndjson、memory 下的 md）：两边各自往后追加，
#: 整文件覆盖必然丢行 → 这类文件改走「行级并集」。
UNION_SUFFIX = (".jsonl", ".ndjson")

#: 冲突判定容差（秒）：本机文件 mtime 比对方快照时间还新这么多，就认为两边都改过。
CLOCK_GRACE = 120


# ─────────────────────── 清单加载 ───────────────────────

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


def manifest_dir() -> Path:
    """定位 default-manifest.json 所在目录。

    · 便携 / 源码模式：根目录的 wb_sync.py 垫片会通过环境变量
      WB_SYNC_MANIFEST_DIR 指明自己的位置，于是用根目录那份清单
      —— 用户可以直接编辑，不必进到包里面。
    · pip 安装模式：没有任何提示，就用包内自带的那份。

    刻意不去猜 `sys.argv[0]` / `__main__.__file__`：这两种值在不同启动
    方式（脚本 / -m / console script）下时而是绝对路径时而是相对，会随
    工作目录漂移。由垫片显式声明，语义唯一。
    """
    forced = os.environ.get("WB_SYNC_MANIFEST_DIR")
    if forced:
        return Path(forced)
    return Path(__file__).resolve().parent


def load_manifest(cli_dir: Path | None = None) -> tuple:
    """返回 (生效清单, 来源列表)。

    优先级：内置默认 < default-manifest.json < <CONFIG_DIR>/manifest.json
    """
    man = dict(DEFAULT_MANIFEST)
    sources = ["内置默认"]

    shipped = (cli_dir or manifest_dir()) / "default-manifest.json"
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
    """把生效清单写进运行时状态。"""
    state.SYNC_FILES = list(man.get("sync_files") or [])
    state.SYNC_DIRS = list(man.get("sync_dirs") or [])
    state.DB_TABLES = list(man.get("db_tables") or [])
    state.NEVER_SEGMENTS = set(man.get("never_segments") or [])
    state.NEVER_SUFFIX = tuple(man.get("never_suffix") or [])
    state.SENSITIVE_WORDS = tuple(man.get("sensitive_words") or [])
    state.EXTRA_GLOBS = list(man.get("extra_globs") or [])
    state.MAX_WORKSPACE_FILE_MB = int(man.get("max_file_mb") or 200)


# ─────────────────────── 配置加载 ───────────────────────

def load_config() -> dict:
    cfg = _read_json(CONFIG_DIR / "config.json", {})
    return cfg if isinstance(cfg, dict) else {}


def find_onedrive() -> Path | None:
    """自动探测常见同步盘根目录 —— 仅作为首次运行的默认值。

    覆盖 OneDrive / Dropbox / 坚果云 / iCloud 等；探测不到时返回 None，
    由上层引导用户用 config.json 的 sync_root 指定任意「双向同步型」目录
    （坚果云 / 百度网盘 / U 盘 / 局域网共享等）。
    """
    # 1) 各同步盘常见的环境变量
    env_keys = {
        "OneDrive": ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"),
        "Dropbox": ("Dropbox",),
        "Nutstore": ("Nutstore", "NutstoreRoot", "NutstorePath", "NutstoreLocal"),
        "iCloud": ("iCloudDrive", "iCloudDriveRoot"),
    }
    for keys in env_keys.values():
        for k in keys:
            v = os.environ.get(k)
            if v and Path(v).is_dir():
                return Path(v)
    # 2) 常见默认路径
    home = Path.home()
    for sub in ("OneDrive", "Dropbox", "Nutstore", "iCloudDrive",
                "BaiduSyncdisk", "BaiduNetdiskDownload"):
        p = home / sub
        if p.is_dir():
            return Path(p)
    return None


def resolve_sync_root(cfg: dict, override: str | None = None) -> tuple:
    """确定同步目录，返回 (路径, 来源说明)。找不到时返回 (None, 提示文本)。"""
    if override:
        return Path(override), "命令行 --sync-root"

    if cfg.get("sync_root"):
        return Path(cfg["sync_root"]), f"配置 {CONFIG_DIR / 'config.json'}"

    od = find_onedrive()
    if od:
        return od / "wb-sync", f"自动探测 {od}"

    hint = "\n".join([
        "[!] 未能确定同步目录",
        f"    请创建 {CONFIG_DIR / 'config.json'}，内容形如：",
        '      { "sync_root": "D:\\\\Cloud\\\\wb-sync" }',
        "    或用 --sync-root 临时指定。",
        "    支持任意「双向同步型」目录：OneDrive / 坚果云 / 百度网盘 / 局域网共享。",
    ])
    return None, hint


# ─────────────────────── 机器标识 ───────────────────────

def resolve_machine_id(cfg: dict, sync_root: Path) -> tuple:
    """确定本机标识，返回 (标识, 展示名)，并写入 state.HOST / state.MACHINE_NAME。

    稳定的标识是整套方案的前提：直接用主机名时，多台机器重名会互相覆盖对方的包。
        1) config.json / 命令行显式指定  → 用它
        2) <CONFIG_DIR>/machine-id       → 用它（正常情况走这里）
        3) 首次运行、且同步目录里已存在 from-<主机名>.zip → 沿用主机名（兼容老数据）
        4) 其余                          → 生成 m-xxxxxxxx
    """
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

    state.HOST, state.MACHINE_NAME = cid, name
    return state.HOST, state.MACHINE_NAME


def engine_version() -> str:
    return ENGINE_VERSION


def describe_scope() -> None:
    """首次运行时明确告知：要同步什么、往哪传。"""
    print("  本次同步范围：")
    print(f"    单文件 {len(state.SYNC_FILES)} 项 · 目录 {len(state.SYNC_DIRS)} 个 · "
          f"数据库表 {len(state.DB_TABLES)} 张")
    print(f"    排除规则 {len(state.NEVER_SEGMENTS)} 条 · "
          f"敏感词 {', '.join(state.SENSITIVE_WORDS)}")
