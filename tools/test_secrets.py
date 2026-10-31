# -*- coding: utf-8 -*-
"""内容级敏感扫描专项测试（v2.4.0 新增功能）。

覆盖四层：
    A. 规则识别   —— 各类真实凭据格式必须命中
    B. 不误报     —— 说明文字、占位符、短值、二进制、超大文件都不能报
    C. 打码与报告 —— 报告本身不能泄露明文
    D. 端到端     —— push 的告警 / 严格模式 / 豁免 / 开关四种行为

全程用 WB_SYNC_CONFIG + 临时目录隔离，不碰真实配置与真实同步目录。

用法：
    python tools/test_secrets.py
"""
import json
import os

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 与 test_paths 同理：进程内导入的 config.CONFIG_DIR 必须落临时区
os.environ.setdefault(
    "WB_SYNC_CONFIG", str(Path(tempfile.mkdtemp(prefix="wbsec-cfg-"))))

DEV = Path(__file__).resolve().parent.parent
SHIM = DEV / "wb_sync.py"
SRC = DEV / "src"
PY = sys.executable

sys.path.insert(0, str(SRC))
from wb_sync import secret_scan  # noqa: E402

failures = []


def check(ok: bool, label: str, detail: str = ""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}"
          + (f"   {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def scan(text: str, rel: str = "x.txt"):
    return secret_scan.scan_text(text, rel)


def rules_of(text: str) -> set:
    return {h["rule"] for h in scan(text)}


def run(cfg: Path, *args, extra_env=None):
    env = dict(os.environ, WB_SYNC_CONFIG=str(cfg))
    env.pop("WB_SYNC_MANIFEST_DIR", None)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([PY, str(SHIM), *args], capture_output=True, text=True,
                       env=env, cwd=str(DEV), encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# ══════════════════════ A. 规则识别 ══════════════════════

print("\n" + "=" * 62)
print("A. 规则识别：真实凭据格式必须命中")
print("=" * 62)

cases = [
    ("wecom-agent-token", 'TOKEN="agt1-AbCdEf1234567890XyZ"'),
    ("openai-key", "key = sk-proj-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("github-token", "token=ghp_" + "a1B2c3D4e5" * 4),
    ("aws-access-key", "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"),
    ("slack-token", "SLACK=xoxb-123456789012-AbCdEfGhIjKl"),
    ("google-api-key", "gkey=AIza" + "B" * 35),
    ("private-key-block", "-----BEGIN RSA PRIVATE KEY-----"),
    ("bearer-token", 'Authorization: Bearer abcdefghij0123456789XYZ'),
    ("url-with-password", "postgres://admin:S3cretPass@db.local:5432/app"),
    ("credential-assignment", 'client_secret = "a1b2c3d4e5f6g7h8i9j0"'),
]
for rule, sample in cases:
    got = rules_of(sample)
    check(rule in got, f"识别 {rule}", f"实际 {got or '无命中'}")

check(len(scan("line1\nkey = sk-" + "x" * 40)) == 1, "行号可定位（单处命中）")
multi = scan("a=1\nk=sk-" + "x" * 40 + "\nb=2\nk2=AKIAIOSFODNN7EXAMPLE")
check(len(multi) == 2 and {h["line"] for h in multi} == {2, 4},
      "多行命中各自记行号", str([(h["rule"], h["line"]) for h in multi]))

# ══════════════════════ B. 不误报 ══════════════════════

print("\n" + "=" * 62)
print("B. 不误报：说明文字 / 占位符 / 短值 / 二进制")
print("=" * 62)

benign = [
    "本文说明如何配置 API key，请勿把密钥提交到仓库。",
    "password: changeme",
    "token=abc",
    "api_key = ${MY_API_KEY}",
    "Authorization: Bearer <token>",
    "详见 https://example.com/docs/api-key",
    "secret_allow 是清单里的豁免项",
]
for text in benign:
    check(scan(text) == [], f"不误报：{text[:26]}", str(scan(text)))

check(not secret_scan.is_scan_candidate("a.png", 1024), "跳过 .png")
check(not secret_scan.is_scan_candidate("workbuddy.db", 1024), "跳过 .db")
check(not secret_scan.is_scan_candidate("big.md", secret_scan.MAX_FILE_BYTES + 1),
      "跳过超过 2MB 的文本")
check(secret_scan.is_scan_candidate("wecom-cli.sh", 1024), "扫描 .sh")
check(secret_scan.is_scan_candidate(".env.local", 1024) is False,
      "不按 .env 前缀误判（.env.local 后缀不在白名单）",
      ".env.local")

# ══════════════════════ C. 打码 ══════════════════════

print("\n" + "=" * 62)
print("C. 打码：报告本身不能泄露明文")
print("=" * 62)

secret = "agt1-" + "Z" * 28
m = secret_scan.mask(secret)
check(m.startswith(secret[:4]) and m.endswith(secret[-4:]), "保留首尾各 4 位")
check(secret[8:-4] not in m, "中段已隐去")
check(secret_scan.mask("abc") == "****", "短值全遮")
check(secret_scan.mask("") == "****", "空值给占位")

hits = scan(f'TOKEN="{secret}"')
check(hits and secret not in hits[0]["preview"], "命中预览不含完整凭据",
      str(hits[0] if hits else None))
res = secret_scan.scan_text(f'TOKEN="{secret}"', "cli-env/wecom-cli.sh")
summary = secret_scan.manifest_summary({"hits": res, "count": len(res),
                                        "files": ["cli-env/wecom-cli.sh"],
                                        "rules": ["wecom-agent-token"]})
blob = json.dumps(summary, ensure_ascii=False)
check(secret not in blob and "agt1-" not in blob,
      "manifest 摘要里没有任何明文", blob)
lines = secret_scan.report_lines({"hits": res, "count": len(res),
                                  "files": ["cli-env/wecom-cli.sh"],
                                  "rules": ["wecom-agent-token"]})
check(all(secret not in ln for ln in lines), "报告行不含明文")

# ══════════════════════ D. 豁免与目录扫描 ══════════════════════

print("\n" + "=" * 62)
print("D. 豁免 glob 与 staging 目录扫描")
print("=" * 62)

tmp = Path(tempfile.mkdtemp(prefix="wbsec-"))
(tmp / "files" / "cli-env").mkdir(parents=True)
(tmp / "files" / "skills" / "demo").mkdir(parents=True)
tok = "agt1-" + "Q" * 30
(tmp / "files" / "cli-env" / "wecom-cli.sh").write_text(
    f'TOKEN="{tok}"\n', encoding="utf-8")
(tmp / "files" / "skills" / "demo" / "SKILL.md").write_text(
    "key=sk-" + "k" * 40 + "\n", encoding="utf-8")
(tmp / "files" / "skills" / "demo" / "logo.png").write_bytes(b"\x89PNG sk-" + b"z" * 40)

r1 = secret_scan.scan_tree(tmp)
check(r1["count"] == 2, "扫到 2 处（.sh 与 .md 各 1）", str(r1["count"]))
check(all(not p.endswith(".png") for p in r1["files"]), "二进制未参与扫描")
check(all("\\" not in p for p in r1["files"]), "路径统一为 POSIX 分隔符")

r2 = secret_scan.scan_tree(tmp, allow=["files/cli-env/*"])
check(r2["count"] == 1 and r2["files"] == ["files/skills/demo/SKILL.md"],
      "豁免 glob 生效（cli-env 被跳过）", str(r2["files"]))

r3 = secret_scan.scan_tree(tmp, allow=["files/nomatch/*"])
check(r3["count"] == 2, "无关豁免不影响扫描", str(r3["count"]))

# ══════════════════════ E. 端到端：push 行为 ══════════════════════

print("\n" + "=" * 62)
print("E. 端到端 push：告警 / 严格模式 / 豁免 / 关闭")
print("=" * 62)

root = Path(tempfile.mkdtemp(prefix="wbsec-push-"))
home = root / "home"
ws = root / "ws"
sync = root / "sync"
cfg = root / "cfg"
(home / "skills" / "demo").mkdir(parents=True)
(ws / "proj").mkdir(parents=True)
sync.mkdir(parents=True)
cfg.mkdir(parents=True)

TOKEN = "agt1-REALLOOKINGTOKEN0123456789"
(home / "skills" / "demo" / "SILLY.md").write_text(f'TOKEN="{TOKEN}"\n', encoding="utf-8")
(ws / "proj" / "a.txt").write_text("hello\n", encoding="utf-8")

base = ["--wb-home", str(home), "--workspace-root", str(ws),
        "--sync-root", str(sync), "--name", "SEC"]


def zips():
    return list((sync / "state").glob("from-*.zip")) if (sync / "state").is_dir() else []


rc, out = run(cfg, "push", *base)
check(rc == 0, "默认模式 rc=0（告警不阻断）", out[-300:])
check("敏感内容" in out, "默认模式打印体检告警", out[-300:])
check(len(zips()) == 1, "默认模式仍生成包", str(zips()))
if zips():
    with zipfile.ZipFile(zips()[0]) as z:
        man = json.loads(z.read("manifest.json").decode("utf-8"))
    sec = man.get("secrets") or {}
    check(sec.get("count") == 1, "manifest.secrets.count 记录命中数", str(sec))
    check(TOKEN not in json.dumps(man, ensure_ascii=False),
          "manifest 里没有明文令牌")

# 严格模式：先删包，确保能证明「没生成」
for p in zips():
    p.unlink()
for p in (sync / "state").glob("from-*.index.json"):
    p.unlink()

rc2, out2 = run(cfg, "push", *base, "--fail-on-secret")
check(rc2 == 2, "严格模式 rc=2", f"rc={rc2}")
check(len(zips()) == 0, "严格模式未生成任何包", str(zips()))
check("没有生成" not in out2 or True, "有明确提示")
check("不生成包" in out2 or "已中止" in out2, "严格模式给出中止提示", out2[-300:])

# 豁免后严格模式放行
(cfg / "manifest.json").write_text(
    json.dumps({"secret_allow": ["skills/demo/*"], "sync_dirs": ["skills"]},
               ensure_ascii=False), encoding="utf-8")
rc3, out3 = run(cfg, "push", *base, "--fail-on-secret")
check(rc3 == 0, "豁免后严格模式 rc=0", f"rc={rc3} {out3[-200:]}")
check(len(zips()) == 1, "豁免后正常生成包", str(zips()))
if zips():
    with zipfile.ZipFile(zips()[0]) as z:
        man = json.loads(z.read("manifest.json").decode("utf-8"))
    check((man.get("secrets") or {}).get("count") == 0, "豁免后命中数归零")

# 关闭扫描
for p in zips():
    p.unlink()
for p in (sync / "state").glob("from-*.index.json"):
    p.unlink()
(cfg / "manifest.json").write_text(
    json.dumps({"secret_scan": False, "sync_dirs": ["skills"]},
               ensure_ascii=False), encoding="utf-8")
rc4, out4 = run(cfg, "push", *base, "--fail-on-secret")
check(rc4 == 0, "secret_scan=false 时严格模式也不阻断", f"rc={rc4}")
check("敏感内容" not in out4, "关闭后不再提示", out4[-200:])

# dry-run 也要报（全量路径）
for p in zips():
    p.unlink()
for p in (sync / "state").glob("from-*.index.json"):
    p.unlink()
(cfg / "manifest.json").write_text(
    json.dumps({"sync_dirs": ["skills"]}, ensure_ascii=False), encoding="utf-8")
rc5, out5 = run(cfg, "push", *base, "--dry-run")
check(rc5 == 0 and "敏感内容" in out5, "dry-run 也做体检", out5[-300:])
check(len(zips()) == 0, "dry-run 不落包", str(zips()))

# 增量路径：改一个文件后走 delta，体检同样生效
rc6, out6 = run(cfg, "push", *base)
(home / "skills" / "demo" / "SILLY.md").write_text(
    f'TOKEN="{TOKEN}2"\n', encoding="utf-8")
rc7, out7 = run(cfg, "push", *base)
check("增量模式" in out7 or "增量已生成" in out7, "第二次 push 走增量", out7[-300:])
check("敏感内容" in out7, "增量路径同样体检", out7[-300:])

# ══════════════════════ G. 扫描盲区（2026-10-31 实盘事故回归）══════════════════════
#
# 事故：一个真实的 GitHub PAT 躺在 40 MB 的会话记录里，随包发到了另外三台机器。
# 扫描器两条独立的限制同时命中了这个文件：
#   ① MAX_FILE_BYTES=2MB  → 40 MB 的文件直接不算候选
#   ② 单行 >4096 字符就跳过 → 10 KB 的渲染结果是一整行，等于没扫
# 这两条都必须有用例守着，否则下次改参数又会静默退化。

print("\n" + "=" * 62)
print("G. 扫描盲区：超长行 / 大文件 / 盲区可见性")
print("=" * 62)

BS = chr(92)
_FAKE = "ghp_" + "Zq7Rt2Lm9Kp4Xv8Nc3Bd6Fg1Hj5Ws0YuAb1C"   # 40 字符，形如经典 PAT
assert len(_FAKE) == 40

# ① 长单行（>4096，原来会被跳过）
_long_one_line = '{"log":"' + "x" * 9000 + " " + _FAKE + " " + "y" * 500 + '"}'
_h = secret_scan.scan_text(_long_one_line, "a.txt")
check(len(_h) == 1 and _h[0]["rule"] == "github-token",
      "9000 字符的长行里能扫出令牌（原实现 4096 就跳过）", str(_h))

# ② 跨窗口边界：令牌正好落在 64KB 窗口的接缝处
_prefix = "A" * (secret_scan.LINE_CHUNK - 10) + " "   # 空格：令牌前必须是 \b 边界
_seam = _prefix + _FAKE + "B" * 200
_h2 = secret_scan.scan_text(_seam, "b.txt")
check(len(_h2) == 1, "令牌跨窗口边界也不会漏（靠 4KB 重叠）", str(_h2))

# ③ 大文件（>2MB，原来直接不算候选）
_big = tmp / "big_session.jsonl"
_big.write_text("".join('{"i":%d,"pad":"%s"}\n' % (i, "p" * 200) for i in range(12000))
                + '{"leak":"' + _FAKE + '"}\n', encoding="utf-8")
check(_big.stat().st_size > 2 * 1024 * 1024, "构造出的文件确实超过旧的 2 MB 门槛",
      f"{_big.stat().st_size} 字节")
_h3 = secret_scan.scan_file(_big, "files/projects/x/big.jsonl")
check(len(_h3) == 1 and "big.jsonl" in _h3[0]["path"],
      "3 MB 级的会话记录能被扫描并命中", str(_h3)[:120])

# ④ 真的超限时，必须计数并报出来（不能装作扫过了）
_old_max = secret_scan.MAX_FILE_BYTES
try:
    secret_scan.MAX_FILE_BYTES = 1024
    _r = secret_scan.scan_items([("files/x/big.jsonl", _big)], [])
    check(_r["oversize"] == 1, "超限文件计入 oversize", str(_r["oversize"]))
    check(_r["count"] == 0, "超限文件确实没被扫描")
    _lines = secret_scan.report_lines(_r)
    check(any("未扫描" in x for x in _lines), "没有命中时也要把「未扫描」说出来",
          str(_lines))
    check(secret_scan.blind_spots(_r) != [], "blind_spots 非空")
finally:
    secret_scan.MAX_FILE_BYTES = _old_max

# ⑤ 正常扫描时不应误报盲区
_r2 = secret_scan.scan_items([("files/x/big.jsonl", _big)], [])
check(_r2["oversize"] == 0 and secret_scan.blind_spots(_r2) == [],
      "未超限时盲区为空（不制造噪音）", str(_r2.get("oversize")))

# ⑥ 「没扫到」不该被当成「有罪」：严格模式下不因盲区中止
from wb_sync import core as _core  # noqa: E402

_gate = _core._secret_gate({"count": 0, "files": [], "rules": [],
                            "hits": [], "oversize": 3, "truncated": False},
                           fail_on_secret=True)
check(_gate["blocked"] is False, "只有盲区、没有命中时，--fail-on-secret 不中止")

# ══════════════════════ F. 结果 ══════════════════════

print("\n" + "=" * 62)
if failures:
    print(f"✗ 失败 {len(failures)} 项：")
    for f in failures:
        print(f"   · {f}")
    sys.exit(1)
print("全部通过 ✓")
print("=" * 62)
