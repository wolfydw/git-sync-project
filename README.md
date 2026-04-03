# Git Sync Project

这个目录现在只保留主入口批量同步能力。

## 主要文件

- `git-sync.command`
  macOS 入口。双击启动主程序。

- `git-sync.sh`
  Linux / macOS 通用命令行入口。可执行 `./git-sync.sh`。

- `git-sync-manager.py`
  主程序。负责监控仓库管理、交互界面、仓库扫描、批量提交、批量推送，以及 AI 生成 commit message。

- `git-sync-config.json`
  配置文件。可调整扫描根目录、监控仓库列表、忽略目录、是否启用 AI、AI 模型、diff 截断长度等。

## 主程序功能

- 启动后主界面同屏展示“已监控仓库列表 + 主菜单”
- 主界面的已监控仓库列表只显示：仓库名、Push 主机、路径
- 主菜单提供两个核心动作：
  - `1` 修改监控仓库列表
  - `2` 提交所有已监控仓库
- 修改监控列表时，才会扫描可访问范围内的 Git 仓库
- 交互式多选采用“上方单行列表 + 下方当前项详情”的布局
- 交互式多选支持上下键移动、空格勾选、回车确认
- 额外支持 `a` 全选、`c` 全不选、`p` 为当前仓库设置 push 地址、`q` 取消、`j/k` 上下移动
- 交互界面的操作提示固定在底部，并用分隔线与主内容区分
- 交互界面标题统一使用“分隔线包裹标题”的样式
- 非交互终端下，仍支持输入 `1 3 5`、`2-4`、`all`
- 默认会优先保留当前已监控仓库；如果还没有监控项，则默认选中“全部有改动仓库”
- 提交时只处理“已监控仓库”
- 每个仓库提交前先执行 `git pull --rebase --autostash`
- 检测到 `OPENAI_API_KEY` 时，优先尝试用 AI 生成 commit message
- AI 失败时自动回退到规则生成的默认提交信息

## 跨平台说明

- `git-sync-manager.py` 本身可运行在 macOS 和常见 Linux 系统
- macOS 可双击 `git-sync.command`
- Linux 建议直接执行 `./git-sync.sh`
- 仓库扫描在 macOS 下会优先尝试 `mdfind`，在 Linux 或不可用场景下会自动回退到目录遍历
- 交互模式使用 ANSI/VT100 控制序列和备用屏幕（alternate screen），在常见 macOS / Linux 终端中可用
- 退出交互模式后会自动恢复原终端内容，不会把每次界面重绘堆到普通终端历史里

## AI 提交信息

默认读取环境变量 `OPENAI_API_KEY`。

如果没有配置 API Key，主程序仍然可以正常使用，只是 commit message 会退回到规则生成，例如：

- `sync: 更新 3 个文件`
- `sync: 更新 README.md`

当前默认模型在配置文件中是 `gpt-5`，接口是 OpenAI Responses API。

## 常见配置

如果只想扫描常用工作目录，可以把 `git-sync-config.json` 里的：

```json
"scan_roots": ["~"]
```

改成例如：

```json
"scan_roots": [
  "~/Documents",
  "~/Desktop"
]
```

这样扫描会更快。

如果希望更换 AI 模型或关闭 AI commit message，也可以直接修改：

```json
"use_ai_commit": true,
"openai_model": "gpt-5"
```

## 测试命令

```bash
python3 git-sync-manager.py --list
python3 git-sync-manager.py --refresh-monitored
python3 git-sync-manager.py --commit-monitored
python3 git-sync-manager.py --commit-monitored --no-ai
```
