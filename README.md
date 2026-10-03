# WorkBuddy 同步助手

> 技术名 `workbuddy-sync`（PyPI 包名）· 命令 `wb-sync`

在多台电脑之间**双向同步** WorkBuddy 的全部个人资产 —— 配置、技能、记忆、对话记录、
任务、自动化、空间（工作目录）。

通过一个共享的「同步目录」中转，**不需要 VPN，也不需要服务器**。
同步目录可以是 OneDrive、坚果云、百度网盘、局域网共享等任何**双向同步型**目录
（默认自动探测 OneDrive）。

```
   家里台式机                              公司笔记本
  ~/.workbuddy                            ~/.workbuddy
       │                                        │
       │  push                                  │  push
       ▼                                        ▼
  ┌──────────────────────────────────────────────────┐
  │        共享同步目录  <网盘>\wb-sync\state\         │
  │   from-<机器A>.zip          from-<机器B>.zip      │
  │        （各写各的包，永不互相覆盖）                 │
  └──────────────────────────────────────────────────┘
       ▲                                        ▲
       │  pull                                  │  pull
       └──────────── 双向合并 ───────────────────┘
```

---

## 它解决什么问题

WorkBuddy 的数据默认只在本机（`~/.workbuddy`）。两台机器同时使用就会出现**数据分叉**：
家里新建的技能公司看不到，公司跑的自动化家里看不到，对话记录各存各的。

本工具把这些资产通过共享目录双向合并，让多台机器最终**收敛到同一份数据**。

典型场景：

- 家里台式机 + 公司笔记本，两台都要用，希望数据一致
- 换新电脑，想把旧机器的对话/技能/任务一起带过去，**并且继续双向同步**
- 已经分叉了（A 机有对话没任务、B 机有任务没对话），想把两边合起来

---

## 特性

| | |
|---|---|
| **零第三方依赖** | 只用 Python 标准库，受限环境也能跑 |
| **per-machine 包** | 每台机器只写自己的 zip，永不覆盖对方 —— 从根上避开网盘冲突副本 |
| **不丢数据** | 追加型日志（对话记录）走**行级并集**；冲突文件留 `.conflict-` 备份 |
| **数据库安全** | 用 SQLite backup API 取一致性快照，**不会漏掉 WAL 里未合并的数据** |
| **幂等** | 数据库按主键去重（`INSERT OR IGNORE`），重复执行不产生重复数据 |
| **白名单是数据** | 同步范围写在 JSON 里，客户端升级新增资产时**改 JSON 即可，不用改代码** |
| **未知资产检测** | 主动发现「客户端升级后新增、但白名单没覆盖」的目录，解决静默漏同步 |
| **隐私默认值** | 内置敏感词 / 密码类后缀 / `auth/` 目录排除，首次运行明确告知上传范围 |

---

## 安装

> **上手三步**：① 装好（下面任选一种）→ ② 打开面板 → ③ 面板里点「自检 / 上传 / 拉取」。
> 面板就是操作台，**不用记任何命令**。

### 装完之后的第一步：打开面板

用**其中一种方式**打开面板，浏览器会自动打开 `http://127.0.0.1:8765`：

| 你的安装方式 | 打开面板的方式 |
|---|---|
| 便携模式 | 双击目录里的 `wb-ui.cmd` |
| pip 安装 | 命令行运行 `wb-sync ui` |

之后所有操作（自检 / 看差异 / 上传 / 拉取 / 勾选同步范围）都在面板里点按钮完成。

> 下面三种方式**功能完全等价**。包当前**尚未发布到 PyPI**，请先用方式一或方式二。
> 对「两台机器长期同步」这个场景，**方式二（便携模式）其实最省事** —— 代码在共享盘里
> 只有一份，改一处两台机器同时生效，不必每台都装。

### 方式一：从 GitHub Release 安装 wheel

到 [Releases](https://github.com/iushiyou/workbuddy-sync/releases/latest) 下载
`workbuddy_sync-<版本>-py3-none-any.whl`，然后：

```bash
# 把 <版本> 换成实际下载的版本号（如 2.1.0）
pip install ./workbuddy_sync-<版本>-py3-none-any.whl
wb-sync doctor
```

### 方式二：便携模式（无需安装）

把发行包解压到同步盘目录（例如 `D:\OneDrive\wb-sync\`），直接：

```bash
python wb_sync.py doctor
```

> 找不到 Python？先打开一次 WorkBuddy —— 它自带 Python 运行时。便携模式也可以
> 双击 `pull.cmd` / `push.cmd`，脚本会自己去找。

### 方式三：PyPI（即将上线）

```bash
pip install workbuddy-sync
wb-sync doctor
```

**系统要求**：Windows + Python 3.10 及以上。目前仅支持 Windows
（进程检测与自启接管依赖 Windows 能力）。

---

## 快速开始（两台机器）

> 无论哪台机器，**第一步都是打开面板**（双击 `wb-ui.cmd` 或运行 `wb-sync ui`）。
> 下面用面板按钮描述，对应的 CLI 命令见下一章「命令」。

### 第 1 台机器（数据源）

1. 打开面板 → 点「🔍 自检」，确认环境与同步范围正常
2. 展开「🎯 选择同步范围」勾选要同步的内容（不勾 = 全量）
3. 点「⬆️ 上传」，然后等网盘把数据同步到云端

### 第 2 台机器（目标）

1. 等网盘把同步目录同步下来（托盘图标变绿）
2. **完全退出 WorkBuddy**（含托盘图标）—— 拉取要写数据库
3. 打开面板 → 点「⬇️ 拉取」
4. 重新打开 WorkBuddy —— 对话、任务、空间就都在了
5. 再点「⬆️ 上传」把这台机器的数据也推回去

之后日常就一句话：**到哪台机器先拉取，干完活上传。**

### 三条铁律

1. **`pull` 之前必须完全退出 WorkBuddy** —— 它要写数据库，客户端开着会抢锁。
   脚本会自己检测并拒绝执行（而不是冒风险写坏）。
2. **`push` 可以随时跑** —— 它是只读快照，不影响正在运行的 WorkBuddy。
3. **同一时间只让一台机器 push** —— 避免两端基于不同的旧状态各自提交。

---

## 命令

```
wb-sync ui          打开本地 Web 面板（可视化操作，推荐）
wb-sync doctor      环境自检（含未知资产检测）
wb-sync where       显示当前生效的配置与清单来源
wb-sync status      看两侧差异，不动文件
wb-sync push        本机 -> 同步目录
wb-sync pull        同步目录 -> 本机
wb-sync push --dry-run    预演，只打印计划
```

可选参数：

| 参数 | 作用 |
|---|---|
| `--sync-root <路径>` | 临时覆盖同步目录 |
| `--name <名称>` | 覆盖本机显示名 |
| `--force` | WorkBuddy 运行时仍强制写库（**有损坏风险**，仅在确认已退出但检测误判时使用） |
| `--manifest-dir <路径>` | 指定 `default-manifest.json` 所在目录 |

---

## 工作原理

### 包与合并

每台机器把自己的全量资产打包成 `state/from-<机器标识>.zip`，**只写自己的那个文件**。

拉取时读**其他机器**的包，逐层合并：

```
包结构
├── manifest.json     来源、导出时间、引擎版本、数据库行数
├── files/            ~/.workbuddy 下的白名单资产
├── workspaces/       ~/WorkBuddy 下的空间文件
└── db/workbuddy.db   数据库一致性快照
```

### 合并语义（为什么不会丢数据）

| 文件类型 | 合并方式 |
|---|---|
| **追加型**：`projects/**/*.jsonl`、`**/memory/*.md` | **行级并集** —— 两边各自续写同一个对话/日志也安全，对方的行会补进来，不再整文件覆盖 |
| 其余文件 | 按 mtime 取较新的一方；若**两边都改过**，取较新的对方版本，本机版本另存为 `<文件名>.conflict-<标识>-<时间戳>` |

- `pull` 开头会自动**自愈**：把历史上被整文件覆盖丢掉的日志行，从同目录的 `.bak` 里并回来
  （幂等，重复跑不会重复添加）。
- 被覆盖的文件一律留 `.bak-<时间戳>`，想回滚直接改名即可。
- 数据库用 `INSERT OR IGNORE` + 列交集 —— 幂等、可重复执行、源端加列不炸。
  本机 `user_id` 不同时会自动映射过来。
- ⚠️ 同一个**非追加型**文件不要在两台机器同时改（比如同时改同一个 Excel）。
  "两边都改过"只保留较新的一方 + 一份 `.conflict-` 备份，不会自动合并内容。

### 为什么数据库要用 backup API

直接复制 `workbuddy.db` 会**丢掉还在 `-wal` 文件里、尚未合并进主库的数据**。
实测过：主库 204KB、`-wal` 4.1MB 时，裸拷贝会当场丢 4 条会话记录。

本工具用 SQLite 的 backup API 取一致性快照，拿到的才是完整状态。

---

## 安全边界

同步的是**个人敏感数据**，包括对话记录、记忆文件、技能（可能含 API Key）。
内置的隐私默认值：

- 路径含 `secret` / `credential` / `password` / `token` / `apikey` / `cookie`
  / `auth-state` 等字样的文件**不参与同步**
- `auth/` 目录整体排除（登录态的常见存放位置）
- `.key` / `.pem` / `.env` / `.db` 等敏感后缀默认排除
- `workbuddy.db` 本身不进同步目录（只以快照形式进包）
- 单文件超过 200MB 跳过（防视频 / 模型混入）

**但排除规则不可能覆盖所有情况。** 首次 push 前请把 `wb-sync doctor` 打印的
同步范围与排除规则看一遍，确认没有不应该上传的东西。
`where` 会打印各清单条目的数量，便于核对。

> `auth/` 走的是目录名精确匹配。如果某个工作空间里的 `auth/` 是源码目录
> 而非凭证目录，把它从 `default-manifest.json` 的 `never_segments` 里移除即可。

---

## 配置

所有配置都在**本机配置目录**下（Windows 默认 `%LOCALAPPDATA%\wb-sync\`）：

| 文件 | 作用 | 需要手改吗 |
|---|---|---|
| `machine-id` | 本机标识，首次运行自动生成 | 不用 |
| `config.json` | 同步目录、本机显示名 | 按需 |
| `manifest.json` | 追加 / 覆盖同步白名单 | 按需 |

### config.json

不填也能跑（自动探测 OneDrive）。要指定其他同步盘时：

```json
{
  "sync_root": "D:\\坚果云\\wb-sync",
  "machine_name": "家里的台式机"
}
```

### manifest.json —— 白名单是数据，不是代码

同步范围写在 `default-manifest.json` 里，不在 Python 代码里。好处是 WorkBuddy
客户端升级新增资产时，**改 JSON 就能跟上，不必改代码**。

三级来源（后者覆盖前者）：

1. 包内 / 发行包目录的 `default-manifest.json` —— 随版本发布的默认值
2. `<配置目录>\manifest.json` —— 用户自己的增删，列表项**追加**在默认之上
3. `--manifest-dir` 参数或 `WB_SYNC_MANIFEST_DIR` 环境变量 —— 临时指定

要排除自己环境里体积大、另有备份的路径：

```json
{
  "extra_globs": [
    "*/我的大文件库/*",
    "*Setup.exe"
  ]
}
```

可用键：`sync_files`、`sync_dirs`、`db_tables`、`never_segments`、
`never_suffix`、`sensitive_words`、`extra_globs`、`max_file_mb`。

**建议不要改发行包里的那份**（升级会被覆盖），要加目录就写用户级 `manifest.json`。

---

## 全自动同步（可选）

### 为什么不能简单地「定时 pull」

`pull` 要写数据库，必须 WorkBuddy 处于关闭状态。而 WorkBuddy **默认是开机自启的**
（`HKCU\...\CurrentVersion\Run`），所以任何「登录后定时跑 pull」都会和它撞锁。

### 解法：让 WorkBuddy 经由启动器打开

把开机自启项从 `WorkBuddy.exe` 换成 `wb-launch.cmd`：

```
登录  →  等同步盘就绪（最多 90 秒）
      →  pull（此刻 WorkBuddy 还没启动，数据库空闲 ✅）
      →  启动 WorkBuddy
```

这样 pull 永远发生在「安全窗口」内，且每次开机必跑。
即使 pull 失败，启动器也**一定会**把 WorkBuddy 启起来，不会让你打不开软件。

在 `wb-sync` 目录下双击 `install-autostart.cmd` 完成安装（每台机器各做一次）：

1. 记录 WorkBuddy.exe 位置，并**备份**当前的开机自启项
2. 自启项改指向 `wb-launch.cmd` → **开机自动 pull**
3. 注册计划任务 → **每天定时自动 push**

想改 push 时间：`.\install-autostart.ps1 -PushTimes "09:00,13:00,19:00"`

**还原**：双击 `uninstall-autostart.cmd`，恢复原自启项并删掉计划任务。

### 日志

| 文件 | 内容 |
|---|---|
| `%LOCALAPPDATA%\wb-sync\launch.log` | 每次开机 pull 的退出码 |
| `%LOCALAPPDATA%\wb-sync\push.log` | 每次定时 push 的完整输出（超过 1MB 自动清空） |

### 注意事项

- **首次开机同步会慢**：第一个包要完整过一遍。之后靠
  `state\.last-pull-<标识>.json` 记账，**包没变就秒过**，不影响开机速度。
- **WorkBuddy 升级 / 重装后**可能把开机自启项重置回 `WorkBuddy.exe`。
  发现不再自动同步时，重跑一次 `install-autostart.cmd` 即可。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| `pull` 报「请先退出 WorkBuddy」 | 真的关掉（含托盘图标）再试。确认已退出但被误判时可用 `--force` |
| 找不到 OneDrive | 写 `config.json` 指定 `sync_root`，或用 `--sync-root <路径>` |
| 同步目录是「仅在线」占位符 | 右键该文件夹 → 设为「**始终保留在此设备上**」，否则读不到内容 |
| 覆盖错了想回滚 | 被覆盖的文件都留有 `.bak-<时间戳>` 备份 |
| 升级 WorkBuddy 后某些数据没同步 | 跑 `doctor` 看**未知资产检测** —— 新版本可能引入了新目录，按提示加进 `manifest.json` 的 `sync_dirs` |
| 想确认有没有漏同步 | 在任意一端执行 `wb-sync status` |
| 换了网盘 / 改了同步目录 | 改 `config.json` 的 `sync_root`，重新 push 一次（包是全量的，会重传） |

---

## 开发

```
workbuddy-sync/
├── pyproject.toml              # 打包配置（console_scripts: wb-sync）
├── wb_sync.py                  # 便携模式入口垫片（把 src 挂到 sys.path）
├── default-manifest.json       # 便携模式用的默认清单
├── src/wb_sync/
│   ├── __init__.py             # 版本号（ENGINE_VERSION / MANIFEST_SCHEMA）
│   ├── cli.py                  # 命令行入口
│   ├── ui.py                   # Web 面板（wb-sync ui 的落地）
│   ├── core.py                 # 同步内核：收集 / 合并 / 打包
│   ├── config.py               # 清单与配置加载、机器标识
│   ├── state.py                # 运行时可变状态（集中管理，避免跨模块拿到旧值）
│   ├── platform_win.py         # Windows 进程检测
│   ├── utils.py                # 时间戳 / 摘要 / 体积格式化
│   └── default-manifest.json   # 包内默认清单
├── skill/SKILL.md              # 技能市场门面
├── tools/
│   ├── selftest.py             # 端到端自测（全程临时目录，不碰真实数据）
│   └── parity_check.py         # 新旧版本等价性对照（证明"同步范围零变化"）
└── *.cmd / *.ps1               # Windows 自动化脚本
```

### 架构要点

**为什么有 `state.py`**：程序里有很多全局状态（同步目录、白名单、机器标识），
命令行参数还会覆写其中一部分。如果让别的模块 `from .config import WB_HOME`，
拿到的是**导入那一刻的值**，之后 cli 里的赋值对 core 不可见 —— 这类 bug 极难查。
所以所有可变状态集中在 `state.py`，任何地方都通过 `state.X` 访问（运行时属性查找）。

### 跑测试

```bash
python tools/selftest.py        # 端到端：标识、配置、未知资产、push/pull、两种入口、版本提示
python tools/parity_check.py    # 与旧版对照，确认重构没有改变同步范围
```

两个脚本都只在系统临时目录里操作，**不会读写真实的 WorkBuddy 数据**。

### 构建

```bash
pip install setuptools wheel
python -m pip wheel . --no-deps --no-build-isolation -w dist
```

---

## 与官方「迁移助手」类技能的关系

| | 官方迁移技能 | 本项目 |
|---|---|---|
| 场景 | **一次性搬家**（旧机 → 新机） | **长期双向同步**（两台机器都要继续用） |
| 传输 | zip 包（微信 / U 盘） | 共享目录（网盘自动同步） |
| 频率 | 搬一次就结束 | 每次开机自动 |

两者定位不同、可以互补。

---

## License

[MIT](LICENSE)
