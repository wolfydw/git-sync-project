# Git Sync Project

一个用于本机 Git 仓库批量同步的终端工具。

核心思路：

- 先维护一份“已监控仓库列表”
- 平时只对这份列表做批量提交和推送
- 修改监控列表时，才扫描本机可访问范围内的 Git 仓库

## 快速开始

macOS：

```bash
./git-sync.command
```

Linux / macOS 命令行：

```bash
./git-sync.sh
```

首次使用建议流程：

1. 启动主程序
2. 进入“修改监控仓库列表”
3. 用交互式多选界面选出需要长期管理的仓库
4. 返回主界面后，使用“提交所有已监控仓库”

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

## 交互操作

主界面：

- `[↑/↓]` 或 `j/k`：移动菜单焦点
- `[回车]`：确认当前菜单项
- `[q]`：退出

仓库多选界面：

- `[↑/↓]` 或 `j/k`：移动当前仓库
- `[空格]`：勾选 / 取消勾选当前仓库
- `[回车]`：确认当前选择
- `[a]`：全选
- `[c]`：全不选
- `[p]`：为当前仓库设置或覆盖 push 地址
- `[q]`：取消并返回

当前仓库详情区会显示：

- 名称
- 状态
- 分支
- 上游
- 完整 Push 地址
- 路径

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

AI commit message 只影响提交说明的生成，不影响 Git 提交流程本身：

- 有可用 API Key：优先调用 OpenAI 生成中文提交信息
- 无 API Key 或调用失败：自动退回规则生成的默认提交信息

## 配置文件示例

配置文件路径：

```bash
git-sync-config.json
```

主要字段：

- `scan_roots`：扫描 Git 仓库时使用的根目录
- `monitored_repos`：已监控仓库的绝对路径列表
- `exclude_dir_names`：扫描时忽略的目录名
- `use_ai_commit`：是否启用 AI 提交信息
- `openai_model`：AI 模型名
- `diff_char_limit`：发送给 AI 的 diff 文本截断长度
- `include_clean_repos`：扫描结果是否包含无改动仓库

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

如果希望手动维护监控仓库列表，也可以直接编辑：

```json
"monitored_repos": [
  "/Users/yangdawei/Documents/KeyDocs",
  "/Users/yangdawei/Documents/vitepress"
]
```

## Push 地址说明

主界面里只显示 Push 主机，例如：

- `github.com`
- `gitea.ydw.cool`

在“修改监控仓库列表”的详情区会显示完整 Push URL。

如果当前仓库没有 push 地址，可以在多选界面按 `p` 直接设置。

## 测试命令

```bash
python3 git-sync-manager.py --list
python3 git-sync-manager.py --refresh-monitored
python3 git-sync-manager.py --commit-monitored
python3 git-sync-manager.py --commit-monitored --no-ai
```
