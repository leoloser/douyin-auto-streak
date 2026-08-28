# 抖音续火花脚本

一个用于抖音网页版私信的自动化脚本，面向 Microsoft Edge。

## 你要改的地方

- `douyin_keep_streak.py` 顶部的 `TARGET_FRIENDS`，这里填写聊天列表能搜到的昵称
- 或复制 `config.example.json` 为 `config.local.json`，在本地私有配置里填写好友名单
- `MESSAGE_TO_SEND`
- `SKIP_IF_TODAY_ALREADY_ACTIVE`
- `CLEAN_STALE_AUTOMATION_EDGE_ON_START`
- `CLOSE_STARTED_EDGE_ON_EXIT`
- `DAILY_RUN_AT`
- `EDGE_PROFILE_DIRECTORY`

## 运行

```bash
pip install -r requirements.txt
python douyin_keep_streak.py
```

更推荐双击桌面快捷方式，桌面快捷方式会运行 `launch_douyin_keep_streak.py` 并保留日志窗口。

只测试“搜索好友并打开聊天框”、不发送表情：

```powershell
$env:DOUYIN_DRY_RUN='1'
python douyin_keep_streak.py
```

如果要放进 Windows 任务计划程序，程序/脚本指向你的 Python 可执行文件，参数填写：

```text
launch_douyin_keep_streak.py
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
- 公开仓库里不包含真实好友名单，请在本地 `config.local.json` 或 `TARGET_FRIENDS` 里自行填写
- 如果页面结构更新，可能需要微调选择器
- 想真正接管“当前正在运行”的 Edge，请先用远程调试端口启动它，然后保持 `REMOTE_DEBUGGING_PORT = 9222`

## 风险提示

自动化操作第三方网页存在账号风控、限流甚至封号风险，仅作技术演示。
