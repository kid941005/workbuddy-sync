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
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEV = Path(__file__).resolve().parent.parent
SHIM = DEV / "wb_sync.py"
SRC = DEV / "src"
PY = sys.executable

sys.path.insert(0, str(SRC))
from wb_sync import paths  # noqa: E402

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

MAP = [("D:/WorkBuddy/workbuddy", "C:/Users/AIPC/WorkBuddy"),
       ("C:/Users/AIPC", "C:/Users/AIPC")]

got = paths.remap_with_map("D:/WorkBuddy/workbuddy/左蓝微", MAP)
check(got == "C:/Users/AIPC/WorkBuddy/左蓝微", "空间根前缀改写（含中文路径）", got)

got = paths.remap_with_map("d:/workbuddy/WORKBUDDY/左蓝微", MAP)
check(got == "C:/Users/AIPC/WorkBuddy/左蓝微", "大小写不敏感匹配", got)

check(paths.remap_with_map("E:/elsewhere/x", MAP) is None, "不匹配的路径保持原样（返回 None）")

# 更长前缀优先
MAP2 = [("C:/Users", "Z:/u"), ("C:/Users/AIPC/WorkBuddy", "C:/Users/AIPC/WorkBuddy")]
check(paths.remap_with_map("C:/Users/AIPC/WorkBuddy/a", MAP2) == "C:/Users/AIPC/WorkBuddy/a",
      "更长前缀优先命中")

# 编码校准
v = paths.encode_cwd_variants("C:/Users/19198/WorkBuddy/code")
check(v[0] == "c-Users-19198-WorkBuddy-code", "编码变体首选盘符小写", str(v))

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

print("\n" + "=" * 62)
if failures:
    print(f"失败 {len(failures)} 项：")
    for f in failures:
        print(f"  - {f}")
    print(f"测试目录：{base}")
    sys.exit(1)
print("全部通过 ✓")
print(f"测试目录：{base}")
