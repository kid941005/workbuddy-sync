# -*- coding: utf-8 -*-
"""包内路径中立化（portable paths）。

为什么需要这一层
    引擎原先的做法是「枚举哪里有绝对路径」：sessions.cwd、workspaces.path、
    automation_runs.source_cwd、runs_json…… 每发现一处补一处。实测本机
    11 张表里有 6 个列、抽样 156 个配置文件里至少 12 个含绝对路径。
    枚举必然漏 —— 客户端每新增一个带路径的字段就再犯一次。这就是
    「路径问题反复出现」的机制性原因，不是某次修漏了。

    这一层不再猜字段位置，改在**包的边界**做统一编码：

        打包时   本机根 → {{WB:WS}} / {{WB:WB}} / {{WB:HOME}}
        拉取时   占位符 → 本机根

    替换作用于内容本身，不关心它是什么字段：数据库所有 TEXT 列、所有
    文本文件都被统一处理，新载体天然覆盖，不需要维护清单。

为什么用文本替换而不是解析结构化数据
    JSON / SQLite 的结构随客户端版本变，解析反而脆。按前缀替换对任何
    结构都成立，代价是必须覆盖「同一条路径的三种书写形式」：

        C:\\\\Users\\\\x   JSON 里的转义形式（磁盘上两个反斜杠）
        C:\\Users\\x     单反斜杠（脚本、纯文本、命令输出）
        C:/Users/x      正斜杠（引擎自己产出的形式）

    漏掉任何一种都会出现「替换一半、留下半截」—— 那比不替换更难查。

为什么占位符里不带分隔符
    带的话占位符本身就要有三种形态，包里会很难读。这里改成：替换只吃掉
    「根」这一段，后面的分隔符原样保留；本地化时用前瞻捕获那个分隔符，
    按同样的风格把本机根接回去。于是 JSON 里的 ``\\\\`` 仍然是 ``\\\\``、
    纯文本里的 ``\\`` 仍然是 ``\\`` —— 不会把合法 JSON 变成非法转义。
"""
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from . import state

#: 包内路径编码的版本。写进 manifest 的 ``portable`` 字段。
#: 读包方看到比自己支持的值更高时必须拒绝 —— 错读新编码等于把占位符
#: 当成真实路径写进用户的库。
PORTABLE_VERSION = 1

PH_WS = "{{WB:WS}}"
PH_WB = "{{WB:WB}}"
PH_HOME = "{{WB:HOME}}"

#: 单文件处理上限。超过这个大小的文本文件跳过 —— 会话正文与配置都在几 MB 内，
#: 跳过超大文件是为了不让一个被误放大的日志把 push 拖成十几分钟。
MAX_TEXT_MB = 8

#: 认作文本的扩展名。没收录的一律当二进制，宁可不改也不写坏文件。
TEXT_SUFFIXES = frozenset("""
.json .jsonl .json5 .ndjson .md .markdown .txt .text .rst .log .csv .tsv
.yml .yaml .toml .ini .cfg .conf .properties .env .lock
.py .pyi .js .mjs .cjs .jsx .ts .tsx .vue .svelte
.sh .bash .zsh .fish .ps1 .psm1 .psd1 .cmd .bat .vbs
.rb .go .rs .java .kt .c .h .hpp .cc .cpp .cs .php .lua .pl .r
.html .htm .xhtml .css .scss .less .xml .svg .sql .gradle .mk
""".split())

#: 无扩展名但明确是文本的文件名
TEXT_NAMES = frozenset("""
LICENSE LICENCE COPYING NOTICE README CHANGELOG AUTHORS Makefile Dockerfile
.gitignore .gitattributes .npmrc .editorconfig .env .bashrc .zshrc
""".split())

BS = "\\"
DBL = BS * 2
#: 路径名允许的字符（用于判断「根后面是否还有名字尾巴」）
_BOUNDARY = r"(?![A-Za-z0-9_.\-])"

#: 分隔符风格 → 占位符后缀。风格必须编码进占位符，不能靠上下文猜：
#: 一条路径常常就是整个字符串值（``"workspaceRoot": "C:\\\\Users\\\\x"``），
#: 替换之后原文里一个反斜杠都不剩，还原时无从判断 —— 只能把它记在标记里。
_SEP_TAG = {"/": "", BS: ":1", DBL: ":2"}
_STYLES = (DBL, BS, "/")


def _marker(ph: str, style: str) -> str:
    """``{{WB:WS}}`` + 风格 → ``{{WB:WS:2}}``（双反斜杠）。"""
    return ph[:-2] + _SEP_TAG[style] + "}}"


def _render(base: str, style: str) -> str:
    return base if style == "/" else base.replace("/", style)


# ─────────────────────────── 占位符映射 ───────────────────────────

def _fwd(p) -> str:
    """统一成正斜杠、去掉末尾分隔符。空值与根目录返回空串。"""
    if not p:
        return ""
    s = str(p).replace("\\", "/").rstrip("/")
    return s


def build_map(ws_root=None, wb_home=None, home=None) -> list:
    """构造 [(真实根, 占位符)]，按长度降序 —— 长根优先，避免被短根吞掉。

    三个根是包含关系（``C:/Users/x`` ⊃ ``C:/Users/x/WorkBuddy``），
    如果短根先替换，长根就再也不会被匹配到，空间路径会被写成
    ``{{WB:HOME}}/WorkBuddy/...`` —— 换机后仍然能还原成正确路径，
    但占位符语义就乱了，排查时会误判。所以顺序必须是长 → 短。
    """
    pairs = ((ws_root, PH_WS), (wb_home, PH_WB), (home, PH_HOME))
    out, seen = [], set()
    for root, ph in pairs:
        b = _fwd(root)
        if not b or b.lower() in seen:
            continue
        seen.add(b.lower())
        out.append((b, ph))
    out.sort(key=lambda kv: len(kv[0]), reverse=True)
    return out


def _local_map() -> list:
    """占位符 → 本机根。顺序无关，但保持与 build_map 一致的稳定性。"""
    pairs = ((PH_WS, _fwd(state.WORKSPACE_ROOT)),
             (PH_WB, _fwd(state.WB_HOME)),
             (PH_HOME, _fwd(Path.home())))
    return [(ph, root) for ph, root in pairs if root]


# ─────────────────────────── 文本级替换 ───────────────────────────

def neutralize_text(text: str, mapping: list) -> str:
    """把本机绝对路径换成占位符。mapping = build_map() 的返回值。

    只吃掉「根」这一段，后面的分隔符与文件名原样保留 —— 因此替换是逐字节
    可逆的：原文是什么风格，还原出来还是什么风格。

    单值入口：批量处理请用 neutralize_tree（它把正则预编译一次）。
    这里对编译结果做了小缓存，避免逐条调用时反复编译 9 条规则。
    """
    if not text or not mapping:
        return text
    return _cached_neutralizer(mapping)(text)


#: neutralize_text 的编译缓存。mapping 只有三对、且进程内基本不变，
#: 上限设小一点即可；超了整体清空，不做 LRU（没有热点区分的必要）。
_NEUT_CACHE: dict = {}
_NEUT_CACHE_MAX = 8


def _cached_neutralizer(mapping: list):
    key = tuple(mapping)
    fn = _NEUT_CACHE.get(key)
    if fn is None:
        if len(_NEUT_CACHE) >= _NEUT_CACHE_MAX:
            _NEUT_CACHE.clear()
        fn = _neutralizer(mapping)
        _NEUT_CACHE[key] = fn
    return fn


def _neutralizer(mapping: list):
    """把 mapping 预编译成可复用的替换函数。

    为什么不用一条交替式正则
        试过把 9 条规则合成 ``(A)|(B)|...`` 一次扫完，实测反而慢一倍：
        交替式正则在每个字符位置都要依次试 9 个分支，而 9 次独立的 ``sub``
        每次都能走字面量前缀的快速路径（230 MB 样本：6.8s → 13.3s）。
        这里只把 ``re.escape`` 与编译提到循环外，规则本身仍逐条执行。
    """
    rules = []
    for base, ph in mapping:
        for sty in _STYLES:
            rules.append((
                re.compile(re.escape(base.replace("/", sty)) + _BOUNDARY, re.I),
                _marker(ph, sty),
            ))

    def _apply(text: str) -> str:
        if not text:
            return text
        out = text
        for pat, rep in rules:
            # 边界断言必须有：否则 C:\\Users\\x 会把 C:\\Users\\xy 的前缀也吃掉，
            # 换机后 xy 变成 x + "y" 这种不存在的路径。
            out = pat.sub(lambda _m, r=rep: r, out)
        return out

    return _apply


def localize_text(text: str, mapping: list) -> str:
    """把占位符换回本机绝对路径。mapping = [(占位符, 本机根), ...]。"""
    if not text or not mapping:
        return text
    out = str(text)
    for ph, root in mapping:
        base = _fwd(root)
        if not base:
            continue
        for sty in _STYLES:
            out = out.replace(_marker(ph, sty), _render(base, sty))
    return out


# ─────────────────────────── 文件遍历 ───────────────────────────

def _looks_text(p: Path) -> bool:
    name = p.name
    if name in TEXT_NAMES:
        return True
    suf = p.suffix.lower()
    if suf:
        return suf in TEXT_SUFFIXES
    return True          # 无扩展名：交给内容嗅探决定


def _read_text(p: Path) -> str | None:
    """读出 UTF-8 文本；二进制 / 非 UTF-8 / 过大一律返回 None。

    UTF-8 解码再编码是逐字节可逆的，所以「读出来又原样写回」不会破坏文件。
    非 UTF-8（GBK 编码的中文文档等）直接跳过 —— 猜错编码写回去就是乱码，
    宁可让这类文件的路径留在原地，靠 pull 侧的 remap 兜底。
    """
    try:
        if p.stat().st_size > MAX_TEXT_MB * 1024 * 1024:
            return None
        raw = p.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def rewrite_tree(root: Path, func) -> dict:
    """对 root 下所有文本文件做内容替换。返回 {"scanned", "files"}。

    ★ 只跳过「包根的 manifest.json」
        包清单本身要记真实 home / workspace_root（旧引擎靠它做路径映射），
        不能被中立化。但这个名字在 plugins/、skills/ 里到处都是（插件清单），
        按文件名全局跳过会让那些文件的内容完全不处理 —— 插件清单里一旦带
        安装路径就漏了。所以只在 dirpath == root 时跳过。
    """
    root = Path(root)
    stats = {"scanned": 0, "files": 0}
    if not root.is_dir():
        return stats
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        at_root = Path(dirpath) == root
        for name in sorted(filenames):
            if at_root and name == "manifest.json":
                continue
            p = Path(dirpath) / name
            if not _looks_text(p):
                continue
            text = _read_text(p)
            if text is None:
                continue
            stats["scanned"] += 1
            new = func(text)
            if new == text or new is None:
                continue
            try:
                p.write_bytes(new.encode("utf-8"))
            except OSError:
                continue
            stats["files"] += 1
    return stats


# ─────────────────────────── 数据库 ───────────────────────────

def rewrite_db(db_path: Path, func) -> dict:
    """对库里所有 TEXT 值做内容替换，返回 {"rows": 改动行数}。

    为什么扫全表全列而不是维护一张列清单
        列清单就是白名单，白名单必然漏 —— 这正是本次要根治的病根。
        库只有几 MB，逐列 ``typeof(col)='text'`` 扫一遍的代价可以忽略。

    主键冲突
        ``workspaces.path`` 自己就是主键。归一之后两行可能撞成同一个值
        （历史上同一空间以「别机路径 + 本机路径」各存一份），UPDATE 会抛
        IntegrityError。处理方式与 pull 侧一致：保留已有行，把本行的
        last_opened_at 并过去，然后删掉本行 —— 归一后它们本来就是同一个空间。
    """
    stats = {"rows": 0}
    db_path = Path(db_path)
    if not db_path.is_file():
        return stats
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.Error:
        return stats
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            except sqlite3.Error:
                continue
            for c in cols:
                try:
                    rows = conn.execute(
                        f'SELECT rowid, "{c}" FROM "{t}" WHERE typeof("{c}")=\'text\''
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for rid, val in rows:
                    if not isinstance(val, str) or not val:
                        continue
                    new = func(val)
                    if not new or new == val:
                        continue
                    try:
                        conn.execute(f'UPDATE "{t}" SET "{c}"=? WHERE rowid=?',
                                     (new, rid))
                        stats["rows"] += 1
                    except sqlite3.IntegrityError:
                        _merge_into_existing(conn, t, c, cols, rid, new)
                        stats["rows"] += 1
                    except sqlite3.Error:
                        pass
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return stats


def _merge_into_existing(conn, table: str, col: str, cols: list,
                         rid: int, new_value: str) -> None:
    """归一后撞主键：把本行的时间并到已有行上，再删掉本行。

    时间必须取「本行自己的值」去比，不能从目标行再查一遍 —— 目标行就是
    即将被保留的那一行，用它自己的时间跟自己取 MAX 等于什么都没做，
    本行较大的 last_opened_at 会随 DELETE 一起丢掉（「最近打开」倒退）。
    """
    if "last_opened_at" in cols:
        try:
            mine = conn.execute(
                f'SELECT last_opened_at FROM "{table}" WHERE rowid=?', (rid,)).fetchone()
            if mine and mine[0] is not None:
                conn.execute(
                    f'UPDATE "{table}" SET last_opened_at='
                    f'MAX(COALESCE(last_opened_at,0), ?) WHERE "{col}"=?',
                    (mine[0], new_value))
        except sqlite3.Error:
            pass
    try:
        conn.execute(f'DELETE FROM "{table}" WHERE rowid=?', (rid,))
    except sqlite3.Error:
        pass


# ─────────────────────────── 组合入口 ───────────────────────────

def count_in_db(db_path: Path) -> int:
    """统计库里还有多少个 TEXT 值带着占位符（只读）。

    健康状态必然是 0：占位符只该出现在**包里**，一旦落进本机库就说明
    有一次拉取没做完本地化 —— 那是会把 {{WB:WS}} 当路径显示给用户的事故，
    doctor 要能当场发现。
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return 0
    n = 0
    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=20)
    except sqlite3.Error:
        return 0
    try:
        conn.execute("PRAGMA busy_timeout=20000")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            try:
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")')]
            except sqlite3.Error:
                continue
            for c in cols:
                try:
                    n += conn.execute(
                        f'SELECT COUNT(*) FROM "{t}" WHERE "{c}" LIKE \'%{{{{WB:%\''
                    ).fetchone()[0]
                except sqlite3.Error:
                    continue
    except sqlite3.Error:
        pass
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return n


def neutralize_tree(root: Path, mapping: list | None = None) -> dict:
    """打包侧：把 root 下所有文本与库内容里的本机路径换成占位符。"""
    mapping = mapping if mapping is not None else build_map(
        state.WORKSPACE_ROOT, state.WB_HOME, Path.home())
    if not mapping:
        return {"scanned": 0, "files": 0, "rows": 0}
    apply = _neutralizer(mapping)
    stats = rewrite_tree(root, apply)
    stats["rows"] = rewrite_db(Path(root) / "db" / "workbuddy.db", apply)["rows"]
    return stats


def localize_tree(root: Path, mapping: list | None = None) -> dict:
    """拉取侧：把 root 下所有文本与库内容里的占位符换回本机路径。"""
    mapping = mapping if mapping is not None else _local_map()
    if not mapping:
        return {"scanned": 0, "files": 0, "rows": 0}
    stats = rewrite_tree(root, lambda t: localize_text(t, mapping))
    stats["rows"] = rewrite_db(Path(root) / "db" / "workbuddy.db",
                               lambda v: localize_text(v, mapping))["rows"]
    return stats
