# -*- coding: utf-8 -*-
"""路径解析与跨设备重映射。

为什么单独一个模块
    原实现把两个关键路径写死在 state.py 里：

        WB_HOME       = Path.home() / ".workbuddy"
        WORKSPACE_ROOT = Path.home() / "WorkBuddy"

    这带来两类问题，且都是**静默失败**（不报错，只是同步错东西）：

    1. 源目录解析错误
       WorkBuddy 的空间根目录实际由 <WB_HOME>/app/app-config.json 的
       defaultWorkspacePath 决定，可以位于任意盘符。写死 ~/WorkBuddy 时，
       用户一旦改过工作区位置，wb-sync 扫的就是一个空目录或旧目录，
       真实数据一个字节都不会同步，而命令返回 0。

    2. 目标目录解析错误
       sync_root 直接 Path(cfg["sync_root"])，不做展开与校验：
       · "~\\Cloud" 不会展开，变成字面量目录名
       · "%OneDrive%\\wb-sync" 不展开环境变量
       · 相对路径按**当前工作目录**解析，计划任务与手动执行会指向不同位置
       · 复制粘贴带上的引号 / 首尾空格原样进入路径
       · 路径不存在时 build_package 会 mkdir 出一个新的空目录，"推送成功"
         但数据从未进入真正的共享目录

    本模块把「路径怎么定」集中成一处，并提供跨设备的路径重写工具。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: WorkBuddy 客户端记录「空间根目录」的配置文件（相对 WB_HOME）
APP_CONFIG_REL = Path("app") / "app-config.json"

#: 环境变量名（供自动化脚本 / 守护进程使用，不依赖命令行参数）
ENV_WB_HOME = "WB_SYNC_WB_HOME"
ENV_WORKSPACE_ROOT = "WB_SYNC_WORKSPACE_ROOT"

#: 判断一个目录「像不像」WorkBuddy 数据目录
_WB_MARKERS = ("workbuddy.db", "projects", "settings.json")


# ─────────────────────────── 基础规范化 ───────────────────────────

def normalize(raw, base: Path | None = None) -> Path | None:
    """把用户写的路径字符串规范化为绝对 Path。

    处理顺序（顺序本身是缺陷修复的一部分）：
        去引号/空白 → 展开环境变量 → 展开 ~ → 相对路径基于 base → resolve

    base 默认取「配置目录」而不是当前工作目录：配置里的相对路径是相对配置
    文件说的，不是相对「你碰巧在哪个目录敲命令」。CWD 漂移是原实现里
    计划任务和手动执行结果不一致的直接原因。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    # 用户从资源管理器「复制为路径」会带上引号；JSON 里手写也常这样
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if not s:
        return None

    s = os.path.expandvars(s)          # %OneDrive% / $HOME
    s = os.path.expanduser(s)          # ~ / ~user
    p = Path(s)
    if not p.is_absolute():
        anchor = Path(base) if base else Path.cwd()
        p = anchor / p
    try:
        return p.resolve()
    except OSError:
        # 盘符不存在等情况下 resolve 也会抛；退回绝对化但不校验
        return Path(os.path.abspath(str(p)))


def same_path(a: Path | None, b: Path | None) -> bool:
    """Windows 下大小写不敏感，比较必须以 normcase 为准。"""
    if a is None or b is None:
        return False
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


# ─────────────────────────── 源目录发现 ───────────────────────────

def looks_like_wb_home(p: Path) -> bool:
    if not p.is_dir():
        return False
    return any((p / m).exists() for m in _WB_MARKERS)


def find_workbuddy_home(explicit: str | None = None,
                        fallback: Path | None = None) -> tuple:
    """确定 WorkBuddy 数据目录，返回 (路径, 来源说明)。

    优先级：显式参数 > 环境变量 > ~/.workbuddy（且看起来像）> fallback
    """
    if explicit:
        p = normalize(explicit, base=Path.cwd())
        if p:
            return p, "命令行 --wb-home"
    env = os.environ.get(ENV_WB_HOME)
    if env:
        p = normalize(env, base=Path.cwd())
        if p:
            return p, f"环境变量 {ENV_WB_HOME}"
    default = fallback or (Path.home() / ".workbuddy")
    if looks_like_wb_home(default):
        return default, "默认 ~/.workbuddy"
    return default, "默认 ~/.workbuddy（未检测到数据库，可能是全新安装）"


def _read_default_workspace_path(wb_home: Path) -> tuple:
    """从客户端自己的配置里读空间根目录。

    app-config.json 的 defaultWorkspacePath 是 WorkBuddy 判断「空间放哪」的
    权威来源；用户改过工作区位置时，只有这里知道新位置。
    """
    cfg_path = wb_home / APP_CONFIG_REL
    if not cfg_path.is_file():
        return None, ""
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    raw = data.get("defaultWorkspacePath") or data.get("workspaceRoot")
    if not raw:
        return None, ""
    p = normalize(raw, base=wb_home)
    if p is None:
        return None, ""
    return p, f"{cfg_path} 的 defaultWorkspacePath"


def find_workspace_root(wb_home: Path, explicit: str | None = None,
                        cfg: dict | None = None) -> tuple:
    """确定空间根目录，返回 (路径, 来源说明)。

    优先级（后出现的更「人为」，因此更优先）：
        1. 显式参数 --workspace-root
        2. 环境变量 WB_SYNC_WORKSPACE_ROOT
        3. 本工具 config.json 的 workspace_root
        4. **WorkBuddy 客户端 app-config.json 的 defaultWorkspacePath**
        5. 兜底 ~/WorkBuddy
    """
    if explicit:
        p = normalize(explicit, base=Path.cwd())
        if p:
            return p, "命令行 --workspace-root"
    env = os.environ.get(ENV_WORKSPACE_ROOT)
    if env:
        p = normalize(env, base=Path.cwd())
        if p:
            return p, f"环境变量 {ENV_WORKSPACE_ROOT}"
    if cfg and cfg.get("workspace_root"):
        p = normalize(cfg["workspace_root"], base=wb_home)
        if p:
            return p, "本工具 config.json 的 workspace_root"
    p, src = _read_default_workspace_path(wb_home)
    if p:
        return p, src
    return (Path.home() / "WorkBuddy"), "默认 ~/WorkBuddy"


# ─────────────────────────── 会话目录编码 ───────────────────────────
#
# WorkBuddy 把会话正文按 <projects>/<编码后的 cwd>/<cid>.jsonl 存放。
# 编码规则在不同客户端版本间**并不一致**（实测同一台机器上同时存在
# `c-Users-demo-WorkBuddy-xxx` 与 `D-WorkBuddy-workbuddy-xxx`，盘符大小写
# 处理不同）。因此这里不假设规则，而是**用本机已有数据校准**：
# 拿本机 sessions 的 cwd 与真实目录名对照，选出命中率最高的变体。

_DRIVE_RE = re.compile(r"^([A-Za-z]):")


def _split_cwd(cwd: str) -> tuple:
    s = str(cwd).replace("\\", "/").rstrip("/")
    m = _DRIVE_RE.match(s)
    if m:
        return m.group(1), s[2:].lstrip("/")
    return "", s


def encode_cwd_variants(cwd: str) -> list:
    """给出 cwd 目录名的候选编码，按「猜测优先级」排序。"""
    drive, rest = _split_cwd(cwd)
    out = []
    if drive:
        for d in (drive.lower(), drive.upper()):
            out.append(f"{d}-{rest.replace('/', '-')}" if rest else d)
    else:
        out.append(rest.replace("/", "-"))
    return list(dict.fromkeys(out))


def build_cid_index(projects_root: Path) -> dict:
    """cid -> 所在目录名。一次遍历，避免为每个会话都 rglob。"""
    idx: dict = {}
    if not projects_root.is_dir():
        return idx
    for p in projects_root.rglob("*.jsonl"):
        idx.setdefault(p.stem, p.parent.name)
    return idx


def calibrate_encoder(projects_root: Path, cwd_by_cid: dict) -> tuple:
    """用本机数据确定编码变体。

    返回 (变体序号, 命中数, 样本数)。变体序号对应 encode_cwd_variants 的下标。
    校验失败（数据为空 / 全部不命中）时退回 0，即「盘符小写」这一最常见形式。
    """
    idx = build_cid_index(projects_root)
    score = {}
    total = 0
    for cid, cwd in cwd_by_cid.items():
        real = idx.get(cid)
        if not real or not cwd:
            continue
        total += 1
        for i, cand in enumerate(encode_cwd_variants(cwd)):
            if os.path.normcase(cand) == os.path.normcase(real):
                score[i] = score.get(i, 0) + 1
    if not total or not score:
        return 0, 0, total
    best = max(score.items(), key=lambda kv: kv[1])
    return best[0], best[1], total


def encode_cwd(cwd: str, variant: int = 0) -> str:
    cands = encode_cwd_variants(cwd)
    return cands[variant] if 0 <= variant < len(cands) else cands[0]


# ─────────────────────────── 跨设备重映射 ───────────────────────────

def remap_path(value: str, peer_roots: list, local_root: Path) -> str | None:
    """把对方机器上的绝对路径改写成本机对应路径。

    peer_roots 是「对方机器的根目录」候选，按长度降序尝试（越长越精确）。
    只在大小写不敏感的前缀匹配成功时改写，否则返回 None —— 宁可不动，
    也不要猜错路径把数据写到别处。
    """
    if not value or not peer_roots or local_root is None:
        return None
    v = str(value).replace("\\", "/")
    for root in sorted((r for r in peer_roots if r), key=len, reverse=True):
        r = str(root).replace("\\", "/").rstrip("/")
        if not r:
            continue
        if os.path.normcase(v) == os.path.normcase(r):
            return str(local_root).replace("\\", "/")
        head = v[: len(r) + 1]
        if os.path.normcase(head) == os.path.normcase(r + "/"):
            tail = v[len(r) + 1:]
            return str(Path(local_root) / Path(tail)).replace("\\", "/")
    return None


def remap_with_map(value: str, mapping: list) -> str | None:
    """按 [(对方根, 本机根), ...] 依次尝试重映射，命中即返回。

    按「对方根」长度降序，保证更长（更精确）的前缀优先于短前缀。
    """
    if not value:
        return None
    for peer_root, local_root in sorted(
            ((p, lr) for p, lr in mapping if p and lr),
            key=lambda kv: len(str(kv[0])), reverse=True):
        got = remap_path(value, [peer_root], local_root)
        if got is not None:
            return got
    return None
