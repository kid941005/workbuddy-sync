# -*- coding: utf-8 -*-
"""运行时可变状态。

★ 为什么单独一个模块
    这套程序里有相当多的「全局状态」：同步目录、白名单、机器标识……
    它们都需要被多个函数共享，而且命令行参数会覆写其中一部分。

    如果拆包时让别的模块 `from .config import WB_HOME`，那么拿到的是
    **导入那一刻的值**；之后 cli 里 `WB_HOME = Path(args.wb_home)`
    只会改 config 模块自己的变量，core 里根本看不到 —— 这类 bug 极难查。

    所以：所有可变状态只在这里定义，任何地方访问都用 `state.WB_HOME`
    （属性查找发生在运行时，永远拿到最新值）。

不可变常量（版本号、默认清单、时钟容差……）放在 config.py。
"""
from __future__ import annotations

from pathlib import Path

# ── 路径 ──

HOME = Path.home()

#: WorkBuddy 用户级数据目录，可用 --wb-home 覆写（测试用）
WB_HOME: Path = HOME / ".workbuddy"

#: 工作空间（「空间」）根目录，可用 --workspace-root 覆写（测试用）
#: 实际值由 paths.find_workspace_root() 解析（会读客户端 app-config.json），
#: 这里只给一个与旧行为一致的兜底初值。
WORKSPACE_ROOT: Path = HOME / "WorkBuddy"

#: 两个源目录各自的来源说明，仅供 where / doctor 展示
SOURCE_SRC: tuple = ("", "")


# ── 白名单（由 config.apply_manifest() 按生效清单填充）──

SYNC_FILES: list = []
SYNC_DIRS: list = []
DB_TABLES: list = []
NEVER_SEGMENTS: set = set()
NEVER_SUFFIX: tuple = ()
SENSITIVE_WORDS: tuple = ()
EXTRA_GLOBS: list = []
MAX_WORKSPACE_FILE_MB: int = 200

#: 内容级敏感扫描（v2.4.0）：开关与豁免 glob。
#: 豁免的意义是「这个文件里有令牌，但我就是要同步它」——
#: 例如 cli-env/wecom-cli.sh。写进清单而不是代码，用户自己就能改。
SECRET_SCAN: bool = True
SECRET_ALLOW: list = []


# ── 机器标识（由 config.resolve_machine_id() 填充）──

#: 用于构成包文件名，必须稳定且唯一 —— state/from-<HOST>.zip
HOST: str = ""

#: 仅供人读的展示名（默认取主机名）
MACHINE_NAME: str = ""


def snapshot() -> dict:
    """当前状态快照，仅供自检/诊断输出使用。"""
    return {
        "wb_home": str(WB_HOME),
        "workspace_root": str(WORKSPACE_ROOT),
        "host": HOST,
        "machine_name": MACHINE_NAME,
        "sync_files": len(SYNC_FILES),
        "sync_dirs": len(SYNC_DIRS),
        "db_tables": len(DB_TABLES),
        "never_segments": len(NEVER_SEGMENTS),
        "never_suffix": len(NEVER_SUFFIX),
        "sensitive_words": len(SENSITIVE_WORDS),
        "extra_globs": len(EXTRA_GLOBS),
        "max_file_mb": MAX_WORKSPACE_FILE_MB,
        "secret_scan": SECRET_SCAN,
        "secret_allow": len(SECRET_ALLOW),
    }
