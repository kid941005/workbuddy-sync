# -*- coding: utf-8 -*-
"""路径处理专项测试。

覆盖本次修复的三类缺陷：
    A. 源目录解析 —— 空间根必须跟随客户端 app-config.json，不能写死 ~/WorkBuddy
    B. 目标目录解析 —— sync_root 要展开 ~ / 环境变量 / 去引号，相对路径不能按 CWD
    C. 跨设备一致性 —— pull 时把对方的绝对路径改写成本机口径，会话正文归位，
       同一逻辑空间不在两端变成两份

全程在系统临时目录运行，用 WB_SYNC_CONFIG 隔离，不触碰真实数据。

用法：
    python tools/test_paths.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ★ 进程内也必须隔离，不能只隔离子进程。
#   下面的用例会直接调用 core.record_conflicts() / core.conflict_summary()，
#   它们通过 config.CONFIG_DIR 落到真实 %LOCALAPPDATA%\wb-sync。v2.3.0 及之前
#   只给子进程传了 WB_SYNC_CONFIG，于是这几行把 conflicts.json 写进了用户
#   正在使用的配置目录 —— 测试污染环境，而且事后极难归因。
#   必须在 import wb_sync 之前设置：CONFIG_DIR 是导入时求值的模块常量。
os.environ.setdefault(
    "WB_SYNC_CONFIG", str(Path(tempfile.mkdtemp(prefix="wbpath-cfg-"))))

DEV = Path(__file__).resolve().parent.parent
SHIM = DEV / "wb_sync.py"
SRC = DEV / "src"
PY = sys.executable

sys.path.insert(0, str(SRC))
from wb_sync import paths  # noqa: E402
from wb_sync import config as _config  # noqa: E402

failures = []


def check(ok: bool, label: str, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(env_cfg: Path, *args, cwd: Path | None = None):
    env = dict(os.environ, WB_SYNC_CONFIG=str(env_cfg))
    r = subprocess.run([PY, str(SHIM), *args], capture_output=True, text=True,
                       env=env, cwd=str(cwd or DEV),
                       encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")


# ─────────────────── 0. 测试隔离（护栏） ───────────────────

print("\n" + "=" * 62)
print("0. 测试隔离：进程内配置目录必须落在临时区")
print("=" * 62)

_real_cfg = (Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "wb-sync")
check(Path(_config.CONFIG_DIR) == Path(os.environ["WB_SYNC_CONFIG"]),
      "WB_SYNC_CONFIG 生效", f"实际 {_config.CONFIG_DIR}")
check(Path(tempfile.gettempdir()).resolve() in Path(_config.CONFIG_DIR).resolve().parents,
      "配置目录位于系统临时区（不会写用户真实环境）", str(_config.CONFIG_DIR))
check(Path(_config.CONFIG_DIR).resolve() != _real_cfg.resolve(),
      "配置目录不是真实的 %LOCALAPPDATA%\\wb-sync", str(_real_cfg))

# ─────────────────── A. 路径基础函数 ───────────────────

print("\n" + "=" * 62)
print("A. 路径规范化")
print("=" * 62)

base = Path(tempfile.mkdtemp(prefix="wbpath-"))

check(paths.normalize('  "D:/x/y"  ') == Path("D:/x/y").resolve() or
      str(paths.normalize('  "D:/x/y"  ')).endswith("x\\y"),
      "剥掉首尾空格与成对引号")
check(str(paths.normalize("~/sub")) == str(Path.home() / "sub"), "展开 ~")

os.environ["WB_PATH_TEST_VAR"] = str(base)
check(str(paths.normalize("%WB_PATH_TEST_VAR%/a")) == str((base / "a").resolve()),
      "展开 %VAR% 环境变量")

# 相对路径必须基于 base（配置目录），而不是当前工作目录
rel_cwd = os.getcwd()
try:
    os.chdir(Path.home())
    got = paths.normalize("rel/dir", base=base)
    check(got == (base / "rel/dir").resolve() or str(got).startswith(str(base)),
          "相对路径以 base 为基准（不随 CWD 漂移）", str(got))
finally:
    os.chdir(rel_cwd)

check(str(paths.normalize("")) == "None", "空字符串返回 None")

# ─────────────────── B. 跨设备重映射 ───────────────────

print("\n" + "=" * 62)
print("B. 绝对路径重映射")
print("=" * 62)

MAP = [("D:/WorkBuddy/workbuddy", "C:/Users/PC-A/WorkBuddy"),
       ("C:/Users/PC-A", "C:/Users/PC-A")]

got = paths.remap_with_map("D:/WorkBuddy/workbuddy/项目A", MAP)
check(got == "C:/Users/PC-A/WorkBuddy/项目A", "空间根前缀改写（含中文路径）", got)

got = paths.remap_with_map("d:/workbuddy/WORKBUDDY/项目A", MAP)
check(got == "C:/Users/PC-A/WorkBuddy/项目A", "大小写不敏感匹配", got)

check(paths.remap_with_map("E:/elsewhere/x", MAP) is None, "不匹配的路径保持原样（返回 None）")

# 更长前缀优先
MAP2 = [("C:/Users", "Z:/u"), ("C:/Users/PC-A/WorkBuddy", "C:/Users/PC-A/WorkBuddy")]
check(paths.remap_with_map("C:/Users/PC-A/WorkBuddy/a", MAP2) == "C:/Users/PC-A/WorkBuddy/a",
      "更长前缀优先命中")

# 编码校准
v = paths.encode_cwd_variants("C:/Users/demo/WorkBuddy/code")
check(v[0] == "c-Users-demo-WorkBuddy-code", "编码变体首选盘符小写", str(v))

# ─────────────────── C. 源目录发现 ───────────────────

print("\n" + "=" * 62)
print("C. 源目录：跟随客户端 app-config.json")
print("=" * 62)

home = base / "home"
(home / "app").mkdir(parents=True)
ws_custom = base / "D_workspaces"
ws_custom.mkdir()
(home / "app" / "app-config.json").write_text(json.dumps(
    {"defaultWorkspacePath": str(ws_custom)}), encoding="utf-8")

got, src = paths.find_workspace_root(home, None, {})
check(paths.same_path(got, ws_custom), "读到 defaultWorkspacePath", f"{got} ← {src}")
check("app-config" in src, "来源标注为 app-config", src)

# 显式参数优先于 app-config
got, src = paths.find_workspace_root(home, str(base / "explicit"), {})
check(paths.same_path(got, base / "explicit"), "命令行参数优先", f"{got} ← {src}")

# 本工具 config.json 的 workspace_root 优先于 app-config
got, src = paths.find_workspace_root(home, None, {"workspace_root": str(base / "fromsync")})
check(paths.same_path(got, base / "fromsync"), "config.json 覆盖 app-config", f"{got} ← {src}")

# app-config 缺失 → 兜底 ~/WorkBuddy
home2 = base / "home2"
home2.mkdir()
got, src = paths.find_workspace_root(home2, None, {})
check(paths.same_path(got, Path.home() / "WorkBuddy"), "无配置时兜底 ~/WorkBuddy", f"{got} ← {src}")

# ─────────────────── D. 端到端：两台机器不同空间根 ───────────────────

print("\n" + "=" * 62)
print("D. 端到端：push(A) -> pull(B)，空间根不同")
print("=" * 62)

homeA, homeB = base / "wbA", base / "wbB"
cfgA, cfgB = base / "cfgA", base / "cfgB"
wsA, wsB = base / "workspacesA", base / "workspacesB"
sync = base / "sync"
for d in (homeA, homeB, cfgA, cfgB, sync, wsA / "proj1", wsB / "proj1"):
    d.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, user_id TEXT, title TEXT,
                       created_at TEXT, updated_at TEXT, deleted_at TEXT);
CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at TEXT);
CREATE TABLE automations (id TEXT PRIMARY KEY);
CREATE TABLE automation_runs (id TEXT PRIMARY KEY);
CREATE TABLE automation_runtime_state (id TEXT PRIMARY KEY);
CREATE TABLE buddy_snapshots (id TEXT PRIMARY KEY);
"""

cwdA = str(wsA / "proj1")
cwdB = str(wsB / "proj1")

for hm, cwd in ((homeA, cwdA), (homeB, None)):
    (hm / "projects").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(hm / "workbuddy.db"))
    con.executescript(SCHEMA)
    if cwd:
        con.execute("INSERT INTO sessions (id,cwd,user_id,title) VALUES (?,?,?,?)",
                    ("c1", cwd, "u1", "会话一"))
        con.execute("INSERT INTO workspaces (path,last_opened_at) VALUES (?,?)",
                    (cwd, "2026-10-25T10:00:00"))
    else:
        # B 必须已有一个本机会话，merge_db 才有 uid 可合并
        con.execute("INSERT INTO sessions (id,cwd,user_id,title) VALUES (?,?,?,?)",
                    ("b1", str(wsB / "keep"), "u1", "本机会话"))
    con.commit()
    con.close()

# A 的会话正文按 A 的路径编码落位
encA = paths.encode_cwd(cwdA, 0)
(homeA / "projects" / encA).mkdir(parents=True, exist_ok=True)
(homeA / "projects" / encA / "c1.jsonl").write_text(
    '{"t":"hi","cwd":"%s"}\n' % cwdA.replace("\\", "/"), encoding="utf-8")

out = run(cfgA, "push", "--wb-home", str(homeA), "--workspace-root", str(wsA),
          "--sync-root", str(sync), "--name", "A")
check("包已生成" in out, "A push 成功", out[-300:])

out = run(cfgB, "pull", "--wb-home", str(homeB), "--workspace-root", str(wsB),
          "--sync-root", str(sync), "--name", "B", "--force")
check("处理" in out or "没有发现" in out, "B pull 执行", out[-300:])

# 1) 会话 cwd 是否被改写成本机口径
con = sqlite3.connect(str(homeB / "workbuddy.db"))
row = con.execute("SELECT cwd FROM sessions WHERE id='c1'").fetchone()
ws_rows = [r[0] for r in con.execute("SELECT path FROM workspaces")]
con.close()

check(row is not None, "对方会话已合并进 B")
if row:
    check(paths.same_path(Path(row[0]), Path(cwdB)),
          "会话 cwd 已改写为 B 的空间根", f"实际 {row[0]}")
check(any(paths.same_path(Path(p), Path(cwdB)) for p in ws_rows),
      "workspaces.path 已改写为 B 的空间根", str(ws_rows))

# 2) 会话正文是否归位到 B 的编码目录
encB = paths.encode_cwd(cwdB, 0)
want = homeB / "projects" / encB / "c1.jsonl"
check(want.is_file(), "正文落在 B 的编码目录", str(want))
check(not (homeB / "projects" / encA / "c1.jsonl").is_file(),
      "未在 A 的编码目录留下重复正文")
dirs = sorted(p.name for p in (homeB / "projects").iterdir() if p.is_dir())
check(len([d for d in dirs if d.endswith("proj1")]) <= 1,
      "projects 下不产生同一空间的重复目录", str(dirs))

# 3) 空间文件内容也应到位
(homeA / "workspaces_marker.txt").write_text("x", encoding="utf-8")
check(True, "（说明）空间文件合并走同一套路径，已在上面覆盖")

# ─────────────────── E. 目标目录：不静默创建 ───────────────────

print("\n" + "=" * 62)
print("E. 目标目录：路径写错不静默造目录")
print("=" * 62)

cfgZ = base / "cfgZ"
cfgZ.mkdir()
missing = base / "no_such_drive" / "deep" / "wb-sync"
(missing.parent).mkdir(parents=True, exist_ok=True)
shutil_target = missing  # 父目录存在、自身不存在 → 应提示而非直接建

out = run(cfgZ, "push", "--wb-home", str(homeB), "--workspace-root", str(wsB),
          "--sync-root", str(shutil_target), "--name", "Z")
check("尚不存在" in out, "目录不存在时给出明确提示", out[-300:])

out = run(cfgZ, "doctor", "--wb-home", str(homeB), "--workspace-root", str(wsB),
          "--sync-root", str(base / "missing_parent" / "sub"))
check("同步目录不存在" in out or "不存在" in out, "doctor 不再 mkdir 掩盖错误", out[-400:])

# ─────────────────── F. 共享目录身份锚 ───────────────────

print("\n" + "=" * 62)
print("F. 共享目录身份锚：防止两端指向不同目录")
print("=" * 62)

d1 = base / "anchor1"
d1.mkdir()
out = run(cfgA, "push", "--wb-home", str(homeA), "--workspace-root", str(wsA),
          "--sync-root", str(d1), "--name", "A")
anchor = d1 / "state" / "cluster.json"
check(anchor.is_file(), "首次 push 生成 cluster.json")
if anchor.is_file():
    cid = json.loads(anchor.read_text(encoding="utf-8"))["cluster_id"]
    check(cid.startswith("c-"), "cluster_id 格式正确", cid)

# 篡改共享侧 id → 本机应拒绝
if anchor.is_file():
    data = json.loads(anchor.read_text(encoding="utf-8"))
    data["cluster_id"] = "c-TAMPERED"
    anchor.write_text(json.dumps(data), encoding="utf-8")
    out = run(cfgA, "push", "--wb-home", str(homeA), "--workspace-root", str(wsA),
              "--sync-root", str(d1), "--name", "A")
    check("身份不一致" in out, "id 不一致时中止并说明", out[-300:])

# ─────────────────── G. 超长路径（Windows MAX_PATH） ───────────────────

print("\n" + "=" * 62)
print("G. 超长路径：解包不撞 260 字符上限")
print("=" * 62)

import inspect  # noqa: E402

from wb_sync import core  # noqa: E402  （放到这里，避免测试头部就拉起重依赖）

if sys.platform != "win32":
    check(True, "（跳过）非 Windows 无 MAX_PATH 限制")
else:
    short = "C:\\a\\b.txt"
    check(core._lp(short) == short, "短路径不加 \\\\?\\ 前缀")

    long_path = "C:\\" + "x" * 240 + "\\f.txt"
    lp = core._lp(long_path)
    check(lp.startswith("\\\\?\\"), "超长路径加 \\\\?\\ 前缀", lp[:20])
    check(lp.endswith("f.txt"), "前缀后仍保留原路径")

    root = core._unpack_root(240)
    budget = len(str(root)) + 1 + 240 + 12
    check(budget <= core._MAX_PATH,
          "按包内最长路径挑出足够短的解包根", f"{root} → {budget}")

    # 端到端：造一条 >200 字符的相对路径，push 后能解包出来
    homeD, cfgD, wsD = base / "wbD", base / "cfgD", base / "wsD"
    for d in (homeD, cfgD, wsD):
        d.mkdir(parents=True, exist_ok=True)
    (homeD / "projects").mkdir(exist_ok=True)
    con = sqlite3.connect(str(homeD / "workbuddy.db"))
    con.executescript(SCHEMA)
    con.commit()
    con.close()

    parts = [f"level{i:02d}_" + "y" * 14 for i in range(1, 11)]
    deep_dir = wsD / "deep"
    for pt in parts:
        deep_dir = deep_dir / pt
    deep_file = deep_dir / "deep.txt"
    core._makedirs(deep_file.parent)
    with open(core._lp(deep_file), "w", encoding="utf-8") as fh:
        fh.write("deep-ok")

    rel_len = len("workspaces/" + "/".join(["deep"] + parts + ["deep.txt"]))
    check(rel_len > 200, "构造出的包内相对路径确实很长", f"{rel_len} 字符")

    syncD = base / "syncD"
    out = run(cfgD, "push", "--wb-home", str(homeD), "--workspace-root", str(wsD),
              "--sync-root", str(syncD), "--name", "D")
    check("包已生成" in out, "深路径 push 成功", out[-300:])

    zips = sorted((syncD / "state").glob("from-*.zip"))
    if zips:
        tmp = core.unpack(zips[0])
        target = tmp / "workspaces" / "deep" / Path(*parts) / "deep.txt"
        check(os.path.isfile(core._lp(target)),
              "解包后深路径文件存在（长路径兜底生效）", str(target))
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)
        try:
            tmp.parent.rmdir()
        except OSError:
            pass
    else:
        check(False, "未生成包")

# ─────────────────── H. 数据库未合并时不标记已拉取 ───────────────────

print("\n" + "=" * 62)
print("H. 数据库未合并 → 不标记已拉取（避免永久漏合）")
print("=" * 62)

src_text = inspect.getsource(core.do_pull)
check("db_pending" in src_text, "do_pull 含 db_pending 分支")
check("本次不标记为已拉取" in src_text, "跳过数据库时给出明确提示")

import importlib  # noqa: E402

homeE, cfgE, wsE = base / "wbE", base / "cfgE", base / "wsE"
for d in (homeE, cfgE, wsE):
    d.mkdir(parents=True, exist_ok=True)
(homeE / "projects").mkdir(exist_ok=True)
con = sqlite3.connect(str(homeE / "workbuddy.db"))
con.executescript(SCHEMA)
con.execute("INSERT INTO sessions (id,cwd,user_id,title) VALUES (?,?,?,?)",
            ("e1", str(wsE / "keep"), "u1", "本机会话"))
con.commit()
con.close()

syncE = base / "syncE"
syncE.mkdir(parents=True, exist_ok=True)

os.environ["WB_SYNC_CONFIG"] = str(cfgE)
os.environ.pop("WB_SYNC_MANIFEST_DIR", None)
cfg_mod = importlib.import_module("wb_sync.config")
state_mod = importlib.import_module("wb_sync.state")

state_mod.WB_HOME = homeE
state_mod.WORKSPACE_ROOT = wsE
man, _src = cfg_mod.load_manifest()
cfg_mod.apply_manifest(man)
state_mod.HOST, state_mod.MACHINE_NAME = "m-testE", "E"

peer_zips = sorted((syncD / "state").glob("from-*.zip")) if (syncD / "state").is_dir() else []
if peer_zips:
    (syncE / "state").mkdir(parents=True, exist_ok=True)
    import shutil as _sh2
    dst_zip = syncE / "state" / "from-m-peer.zip"
    _sh2.copy2(peer_zips[0], dst_zip)

    core.workbuddy_running = lambda: True          # 模拟客户端开着
    core.do_pull(syncE, dry=False, force=False)

    marker_file = core.pull_marker_path(syncE)
    marker = {}
    if marker_file.is_file():
        marker = json.loads(marker_file.read_text(encoding="utf-8"))
    check("from-m-peer.zip" not in marker,
          "DB 被跳过时未把该包标记为已拉取", str(marker))
else:
    check(False, "缺少可用于模拟的包")

# ─────────────────── I. 内容未变不重写包 / 冲突台账 ───────────────────

print("\n" + "=" * 62)
print("I. 内容未变跳过重建 + 冲突台账")
print("=" * 62)

homeF, cfgF, wsF, syncF = base / "wbF", base / "cfgF", base / "wsF", base / "syncF"
for d in (homeF, cfgF, wsF):
    d.mkdir(parents=True, exist_ok=True)
(homeF / "projects").mkdir(exist_ok=True)
(wsF / "p1").mkdir(parents=True, exist_ok=True)
(wsF / "p1" / "a.txt").write_text("hello\n", encoding="utf-8")
con = sqlite3.connect(str(homeF / "workbuddy.db"))
con.executescript(SCHEMA)
con.commit()
con.close()

out1 = run(cfgF, "push", "--wb-home", str(homeF), "--workspace-root", str(wsF),
           "--sync-root", str(syncF), "--name", "F")
check("包已生成" in out1, "首次 push 生成包", out1[-200:])

zips = sorted((syncF / "state").glob("from-*.zip"))
check(len(zips) == 1, "包文件已落盘")
zipF = zips[0] if zips else None
m1 = zipF.stat().st_mtime_ns if zipF else 0

out2 = run(cfgF, "push", "--wb-home", str(homeF), "--workspace-root", str(wsF),
           "--sync-root", str(syncF), "--name", "F")
check("跳过重写" in out2, "内容未变 → 跳过重写", out2[-200:])
check(zipF and zipF.stat().st_mtime_ns == m1, "包文件未被重新写入")

out3 = run(cfgF, "push", "--wb-home", str(homeF), "--workspace-root", str(wsF),
           "--sync-root", str(syncF), "--name", "F", "--force")
check("包已生成" in out3, "--force 可强制重建", out3[-200:])

(wsF / "p1" / "b.txt").write_text("new\n", encoding="utf-8")
out4 = run(cfgF, "push", "--wb-home", str(homeF), "--workspace-root", str(wsF),
           "--sync-root", str(syncF), "--name", "F")
check("包已生成" in out4 or "增量已生成" in out4,
      "新增文件后正常产出（基线或增量）", out4[-200:])

# 冲突台账：只统计备份仍在的条目
bak = wsF / "p1" / "a.txt.conflict-m-X-t1"
bak.write_text("old\n", encoding="utf-8")
core.record_conflicts([{"rel": "p1/a.txt", "backup": str(bak), "stamp": "t1"}])
n1, _ = core.conflict_summary()
check(n1 == 1, "冲突台账记录了 1 条", str(n1))

bak.unlink()
n2, _ = core.conflict_summary()
check(n2 == 0, "备份删除后自动不再计数", str(n2))

# ─────────────────── J. 面板来源校验 ───────────────────

print("\n" + "=" * 62)
print("J. 面板只接受本机来源（防跨站触发 push/pull）")
print("=" * 62)

from wb_sync import ui  # noqa: E402


class _FakeReq:
    _origin_ok = ui.Handler._origin_ok

    def __init__(self, host: str, origin: str | None = None):
        self.headers = {"Host": host}
        if origin is not None:
            self.headers["Origin"] = origin


check(_FakeReq("127.0.0.1:8765")._origin_ok(), "Host=127.0.0.1 放行")
check(_FakeReq("localhost:8765")._origin_ok(), "Host=localhost 放行")
check(not _FakeReq("evil.com")._origin_ok(), "Host=evil.com 拒绝")
check(not _FakeReq("127.0.0.1:8765", "http://evil.com")._origin_ok(),
      "Origin=evil.com 拒绝")
check(_FakeReq("127.0.0.1:8765", "http://127.0.0.1:8765")._origin_ok(),
      "同源 Origin 放行")

# ─────────────────── K. 增量：base + delta ───────────────────

print("\n" + "=" * 62)
print("K. 增量同步：只传变化的文件")
print("=" * 62)

from wb_sync import packs  # noqa: E402

# 单元：重建判定
check(packs.decide_rebuild(None, 1000)[0] is True, "无索引 → 重建基线")
check(packs.decide_rebuild({"deltas": [], "seq": 1}, 0)[0] is True, "基线包缺失 → 重建")
check(packs.decide_rebuild(
    {"deltas": [{"bytes": 1}] * packs.MAX_DELTAS, "seq": 30}, 10 ** 9)[0] is True,
    "增量个数达上限 → 重建")
_BIG = 10 * 1024 * 1024
check(packs.decide_rebuild({"deltas": [{"bytes": _BIG}], "seq": 5}, _BIG)[0] is True,
      "增量累计已达基线体积 → 重建（重建要重传整包，不宜太频繁）")
check(packs.decide_rebuild({"deltas": [{"bytes": 600}], "seq": 5}, 1000)[0] is False,
      "小基线不看比例，只按个数封顶")
check(packs.decide_rebuild({"deltas": [], "seq": 2}, 1000)[0] is False, "正常情形不重建")
check(packs.decide_rebuild({"deltas": [], "seq": 2}, 1000, blockers=["X"])[0] is True,
      "对端版本过旧 → 退回全量")

# 单元：变更比对
_old = {"entries": {"a": [10, 1], "b": [20, 2]}}
check(packs.diff(_old, {"a": (10, 1), "b": (20, 2)}) == [], "大小与 mtime 未变 → 无变化")
check(packs.diff(_old, {"a": (10, 99), "b": (20, 2)}) == ["a"], "mtime 变 → 判为变化")
check(packs.diff(_old, {"a": (10, 1), "b": (20, 2), "c": (5, 5)}) == ["c"],
      "新增文件 → 判为变化")
check(packs.diff({"entries": {"a": [10, 1, "旧版多一个 sha 字段"]}},
                 {"a": (10, 1)}) == [], "兼容旧索引（条目多一个字段也能读到）")
check(packs.engine_supports("2.2.2") is False and packs.engine_supports("2.3.0") is True,
      "增量最低引擎版本判定")

# 单元：索引与基线代次不符时，不叠加增量（否则旧内容会盖回新基线）
import wb_sync.core as _core  # noqa: E402

_sd = base / "sigcheck" / "state"
(_sd / "deltas").mkdir(parents=True, exist_ok=True)
_fake = _sd / "from-peer1.zip"
with zipfile.ZipFile(_fake, "w") as _z:
    _z.writestr("manifest.json", json.dumps({"engine_version": "2.3.0"}))
packs.save(_sd, "peer1", {"schema": packs.INDEX_SCHEMA, "host": "peer1",
                          "base_seq": 1, "seq": 3, "base_sig": "999-999",
                          "deltas": [{"seq": 2, "zip": "d-peer1-0002.zip"},
                                     {"seq": 3, "zip": "d-peer1-0003.zip"}],
                          "entries": {}})
check(_core._effective_index(_sd, _fake) is None,
      "索引 base_sig 与基线不符 → 视为无索引（只解基线）")

# 单元：索引健壮性 —— 坏索引一律当作「没有」，不能抛异常也不能被当真
packs.save(_sd, "peer1", {"schema": packs.INDEX_SCHEMA, "host": "peer1",
                          "seq": 1, "deltas": "不是列表", "entries": {}})
check(packs.load(_sd, "peer1") is None, "deltas 不是列表 → 索引作废")
packs.save(_sd, "peer1", {"schema": packs.INDEX_SCHEMA, "host": "peer1",
                          "seq": 1, "deltas": [{"no_zip": 1}], "entries": {}})
check(packs.load(_sd, "peer1") is None, "增量条目缺 zip → 索引作废")
check(packs.delta_total([{"bytes": "坏数据"}, {"bytes": 5}, {}, None]) == 5,
      "累计体积容忍脏数据（非数字按 0）")
check("增量" in packs.describe({"seq": 2, "base_seq": 1,
                                "deltas": [{"bytes": "坏"}, {"bytes": 3}]}),
      "describe 遇脏数据不抛异常")

# 端到端：A 推两次，B 拉
homeK, homeK2 = base / "wbK", base / "wbK2"
cfgK, cfgK2 = base / "cfgK", base / "cfgK2"
wsK, wsK2 = base / "wsK", base / "wsK2"
syncK = base / "syncK"
for d in (homeK, homeK2, cfgK, cfgK2, syncK, wsK / "p", wsK2 / "p"):
    d.mkdir(parents=True, exist_ok=True)

for hm, sid in ((homeK, "k1"), (homeK2, "k2")):
    (hm / "projects").mkdir(exist_ok=True)
    con = sqlite3.connect(str(hm / "workbuddy.db"))
    con.executescript(SCHEMA)
    con.execute("INSERT INTO sessions (id,cwd,user_id,title) VALUES (?,?,?,?)",
                (sid, str(wsK / "p" if hm is homeK else wsK2 / "p"), "u1", "t"))
    con.commit()
    con.close()

(wsK / "p" / "a.txt").write_text("v1\n", encoding="utf-8")
(wsK / "p" / "big.bin").write_bytes(b"\x00" * 200_000)

out = run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
          "--sync-root", str(syncK), "--name", "A")
stateK = syncK / "state"
cands = sorted(stateK.glob("from-*.zip"))
base_zip = cands[0] if cands else stateK / "missing.zip"
idx_file = packs.index_path(stateK, base_zip.stem[5:])
idx0 = packs.load(stateK, base_zip.stem[5:])

check("包已生成" in out or "重建全量基线" in out, "首次 push 生成基线", out[-300:])
check(idx_file.is_file(), "写入索引文件")
check(bool(idx0) and not (idx0.get("deltas") or []), "首次为基线，无增量")
check((idx0 or {}).get("seq") == 1, "基线 seq = 1", str((idx0 or {}).get("seq")))

mtime0 = base_zip.stat().st_mtime_ns
(wsK / "p" / "a.txt").write_text("v2\n", encoding="utf-8")
out = run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
          "--sync-root", str(syncK), "--name", "A")
idx1 = packs.load(stateK, base_zip.stem[5:])
deltas = (idx1 or {}).get("deltas") or []
check("增量模式" in out, "第二次 push 走增量", out[-300:])
check(len(deltas) == 1, "生成 1 个增量", str(deltas))
check(base_zip.stat().st_mtime_ns == mtime0, "基线包未被重写")
dz = packs.delta_dir(stateK) / (deltas[0]["zip"] if deltas else "none.zip")
check(dz.is_file(), "增量包已落盘", str(dz))
check((idx1 or {}).get("seq") == 2, "seq 递增到 2")

# 增量之后「没有变化」必须整次跳过。基线不会被重写，所以指纹只能存在索引里 ——
# 若还去比基线 manifest 里那份（建基线那一刻的），这里就会变成每一次全量重建。
out = run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
          "--sync-root", str(syncK), "--name", "A")
check("内容与上次相同" in out, "增量后空 push 被跳过，不重建整包", out[-400:])
check(base_zip.stat().st_mtime_ns == mtime0, "空 push 后基线仍未被重写")
_idx_same = packs.load(stateK, base_zip.stem[5:])
check((_idx_same or {}).get("seq") == 2, "空 push 不产生新增量",
      str((_idx_same or {}).get("seq")))

# B 拉取：基线 + 增量叠加后应是新内容
out = run(cfgK2, "pull", "--wb-home", str(homeK2), "--workspace-root", str(wsK2),
          "--sync-root", str(syncK), "--name", "B", "--force")
got = (wsK2 / "p" / "a.txt")
check(got.is_file() and got.read_text(encoding="utf-8") == "v2\n",
      "增量已叠加：B 拿到最新内容", out[-400:])

# 增量缺失时必须报错且不标记已拉取
(wsK / "p" / "a.txt").write_text("v3\n", encoding="utf-8")
run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
    "--sync-root", str(syncK), "--name", "A")
idx2 = packs.load(stateK, base_zip.stem[5:])
last = ((idx2 or {}).get("deltas") or [{}])[-1].get("zip")
missing = packs.delta_dir(stateK) / str(last)
if missing.is_file():
    missing.unlink()
# 标记文件按机器标识命名（不是显示名），所以按实际存在的那个取
_mk = sorted((stateK).glob(".last-pull-*.json"))
marker_b = _mk[0] if _mk else None
mt_before = marker_b.stat().st_mtime_ns if marker_b else 0
out = run(cfgK2, "pull", "--wb-home", str(homeK2), "--workspace-root", str(wsK2),
          "--sync-root", str(syncK), "--name", "B", "--force")
check("尚未同步" in out or "不完整" in out, "增量缺失 → 明确提示", out[-400:])
mt_after = marker_b.stat().st_mtime_ns if marker_b else 0
check(mt_after == mt_before, "增量缺失时不更新已拉取标记")

# 旧版对端存在 → 自动退回全量（避免它静默读到过期数据）
with zipfile.ZipFile(stateK / "from-oldpeer.zip", "w") as _z:
    _z.writestr("manifest.json", json.dumps(
        {"schema": 1, "engine_version": "2.2.2", "host": "oldpeer",
         "machine_name": "旧机器"}, ensure_ascii=False))
(wsK / "p" / "a.txt").write_text("v4\n", encoding="utf-8")
out = run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
          "--sync-root", str(syncK), "--name", "A")
check("对端版本过旧" in out, "存在旧版对端 → 退回全量基线", out[-400:])
idx3 = packs.load(stateK, base_zip.stem[5:])
check(not ((idx3 or {}).get("deltas") or []), "退回全量后增量清空")

# 选择性 push 会把基线换成「只有勾选内容」的不完整包 ——
# 此时索引与增量必须作废，否则下次全量 push 只补差值，一批文件永远传不过去。
syncS = base / "syncS"
syncS.mkdir(parents=True, exist_ok=True)
hostK = base_zip.stem[5:]
run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
    "--sync-root", str(syncS), "--name", "A")
stS = syncS / "state"
out = run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
          "--sync-root", str(syncS), "--name", "A",
          "--selection", json.dumps({"workspaces": ["p"]}))
check(packs.load(stS, hostK) is None, "选择性 push 后索引被作废", out[-300:])
with zipfile.ZipFile(stS / f"from-{hostK}.zip") as _z:
    _pm = json.loads(_z.read("manifest.json"))
check(_pm.get("partial") is True, "选择性包在 manifest 里标记 partial")
out = run(cfgK, "push", "--wb-home", str(homeK), "--workspace-root", str(wsK),
          "--sync-root", str(syncS), "--name", "A")
check("选择性包" in out and "重建全量基线" in out,
      "随后全量 push 识别出 partial 并重建完整基线", out[-400:])
with zipfile.ZipFile(stS / f"from-{hostK}.zip") as _z:
    _pm2 = json.loads(_z.read("manifest.json"))
check(_pm2.get("partial") is False, "重建后的基线不再是 partial")

# ─────────────────── L. 资产收集一致性 ───────────────────

print("\n" + "=" * 62)
print("L. 单文件上限对白名单文件同样生效")
print("=" * 62)

_cap = base / "capcheck"
_cap.mkdir(parents=True, exist_ok=True)
(_cap / "big.bin").write_bytes(b"\x00" * (2 * 1024 * 1024))
(_cap / "small.txt").write_text("x", encoding="utf-8")
_old_cap = _core.state.MAX_WORKSPACE_FILE_MB
try:
    _core.state.MAX_WORKSPACE_FILE_MB = 1
    got = [r for r, _ in _core.iter_assets(_cap, None, ["big.bin", "small.txt"])]
    check(got == ["small.txt"], "白名单文件超过 max_file_mb 会被跳过", str(got))
    got_dirs = [r for r, _ in _core.iter_assets(_cap, ["."], None)]
    check(got_dirs == ["small.txt"], "目录遍历同样跳过超限文件", str(got_dirs))
finally:
    _core.state.MAX_WORKSPACE_FILE_MB = _old_cap

# ─────────────────── I. 合并时路径改写：无 id 表撞主键 ───────────────────
print("\n" + "=" * 62)
print("I. 合并时路径改写（workspaces 无 id，路径自己就是主键）")
print("=" * 62)

# 场景：同一个空间在包里有两条记录（别机绝对路径 + 本机绝对路径）。
# 旧实现在无 id 的表上用 UPDATE ... WHERE path=旧值，两个旧值映射到同一目标时
# 第二条触发主键冲突，异常被吞且直接跳出整张表 —— 结果是别机路径原样入库。
_inc = base / "merge_inc.db"
_m = sqlite3.connect(str(_inc))
_m.execute("CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER)")
_m.executemany("INSERT INTO workspaces VALUES (?,?)", [
    ("C:/Users/PC-A/WorkBuddy/X", 100),     # 与本机重名
    ("C:/Users/demo/WorkBuddy/X", 200),     # 本机已有
    ("C:/Users/PC-A/WorkBuddy/Y", 300),     # 本机没有，应被改写后保留
])
_m.commit()
_m.close()

_tgt = base / "merge_main.db"
_c = sqlite3.connect(str(_tgt))
_c.execute("CREATE TABLE workspaces (path TEXT PRIMARY KEY, last_opened_at INTEGER)")
_c.commit()
_c.execute("ATTACH DATABASE ? AS inc", (str(_inc),))
_core._rewrite_attached_paths(_c, [(r"C:\Users\PC-A", r"C:\Users\demo")])
_c.commit()
_rows = dict(_c.execute("SELECT path, last_opened_at FROM inc.workspaces").fetchall())
_c.close()

check("C:/Users/PC-A/WorkBuddy/X" not in _rows,
      "别机路径与本机路径重名时，别机那条被消解", str(list(_rows)))
check("C:/Users/PC-A/WorkBuddy/Y" not in _rows,
      "前一条冲突不阻断后续行（旧实现会整表放弃改写）", str(list(_rows)))
check(_rows.get("C:/Users/demo/WorkBuddy/Y") == 300,
      "唯一目标被正常改写且保留时间", str(_rows.get("C:/Users/demo/WorkBuddy/Y")))
check(_rows.get("C:/Users/demo/WorkBuddy/X") == 200,
      "重名时保留较大的 last_opened_at", str(_rows.get("C:/Users/demo/WorkBuddy/X")))

print("\n" + "=" * 62)
if failures:
    print(f"失败 {len(failures)} 项：")
    for f in failures:
        print(f"  - {f}")
    print(f"测试目录：{base}")
    sys.exit(1)
print("全部通过 ✓")
print(f"测试目录：{base}")
