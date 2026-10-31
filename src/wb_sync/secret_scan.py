# -*- coding: utf-8 -*-
"""内容级敏感信息扫描 —— 补上「路径过滤」拦不住的那一类泄漏。

★ 为什么需要这一层
    既有过滤（core.is_excluded）只看**路径名**：sensitive_words / never_suffix
    命中时整个文件不进包。但凭据常常待在名字完全正常的文件里 —— 例如
    cli-env/wecom-cli.sh 里的 agt1-… 令牌，路径层面看不出任何异常。一旦 push，
    这个包会被同步盘复制到每台机器，等于把密钥主动扩散到所有设备。

    所以这里补一层**内容扫描**：只读文本、按规则匹配，命中就报
    「文件 + 规则 + 行号 + 打码片段」—— 既不漏报，也不把明文再打印一遍。

★ 设计取舍（都是刻意的）
    · 默认「告警不阻断」：白名单里可能有用户明确要同步的令牌文件（如 cli-env），
      默认阻断会让每次 push 都失败。需要严格把关时用 --fail-on-secret。
    · 只扫文本类文件、单文件 2 MB、单次 512 MB 上限：push 本来就要把这些字节
      复制一遍，多一次顺序读的代价可以接受；但不能因为一个 500 MB 的模型文件
      把 push 拖成两次 I/O。
    · 规则只认「有明确前缀/结构」的凭据（agt1- / sk- / ghp_ / AKIA / 私钥块…），
      不认「高熵随机串」：后者误报太多，报多了用户就会整体忽略告警，等于没有。
    · 命中值一律打码后再输出。扫描报告本身是会被贴进聊天窗口和日志的。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

# ─────────────────────────── 扫描参数 ───────────────────────────

#: 单文件扫描上限。超过就不读 —— 凭据不会藏在 200 MB 的二进制尾部，
#: 而读它要付真实代价。
MAX_FILE_BYTES = 2 * 1024 * 1024

#: 单次扫描的累计读取上限，防止首次全量 push 时把 2 GB 文本全过一遍。
MAX_TOTAL_BYTES = 512 * 1024 * 1024

#: 认定为「文本」的扩展名。故意不含 .zip/.png/.db 等二进制。
TEXT_SUFFIX = (
    ".json", ".jsonl", ".md", ".markdown", ".txt", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".conf", ".config", ".properties", ".env", ".sh", ".bash",
    ".zsh", ".cmd", ".bat", ".ps1", ".vbs", ".py", ".js", ".mjs", ".cjs", ".ts",
    ".tsx", ".jsx", ".xml", ".html", ".htm", ".css", ".sql", ".log", ".gitconfig",
)

#: 没有扩展名但按文本处理的文件名（点开头，Path.suffix 拿不到）
TEXT_NAMES = (".env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass")

# ─────────────────────────── 规则 ───────────────────────────
# 每条 = (规则名, 正则)。规则名会出现在报告和 manifest 里，保持稳定、可检索。
# 正则只匹配「凭据本体」，上下文交给行号；命中值统一走 mask()。

RULES = (
    # 企业微信 AI 机器人令牌：本项目最现实的一条 —— cli-env/wecom-cli.sh 里就是它
    ("wecom-agent-token", re.compile(r"agt1-[A-Za-z0-9_\-]{16,}"), False),
    # OpenAI / 兼容网关
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"), False),
    # GitHub PAT（经典 + 细粒度）
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}"
                                r"|\bgithub_pat_[A-Za-z0-9_]{20,}"), False),
    # AWS access key id
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), False),
    # Slack
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), False),
    # Google API key
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), False),
    # 私钥块：一旦同步出去无法「换回来」，只能吊销
    ("private-key-block",
     re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
     False),
    # 请求头里的 Bearer
    ("bearer-token",
     re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?bearer\s+([A-Za-z0-9._\-]{20,})"),
     True),
    # URL 内嵌密码：postgres://user:pw@host、https://user:pw@git…
    # 密码至少 6 位（模板里的 user:pass@ 只有 4 位，属于文档占位符）
    ("url-with-password",
     re.compile(r"(?i)\b[a-z][a-z0-9+.\-]{1,15}://[^\s:/@]{1,64}:([^\s@/:]{6,})@"),
     True),
    # 赋值式（带引号的字面量）：api_key = "…" / client_secret: '…'
    # ★ 只认引号内的值。v2.4.0 初版允许不带引号，实测在 pip / requests 这类
    #   第三方源码上误报一片 —— self.password、raw_token.strip()、cls.token
    #   全被当成凭据。带引号才是「写死在配置里的值」。
    ("credential-assignment",
     re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token"
                r"|client[_-]?secret|app[_-]?secret|password|passwd|token)\b\s*[:=]\s*"
                r"[\"']([^\"'\s]{16,})[\"']"),
     True),
    # 赋值式（不带引号但足够长且是纯令牌字符）：api_key: 4f9c…（32 位以上）
    ("credential-assignment-bare",
     re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token"
                r"|client[_-]?secret)\b\s*[:=]\s*([A-Za-z0-9_\-]{32,})\b"),
     True),
)

#: 明显不是凭据的取值：文档占位符、示例值、空壳。
#: 放过它们不是为了「少报」，而是为了让每一条告警都值得看一眼 ——
#: 一份全是 self.password 的报告，用户第二次就会整个忽略。
PLACEHOLDERS = frozenset({
    "pass", "password", "passwd", "pwd", "secret", "changeme", "change-me",
    "change_me", "example", "yourpassword", "your_password", "your-password",
    "token", "apikey", "api_key", "api-key", "dummy", "test", "testing",
    "redacted", "placeholder", "none", "null", "undefined", "xxx", "****",
})


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v or v in PLACEHOLDERS:
        return True
    # 模板变量与文档写法：{token} / ${KEY} / <your-key> / %s
    if any(ch in v for ch in "{}<>$%"):
        return True
    # your_xxx / xxx_key / example-… 这类命名式占位
    return any(w in v for w in ("your_", "your-", "example", "xxx", "placeholder"))


def mask(value: str) -> str:
    """把凭据打码成「前 4 + 后 4」，长度不足时全遮。

    短令牌（< 12）保留首尾各 4 位会露出大半，所以直接全遮。
    """
    v = (value or "").strip()
    if len(v) < 12:
        return "*" * max(4, len(v))
    return f"{v[:4]}…{v[-4:]}"


def is_scan_candidate(name: str, size: int) -> bool:
    """按文件名与体积判断是否值得读。"""
    if size <= 0 or size > MAX_FILE_BYTES:
        return False
    low = name.lower()
    if low in TEXT_NAMES:
        return True
    return low.endswith(TEXT_SUFFIX)


def _is_allowed(rel: str, allow) -> bool:
    """命中豁免清单（glob）就跳过 —— 给「明知有令牌但要同步」留出口。

    两种写法都认：`cli-env/*` 与 `files/cli-env/*`。
    扫描发生在 staging 上，相对路径带 `files/` / `workspaces/` 前缀，但用户
    写豁免时想的是「源目录里的哪个文件」，不该被迫记住这个内部前缀。
    """
    if not allow:
        return False
    norm = rel.replace("\\", "/")
    bare = norm.split("/", 1)[1] if norm.startswith(("files/", "workspaces/")) else norm
    for g in allow:
        if fnmatch.fnmatch(norm, g) or fnmatch.fnmatch(bare, g):
            return True
    return False


def scan_text(text: str, rel: str) -> list:
    """扫一段文本，返回命中列表 [{path, rule, line, preview}]。

    逐行扫而不是整篇扫：一是要行号，二是避免跨行正则把无关内容串起来。
    """
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line or len(line) > 4096:
            # 超长单行（压缩过的 js / base64 块）跳过：既没行号价值，也慢
            continue
        for rule_name, pattern, filt in RULES:
            m = pattern.search(line)
            if not m:
                continue
            raw = m.group(1) if m.groups() else m.group(0)
            if filt and _is_placeholder(raw):
                # 占位符不算凭据，换下一条规则继续看
                continue
            hits.append({
                "path": rel,
                "rule": rule_name,
                "line": lineno,
                "preview": mask(raw),
            })
            # 一行只报第一条命中的规则：同一行的 token 重复报没有意义
            break
    return hits


def scan_file(path: Path, rel: str) -> list:
    """扫单个文件。读不了就返回空 —— 扫描是辅助功能，不能影响 push 主流程。"""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if not is_scan_candidate(path.name, size):
        return []
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)[:MAX_FILE_BYTES]
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace")
    return scan_text(text, rel)


def scan_items(items, allow=None) -> dict:
    """扫一组 (相对路径, 绝对路径)，返回汇总。

    汇总结构直接可做 manifest 字段 —— 只有「路径 + 规则名」，**不含任何明文**，
    所以可以安全地随包传给对端，让另一台机器也知道「这个包里带了令牌」。
    """
    allow = tuple(allow or ())
    hits: list = []
    scanned = 0
    budget = MAX_TOTAL_BYTES
    truncated = False
    for rel, path in items:
        norm = str(rel).replace("\\", "/")
        if _is_allowed(norm, allow):
            continue
        if budget <= 0:
            truncated = True
            break
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not is_scan_candidate(path.name, size):
            continue
        scanned += 1
        budget -= min(size, MAX_FILE_BYTES)
        hits.extend(scan_file(path, norm))

    files = sorted({h["path"] for h in hits})
    rules = sorted({h["rule"] for h in hits})
    return {
        "hits": hits,
        "count": len(hits),
        "files": files,
        "rules": rules,
        "scanned": scanned,
        "truncated": truncated,
    }


def scan_tree(root: Path, allow=None) -> dict:
    """扫一棵已经 staged 的目录树（包内路径 = 相对 root 的 POSIX 路径）。

    在 staging 上扫的好处：看到的就是「即将进包的那批字节」，
    全量 / 选择性 / 增量三种模式共用同一段代码，不会各写一套漏掉一条路径。
    """
    items = []
    try:
        for p in root.rglob("*"):
            if p.is_file():
                items.append((p.relative_to(root).as_posix(), p))
    except OSError:
        pass
    return scan_items(items, allow)


def report_lines(result: dict, limit: int = 8) -> list:
    """把汇总渲染成人读的几行，不含明文。"""
    if not result or not result.get("count"):
        return []
    lines = [f"发现 {result['count']} 处疑似凭据，分布在 "
             f"{len(result['files'])} 个文件（规则：{', '.join(result['rules'])}）"]
    shown = 0
    for h in result.get("hits") or []:
        if shown >= limit:
            lines.append(f"  … 另有 {result['count'] - shown} 处未列出")
            break
        lines.append(f"  {h['path']}:{h['line']}  {h['rule']}  {h['preview']}")
        shown += 1
    if result.get("truncated"):
        lines.append("  ⚠ 扫描达到总量上限，后段文件未检查")
    return lines


def manifest_summary(result: dict) -> dict:
    """写进包 manifest 的精简版（无明文、无行号细节）。"""
    return {
        "count": int(result.get("count") or 0),
        "files": list(result.get("files") or [])[:50],
        "rules": list(result.get("rules") or []),
    }
