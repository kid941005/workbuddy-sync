# -*- coding: utf-8 -*-
"""WorkBuddy 同步助手 —— 本地 Web 面板。

零第三方依赖，复用 wb_sync 引擎。启动后浏览器打开 localhost 面板，
可视化执行 doctor / status / push / pull 并查看输出。

用法：
    wb-sync ui                 # pip 安装后启动面板（默认端口 8765）
    python -m wb_sync.ui --port 9000
"""
import json
import os
import sys
import threading
import time
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path

from . import ENGINE_VERSION, config, core, paths, state

DEFAULT_PORT = 8765


def _init_env():
    """复刻 cli.main 的初始化，供 /api/info 读取当前状态。

    必须与 cli 走同一套源目录解析（paths.*）：面板显示的「空间文件」若按
    写死的 ~/WorkBuddy 统计，用户改过工作区位置后会看到 0 个文件却不知道为什么。
    """
    cfg = config.load_config()
    wb_home, wb_src = paths.find_workbuddy_home(None)
    state.WB_HOME = wb_home
    ws_root, ws_src = paths.find_workspace_root(wb_home, None, cfg)
    state.WORKSPACE_ROOT = ws_root
    state.SOURCE_SRC = (wb_src, ws_src)

    man, sources = config.load_manifest()
    config.apply_manifest(man)
    sync_root, root_src = config.resolve_sync_root(cfg, None)
    config.resolve_machine_id(cfg, sync_root)
    return cfg, sync_root, root_src, sources


def _ago(ts: float) -> str:
    """把时间戳说成人话。面板上"3 小时前"比 ISO 串更有用。"""
    try:
        delta = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "—"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def _peers(sync_root) -> list:
    """集群成员一览。

    「最后上线」= 该机器最后一次 push 的时间，也就是它那个包的 mtime。
    这是唯一能跨机看到的活性信号：引擎不常驻，没有心跳，只有包在动。
    """
    if not sync_root:
        return []
    root = Path(sync_root)
    sd = root / "state"
    if not sd.is_dir():
        return []
    try:
        marker = core.load_pull_marker(root)
    except Exception:
        marker = {}
    out = []
    for zp in sorted(sd.glob("from-*.zip")):
        host = zp.stem[5:] if zp.stem.startswith("from-") else zp.stem
        try:
            st = zp.stat()
        except OSError:
            continue
        health = {}
        hp = sd / f"{zp.stem}.health.json"
        if hp.is_file():
            try:
                health = json.loads(hp.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError):
                health = {}
        is_self = host == state.HOST
        try:
            synced = is_self or marker.get(zp.name) == core.peer_sig(sd, zp)
        except Exception:
            synced = False
        out.append({
            "host": host,
            "name": health.get("machine_name") or (state.MACHINE_NAME if is_self else host),
            "engine": health.get("engine") or "—",
            "last_seen": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "last_seen_ago": _ago(st.st_mtime),
            "bytes": st.st_size,
            "files": health.get("files"),
            "unreachable": health.get("workspaces_unreachable"),
            "is_self": is_self,
            "synced": synced,
        })
    out.sort(key=lambda x: (not x["is_self"], x["host"]))
    return out


def build_info() -> dict:
    cfg, sync_root, root_src, sources = _init_env()
    wb_src, ws_src = state.SOURCE_SRC
    cluster_ok, cluster = (config.check_cluster(sync_root, create=False)
                           if sync_root else (True, ""))
    peers = _peers(sync_root)
    me = next((p for p in peers if p["is_self"]), None)

    # 最后推送 = 本机包的 mtime；最后拉取 = 本机 pull 标记的 mtime。
    # 两者分开显示，才能一眼看出"一直在推、从没拉过"这类偏科。
    last_push_ts = last_pull_ts = None
    if sync_root:
        sd = Path(sync_root) / "state"
        zp = sd / f"from-{state.HOST}.zip"
        if zp.is_file():
            try:
                last_push_ts = zp.stat().st_mtime
            except OSError:
                pass
        pm = sd / f".last-pull-{state.HOST}.json"
        if pm.is_file():
            try:
                last_pull_ts = pm.stat().st_mtime
            except OSError:
                pass
    return {
        "engine": ENGINE_VERSION,
        "host": state.HOST,
        "name": state.MACHINE_NAME,
        "sync_root": str(sync_root) if sync_root else None,
        "sync_src": root_src,
        "wb_home": str(state.WB_HOME),
        "wb_home_src": wb_src,
        "workspace_root": str(state.WORKSPACE_ROOT),
        "workspace_root_src": ws_src,
        "workspace_root_ok": state.WORKSPACE_ROOT.is_dir(),
        "cluster_ok": cluster_ok,
        "cluster_id": cluster,
        "files": len(state.SYNC_FILES),
        "dirs": len(state.SYNC_DIRS),
        "tables": len(state.DB_TABLES),
        "manifest": " < ".join(sources),
        "last_seen": me["last_seen"] if me else None,
        "last_seen_ago": me["last_seen_ago"] if me else "尚未上传",
        "last_push": (datetime.fromtimestamp(last_push_ts).isoformat(timespec="seconds")
                      if last_push_ts else None),
        "last_push_ago": _ago(last_push_ts) if last_push_ts else "尚未推送",
        "last_pull": (datetime.fromtimestamp(last_pull_ts).isoformat(timespec="seconds")
                      if last_pull_ts else None),
        "last_pull_ago": _ago(last_pull_ts) if last_pull_ts else "尚未拉取",
        "peers": peers,
    }


def get_config() -> dict:
    cfg = config.load_config()
    return {
        "sync_root": cfg.get("sync_root"),
        "machine_name": cfg.get("machine_name"),
        "config_file": str(config.CONFIG_DIR / "config.json"),
    }


def set_sync_root(path: str) -> dict:
    """把同步目录写入 config.json（支持任意云盘 / U 盘 / 局域网共享路径）。

    统一走 paths.normalize：面板里手输的 "~\\Cloud"、带引号的粘贴路径、
    相对路径都会先规范成绝对路径再落盘，避免写进去的是"看似能用"的字符串。
    """
    path = (path or "").strip()
    if not path:
        return {"ok": False, "error": "路径为空"}
    p = paths.normalize(path, base=config.CONFIG_DIR)
    if p is None:
        return {"ok": False, "error": f"路径无法解析：{path}"}
    if not p.is_dir():
        return {"ok": False, "error": f"目录不存在：{p}"}
    cfg = config.load_config()
    cfg["sync_root"] = str(p)
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (config.CONFIG_DIR / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "sync_root": str(p)}


def set_machine_name(name: str) -> dict:
    """保存本机备注名。

    只影响展示（面板与 health.json 里的 machine_name），**不改变机器标识**：
    HOST 由 machine-id 派生、决定包文件名，改了会让这台机器在集群里变成新成员。
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "备注名不能为空"}
    if len(name) > 40:
        return {"ok": False, "error": "备注名最多 40 个字符"}
    if any(ch in name for ch in "\r\n\t"):
        return {"ok": False, "error": "备注名不能包含换行或制表符"}
    cfg = config.load_config()
    cfg["machine_name"] = name
    try:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        (config.CONFIG_DIR / "config.json").write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": str(e)}
    state.MACHINE_NAME = name
    return {"ok": True, "machine_name": name}


#: 当前正在执行的动作。面板一次只跑一个，前端轮询 /api/progress 读它。
_RUN = {"running": False, "action": "", "started": 0.0, "pct": 0,
        "label": "", "buf": None, "code": None}
_RUN_LOCK = threading.Lock()


def run_action(action: str, selection: dict | None = None) -> dict:
    """执行一个动作，复用 cli.main 的完整逻辑，捕获 stdout。

    执行期间通过 core.PROGRESS_HOOK 把进度写进 _RUN，前端轮询 /api/progress
    就能拿到百分比、当前阶段和已用时间 —— push/pull 动辄几分钟，没有反馈的
    按钮很容易被当成卡死。

    ★ 同一时刻只允许一个动作
        服务是多线程的，浏览器开两个标签页、或按钮被重复点击，就会有两个
        cli.main 同时跑：它们共用一个 PROGRESS_HOOK，先结束的那个在 finally
        里把钩子置 None，另一个的进度直接断掉；更糟的是两次 push 会同时写
        同一个 zip。所以「占坑」必须与检查在同一个原子区间内完成。
    """
    from . import cli
    buf = StringIO()
    with _RUN_LOCK:
        if _RUN["running"]:
            return {"action": action, "code": 2, "busy": True,
                    "output": f"已有操作正在执行（{_RUN['action']}），请等它结束。"}
        _RUN.update(running=True, action=action, started=time.time(),
                    pct=0, label="准备中", buf=buf, code=None)

    def _hook(pct: int, label: str) -> None:
        with _RUN_LOCK:
            _RUN["pct"] = pct
            if label:
                _RUN["label"] = label

    code = 0
    argv = [action]
    if selection and action == "push":
        argv += ["--selection", json.dumps(selection, ensure_ascii=False)]
    core.PROGRESS_HOOK = _hook
    try:
        with redirect_stdout(buf):
            code = cli.main(argv)
    except SystemExit as e:
        code = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        buf.write(f"[异常] {e}\n")
        code = 1
    finally:
        core.PROGRESS_HOOK = None
        with _RUN_LOCK:
            _RUN.update(running=False, pct=100, code=code)
    return {"action": action, "code": code, "output": buf.getvalue()}


def run_progress() -> dict:
    """给前端轮询的进度快照。tail 只回最后一段，避免每秒几百 KB。"""
    with _RUN_LOCK:
        started = _RUN["started"]
        buf = _RUN["buf"]
        return {
            "running": _RUN["running"],
            "action": _RUN["action"],
            "pct": _RUN["pct"],
            "label": _RUN["label"],
            "elapsed": int(time.time() - started) if started else 0,
            "tail": (buf.getvalue()[-2000:] if buf is not None else ""),
        }


# ─────────────────────────── 前端页面 ───────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WorkBuddy 同步助手</title>
<style>
  :root {
    --bg: #f4f5f7; --card: #ffffff; --line: #e6e8eb;
    --text: #1f2329; --muted: #8a919c;
    --brand: #6c5ce7; --brand-soft: #eeecfb;
    --ok: #16a34a; --warn: #d97706; --err: #dc2626;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding: 32px 20px;
  }
  .wrap { max-width: 860px; margin: 0 auto; }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }
  header h1 { font-size: 24px; font-weight: 700; letter-spacing: -.3px; }
  header .ver {
    font-size: 12px; color: var(--brand); background: var(--brand-soft);
    padding: 2px 8px; border-radius: 999px; font-weight: 600;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 16px 18px;
  }
  .card .k { font-size: 12px; color: var(--muted); margin-bottom: 6px; }
  .card .v { font-size: 15px; font-weight: 600; word-break: break-all; line-height: 1.4; }
  .edit-link { color: var(--brand); cursor: pointer; font-size: 12px; font-weight: 500; margin-left: 6px; }
  .edit-link:hover { text-decoration: underline; }
  .actions { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }
  button {
    appearance: none; border: 1px solid var(--line); background: var(--card);
    border-radius: 14px; padding: 18px 12px; cursor: pointer; font-size: 14px;
    font-weight: 600; color: var(--text); transition: all .15s; font-family: inherit;
  }
  button:hover { border-color: var(--brand); background: var(--brand-soft); color: var(--brand); }
  button .ico { display: block; font-size: 24px; margin-bottom: 6px; }
  button .sub { display: block; font-size: 11px; color: var(--muted); font-weight: 400; margin-top: 3px; }
  button.busy { opacity: .5; pointer-events: none; }
  button.pull { border-color: #f0c7a0; }
  .log {
    background: #14161a; color: #d6dbe2; border-radius: 14px; padding: 16px 18px;
    font-family: "Cascadia Code", "JetBrains Mono", Consolas, monospace;
    font-size: 12.5px; line-height: 1.6; white-space: pre-wrap; min-height: 180px;
    overflow: auto; max-height: 420px;
  }
  .log .dim { color: #6b7280; }
  .log .ok { color: #4ade80; } .log .warn { color: #fbbf24; } .log .err { color: #f87171; }
  .badge { font-size: 12px; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .badge.ok { background: #dcfce7; color: var(--ok); }
  .badge.err { background: #fee2e2; color: var(--err); }
  .select-card { margin: 16px 0; }
  .select-head { display: flex; justify-content: space-between; align-items: center; cursor: pointer; padding: 14px 18px; }
  .select-head .t { font-weight: 700; }
  .select-head .hint { font-size: 12px; color: var(--muted); }
  .select-body { padding: 0 18px 16px; }
  .select-toolbar { display: flex; gap: 8px; margin-bottom: 12px; }
  .select-toolbar input { flex: 1; padding: 8px 10px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; font-family: inherit; }
  .select-toolbar .mini { padding: 6px 12px; font-size: 12px; }
  .select-group { margin-bottom: 14px; }
  .select-group h3 { font-size: 13px; color: var(--muted); margin-bottom: 6px; font-weight: 600; }
  .check-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 4px; max-height: 240px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; padding: 8px; background: #fafbfc; }
  .check-list label { display: flex; align-items: center; gap: 8px; font-size: 13px; padding: 4px 6px; border-radius: 6px; cursor: pointer; }
  .check-list label:hover { background: var(--brand-soft); }
  .check-list input { accent-color: var(--brand); }
  .check-list .cnt { color: var(--muted); font-size: 11px; margin-left: auto; }
  .check-list.sess { grid-template-columns: 1fr; max-height: 380px; }
  .check-list.sess label { gap: 6px; }
  .check-list.sess .sess-main { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .check-list .ws-tag { font-size: 10.5px; color: var(--brand); background: var(--brand-soft); padding: 1px 6px; border-radius: 999px; white-space: nowrap; flex: none; }
  .check-list .t-badge { font-size: 10.5px; color: #0e7490; background: #dbeafe; padding: 1px 6px; border-radius: 999px; white-space: nowrap; flex: none; }
  .check-list .time { color: var(--muted); font-size: 11px; white-space: nowrap; flex: none; }
  .sort-sel { font-size: 12px; padding: 4px 6px; border: 1px solid var(--line); border-radius: 6px; color: var(--text); background: #fff; font-family: inherit; }
  .grp-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .mono { font-size: 11.5px; color: var(--muted); font-family: "Cascadia Code", Consolas, monospace; margin-top: 4px; word-break: break-all; }
  table.peers { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.peers th { text-align: left; color: var(--muted); font-weight: 600; font-size: 11.5px; padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
  table.peers td { padding: 9px 8px; border-bottom: 1px solid #f2f4f6; vertical-align: top; }
  table.peers tr:last-child td { border-bottom: 0; }
  table.peers tr.self td { background: #faf9ff; }
  .tag { display: inline-block; font-size: 11px; padding: 1px 8px; border-radius: 999px; background: var(--brand-soft); color: var(--brand); font-weight: 600; white-space: nowrap; }
  .tag.ok { background: #dcfce7; color: var(--ok); }
  .tag.warn { background: #fef3c7; color: var(--warn); }
  .tag.idle { background: #f1f3f5; color: var(--muted); }
  .progress { height: 8px; background: #e6e8eb; border-radius: 999px; overflow: hidden; }
  .progress > i { display: block; height: 100%; width: 0; border-radius: 999px;
    background: linear-gradient(90deg, #6c5ce7, #8b7cf6); transition: width .35s ease; }
  #prog.indet .progress > i { width: 32% !important; animation: wbslide 1.2s infinite ease-in-out; }
  @keyframes wbslide { 0% { margin-left: -32% } 100% { margin-left: 100% } }
  @media (max-width: 640px) { .actions { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>WorkBuddy 同步助手</h1>
    <span class="ver" id="ver">加载中…</span>
  </header>

  <div class="card help-card" style="margin-bottom:16px">
    <div class="select-head" onclick="toggleHelp()">
      <span class="t">📖 使用说明</span>
      <span class="hint">点开看步骤</span>
    </div>
    <div id="help-body" style="display:none;padding:4px 18px 14px">
      <ol style="font-size:13px;line-height:2.1;color:#3a3f47;margin:0;padding-left:20px">
        <li><b>自检</b>：第一次先用它确认环境正常。</li>
        <li><b>看差异</b>：对比本机和同步目录，只看不改。</li>
        <li><b>选择同步范围</b>：勾选要同步的空间/任务/对话（不勾 = 全量）。</li>
        <li><b>上传</b>：本机 → 同步目录，只传勾选的内容。</li>
        <li><b>拉取</b>：同步目录 → 本机，<b style="color:#dc2626">需先完全退出 WorkBuddy</b>。</li>
      </ol>
      <div style="font-size:12px;color:#8a919c;margin-top:6px">
        双机同步：数据源那台「勾选 + 上传」，目标那台「拉取」。
      </div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="k">本机 <span class="edit-link" onclick="editName()">✎ 备注</span></div>
      <div class="v" id="local-name">—</div>
      <div class="mono" id="host">—</div>
    </div>
    <div class="card">
      <div class="k">最后推送 ↑</div>
      <div class="v" id="last-push">—</div>
      <div class="mono" id="last-push-at"></div>
    </div>
    <div class="card">
      <div class="k">最后拉取 ↓</div>
      <div class="v" id="last-pull">—</div>
      <div class="mono" id="last-pull-at"></div>
    </div>
    <div class="card"><div class="k">同步目录 <span class="edit-link" onclick="editSyncRoot()">✎ 修改</span></div><div class="v" id="sync">—</div></div>
    <div class="card"><div class="k">同步范围</div><div class="v" id="scope">—</div></div>
    <div class="card"><div class="k">清单来源</div><div class="v" id="manifest">—</div></div>
  </div>

  <div class="card" style="margin-top:12px">
    <div class="select-head" style="cursor:default">
      <span class="t">🖥 集群成员 <span class="hint" id="peer-count"></span></span>
      <span class="hint">最后上线 = 该机最后一次上传的时间</span>
    </div>
    <div style="padding:0 18px 14px">
      <table class="peers" id="peers"><tr><td class="mono">加载中…</td></tr></table>
    </div>
  </div>

  <div class="actions">
    <button data-action="doctor"><span class="ico">🔍</span>自检<span class="sub">环境与白名单</span></button>
    <button data-action="status"><span class="ico">📊</span>看差异<span class="sub">两侧对比，不动文件</span></button>
    <button data-action="push"><span class="ico">⬆️</span>上传<span class="sub">本机 → 同步目录</span></button>
    <button data-action="pull" class="pull"><span class="ico">⬇️</span>拉取<span class="sub">同步目录 → 本机</span></button>
  </div>

  <div class="card select-card">
    <div class="select-head" onclick="toggleSelect()">
      <span class="t">🎯 选择同步范围</span>
      <span class="hint" id="sel-hint">默认全量</span>
    </div>
    <div class="select-body" id="sel-body" style="display:none">
      <div class="select-toolbar">
        <input id="sel-search" placeholder="搜索空间 / 任务 / 对话…" oninput="renderItems()">
        <button class="mini" onclick="selAll(true)">全选</button>
        <button class="mini" onclick="selAll(false)">清空</button>
      </div>
      <div class="select-group">
        <h3>空间（<span id="ws-count">0</span>）</h3>
        <div class="check-list" id="ws-list"></div>
      </div>
      <div class="select-group">
        <h3>任务（<span id="task-count">0</span>）</h3>
        <div class="check-list" id="task-list"></div>
      </div>
      <div class="select-group">
        <div class="grp-head">
          <h3 style="margin:0">对话（<span id="sess-count">0</span>）</h3>
          <select id="sess-sort" class="sort-sel" onchange="renderItems()">
            <option value="created_desc">创建时间 ↓（新→旧）</option>
            <option value="created_asc">创建时间 ↑（旧→新）</option>
            <option value="updated_desc">更新时间 ↓</option>
            <option value="name">名称 A-Z</option>
          </select>
        </div>
        <div class="check-list sess" id="sess-list"></div>
      </div>
      <div class="dim" style="font-size:12px;color:var(--muted)">勾选「对话」会自动带上它所属的空间和关联任务；勾选后点「上传」只同步勾选的内容，不勾选 = 全量同步。</div>
    </div>
  </div>

  <div id="prog" style="display:none;margin:18px 0 8px">
    <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px">
      <span id="prog-label">准备中…</span>
      <span id="prog-time">0 秒</span>
    </div>
    <div class="progress"><i id="prog-bar"></i></div>
  </div>

  <div class="log" id="log"><span class="dim">点击上方按钮执行操作，输出会显示在这里。</span></div>
</div>

<script>
async function loadInfo() {
  try {
    const r = await fetch('/api/info'); const d = await r.json();
    document.getElementById('ver').textContent = 'v' + d.engine;
    document.getElementById('local-name').textContent = d.name || '未命名';
    document.getElementById('host').textContent = d.host || '—';
    document.getElementById('last-push').textContent = d.last_push_ago || '尚未推送';
    document.getElementById('last-push-at').textContent = d.last_push ? d.last_push.replace('T', ' ') : '';
    document.getElementById('last-pull').textContent = d.last_pull_ago || '尚未拉取';
    document.getElementById('last-pull-at').textContent = d.last_pull ? d.last_pull.replace('T', ' ') : '';
    document.getElementById('sync').textContent = d.sync_root || '(未探测到)';
    document.getElementById('scope').textContent = d.files + ' 文件 · ' + d.dirs + ' 目录 · ' + d.tables + ' 表';
    document.getElementById('manifest').textContent = d.manifest;
    renderPeers(d.peers);
  } catch (e) {
    document.getElementById('ver').textContent = '连接失败';
    // 成员表默认是「加载中…」，请求失败时必须换掉，否则它会永远停在那句话上，
    // 看起来像面板卡死而不是网络失败。
    const tb = document.getElementById('peers');
    if (tb && tb.innerHTML.indexOf('加载中') >= 0) {
      tb.innerHTML = '<tr><td class="mono">获取失败 · '
        + '<a href="#" onclick="loadInfo();return false;">点击重试</a></td></tr>';
    }
  }
}

function esc(s) { return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function fmtSize(n) {
  if (!n) return '—';
  const mb = n / 1048576;
  return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB';
}

function renderPeers(list) {
  const tb = document.getElementById('peers');
  const cnt = document.getElementById('peer-count');
  if (!tb) return;
  if (!list || !list.length) {
    tb.innerHTML = '<tr><td class="mono">同步目录里还没有任何机器的包</td></tr>';
    if (cnt) cnt.textContent = '';
    return;
  }
  if (cnt) cnt.textContent = '（' + list.length + ' 台）';
  let h = '<tr><th>备注 / 标识</th><th>引擎</th><th>最后上线</th><th>包大小</th><th>状态</th></tr>';
  for (const p of list) {
    let st;
    if (p.is_self) st = '<span class="tag">本机</span>';
    else if (p.synced) st = '<span class="tag ok">已同步</span>';
    else st = '<span class="tag warn">待拉取</span>';
    h += '<tr' + (p.is_self ? ' class="self"' : '') + '>' +
      '<td><b>' + esc(p.name || p.host) + '</b><div class="mono">' + esc(p.host) + '</div></td>' +
      '<td>' + esc(p.engine || '—') + '</td>' +
      '<td>' + esc(p.last_seen_ago || '—') +
        '<div class="mono">' + esc((p.last_seen || '').replace('T', ' ')) + '</div></td>' +
      '<td>' + fmtSize(p.bytes) + '</td>' +
      '<td>' + st + '</td></tr>';
  }
  tb.innerHTML = h;
}

async function editName() {
  const cur = (document.getElementById('local-name').textContent || '').trim();
  const v = prompt('本机备注名（只改面板与健康快照里的显示，不改变机器标识）',
                   cur === '—' ? '' : cur);
  if (v === null) return;
  try {
    const r = await fetch('/api/name', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: v })
    });
    const d = await r.json();
    if (!d.ok) { alert(d.error || '保存失败'); return; }
    await loadInfo();
  } catch (e) { alert('保存失败：' + e); }
}

let items = { workspaces: [], tasks: [], sessions: [] };
let checked = { workspaces: new Set(), tasks: new Set(), sessions: new Set() };

function escAttr(s) { return String(s).replace(/"/g, '&quot;'); }

function fmtTime(ms) {
  if (!ms) return '';
  const d = new Date(ms);
  const p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

async function loadItems() {
  try {
    const r = await fetch('/api/items'); items = await r.json();
    renderItems();
  } catch (e) {}
}

function sortSessions(ss) {
  const mode = document.getElementById('sess-sort').value;
  if (mode === 'name') return ss.slice().sort((a, b) => (a.name || '').localeCompare(b.name || '', 'zh'));
  if (mode === 'created_asc') return ss.slice().sort((a, b) => (a.created || 0) - (b.created || 0));
  if (mode === 'updated_desc') return ss.slice().sort((a, b) => (b.updated || 0) - (a.updated || 0));
  return ss.slice().sort((a, b) => (b.created || 0) - (a.created || 0));
}

function renderItems() {
  const q = (document.getElementById('sel-search').value || '').toLowerCase();
  const ws = items.workspaces.filter(w => w.name.toLowerCase().includes(q));
  const ts = items.tasks.filter(t => (t.name || t.path).toLowerCase().includes(q));
  const ss = sortSessions((items.sessions || []).filter(s => s.name.toLowerCase().includes(q)));
  document.getElementById('ws-count').textContent = ws.length;
  document.getElementById('task-count').textContent = ts.length;
  document.getElementById('sess-count').textContent = ss.length;
  document.getElementById('ws-list').innerHTML = ws.map(w =>
    '<label><input type="checkbox" value="' + escAttr(w.path) + '"' + (checked.workspaces.has(w.path) ? ' checked' : '') + ' onchange="onWsChange(this)">' + esc(w.name) + '</label>').join('');
  document.getElementById('task-list').innerHTML = ts.map(t =>
    '<label><input type="checkbox" value="' + escAttr(t.path) + '"' + (checked.tasks.has(t.path) ? ' checked' : '') + ' onchange="onTaskChange(this)">' + esc(t.name) + '<span class="cnt">' + (t.count || '') + '</span></label>').join('');
  document.getElementById('sess-list').innerHTML = ss.map(s => {
    const wsTag = s.workspace ? '<span class="ws-tag" title="所属空间">📁 ' + esc(s.workspace) + '</span>' : '';
    const tBadge = s.has_tasks ? '<span class="t-badge">📋 有任务</span>' : '';
    return '<label><input type="checkbox" value="' + escAttr(s.id) + '"' + (checked.sessions.has(s.id) ? ' checked' : '') + ' onchange="onSessChange(this)">' +
      '<span class="sess-main" title="' + escAttr(s.name) + '">' + esc(s.name) + '</span>' + wsTag + tBadge +
      '<span class="time">' + fmtTime(s.created) + '</span></label>';
  }).join('');
  updateHint();
}

function onWsChange(cb) { if (cb.checked) checked.workspaces.add(cb.value); else checked.workspaces.delete(cb.value); updateHint(); }
function onTaskChange(cb) { if (cb.checked) checked.tasks.add(cb.value); else checked.tasks.delete(cb.value); updateHint(); }
function onSessChange(cb) {
  const cid = cb.value;
  if (cb.checked) {
    checked.sessions.add(cid);
    const s = (items.sessions || []).find(x => x.id === cid);
    if (s && s.workspace) checked.workspaces.add(s.workspace);   // 联动：所属空间
    if (s && s.has_tasks) checked.tasks.add(cid);                // 联动：关联任务目录
    renderItems();                                               // 联动后刷新空间/任务勾选
  } else {
    checked.sessions.delete(cid);                                // 取消对话不反向取消，避免误删手动勾选
  }
  updateHint();
}

function getSelection() {
  return { workspaces: [...checked.workspaces], tasks: [...checked.tasks], sessions: [...checked.sessions] };
}

function updateHint() {
  const n = checked.workspaces.size + checked.tasks.size + checked.sessions.size;
  document.getElementById('sel-hint').textContent = n ? ('已选 ' + n + ' 项') : '默认全量';
}

function toggleSelect() {
  const b = document.getElementById('sel-body');
  b.style.display = (b.style.display === 'none') ? 'block' : 'none';
}

function selAll(v) {
  if (v) {
    items.workspaces.forEach(w => checked.workspaces.add(w.path));
    items.tasks.forEach(t => checked.tasks.add(t.path));
    (items.sessions || []).forEach(s => checked.sessions.add(s.id));
  } else {
    checked.workspaces.clear(); checked.tasks.clear(); checked.sessions.clear();
  }
  renderItems();
  updateHint();
}

function toggleHelp() {
  const b = document.getElementById('help-body');
  b.style.display = (b.style.display === 'none') ? 'block' : 'none';
}

async function editSyncRoot() {
  const cur = document.getElementById('sync').textContent;
  const def = (cur && cur !== '—' && cur !== '(未探测到)') ? cur : '';
  const p = prompt('输入同步目录完整路径（任意云盘 / U盘 / 局域网共享，例如 D:\\坚果云\\wb-sync）：', def);
  if (p === null) return;
  const r = await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sync_root: p})
  });
  const d = await r.json();
  if (d.ok) { alert('已设置同步目录：' + d.sync_root); loadInfo(); }
  else { alert('设置失败：' + d.error); }
}

let _poll = null;

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60);
  return m ? (m + ' 分 ' + (sec % 60) + ' 秒') : (sec + ' 秒');
}

function stopPoll() { if (_poll) { clearInterval(_poll); _poll = null; } }

function startPoll(action) {
  const box = document.getElementById('prog');
  const bar = document.getElementById('prog-bar');
  const lab = document.getElementById('prog-label');
  const tim = document.getElementById('prog-time');
  const logBox = document.getElementById('log');
  box.style.display = 'block';
  box.classList.add('indet');       // 还没拿到第一个百分比时先走不确定动画
  bar.style.width = '0%';
  const tick = async () => {
    try {
      const r = await fetch('/api/progress', { cache: 'no-store' });
      const p = await r.json();
      const pct = p.pct || 0;
      if (pct > 0) box.classList.remove('indet');
      bar.style.width = pct + '%';
      lab.textContent = (p.label || '处理中') + '　' + pct + '%';
      tim.textContent = fmtDur(p.elapsed);
      if (p.tail) {
        logBox.innerHTML = '<span class="dim">$ wb-sync ' + esc(action) + '</span>\n' +
                           '<span class="dim">' + esc(p.tail) + '</span>';
        logBox.scrollTop = logBox.scrollHeight;
      }
    } catch (e) { /* 面板正在执行长任务，偶发取不到就跳过这一轮 */ }
  };
  tick();
  _poll = setInterval(tick, 700);
}

function finishPoll(ok, code) {
  stopPoll();
  const box = document.getElementById('prog');
  const bar = document.getElementById('prog-bar');
  const lab = document.getElementById('prog-label');
  box.classList.remove('indet');
  bar.style.width = '100%';
  lab.textContent = (ok ? '完成' : '已结束（退出码 ' + code + '）') + '　100%';
}

async function run(btn) {
  const action = btn.dataset.action;
  if (action === 'pull') {
    if (!confirm('拉取会写入数据库，需先完全退出 WorkBuddy。\n确认要拉取吗？')) return;
  }
  document.querySelectorAll('button').forEach(b => b.classList.add('busy'));
  const log = document.getElementById('log');
  log.innerHTML = '<span class="dim">执行 ' + action + ' 中…</span>';
  startPoll(action);
  try {
    const body = {action: action};
    if (action === 'push') {
      const sel = getSelection();
      if (sel.workspaces.length || sel.tasks.length || sel.sessions.length) body.selection = sel;
    }
    const r = await fetch('/api/run', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    const ok = d.code === 0;
    const tag = ok ? '<span class="badge ok">完成</span>' : '<span class="badge err">退出码 ' + d.code + '</span>';
    log.innerHTML = '<span class="dim">$ wb-sync ' + action + '　' + tag + '</span>\n' + esc(d.output);
    finishPoll(ok, d.code);
    loadInfo();          // 刷新最后推送/拉取时间与成员表
  } catch (e) {
    log.innerHTML = '<span class="err">请求失败：' + esc(String(e)) + '</span>';
    finishPoll(false, '异常');
  } finally {
    stopPoll();
    document.querySelectorAll('button').forEach(b => b.classList.remove('busy'));
  }
}

document.querySelectorAll('button').forEach(b => b.addEventListener('click', () => run(b)));
loadInfo();
loadItems();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    #: 启动时由 main() 写入实际端口，供同源校验使用
    port = DEFAULT_PORT

    def _origin_ok(self) -> bool:
        """只接受本机来源。

        面板是无鉴权的本地 HTTP 服务：只要浏览器打开任意网页，页面里的脚本
        就能向 http://127.0.0.1:8765/api/run POST 一个 action=push —— 用户
        不会察觉。这里用 Host / Origin 双重校验把跨站请求挡掉：
          · Host 必须是 127.0.0.1 / localhost（挡 DNS rebinding）
          · 若带 Origin，其主机也必须是本机
        curl / 本机脚本不带 Origin，照常放行。
        """
        allowed = {"127.0.0.1", "localhost", "::1"}
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if host and host not in allowed:
            return False
        origin = self.headers.get("Origin")
        if origin:
            try:
                from urllib.parse import urlsplit
                o = urlsplit(origin)
                if (o.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}:
                    return False
            except ValueError:
                return False
        return True

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._origin_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/info":
            self._send(200, json.dumps(build_info(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/progress":
            self._send(200, json.dumps(run_progress(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/items":
            self._send(200, json.dumps(core.list_selectable(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/config":
            self._send(200, json.dumps(get_config(), ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._origin_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        # 面板只服务本机，body 也只该是几百字节的 JSON。给个上限，
        # 免得一个误发的超大请求把内存吃满（服务是常驻的）。
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 1_048_576:
            self._send(413, b"payload too large", "text/plain")
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        if self.path == "/api/config":
            result = set_sync_root(payload.get("sync_root", ""))
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if self.path == "/api/name":
            result = set_machine_name(payload.get("name", ""))
            self._send(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return

        if self.path != "/api/run":
            self._send(404, b"not found", "text/plain")
            return
        action = payload.get("action", "")
        selection = payload.get("selection")
        if action not in ("doctor", "status", "push", "pull", "where"):
            self._send(400, json.dumps({"error": "bad action"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        result = run_action(action, selection)
        # 已有一个动作在跑：409 而不是 200，让前端/脚本能区分「忙」和「跑完」
        status = 409 if result.get("busy") else 200
        self._send(status, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def log_message(self, *args):  # 静默，不打印每次请求
        pass


def main(port: int | None = None) -> int:
    # port 由 cli 传入（wb-sync ui --port N）；直接 `python -m wb_sync.ui` 时
    # 没有 parser，退化为读 sys.argv。
    if port is None:
        port = DEFAULT_PORT
        if "--port" in sys.argv:
            try:
                port = int(sys.argv[sys.argv.index("--port") + 1])
            except (ValueError, IndexError):
                port = DEFAULT_PORT
    # 多线程：push/pull 可能跑几分钟，单线程会让页面（含 /api/info 轮询）整段卡死。
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    Handler.port = port
    url = f"http://127.0.0.1:{port}"

    # pythonw 后台启动（开机自启场景）时 stdout 为 None，print 会抛异常；
    # 统一兜底成静默，避免面板还没 serve 就挂掉。
    def _say(msg: str) -> None:
        try:
            print(msg)
        except Exception:  # noqa: BLE001
            pass

    _say(f"WorkBuddy 同步助手 面板已启动：{url}")
    _say("按 Ctrl+C 停止。")

    # 打开浏览器：放到后台 daemon 线程里做。
    # 原实现在 serve_forever() 之前同步调用 webbrowser.open()，在非交互会话
    # （计划任务 / 后台启动）下该调用可能永久阻塞 —— 端口已 listen，却永远
    # 不 accept，页面一直转圈。改为 daemon 线程后，无论成败都不再挡住服务。
    # 设 WB_SYNC_NO_BROWSER=1 可完全跳过（供后台自启使用）。
    if not os.environ.get("WB_SYNC_NO_BROWSER"):
        try:
            threading.Thread(
                target=webbrowser.open, args=(url,), daemon=True
            ).start()
        except Exception:  # noqa: BLE001
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _say("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
