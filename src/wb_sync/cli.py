# -*- coding: utf-8 -*-
"""命令行入口。

    wb-sync doctor     环境自检（先跑这个）
    wb-sync where      看当前生效的配置与清单来源
    wb-sync status     看两侧差异，不动文件
    wb-sync push       本机 -> 同步目录
    wb-sync pull       同步目录 -> 本机
    wb-sync ui         打开本地 Web 面板（可视化操作）

⚠️ 铁律
    1. push 可以在 WorkBuddy 运行时执行（只读快照）
    2. pull 会写数据库，执行前必须完全退出 WorkBuddy
    3. 同一时间只让一台电脑 push
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ENGINE_VERSION
from . import config
from . import core
from . import paths
from . import state
from .utils import human


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wb-sync",
        description="WorkBuddy 全资产双向同步 —— 通过任意共享目录在机器之间同步配置、"
                    "技能、记忆、对话、任务与空间",
    )
    ap.add_argument(
        "action",
        choices=["doctor", "status", "push", "pull", "where", "ui"],
        help="doctor 自检 / where 看配置 / status 看差异 / push 上传 / pull 拉取 / ui 面板",
    )
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写文件")
    ap.add_argument("--force", action="store_true",
                    help="pull：WorkBuddy 运行时仍强制写库（有损坏风险）；"
                         "push：内容未变也强制重建包")
    ap.add_argument("--sync-root", help="覆盖同步根目录（默认取 config.json）")
    ap.add_argument("--name", help="覆盖本机显示名")
    ap.add_argument("--manifest-dir", help="指定 default-manifest.json 所在目录")
    ap.add_argument("--wb-home", help="覆盖 WorkBuddy 目录（测试用）")
    ap.add_argument("--workspace-root", help="覆盖空间目录（测试用）")
    ap.add_argument("--selection", help='选择性同步 JSON：{"workspaces":[...],"tasks":[...]}（供 UI 使用）')
    ap.add_argument("--version", action="version", version=f"wb-sync {ENGINE_VERSION}")
    return ap


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)

    # ui 面板是独立入口：它自己会做初始化（_init_env），
    # 不参与下面同步流程的「清单 / 配置 / 机器标识」加载。
    if args.action == "ui":
        from . import ui
        return ui.main()

    # 1) 配置（决定「同步到哪」，同时为源目录解析提供 workspace_root 覆盖项）
    cfg = config.load_config()
    if args.name:
        cfg = dict(cfg, machine_name=args.name)

    # 2) 源目录：WB_HOME 与空间根。
    #    空间根优先取 WorkBuddy 客户端 app-config.json 的 defaultWorkspacePath ——
    #    用户把工作区挪到别的盘时，只有这里知道新位置。写死 ~/WorkBuddy 会
    #    静默同步一个空目录。
    wb_home, wb_src = paths.find_workbuddy_home(args.wb_home)
    state.WB_HOME = wb_home
    ws_root, ws_src = paths.find_workspace_root(wb_home, args.workspace_root, cfg)
    state.WORKSPACE_ROOT = ws_root
    state.SOURCE_SRC = (wb_src, ws_src)

    # 3) 清单 —— 决定「同步什么」
    man, man_sources = config.load_manifest(
        Path(args.manifest_dir) if args.manifest_dir else None)
    config.apply_manifest(man)

    # 4) 同步目录 —— 决定「同步到哪」
    sync_root, root_src = config.resolve_sync_root(cfg, args.sync_root)
    if sync_root is None:
        print(root_src)          # root_src 此时是引导文本
        return 1

    # 5) 机器标识 —— 决定「以谁的身份写包」
    first_run = not (config.CONFIG_DIR / "machine-id").is_file()
    config.resolve_machine_id(cfg, sync_root)

    if first_run and args.action in ("push", "pull"):
        print("首次运行，已为本机分配标识")
        config.describe_scope()
        print(f"  同步目录：{sync_root}   ← {root_src}")
        print()

    if args.action == "where":
        print(f"引擎版本   {ENGINE_VERSION}")
        print(f"本机标识   {state.HOST}    （展示名 {state.MACHINE_NAME}）")
        print(f"配置目录   {config.CONFIG_DIR}")
        print(f"WorkBuddy  {state.WB_HOME}   ← {wb_src}")
        print(f"空间根     {state.WORKSPACE_ROOT}   ← {ws_src}")
        print(f"同步目录   {sync_root}   ← {root_src}")
        ok, cinfo = config.check_cluster(sync_root, create=False)
        print(f"共享目录ID {cinfo if ok else '不一致，见 doctor'}")
        print(f"清单来源   {' < '.join(man_sources)}")
        print(f"同步范围   {len(state.SYNC_FILES)} 个文件 · {len(state.SYNC_DIRS)} 个目录 · "
              f"{len(state.DB_TABLES)} 张表")
        print(f"排除规则   {len(state.NEVER_SEGMENTS)} 段 · {len(state.NEVER_SUFFIX)} 后缀 · "
              f"{len(state.EXTRA_GLOBS)} glob · 敏感词 {len(state.SENSITIVE_WORDS)}")
        return 0

    if args.action == "doctor":
        return core.do_doctor(sync_root, root_src, man_sources)
    if args.action == "status":
        return core.do_status(sync_root, root_src)

    if args.action == "push":
        selection = None
        if args.selection:
            try:
                selection = json.loads(args.selection)
            except ValueError:
                print("[!] --selection 不是合法 JSON，已按全量同步处理")

        # 同步目录必须真的存在，否则会在错误盘符上造出一份没人读的空目录，
        # 「推送成功」但数据从未进入共享目录 —— 这是多设备分叉最隐蔽的来源。
        if not sync_root.is_dir():
            parent_ok = sync_root.parent.is_dir()
            if not parent_ok:
                print(f"[!] 同步目录不存在且父目录也不存在：{sync_root}")
                print("    多半是盘符或网盘路径写错，请检查 config.json 的 sync_root。")
                return 1
            print(f"[!] 同步目录尚不存在，即将新建：{sync_root}")
            print("    如果这不是你两台机器共用的那个目录，请先按 Ctrl+C 并修正配置。")

        ok, cinfo = config.check_cluster(sync_root, create=True)
        if not ok:
            print(cinfo)
            return 1

        print(f"方向：{state.HOST} -> {sync_root}")
        info = core.build_package(sync_root, args.dry_run, selection,
                                  force=args.force)
        if args.dry_run:
            return 0
        if info.get("skipped"):
            print(f"  包未变化，沿用：{info['zip']}（{human(info['bytes'])}）")
            print("  要强制重建，加 --force")
            return 0
        print(f"  包已生成：{info['zip']}")
        print(f"  {info['files']} 个文件，压缩后 {human(info['bytes'])}")
        print("  下一步：等同步盘同步完成，再到另一台电脑执行 pull")
        return 0

    if args.action == "pull":
        from .platform_win import workbuddy_running

        if workbuddy_running() and not args.force:
            print("[!] 请先完全退出 WorkBuddy 再执行 pull")
            print("    （数据库需要独占写入；确认风险可加 --force）")
            return 1

        ok, cinfo = config.check_cluster(sync_root, create=False)
        if not ok:
            print(cinfo)
            return 1

        print(f"方向：{sync_root} -> {state.HOST}")
        return core.do_pull(sync_root, args.dry_run, args.force)

    return 0


if __name__ == "__main__":
    sys.exit(main())
