# OpenCode Go 额度追踪

桌面悬浮小部件（主入口）+ 终端 TUI 仪表盘，查看 **OpenCode Go** 订阅的 5 小时 / 每周 / 每月额度、**ZCode Start Plan**（免费领取的 token）的总额度与到期时间，以及两者的**分模型 token 使用统计**。纯只读、零侵入：不改变任何现有配置，不要求 cookie，唯一写入的是本目录下的 `data/snapshots.db`（额度历史快照）与自身 `config.json`。

---

## 致谢：本项目的核心思路来自 [keros68/metrik](https://github.com/keros68/metrik)

**特别感谢 [metrik](https://github.com/keros68/metrik) 的作者。** 在动手设计这个工具之前，我调研了多种获取 OpenCode Go 用量的方案（网页控制台 cookie 探测、本地代理记账等），最终是 metrik 的实现给出了两个决定性的答案，本项目的数据层几乎完全沿着它的路线构建：

1. **官方额度端点**：`GET https://opencode.ai/zen/go/v1/usage`，用 `Authorization: Bearer <API key>` 即可拿到 5 小时（rolling）/ 每周（weekly）/ 每月（monthly）三个窗口的已用百分比与重置时间——这是官方控制台在用的同一套接口，无需登录 cookie。metrik 的 `coding_quota.rs` 是我确认该端点用法（凭据发现顺序、响应解析、错误处理）时的关键参考。

2. **本地存储离线解析**：官方端点只提供三窗口百分比、没有分模型明细。metrik 的方案是不做任何网络请求、也不需要登录态，直接只读解析本机数据库（OpenCode 的 `opencode.db`、ZCode 的 `db.sqlite` 的 `model_usage` 表）来重建逐请求的 token 明细。本项目沿用了这一"零侵入、纯离线"的哲学：`app/readers/` 的数据源选择、`input_tokens` 已含缓存读写的拆分归一化（`input_uncached = max(0, input - cache_read - cache_write)`）等细节都参考了 metrik 的适配器实现。

3. **凭据自动发现**：从本机已有的明文凭据文件（环境变量、ZCode provider 配置、OpenCode `auth.json`）按优先级自动发现 API key，而不是要求用户手动粘贴——这一回退链的思路同样来自 metrik。

本项目为独立实现（Python/Textual/tkinter），未复制 metrik 的任何源码；metrik 本身是 Tauri 桌面应用，功能也更全面（多 Agent、热力图、CSV 导出、多设备同步），如果你需要这些能力，请直接使用 metrik。metrik 采用 AGPL-3.0 许可证，本项目仅参考了其公开文档与设计思路。

---

## 快速开始

```bat
:: Windows
run.bat
```
```bash
# Linux / macOS
./run.sh
```

启动**桌面悬浮小部件**（唯一入口；Windows 用 `pythonw` 静默启动，cmd 窗口一闪即关；Linux/macOS 用 nohup 后台运行）。在**小部件右键菜单**里可以打开 **TUI 仪表盘**（新开一个终端窗口），或手动运行：

```bash
python -X utf8 -m app.main
```

（启动脚本自动检测 Python：Windows 下 PATH → conda 环境（读 `~/.conda/environments.txt`）→ 常见安装位置；Linux/macOS 用 `python3`；也可用 `PYTHON_PATH` 环境变量指定。缺依赖会自动安装。TUI 必须用 `-m` 模块方式启动，建议在 Windows Terminal 里运行。）

## 桌面小部件

无边框悬浮窗，默认置顶半透明：

- **左键拖动**：移动位置
- **右下角拖拽**：调整大小（280×170 起，松开自动保存到 config.json）
- **右键菜单**：刷新 / 切换套餐 / 打开 TUI 仪表盘 / 置顶 / 退出
- 60 秒自动刷新；异常会写入 `data/widget_error.log`

## TUI 仪表盘

四个标签页：**额度**（默认）/ **模型** / **历史** / **设置**

| 按键 | 作用 |
|---|---|
| `1` `2` `3` `4` | 模型页时间范围：今天 / 7天 / 30天 / 全部 |
| `s` | 切换套餐 |
| `r` | 手动刷新 |
| `t` | 测试连接（设置页） |
| `q` | 退出 |

- **额度页**：三窗口进度条 + 剩余百分比 + 重置倒计时；剩余 ≤15% 或已限流时红色警示
- **模型页**：模型 × 请求数 / 输入 / 缓存读 / 缓存写 / 输出 / 推理 tokens / 估算花费（USD，无单价表价目的模型显示 `—`）
- **历史页**：剩余 % 随时间 sparkline（App 运行期间按轮询周期记录）

## 平台支持

| 平台 | 状态 | 启动 | 说明 |
|---|---|---|---|
| **Windows** | ✅ 完整 | 双击 `run.bat` | pythonw 静默启动、置顶无边框、「打开 TUI」新开控制台窗口 |
| **Linux** | ✅ 可用（X11） | `./run.sh` | 需 `python3-tk`（Debian: `sudo apt install python3-tk`；conda 自带）；「打开 TUI」自动探测 gnome-terminal/konsole/xterm 等；**Wayland 下置顶/无边框受合成器限制可能无效** |
| **macOS** | ✅ 可用 | `./run.sh` | conda 自带 tkinter；无边框窗口不接受键盘输入（仅鼠标操作，不影响使用）；Dock 会显示 Python 图标；「打开 TUI」通过 Terminal.app |

数据层（额度端点、本地用量解析、SQLite）三平台行为一致：ZCode `~/.zcode/cli/db/db.sqlite` 与 OpenCode `~/.local/share/opencode/opencode.db` 均为 XDG 标准路径。Windows 专属代码（DPI、新控制台）都有平台分支或异常保护，不影响其它平台。

## 订阅套餐切换

- 小部件右键菜单「切换套餐」，或 TUI 按 `s` / 设置页按钮循环切换
- 当前已实现：
  - **OpenCode Go**（官方 `/usage` 端点）
  - **ZCode Start Plan**（免费领取的 token，如 "ZCode Weekend Build" 这类一次性发放额度；详见下一节）
- 预留槽位：智谱 GLM Coding Plan / Z.ai / Kimi——额度接口待接入（切过去会提示），但**模型页用量统计已可追踪**（按各自 provider 前缀过滤）
- 接入新套餐：在 `app/plans.py` 的 `PLAN_DEFS` 注册 + `QUOTA_FETCHERS` 实现对应 fetcher 即可

## ZCode Start Plan（免费 token）追踪原理

Start Plan 不是"5 小时/周/月"三窗口，而是**一次性发放的单池 token 额度**（带到期时间），所以额度页会显示为一张"总额度 + 到期倒计时"卡片，历史页也是独立的剩余 % 曲线。

数据获取同样零侵入，按优先级：

1. **ZCode 桌面端本地日志**（主路径）：桌面客户端的 `[usage-stats]` 模块每约 2 分钟请求一次 `zcode-plan/billing/balance` 余额接口，并把完整响应写入 `~/.zcode/v2/logs/<日期>.log`。本工具解析其中最新一条，拿到与 ZCode 界面同源的 `total/used/remaining units` 与到期时间。**只要 ZCode 桌面端在运行，数据就会自动保持新鲜，无需任何配置。**
2. **直接请求余额接口**（兜底，日志缺失时）：用 ZCode 本地配置里的 JWT 调 `https://zcode.z.ai/api/v1/zcode-plan/billing/balance`。注意该接口目前对非桌面端请求返回参数错误，属尽力而为的备用路径；也可在 config.json 的 `zcode_token` 手动填入有效 JWT。

模型页统计自动按 ZCode Start Plan 的 provider（`*start-plan` 运行时形态，动态发现）过滤，GLM-5.3-Flash 的每次调用明细都在里面。

## 数据来源（零侵入原理）

1. **三窗口额度**：`GET https://opencode.ai/zen/go/v1/usage`（官方用法，`Authorization: Bearer <key>`），60 秒轮询。`percent` 为官方返回的已用比例。
2. **Start Plan 额度**：见上一节（ZCode 本地日志解析为主）。
3. **分模型统计**：只读解析本机 ZCode 数据库 `~/.zcode/cli/db/db.sqlite` 的 `model_usage` 表（每次请求一行），以及 OpenCode CLI 的 `~/.local/share/opencode/opencode.db`（`session_message` 表）。
4. **API key 自动发现**（设置页可覆盖）：环境变量 `OPENCODE_GO_API_KEY` → ZCode `~/.zcode/v2/provider_config.json`（`opencode-go-*` provider）→ OpenCode `auth.json`。

默认只统计 **OpenCode Go 相关 provider**（自动发现：`opencode-go-chat/messages/responses` + `v2/config.json` 中 baseURL 指向 `opencode.ai/zen/go` 的自定义 provider）。设置页 provider 过滤填 `*` 可计入全部（含智谱/Z.ai 自家套餐的调用）。

## 配置（config.json，首次保存自动生成）

| 字段 | 默认 | 说明 |
|---|---|---|
| `api_key` | 空 | 覆盖自动发现的 OpenCode Go key |
| `zcode_token` | 空 | 覆盖自动发现的 ZCode JWT（仅 Start Plan 日志缺失时的 API 兜底路径使用，一般无需填写） |
| `poll_interval` | 60 | 额度轮询间隔（秒） |
| `refresh_interval` | 30 | TUI 刷新间隔（秒） |
| `provider_filter` | 空 | 空=当前套餐默认；`*`=全部；其它=provider 前缀 |
| `active_plan` | opencode-go | 当前套餐 |
| `background_image` | 空 | 小部件背景图路径（填本地图片路径启用换肤；不含于仓库） |
| `background_dim` | 0.35 | 小部件背景压暗程度 0-1 |
| `widget_width/height` | 340/210 | 小部件尺寸（px，拖右下角可调，自动保存） |
| `widget_opacity` | 0.92 | 小部件整体不透明度 0-1 |
| `widget_topmost` | true | 小部件置顶 |

## 项目结构

```
app/
  main.py        # TUI 主入口（4 标签页、定时刷新）
  widget.py      # 桌面悬浮小部件（tkinter）
  config.py      # 配置 + key/provider 自动发现
  zen_client.py  # OpenCode Go 官方 /usage 端点客户端
  zcode_client.py# ZCode Start Plan 额度（本地日志解析 + 余额接口兜底）
  pricing.py     # 单价表（app/prices.json 可覆盖内置价格）
  plans.py       # 订阅套餐注册表（扩展新套餐的接入点）
  store.py       # 额度快照 SQLite（唯一写入点）
  readers/       # 只读解析 zcode db.sqlite / opencode.db
  ui/            # TUI 四个标签页
run.bat / requirements.txt / README.md
```

## 常见问题

- **额度报 401**：key 失效，去 https://opencode.ai/auth 控制台获取新 key 填入 TUI 设置页
- **Start Plan 额度显示"未找到数据"**：先运行一次 ZCode 桌面端（它会自动拉取余额并写入本地日志，约 2 分钟内本工具即可读到）；确认 `~/.zcode/v2/logs/` 下有当天日志
- **模型页没数据**：确认 ZCode 的 `~/.zcode/cli/db/db.sqlite` 存在；检查 provider 过滤设置
- **TUI 渲染乱码**：换 Windows Terminal 运行
- **估算花费与官方不同**：单价表为近似值（部分取 cache_read=input×10% 近似），可在 `app/prices.json` 修正
- **小部件没弹出来**：看 `data/widget_error.log`
- **Python 未被找到**：`run.bat` 顶部设 `PYTHON_PATH` 指向你的 python.exe
