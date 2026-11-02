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
import re
import socket
import threading
import uuid
from pathlib import Path

from . import ENGINE_VERSION
from . import state
from . import paths
from .utils import now_iso

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

#: config.json 的读-改-写必须串行；面板线程与按钮动作可能同时更新配置。
_CONFIG_LOCK = threading.RLock()

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

    # 内容级敏感扫描：路径过滤只看名字，凭据却常藏在名字正常的文件里。
    # 默认开启，只告警不阻断；严格模式由命令行 --fail-on-secret 决定。
    "secret_scan": True,
    # 明知有凭据但仍要同步的文件（glob，正斜杠）。例如 "cli-env/*"
    "secret_allow": [],
    # 单次扫描的累计读取上限（MB）。调高能覆盖更多文件，代价是 push 更慢；
    # 扫不完的部分会在 push/doctor 输出里明确报出「已扫 X/Y」。
    "secret_scan_mb": 512,

    # 包内路径中立化（v2.5.0）：打包时把本机根换成占位符，拉取时换回本机根。
    # 关掉它 = 退回老的「绝对路径原样进包」，仅用于排查。
    "portable_paths": True,
}

#: 追加型日志（对话记录 jsonl/ndjson、memory 下的 md）：两边各自往后追加，
#: 整文件覆盖必然丢行 → 这类文件改走「行级并集」。
UNION_SUFFIX = (".jsonl", ".ndjson")

#: 冲突判定容差（秒）：本机文件 mtime 比对方快照时间还新这么多，就认为两边都改过。
CLOCK_GRACE = 120


# ─────────────────────── 清单加载 ───────────────────────

#: 用户可编辑 JSON 的上限。清单/配置不该是几 MB，更不该被同步盘里的
#: 异常文件拖成内存事故；超过上限按“未提供”处理。
MAX_JSON_BYTES = 4 * 1024 * 1024


def _read_json(path: Path, default):
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return default
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


def _as_list(v) -> list:
    """清单里的列表项：只接受 list / tuple，元素取字符串。

    手写的 manifest.json 很容易写成 `"sync_dirs": "cli-env"` ——
    直接 `list()` 会把它拆成 ['c','l','i',...]，于是同步范围变成一堆
    单字母目录，命令照样返回 0。这里宁可当空，也不要静默拆错。
    """
    if isinstance(v, (list, tuple)):
        return [x for x in v if isinstance(x, str)]
    return []


def _as_int(v, default: int) -> int:
    """正整数配置项：非法值退回默认，不让一个手误把整个程序打崩。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _as_bool(v, default: bool) -> bool:
    """布尔配置项：字符串 "false" / "0" / "off" 应当被理解成 False。

    原来的 `bool(man.get("secret_scan", True))` 会把 "false" 判成 True ——
    用户以为关了扫描，实际一直开着。

    空串按「没写」处理，退回默认值：与 _as_int / _as_list 的处理保持一致，
    否则 `"portable_paths": ""` 会意外把功能关掉。
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        s = v.strip().lower()
        if not s:
            return default
        return s not in ("0", "false", "no", "off")
    return bool(v)


def apply_manifest(man: dict) -> None:
    """把生效清单写进运行时状态。

    所有取值都经过类型收敛：这份清单是用户可以直接编辑的 JSON，
    写错类型（字符串当列表、数字写成文字）不该让整个命令崩掉。
    """
    state.SYNC_FILES = _as_list(man.get("sync_files"))
    state.SYNC_DIRS = _as_list(man.get("sync_dirs"))
    state.DB_TABLES = _as_list(man.get("db_tables"))
    state.NEVER_SEGMENTS = set(_as_list(man.get("never_segments")))
    state.NEVER_SUFFIX = tuple(_as_list(man.get("never_suffix")))
    state.SENSITIVE_WORDS = tuple(_as_list(man.get("sensitive_words")))
    state.EXTRA_GLOBS = _as_list(man.get("extra_globs"))
    state.MAX_WORKSPACE_FILE_MB = _as_int(man.get("max_file_mb"), 200)
    # 内容级敏感扫描（v2.4.0）。secret_scan=false 可整体关闭；
    # secret_allow 是「明知有凭据但要同步」的豁免 glob 列表。
    state.SECRET_SCAN = _as_bool(man.get("secret_scan"), True)
    state.SECRET_ALLOW = _as_list(man.get("secret_allow"))
    # 扫描的累计读取上限（MB）。包大了 512 不够，扫不完的部分会显式报出来。
    state.SECRET_SCAN_MB = _as_int(man.get("secret_scan_mb"), 512)
    # 版本协商：默认不允许本机去读「比它新」的包（见 state.ALLOW_NEWER_PEER 说明）
    state.ALLOW_NEWER_PEER = _as_bool(man.get("allow_newer_peer"), False)
    # 包内路径中立化（v2.5.0）。默认开启；关掉只为排查。
    state.PORTABLE_PATHS = _as_bool(man.get("portable_paths"), True)


# ─────────────────────── 配置加载 ───────────────────────

def load_config() -> dict:
    with _CONFIG_LOCK:
        cfg = _read_json(CONFIG_DIR / "config.json", {})
        return cfg if isinstance(cfg, dict) else {}


def update_config(**changes) -> dict:
    """原子更新 config.json，避免并发保存互相覆盖。

    先持进程内锁读取当前配置，再合并调用方给出的键；写入临时文件后
    os.replace 提交。读取失败、磁盘写满或进程中断时，原配置仍完整。
    """
    with _CONFIG_LOCK:
        cfg = _read_json(CONFIG_DIR / "config.json", {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg.update(changes)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        path = CONFIG_DIR / "config.json"
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        return cfg


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
    """确定同步目录，返回 (路径, 来源说明)。

    找不到 / 无法解析时返回 (None, 提示文本)。

    与旧实现的差别：
      · 统一走 paths.normalize —— 展开 ~ 与环境变量、剥掉引号、相对路径
        基于「配置目录」而非当前工作目录（否则计划任务与手动执行会指向不同位置）
      · 明确区分「已存在」与「不存在」：不存在时返回说明而不是造一个路径，
        避免 build_package 在错误盘符上 mkdir 出一份没人读的空目录
    """
    raw, src = None, ""
    if override:
        raw, src = override, "命令行 --sync-root"
    elif cfg.get("sync_root"):
        raw, src = cfg["sync_root"], f"配置 {CONFIG_DIR / 'config.json'}"

    if raw:
        p = paths.normalize(raw, base=CONFIG_DIR)
        if p is None:
            return None, f"[!] sync_root 为空或无法解析：{raw!r}（来源 {src}）"
        if p.is_dir():
            return p, src
        return p, f"{src}（⚠ 目录尚不存在）"

    od = find_onedrive()
    if od:
        p = paths.normalize(od / "wb-sync", base=CONFIG_DIR)
        if p:
            return p, f"自动探测 {od}"

    hint = "\n".join([
        "[!] 未能确定同步目录",
        f"    请创建 {CONFIG_DIR / 'config.json'}，内容形如：",
        '      { "sync_root": "D:\\\\Cloud\\\\wb-sync" }',
        "    或用 --sync-root 临时指定。",
        "    支持任意「双向同步型」目录：OneDrive / 坚果云 / 百度网盘 / 局域网共享。",
    ])
    return None, hint


# ─────────────────────── 共享目录身份锚 ───────────────────────
#
# 多设备分叉最常见也最难查的原因：两台机器其实没有指向同一个共享目录
# （网盘改名、换盘符、OneDrive 个人版/商业版混用、相对路径按 CWD 解析）。
# 两边各自 push 都成功，谁也发现不了"对面根本不在这个目录"。
#
# 解法：首次使用时生成 cluster_id，同时写进
#   ① 共享目录 state/cluster.json   ② 本机 CONFIG_DIR/cluster-id
# 之后每次执行都核对；不一致直接拒绝执行，而不是默默把包写到别处。

CLUSTER_FILE = "cluster.json"


def cluster_paths(sync_root: Path) -> tuple:
    return (sync_root / "state" / CLUSTER_FILE, CONFIG_DIR / "cluster-id")


def check_cluster(sync_root: Path, create: bool = False) -> tuple:
    """核对共享目录身份，返回 (ok, 说明)。

    create=True 时，若共享目录里还没有锚文件则写入（首次使用）。
    """
    shared_f, local_f = cluster_paths(sync_root)
    shared_id = None
    if shared_f.is_file():
        try:
            shared_id = (json.loads(shared_f.read_text(encoding="utf-8")) or {}).get("cluster_id")
        except (OSError, ValueError):
            shared_id = None

    local_id = None
    if local_f.is_file():
        try:
            local_id = local_f.read_text(encoding="utf-8").strip() or None
        except OSError:
            local_id = None

    if shared_id and local_id and shared_id != local_id:
        return False, (
            f"[!] 共享目录身份不一致，已中止\n"
            f"    本机记录的 cluster_id : {local_id}\n"
            f"    该目录里的 cluster_id : {shared_id}\n"
            f"    共享目录：{sync_root}\n"
            f"    这通常意味着本机指错了目录（换盘符 / 网盘改名 / OneDrive 账号不同）。\n"
            f"    请确认 config.json 的 sync_root，或删除 {local_f} 后重试。"
        )

    cid = shared_id or local_id
    if (shared_id is not None and not _valid_cluster_id(shared_id)) or (
            local_id is not None and not _valid_cluster_id(local_id)):
        return False, (
            "[!] 共享目录身份锚格式非法，已中止\n"
            f"    cluster_id 必须是 1-64 位字母、数字、点、下划线或连字符："
            f"{shared_id or local_id!r}\n"
            f"    共享目录：{sync_root}"
        )
    if cid is None:
        if not create:
            return True, "未初始化（首次 push 时建立）"
        cid = "c-" + uuid.uuid4().hex[:12]

    if create:
        try:
            shared_f.parent.mkdir(parents=True, exist_ok=True)
            if not shared_f.is_file():
                shared_tmp = shared_f.with_suffix(".json.tmp")
                shared_tmp.write_text(json.dumps(
                    {"cluster_id": cid, "created_at": now_iso(), "created_by": state.HOST or ""},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(shared_tmp, shared_f)
            local_f.parent.mkdir(parents=True, exist_ok=True)
            if local_id != cid:
                local_tmp = local_f.with_suffix(".tmp")
                local_tmp.write_text(cid, encoding="utf-8")
                os.replace(local_tmp, local_f)
        except OSError:
            return True, f"{cid}（写入失败，仅本次生效）"

    return True, cid



# ─────────────────────── 机器标识 ───────────────────────

def _valid_machine_id(value) -> str:
    """校验机器标识：它既是文件名的一部分，也是跨机身份，不能接受路径字符。"""
    if not isinstance(value, str):
        return ""
    cid = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", cid):
        return ""
    if cid in (".", ".."):
        return ""
    return cid


def _valid_cluster_id(value) -> str:
    """共享目录身份锚只允许安全标识字符；它会被打印并写入共享目录。"""
    return _valid_machine_id(value)


def resolve_machine_id(cfg: dict, sync_root: Path) -> tuple:
    """确定本机标识，返回 (标识, 展示名)，并写入 state.HOST / state.MACHINE_NAME。

    稳定且安全的标识是整套方案的前提：直接用主机名时，多台机器重名会互相覆盖
    对方的包；而恶意或误写的路径字符会让 from-<标识>.zip 逃出 state 目录。
    """
    raw_name = cfg.get("machine_name")
    name = raw_name.strip() if isinstance(raw_name, str) else ""
    name = name or socket.gethostname()

    raw_cid = cfg.get("machine_id")
    cid = _valid_machine_id(raw_cid)
    explicit_invalid = bool(raw_cid) and not cid
    if not cid and not explicit_invalid:
        id_file = CONFIG_DIR / "machine-id"
        if id_file.is_file():
            try:
                cid = _valid_machine_id(id_file.read_text(encoding="utf-8"))
            except OSError:
                cid = ""

    if explicit_invalid:
        raise ValueError(
            "machine_id 必须是 1-64 位字母、数字、点、下划线或连字符，且不能为 . 或 .."
        )
    if not cid:
        legacy_name = _valid_machine_id(name)
        legacy = sync_root / "state" / f"from-{legacy_name}.zip" if legacy_name else None
        cid = legacy_name if legacy and legacy.is_file() else "m-" + uuid.uuid4().hex[:8]
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
