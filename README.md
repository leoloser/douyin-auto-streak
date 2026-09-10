# 抖音续火花脚本

一个用于抖音网页版私信的自动化脚本，面向 Microsoft Edge。

维护记录见 [CHANGELOG.md](CHANGELOG.md)。

## 你要改的地方

- `douyin_keep_streak.py` 顶部的 `TARGET_FRIENDS`，这里填写聊天列表能搜到的昵称
- 或复制 `config.example.json` 为 `config.local.json`，在本地私有配置里填写好友名单
- `MESSAGE_TO_SEND`
- `message_candidates`（可选，写在 `config.local.json` 里；非空时每个好友随机选一条发送）
- `SKIP_IF_TODAY_ALREADY_ACTIVE`
- `CLEAN_STALE_AUTOMATION_EDGE_ON_START`
- `CLOSE_STARTED_EDGE_ON_EXIT`
- `DAILY_RUN_AT`
- `EDGE_PROFILE_DIRECTORY`
- `log_dir`
- `failure_screenshot_dir`
- `run_report_path`

## 运行

```bash
pip install -r requirements.txt
python douyin_keep_streak.py
```

更推荐双击桌面快捷方式，桌面快捷方式会运行 `launch_douyin_keep_streak.py` 并保留日志窗口。

手动双击默认会在当前桌面运行，方便你人工观察和调试。

只测试“搜索好友并打开聊天框”、不发送表情：

```powershell
$env:DOUYIN_DRY_RUN='1'
python douyin_keep_streak.py
```

如果想用随机文案池，可以在 `config.local.json` 里加入：

```json
{
  "message_candidates": ["1", "今天续一下", "火花保持"]
}
```

`message_candidates` 留空时仍使用 `message_to_send`，默认行为不变。

如果要放进 Windows 任务计划程序，程序/脚本指向你的 Python 可执行文件，参数填写：

```text
launch_douyin_keep_streak.py --desktop-mode isolated
```

推荐直接在 PowerShell 里运行仓库自带的配置脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_scheduled_task.ps1 `
  -PythonPath "C:\ProgramData\anaconda3\python.exe"
```

它会按项目当前所在目录创建或更新“抖音续火花脚本”任务，并设置每天
`01:00`、`14:00`、`23:00` 执行。以后移动项目目录后，在新目录重新运行一次即可，
避免任务计划仍指向旧路径。

`--desktop-mode isolated` 会在自动触发时创建一个临时 Windows 虚拟桌面，把日志窗口和自动化 Edge 放到那个桌面运行，并在结束时关闭这个临时桌面。脚本会尽量不把你当前桌面切走。
这个模式下，启动器会先在临时桌面里再起一个脚本进程，所以脚本本体和 Edge 会一起待在新桌面里跑。
由于 Windows 虚拟桌面对新窗口归属的限制，启动瞬间可能会短暂切到临时桌面，随后会自动切回原桌面。

手动测试时直接用：

```powershell
python launch_douyin_keep_streak.py --desktop-mode current
```

`--desktop-mode current` 会明确保持在当前桌面运行，适合你人工观察和调试。

只验证定时启动和页面操作、不实际发送消息：

```powershell
python launch_douyin_keep_streak.py --desktop-mode isolated --dry-run
```

也可以临时用环境变量切换：

```powershell
$env:DOUYIN_DESKTOP_MODE='isolated'
python launch_douyin_keep_streak.py
```

## 说明

- 不包含登录逻辑
- 默认打开真实聊天页 `https://www.douyin.com/chat?isPopup=1`
- 使用自动化专用 Edge 配置目录 `%LOCALAPPDATA%\DouyinKeepStreakEdgeProfile`
- 这是为了保证 Selenium 能稳定接管 Edge；你这台机器的默认 Edge 用户目录无法稳定开放控制端口
- 首次使用时如果出现 Edge 欢迎/设置弹窗，脚本会尝试自动点掉；抖音账号仍需要你在该专用窗口里手动登录一次
- 登录后的 Cookie 会保存在这个专用目录里，后续不用每天重复登录
- 默认会先检测今天是否已经有聊天消息/视频活动；如果有，就跳过该好友，避免重复发送
- 默认每次启动前会清理程序自己残留的旧自动化 Edge，避免同时打开两个自动化 Edge
- 默认在本次任务结束前，只关闭脚本本次启动的自动化 Edge，不关闭你手动打开的其他 Edge
- 默认把运行日志写到 `logs/`，把失败截图写到 `screenshots/`
- 默认每次运行结束后写入 `last_run_summary.json`，便于任务计划无人值守时快速查看成功/失败数量
- 定时任务建议使用 `launch_douyin_keep_streak.py --desktop-mode isolated`，手动测试时用 `--desktop-mode current`
- 公开仓库里不包含真实好友名单，请在本地 `config.local.json` 或 `TARGET_FRIENDS` 里自行填写
- 如果页面结构更新，可能需要微调选择器
- 想真正接管“当前正在运行”的 Edge，请先用远程调试端口启动它，然后保持 `REMOTE_DEBUGGING_PORT = 9222`

## 参考过的开源项目

- [MOUMAN888/douyin-spark-auto-renew](https://github.com/MOUMAN888/douyin-spark-auto-renew)：对照了抖音续火花项目常见的配置项、随机文案、异常提醒和 FAQ 写法
- [harshitsidhwa/WhatsApp-bot-selenium](https://github.com/harshitsidhwa/WhatsApp-bot-selenium)：对照了网页版 IM 自动化的联系人列表、发送间隔和失败统计
- [SohanRaidev/WhatsApp-Automation-Studio](https://github.com/SohanRaidev/WhatsApp-Automation-Studio)：对照了消息池、模拟人工节奏、日志面板/可观测性相关设计

## 风险提示

自动化操作第三方网页存在账号风控、限流甚至封号风险，仅作技术演示。
