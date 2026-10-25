# -*- coding: utf-8 -*-
"""WorkBuddy 全资产双向同步。

在多台机器之间，通过一个共享的「同步目录」双向同步 WorkBuddy 的个人资产：
配置、技能、记忆、对话、任务、空间（工作目录）。

同步目录可以是任何「双向同步型」的网盘或共享目录 —— OneDrive、坚果云、
百度网盘、局域网共享都行。不需要 VPN，也不依赖 GitHub。

包内结构：
    state.py          运行时可变状态（集中在一处，避免跨模块拿到旧值）
    utils.py          通用工具：时间戳 / 摘要 / 体积格式化
    paths.py          路径解析与跨设备重映射（源目录发现、路径规范化、cwd 编码校准）
    config.py         常量、清单加载、配置加载、机器标识、共享目录身份锚
    platform_win.py   Windows 平台相关：进程检测
    core.py           同步内核：资产收集 / 数据库合并 / 打包 / 合并远端
    cli.py            命令行入口

对外接口稳定在 `cli.main()`；模块内部结构可能随版本调整。
"""

__all__ = ["__version__", "ENGINE_VERSION", "MANIFEST_SCHEMA", "main"]

__version__ = "2.2.1"

# 引擎版本：写入包内 manifest.json，pull 时用来判断两端是否同版本。
# 跨大版本合并可能语义不同（例如清单结构变化），因此 pull 端会提示。
ENGINE_VERSION = __version__

# 包内 manifest.json 的 schema 版本。结构不兼容变更时递增。
MANIFEST_SCHEMA = 2


def main(argv=None) -> int:
    """延迟导入，避免 `import wb_sync`（只为读版本号）时拉起重依赖。"""
    from .cli import main as _main

    return _main(argv)
