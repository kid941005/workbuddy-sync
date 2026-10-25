"""增量同步的索引与分层包（base + delta）。

为什么单独一个模块：core.py 已经很大，而索引的读写、变更比对、重建判定都是
**纯逻辑**，不涉及文件内容搬运。放在这里可以单独测试，也不必让 core 继续膨胀。

磁盘布局（都在同步目录的 state/ 下）::

    from-<host>.zip              基线包：全量，沿用原有格式（旧版本仍能读）
    from-<host>.index.json       索引：路径 → 大小/mtime/sha，以及增量序列
    deltas/d-<host>-0007.zip     增量包：只含相对上次变化的文件

关键约束 —— **不能让旧版本的对端静默读到过期数据**：
增量包里装的是"变化的部分"，旧版本只会读基线包，看到的将是上次重建基线时
的状态。所以只要发现有对端引擎低于 ``INCREMENTAL_MIN_ENGINE``，就自动退回
"每次全量"（见 core 里的 blockers），宁可慢，不可错。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: 索引自身的结构版本，与包的 MANIFEST_SCHEMA 无关
INDEX_SCHEMA = 1

#: 低于这个引擎版本的对端读不了增量 → 本机自动退回全量
INCREMENTAL_MIN_ENGINE = "2.3.0"

#: 增量个数与累计体积的上限，超过任一就重建基线。
#:
#: 比例取 1.0 而不是更小：重建基线要把整包重传一遍（几百 MB），
#: 比"多叠几个增量"贵得多。只有增量累计到和基线一样大（pull 时要解两遍
#: 同样的体积）才值得重建。
MAX_DELTAS = 24
DELTA_RATIO = 1.0

#: 基线小于这个体积时不看比例，只按个数封顶。
#: 否则几个 KB 的测试包会因为「增量 1.3KB ≥ 基线一半」立刻重建。
RATIO_MIN_BASE = 8 * 1024 * 1024

DELTA_SUBDIR = "deltas"


def index_path(state_dir: Path, host: str) -> Path:
    return Path(state_dir) / f"from-{host}.index.json"


def delta_dir(state_dir: Path) -> Path:
    return Path(state_dir) / DELTA_SUBDIR


def delta_name(host: str, seq: int) -> str:
    """``d-<host>-0007.zip``。刻意不带 ``from-`` 前缀：
    其它地方用 ``from-*.zip`` 找对端包，增量包必须不被匹配到。"""
    return f"d-{host}-{seq:04d}.zip"


def load(state_dir: Path, host: str) -> dict | None:
    """读索引。缺失或损坏都返回 None —— 调用方据此重建基线，绝不猜。"""
    p = index_path(state_dir, host)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != INDEX_SCHEMA:
        return None
    if not isinstance(data.get("entries"), dict):
        return None
    return data


def save(state_dir: Path, host: str, data: dict) -> None:
    """原子写入：先写 .tmp 再 replace，避免同步盘捞到半个索引。"""
    p = index_path(state_dir, host)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                   encoding="utf-8")
    os.replace(tmp, p)


def diff(index: dict | None, entries: dict) -> list:
    """返回内容有变化的 key。

    entries 形如 ``{key: (size, mtime_ns)}``；索引里存的是
    ``[size, mtime_ns, sha]``。用大小 + mtime 判定，不读内容 —— 与
    asset_fingerprint 同一套取舍：极端情况（内容变但 mtime 不变）由 pull
    侧的逐文件哈希兜底。
    """
    old = (index or {}).get("entries") or {}
    changed = []
    for key, (size, mtime) in entries.items():
        rec = old.get(key)
        if not rec or len(rec) < 2 or rec[0] != size or rec[1] != mtime:
            changed.append(key)
    return changed


def reusable_sha(index: dict | None, key: str, size: int, mtime: int) -> str | None:
    """未变化的文件直接复用上次算好的 sha，省掉重读内容。"""
    rec = ((index or {}).get("entries") or {}).get(key)
    if rec and len(rec) >= 3 and rec[0] == size and rec[1] == mtime:
        return rec[2]
    return None


def _ver_tuple(v) -> tuple:
    out = []
    for part in str(v or "").strip().split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out) or (0,)


def engine_supports(version) -> bool:
    """对端引擎是否能消费增量包。缺失/异常版本一律视为不支持。"""
    if not str(version or "").strip():
        return False
    return _ver_tuple(version) >= _ver_tuple(INCREMENTAL_MIN_ENGINE)


def decide_rebuild(index: dict | None, base_size: int,
                   force: bool = False, blockers=None) -> tuple:
    """返回 (是否必须重建基线, 原因)。"""
    if force:
        return True, "强制重建（--force）"
    if not index:
        return True, "尚无索引（首次或迁移）"
    if base_size <= 0:
        return True, "基线包缺失"
    deltas = index.get("deltas") or []
    if len(deltas) >= MAX_DELTAS:
        return True, f"增量已达 {len(deltas)} 个上限"
    total = 0
    for d in deltas:
        try:
            total += int(d.get("bytes") or 0)
        except (TypeError, ValueError):
            pass
    if base_size >= RATIO_MIN_BASE and total >= base_size * DELTA_RATIO:
        return True, (f"增量累计 {total / 1e6:.0f} MB，已达基线 "
                      f"{base_size / 1e6:.0f} MB，重建更划算")
    if blockers:
        return True, "对端版本过旧，暂用全量：" + "、".join(sorted(blockers))
    return False, ""


def describe(index: dict | None) -> str:
    """给 status 用的一句话摘要。"""
    if not index:
        return "无增量索引（下次 push 会重建基线）"
    deltas = index.get("deltas") or []
    if not deltas:
        return "全量基线，暂无增量"
    total = sum(int(d.get("bytes") or 0) for d in deltas)
    return f"增量 {len(deltas)} 个 / {total / 1e6:.1f} MB（基线 seq {index.get('base_seq', 0)}，当前 seq {index.get('seq', 0)}）"
