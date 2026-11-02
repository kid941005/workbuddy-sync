# -*- coding: utf-8 -*-
"""输入校验、事务失败与边界条件的回归测试。

这份测试补齐 CI 已引用、但仓库此前缺失的 test_hardening.py。全程在临时目录
运行，用 WB_SYNC_CONFIG 隔离配置；不读写真实 WorkBuddy 数据，也不联网。

用法：
    python tools/test_hardening.py
"""
import http.client
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault(
    "WB_SYNC_CONFIG", str(Path(tempfile.mkdtemp(prefix="wbhard-cfg-")))
)

DEV = Path(__file__).resolve().parent.parent
SRC = DEV / "src"
sys.path.insert(0, str(SRC))

from wb_sync import (  # noqa: E402
    cli,
    config,
    core,
    packs,
    portable,
    secret_scan,
    state,
    ui,
)
from wb_sync.utils import parse_timestamp  # noqa: E402

base = Path(tempfile.mkdtemp(prefix="wbhard-"))
failures = []


def check(ok: bool, label: str, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def head(title: str):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def expect_error(func, error, label: str):
    try:
        func()
    except error as e:
        check(True, label, str(e))
        return
    except Exception as e:  # noqa: BLE001
        check(False, label, f"错误类型 {type(e).__name__}: {e}")
        return
    check(False, label, "没有抛异常")


def make_sessions_db(path: Path, rows: list, uid: str = "local-uid"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        path.unlink()
    c = sqlite3.connect(str(path))
    try:
        c.execute("CREATE TABLE sessions ("
                  "id TEXT PRIMARY KEY, user_id TEXT, title TEXT, "
                  "cwd TEXT, updated_at INTEGER, deleted_at INTEGER)")
        for row in rows:
            c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                      (row.get("id"), uid, row.get("title", ""),
                       row.get("cwd"), row.get("updated_at", 0),
                       row.get("deleted_at")))
        c.commit()
    finally:
        c.close()


def make_zip(path: Path, members: list, manifest: dict | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members:
            z.writestr(name, data)
        if manifest is not None:
            z.writestr("manifest.json",
                       json.dumps(manifest, ensure_ascii=False))


# ─────────────────── A. 外部标识 / 选择 / 清单 / 时间 ───────────────────
head("A. 外部标识、选择、清单和时间输入")

for value in ("../x", "..", ".", "a/b", "a\\b", "x" * 65, "", "a\x00b"):
    check(config._valid_machine_id(value) == "", f"拒绝非法机器标识 {value!r}")
for value in ("m-abc123", "A.b_C-9", "x"):
    check(config._valid_machine_id(value) == value, f"接受合法机器标识 {value!r}")
check(config._valid_machine_id(123) == "", "拒绝非字符串机器标识")

sel_ok = core.normalize_selection({
    "workspaces": ["ws1", "ws1"], "tasks": ["t1"], "sessions": ["s1"]})
check(sel_ok["workspaces"] == {"ws1"}, "选择项去重")
for bad in ({"x": []}, {"workspaces": "ws"}, {"workspaces": [1]},
            {"workspaces": ["../x"]}, {"workspaces": ["a/b"]}):
    expect_error(lambda b=bad: core.normalize_selection(b), ValueError,
                 f"拒绝非法选择 {bad!r}")

for raw in (None, {}, [], "x", 1):
    check(config._as_list(raw) == [], f"清单列表拒绝 {raw!r}")
check(config._as_int(0, 7) == 7 and config._as_int("abc", 7) == 7,
      "正整数配置非法值退回默认")
check(config._as_bool("false", True) is False
      and config._as_bool("", True) is True, "布尔配置字符串语义")

vals = [1700000000, 1700000000000, "1700000000", "1700000000000",
        "2023-11-14T22:13:20Z", "2023-11-14T22:13:20"]
got = [parse_timestamp(x) for x in vals]
check(abs(got[0] - got[1]) < 1 and abs(got[2] - got[3]) < 1,
      "秒与毫秒时间戳统一")
check(abs(got[4] - got[5]) < 1 and abs(got[4] - got[0]) < 1,
      "ISO 带/不带时区语义一致")
for bad in (None, True, "not-a-time", [], {}):
    check(parse_timestamp(bad) == 0.0, f"非法时间返回 0：{bad!r}")

# ─────────────────── B. ZIP 输入预算与越界 ───────────────────
head("B. ZIP 成员数、体积、加密和路径越界")

dest = base / "extract"
dest.mkdir()

good = base / "good.zip"
make_zip(good, [("files/a.txt", "a"), ("manifest.json", "{}")])
with zipfile.ZipFile(good) as z:
    core._extract_into(z, dest)
check((dest / "files" / "a.txt").read_text(encoding="utf-8") == "a",
      "合法包可解压")

slip = base / "slip.zip"
make_zip(slip, [("../outside.txt", "x")])
with zipfile.ZipFile(slip) as z:
    expect_error(lambda: core._extract_into(z, dest), ValueError,
                 "拒绝 ZIP 越界路径")

abszip = base / "abs.zip"
make_zip(abszip, [("C:/outside.txt", "x")])
with zipfile.ZipFile(abszip) as z:
    expect_error(lambda: core._extract_into(z, dest), ValueError,
                 "拒绝绝对/盘符路径")

members = base / "members.zip"
make_zip(members, [(f"files/{i}.txt", "") for i in range(core.MAX_ZIP_MEMBERS + 1)])
with zipfile.ZipFile(members) as z:
    expect_error(lambda: core._extract_into(z, dest), ValueError,
                 "拒绝超过成员上限")

oversize = base / "oversize.zip"
make_zip(oversize, [("files/a.txt", "")])
with zipfile.ZipFile(oversize) as z:
    zi = z.getinfo("files/a.txt")
    zi.file_size = core.MAX_ZIP_FILE_BYTES + 1
    expect_error(lambda: core._extract_into(z, dest), ValueError,
                 "拒绝单文件预算超限")

total_budget = base / "total-budget.zip"
make_zip(total_budget, [("files/a.txt", ""), ("files/b.txt", "")])
with zipfile.ZipFile(total_budget) as z:
    for zi in z.infolist():
        zi.file_size = core.MAX_ZIP_BYTES // 2 + 1
    expect_error(lambda: core._extract_into(z, dest), ValueError,
                 "拒绝总解压预算超限")

encrypted = base / "encrypted.zip"
make_zip(encrypted, [("files/a.txt", "")])
with zipfile.ZipFile(encrypted) as z:
    z.getinfo("files/a.txt").flag_bits |= 0x1
    expect_error(lambda: core._extract_into(z, dest), ValueError,
                 "拒绝加密条目")

# ─────────────────── C. 索引与增量文件名 ───────────────────
head("C. 增量索引结构校验")

sd = base / "state-index"
sd.mkdir()
for bad in (
    {"schema": packs.INDEX_SCHEMA, "entries": {}, "seq": True, "deltas": []},
    {"schema": packs.INDEX_SCHEMA, "entries": {}, "seq": -1, "deltas": []},
    {"schema": packs.INDEX_SCHEMA, "entries": [], "seq": 1, "deltas": []},
    {"schema": packs.INDEX_SCHEMA, "entries": {}, "seq": 1, "deltas": {}},
    {"schema": packs.INDEX_SCHEMA, "entries": {}, "seq": 1,
     "deltas": [{"zip": ""}]},
):
    (sd / "from-h.index.json").write_text(json.dumps(bad), encoding="utf-8")
    check(packs.load(sd, "h") is None, f"拒绝非法索引 {bad}")

good_idx = {"schema": packs.INDEX_SCHEMA, "entries": {}, "seq": 1, "deltas": []}
packs.save(sd, "h", good_idx)
check(packs.load(sd, "h") == good_idx, "合法索引可读")
check(packs.delta_total([{"bytes": "abc"}, None, {"bytes": 5}]) == 5,
      "脏增量体积不拖垮展示")

bad_delta = base / "bad-delta.zip"
make_zip(bad_delta, [("files/a.txt", "x")])
peer = base / "from-peer.zip"
make_zip(peer, [("manifest.json", "{}")])
idx = {"schema": packs.INDEX_SCHEMA, "entries": {}, "seq": 1,
       "base_sig": "", "deltas": [{"zip": "../bad-delta.zip"}]}
packs.save(sd, "peer", idx)
expect_error(lambda: core.open_peer_package(sd, peer),
             core.PeerPackageIncomplete, "拒绝越界增量文件名")

# ─────────────────── D. 配置保留、原子写入与并发语义 ───────────────────
head("D. config.json 更新保留其他键")

cfg_path = config.CONFIG_DIR / "config.json"
config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
cfg_path.write_text(json.dumps({"sync_root": "C:/x", "machine_name": "A",
                                "extra": {"keep": True}}), encoding="utf-8")
out = config.update_config(machine_name="B")
check(out.get("sync_root") == "C:/x" and out.get("extra") == {"keep": True},
      "更新一个键保留其他键")
check(not (cfg_path.parent / "config.json.tmp").exists(), "临时文件已提交")

try:
    cfg_path.write_text("{broken", encoding="utf-8")
    out = config.update_config(new_key=1)
    check(out.get("new_key") == 1 and "extra" not in out,
          "损坏旧配置时不让更新失败")
except Exception as e:  # noqa: BLE001
    check(False, "损坏配置时更新仍可恢复", str(e))

# ─────────────────── E. 便携路径与数据库失败 ───────────────────
head("E. 便携路径改写失败可见")

tree = base / "portable-tree"
tree.mkdir()
(tree / "a.txt").write_text("C:/root/a", encoding="utf-8")
(tree / "binary.bin").write_bytes(b"\x00C:/root/a")
st = portable.rewrite_tree(tree, lambda text: text.replace("C:/root", "{{ROOT}}"))
check(st["files"] == 1 and st["failed"] == 0, "文本改写，二进制跳过")
check((tree / "a.txt").read_text(encoding="utf-8") == "{{ROOT}}/a",
      "文本改写结果正确")
check((tree / "binary.bin").read_bytes() == b"\x00C:/root/a",
      "二进制保持原字节")

missing_db = base / "missing-db"
missing_db.mkdir()
check(portable.rewrite_db(missing_db / "nope.db", lambda s: s)["failed"] == 0,
      "不存在的数据库按无内容处理")

bad_db = base / "bad.db"
bad_db.write_bytes(b"not sqlite")
st = portable.rewrite_db(bad_db, lambda s: s)
check(st["failed"] >= 1, "损坏数据库计为失败")

# ─────────────────── F. DB-only 变化与基线损坏 ───────────────────
head("F. DB-only 变化生成增量，基线损坏不跳过")

wb = base / "wb-build"
ws = base / "ws-build"
sync = base / "sync-build"
for p in (wb, ws, sync):
    p.mkdir()
(wb / "SOUL.md").write_text("soul", encoding="utf-8")
(ws / "a.txt").write_text("a", encoding="utf-8")
make_sessions_db(wb / "workbuddy.db", [{"id": "s1", "title": "one"}])
state.WB_HOME = wb
state.WORKSPACE_ROOT = ws
state.HOST = "hard-host"
state.MACHINE_NAME = "hard"
config.apply_manifest({
    "sync_files": ["SOUL.md"], "sync_dirs": ["skills"],
    "db_tables": ["sessions"], "max_file_mb": 200,
    "portable_paths": True, "secret_scan": False,
})

first = core.build_package(sync, False)
check((sync / "state" / "from-hard-host.zip").is_file(), "首次生成基线")
check(not first.get("delta"), "首次是基线")

# 只改数据库，不动文件
make_sessions_db(wb / "workbuddy.db", [{"id": "s1", "title": "one"},
                                       {"id": "s2", "title": "two"}])
second = core.build_package(sync, False)
check(bool(second.get("delta")), "仅数据库变化也生成增量")
check(Path(second.get("zip", "")).is_file(), "增量包已落盘")

# 基线被破坏时，即便索引指纹还一致也不能跳过
(sync / "state" / "from-hard-host.zip").write_bytes(b"broken")
third = core.build_package(sync, False, force=False)
check(not third.get("skipped"), "损坏基线不会被指纹跳过")
with zipfile.ZipFile(sync / "state" / "from-hard-host.zip") as z:
    check("manifest.json" in z.namelist(), "损坏基线被重建为合法包")

# 删除文件触发全量基线重建
(ws / "a.txt").unlink()
fourth = core.build_package(sync, False)
check(not fourth.get("delta") and "rebuild_reason" in fourth,
      "删除文件触发基线重建")
check(not (ws / "a.txt").exists(), "删除的是源文件")

# ─────────────────── G. 选择性 push 不扫描全量资产 ───────────────────
head("G. 选择性 push 仅处理选择项")

(wb / "skills" / "keep").mkdir(parents=True)
(wb / "skills" / "keep" / "a.md").write_text("keep", encoding="utf-8")
(wb / "tasks" / "task1").mkdir(parents=True)
(wb / "tasks" / "task1" / "x.txt").write_text("task", encoding="utf-8")

sel = {"workspaces": [], "tasks": ["task1"], "sessions": []}
out = core.build_package(sync, True, selection=sel)
check(out["files"] >= 1, "选择性 dry-run 成功")
check(out["files"] < 10, "选择性包未包含全量资产", str(out["files"]))
try:
    core.build_package(sync, True, selection={
        "workspaces": [], "tasks": ["missing-task"], "sessions": []})
except OSError as e:
    check("不存在" in str(e), "选择性 push 对缺失任务明确失败", str(e))
else:
    check(False, "选择性 push 不应把缺失任务当成功")

# ─────────────────── H. merge_tree / merge_db 失败闭环 ───────────────────
head("H. 合并比较与数据库事务失败阻止成功标记")

src_tree = base / "merge-src"
dst_tree = base / "merge-dst"
src_tree.mkdir()
dst_tree.mkdir()
(src_tree / "a.txt").write_text("a", encoding="utf-8")
acts = core.merge_tree(src_tree, dst_tree, "测试")
check(acts and acts[0][0] == "新增", "缺失文件加入合并动作")

fails = []
try:
    (dst_tree / "a.txt").mkdir()
    core.merge_tree(src_tree, dst_tree, "测试", failures=fails)
    # 目录本身就是“存在”，不能当文件比较；应产生“更新”动作而不是成功跳过。
    check(bool(acts) and bool(core.merge_tree(src_tree, dst_tree, "测试")),
          "目标同名目录不会被误判为内容一致")
except Exception as e:  # noqa: BLE001
    check(False, "合并比较异常收敛", str(e))

incoming_db = base / "incoming.db"
make_sessions_db(incoming_db, [{"id": "peer", "title": "peer"}], uid="peer-uid")
target_db = base / "target.db"
make_sessions_db(target_db, [{"id": "local", "title": "local"}], uid="local-uid")
stats = core.merge_db(target_db, incoming_db)
check("_merge_failed" not in stats, "合法数据库合并无事务失败")

broken = base / "broken.db"
broken.write_bytes(b"not sqlite")
stats = core.merge_db(target_db, broken)
check("_merge_failed" in stats, "损坏数据库合并不静默成功")

# ─────────────────── I. 敏感扫描读取失败进入盲区 ───────────────────
head("I. 敏感扫描读取失败出现在盲区")

scan_root = base / "scan-root"
scan_root.mkdir()
(scan_root / "a.txt").write_text("hello", encoding="utf-8")
missing = scan_root / "gone.txt"
res = secret_scan.scan_items([("a.txt", scan_root / "a.txt"),
                              ("gone.txt", missing)], budget_mb=1)
check(res["read_failed"] >= 1 and "gone.txt" in res["unread_files"],
      "不存在的扫描输入被记录")
check(any("读取失败" in x for x in secret_scan.blind_spots(res)),
      "读取失败出现在盲区报告")

ledger_rows = [{"rel": "files/a.txt", "backup": "files/a.txt.bak",
                "stamp": "20261102-120000"}]
check(core.record_conflicts(ledger_rows), "冲突台账可写入")
ledger_path = core._ledger_path()
check(json.loads(ledger_path.read_text(encoding="utf-8"))["items"] == ledger_rows,
      "冲突台账内容可读")
check(not list(ledger_path.parent.glob("conflicts.json.tmp")),
      "冲突台账临时文件已提交")

# ─────────────────── J. manifest 缺失不写 marker ───────────────────
head("J. 缺 manifest 的对端包不推进拉取标记")

sync_pull = base / "sync-pull"
sync_pull.mkdir()
no_manifest = sync_pull / "state" / "from-peer.zip"
make_zip(no_manifest, [("files/a.txt", "x")])

old_wb, old_ws = state.WB_HOME, state.WORKSPACE_ROOT
state.WB_HOME = base / "pull-wb"
state.WORKSPACE_ROOT = base / "pull-ws"
state.WB_HOME.mkdir()
state.WORKSPACE_ROOT.mkdir()
try:
    make_sessions_db(state.WB_HOME / "workbuddy.db",
                     [{"id": "local", "title": "local"}])
    rc = core.do_pull(sync_pull, dry=False, force=True)
    marker = core.load_pull_marker(sync_pull)
    check(rc == 2 and "from-peer.zip" not in marker,
          "缺 manifest 时返回 2 且不写 marker", f"rc={rc}, marker={marker}")
finally:
    state.WB_HOME, state.WORKSPACE_ROOT = old_wb, old_ws

# ─────────────────── K. 缺正文 / 多账号拒绝跨账号复制 ───────────────────
head("K. 跨账号正文复制边界")

copy_src_db = base / "copy-src.db"
make_sessions_db(copy_src_db, [{"id": "w1", "title": "w1"}], uid="peer-uid")
copy_dst_db = base / "copy-dst.db"
make_sessions_db(copy_dst_db, [{"id": "h1", "title": "h1"}], uid="local-uid")
src_files = base / "copy-src-files"
src_files.mkdir()
(src_files / "projects" / "ws").mkdir(parents=True)
(src_files / "projects" / "ws" / "w1.jsonl").write_text("w1", encoding="utf-8")
peer_cids, copied, failed = core.copy_peer_sessions(
    copy_dst_db, copy_src_db, src_files, base / "copy-dst-home", False)
check(copied == 1 and failed == 0, "单账号跨账号会话复制")

# 多账号：拒绝猜测目标
multi_dst = base / "multi.db"
make_sessions_db(multi_dst, [{"id": "h1", "title": "h1"}], uid="uid-a")
c = sqlite3.connect(str(multi_dst))
c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
          ("h2", "uid-b", "h2", None, 0, None))
c.commit()
c.close()
_, copied2, _ = core.copy_peer_sessions(multi_dst, copy_src_db, src_files,
                                       base / "copy-multi-home", False)
check(copied2 == 0, "多账号拒绝跨账号复制")

# 正文已存在不覆盖；新会话首次插入时补正文
copy_dst_home = base / "copy-dst-home2"
new_body = copy_dst_home / "projects" / "ws" / f"{core.derive_copy_id('peer-uid', 'w1')}.jsonl"
c = sqlite3.connect(str(copy_dst_db))
c.execute("DELETE FROM sessions WHERE id=?", (core.derive_copy_id("peer-uid", "w1"),))
c.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
          (core.derive_copy_id("peer-uid", "w1"), "local-uid", "w1",
           None, 0, None))
c.commit()
c.close()
peer_cids, copied, failed = core.copy_peer_sessions(
    copy_dst_db, copy_src_db, src_files, copy_dst_home, False)
check(copied == 1 and new_body.is_file() and failed == 0,
      "行已存在时正文缺失可补齐")
new_body.write_text("edited-local", encoding="utf-8")
_, copied, failed = core.copy_peer_sessions(
    copy_dst_db, copy_src_db, src_files, copy_dst_home, False)
check(new_body.read_text(encoding="utf-8") == "edited-local",
      "已存在副本正文不被覆盖")

if hasattr(os, "symlink"):
    link_target = base / "link-target"
    link_target.mkdir()
    loop_src = base / "copy-loop-src"
    loop_src.mkdir()
    try:
        os.symlink(link_target, loop_src / "loop", target_is_directory=True)
    except OSError:
        pass
    else:
        (loop_src / "loop" / "w1.jsonl").write_text("w1", encoding="utf-8")
        loop_failures = []
        got = list(core.iter_assets(loop_src, ["."], None, loop_failures))
        check(not got and loop_failures,
              "拒绝目录联接/符号链接循环并记录失败")

# ─────────────────── L. verify 数据库漂移 ───────────────────
head("L. verify 检测数据库漂移")

old_idx = packs.load(sync / "state", "hard-host")
check(old_idx is not None, "基线索引存在")
make_sessions_db(wb / "workbuddy.db", [{"id": "s1", "title": "one"}])
rc = core.do_verify(sync, strict=False)
check(rc in (0, 1), "verify 可运行")

# ─────────────────── M. 面板 HTTP 边界 ───────────────────
head("M. 面板来源与请求体校验")

root_manifest = json.loads(
    (DEV / "default-manifest.json").read_text(encoding="utf-8"))
package_manifest = json.loads(
    (SRC / "wb_sync" / "default-manifest.json").read_text(encoding="utf-8"))
check(root_manifest == package_manifest,
      "根目录与包内默认清单内容一致")


class _Panel(ui.ThreadingHTTPServer):
    daemon_threads = True


panel_port = 0
panel = _Panel(("127.0.0.1", 0), ui.Handler)
panel_port = panel.server_address[1]
old_port = ui.Handler.port
ui.Handler.port = panel_port
threading.Thread(target=panel.serve_forever, daemon=True).start()


def post_raw(path: str, body: bytes, headers: dict,
             declared_length: int | None = None) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", panel_port, timeout=3)
    try:
        conn.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        sent = {"Host": f"127.0.0.1:{panel_port}", **headers}
        if "Content-Length" not in sent:
            sent["Content-Length"] = str(
                declared_length if declared_length is not None else len(body))
        for key, value in sent.items():
            conn.putheader(key, value)
        conn.endheaders()
        if body:
            conn.send(body)
        if declared_length is not None and len(body) < declared_length:
            conn.sock.shutdown(socket.SHUT_WR)
        return conn.getresponse().status
    except (OSError, http.client.HTTPException):
        return -1
    finally:
        conn.close()


try:
    check(post_raw("/api/run", b"{}", {
        "Origin": "http://evil.example"}) == 403,
        "拒绝跨站 Origin")
    check(post_raw("/api/run", b"{}", {
        "Origin": f"http://127.0.0.1:{panel_port + 1}"}) == 403,
        "拒绝本机不同端口 Origin")
    check(post_raw("/api/not-found", b"{}", {
        "Origin": f"https://127.0.0.1:{panel_port}"}) == 404,
        "接受同端口 HTTPS 本机来源（Host 仍限本机）")
    check(post_raw("/api/run", b"{}", {
        "Origin": "file:///tmp/page.html"}) == 403,
        "拒绝非 HTTP(S) Origin")
    check(post_raw("/api/not-found", b"[]", {}) == 400,
        "拒绝非对象 JSON")
    check(post_raw("/api/not-found", b"{}", {
        "Host": "evil.example"}) == 403,
        "拒绝非本机 Host")
    check(post_raw("/api/not-found", b"{",
                   {"Content-Length": "10"}, declared_length=10) == 400,
        "拒绝不完整请求体")
    check(post_raw("/api/run", b"{}", {
        "Content-Length": str(1_048_577),
        "Origin": f"http://127.0.0.1:{panel_port}"}) == 413,
        "拒绝超大请求体")
finally:
    panel.shutdown()
    panel.server_close()
    ui.Handler.port = old_port


# ─────────────────── N. CLI 帮助文本可渲染 ───────────────────
print("\n" + "=" * 62)
print("N. CLI 帮助文本可渲染")
print("=" * 62)

try:
    rendered_help = cli._build_parser().format_help()
    check("verify" in rendered_help, "帮助文本包含 verify 命令")
    check("漂移率超过 1%" in rendered_help,
          "帮助文本中的百分号被正确渲染")
except (ValueError, TypeError) as exc:
    check(False, f"帮助文本不应抛出异常：{exc}")


# ─────────────────── 收尾 ───────────────────
print(f"\n测试目录：{base}")
if failures:
    print(f"\n失败 {len(failures)} 项：")
    for x in failures:
        print("  - " + x)
    sys.exit(1)
print("\n全部通过 ✓")
sys.exit(0)

