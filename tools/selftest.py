# -*- coding: utf-8 -*-
"""wb-sync 自测：端到端验证标识分配、配置覆盖、未知资产检测、push/pull 合并、
两种入口（垫片 / 包）的等价性，以及跨版本合并提示。

全程在系统临时目录里运行，用 WB_SYNC_CONFIG 隔离配置，
不会读取也不会写入真实的 WorkBuddy 数据。

用法：
    python tools/selftest.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DEV = Path(__file__).resolve().parent.parent
SHIM = DEV / "wb_sync.py"          # 便携模式入口
SRC = DEV / "src"                  # 包模式入口
PY = sys.executable

base = Path(tempfile.mkdtemp(prefix="wbtest-"))
cfg_a, cfg_b = base / "cfgA", base / "cfgB"
sync = base / "sync"
sync.mkdir(parents=True)
cfg_a.mkdir(parents=True)
cfg_b.mkdir(parents=True)

failures = []


def run(env_cfg: Path, *args):
    """便携模式：python wb_sync.py <action>"""
    env = dict(os.environ, WB_SYNC_CONFIG=str(env_cfg))
    r = subprocess.run([PY, str(SHIM), *args], capture_output=True, text=True,
                       env=env, encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")


def run_pkg(env_cfg: Path, *args):
    """包模式：PYTHONPATH=src python -m wb_sync <action>

    CWD 必须是别处：若在 dev 根目录跑 `-m wb_sync`，sys.path[0]=CWD 会先命中
    同名的 wb_sync.py 垫片，那样测的就不是包模式了。
    """
    env = dict(os.environ, WB_SYNC_CONFIG=str(env_cfg), PYTHONPATH=str(SRC))
    env.pop("WB_SYNC_MANIFEST_DIR", None)      # 强制走包内清单
    r = subprocess.run([PY, "-m", "wb_sync", *args], capture_output=True, text=True,
                       env=env, cwd=str(base), encoding="utf-8", errors="replace")
    return (r.stdout or "") + (r.stderr or "")


def head(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not ok:
        failures.append(name)


# ── A) 新用户首次运行 ──
head("A) 首次运行：应生成稳定标识")
out = run(cfg_a, "where", "--sync-root", str(sync))
print(out)
id1 = (cfg_a / "machine-id").read_text(encoding="utf-8").strip()
check("生成标识", bool(id1), id1)

# ── B) 标识持久化 ──
head("B) 二次运行：标识应保持不变")
run(cfg_a, "where", "--sync-root", str(sync))
id2 = (cfg_a / "machine-id").read_text(encoding="utf-8").strip()
check("标识已持久化", id1 == id2, f"{id1} -> {id2}")

# ── C) config.json 覆盖 ──
head("C) config.json：应生效且无需命令行参数")
(cfg_a / "config.json").write_text(
    json.dumps({"sync_root": str(sync), "machine_name": "测试机A"},
               ensure_ascii=False, indent=2), encoding="utf-8")
out = run(cfg_a, "where")
print(out)
check("sync_root 来自配置", str(sync) in out)
check("machine_name 来自配置", "测试机A" in out)

# ── D) 未知资产检测 ──
head("D) 未知资产检测：只应提示 newfeature")
fake = base / "fakewb"
for d, f in [("skills", "a.md"), ("newfeature", "data.bin"),
             ("binaries", "big.exe"), ("memory", "log.md")]:
    (fake / d).mkdir(parents=True)
    (fake / d / f).write_text("x", encoding="utf-8")
(fake / "emptydir").mkdir()
out = run(cfg_a, "doctor", "--wb-home", str(fake))
print(out)
warn = ""
if "不在同步白名单里：" in out:
    warn = out.split("不在同步白名单里：", 1)[1].split("可能是客户端", 1)[0]
print(f"── 警告区块 ──{warn}")
check("命中 newfeature", "newfeature" in warn)
check("不误报 binaries（在排除表内）", "binaries" not in warn)
check("不误报空目录", "emptydir" not in warn)
check("不误报已在白名单的目录", "skills" not in warn and "memory" not in warn)

# ── E) 端到端 push / pull ──
head("E) 端到端：A 机 push -> B 机 pull")
wbA, wsA, wbB, wsB = base / "wbA", base / "wsA", base / "wbB", base / "wsB"
for p in (wbA, wsA, wbB, wsB):
    p.mkdir(parents=True)

(wbA / "SOUL.md").write_text("A 的人格文件\n", encoding="utf-8")
(wbA / "skills" / "s1").mkdir(parents=True)
(wbA / "skills" / "s1" / "SKILL.md").write_text("技能一\n", encoding="utf-8")
(wbA / "memory").mkdir(parents=True, exist_ok=True)
(wbA / "memory" / "m.md").write_text("第一行\n", encoding="utf-8")
(wsA / "proj.txt").write_text("A 的空间产物\n", encoding="utf-8")

(wbB / "skills" / "s2").mkdir(parents=True)
(wbB / "skills" / "s2" / "SKILL.md").write_text("技能二\n", encoding="utf-8")
(wbB / "memory").mkdir(parents=True, exist_ok=True)
(wbB / "memory" / "m.md").write_text("第二行\n", encoding="utf-8")

print("── A push ──")
print(run(cfg_a, "push", "--sync-root", str(sync),
          "--wb-home", str(wbA), "--workspace-root", str(wsA)))

print("── B pull ──")
print(run(cfg_b, "pull", "--sync-root", str(sync), "--force",
          "--wb-home", str(wbB), "--workspace-root", str(wsB)))

head("E) 结果断言")
check("对方独有技能已合并", (wbB / "skills" / "s1" / "SKILL.md").is_file())
check("本机独有技能保留", (wbB / "skills" / "s2" / "SKILL.md").is_file())
check("空间目录文件已同步", (wsB / "proj.txt").is_file())
m = (wbB / "memory" / "m.md").read_text(encoding="utf-8")
check("追加型日志行级并集", "第二行" in m and "第一行" in m, repr(m))

# ── F) 两种入口等价 ──
head("F) 便携模式与包模式：配置解析应完全一致")
sync2 = base / "sync2"
sync2.mkdir(parents=True)
cfg_f = base / "cfgF"
cfg_f.mkdir()
r_shim = run(cfg_f, "where", "--sync-root", str(sync2))
r_pkg = run_pkg(cfg_f, "where", "--sync-root", str(sync2))

def norm(text):
    """只比较关键行，忽略清单来源的绝对路径差异。"""
    keep = {}
    for line in text.splitlines():
        for key in ("引擎版本", "同步目录", "同步范围", "排除规则"):
            if line.strip().startswith(key):
                keep[key] = line.split(key, 1)[1].strip()
    return keep

n_shim, n_pkg = norm(r_shim), norm(r_pkg)
for k in n_shim:
    check(f"一致 · {k}", n_shim[k] == n_pkg.get(k),
          f"{n_shim[k]!r} vs {n_pkg.get(k)!r}")

# ── G) 跨版本合并提示 ──
head("G) 对方包版本不同：pull 应给出提示")
zp = sync / "state" / f"from-{id1}.zip"
repacked = base / "repacked.zip"
with zipfile.ZipFile(zp) as zin, zipfile.ZipFile(repacked, "w") as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "manifest.json":
            meta = json.loads(data.decode("utf-8"))
            meta["engine_version"] = "1.9.9"      # 伪造成另一个版本
            meta["schema"] = 1
            data = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        zout.writestr(item, data)
shutil.move(str(repacked), str(zp))

cfg_g = base / "cfgG"
cfg_g.mkdir()
out = run(cfg_g, "pull", "--sync-root", str(sync), "--force",
          "--wb-home", str(wbB), "--workspace-root", str(wsB))
print(out)
check("提示引擎版本不同", "1.9.9" in out and "版本不同" in out)
check("提示包结构不同", "包结构" in out)

# ── H) 非 UTF-8 系统区域：输出重定向不应崩溃 ──
# 回归护栏：英文区域的 Windows 上，输出被重定向到管道/文件时 Python 用 cp1252，
# 打印中文会抛 UnicodeEncodeError，把整个进程带走（CI / 计划任务 / > log.txt 全中）。
head("H) 英文区域（cp1252）：输出重定向不应崩溃")
sync_h = base / "syncH"
sync_h.mkdir(parents=True)
cfg_h = base / "cfgH"
cfg_h.mkdir()
env_h = dict(os.environ, WB_SYNC_CONFIG=str(cfg_h), PYTHONIOENCODING="cp1252")
r_h = subprocess.run([PY, str(SHIM), "where", "--sync-root", str(sync_h)],
                     capture_output=True, text=True, env=env_h,
                     encoding="utf-8", errors="replace")
out_h = (r_h.stdout or "") + (r_h.stderr or "")
print(out_h)
check("未崩溃（退出码 0）", r_h.returncode == 0, f"rc={r_h.returncode}")
check("中文正常输出", "引擎版本" in out_h)
check("无 UnicodeEncodeError", "UnicodeEncodeError" not in out_h)

# ── 收尾 ──
print(f"\n测试目录：{base}")
print("\n" + ("全部通过 ✓" if not failures else f"失败 {len(failures)} 项：{failures}"))
sys.exit(1 if failures else 0)
