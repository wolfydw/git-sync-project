#!/usr/bin/env python3

import argparse
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import termios
import tty
from urllib.parse import urlparse
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "git-sync-config.json"
DECORATION_WIDTH = 60
INTERACTIVE_SCREEN_ACTIVE = False

DEFAULT_CONFIG = {
    "scan_roots": ["~"],
    "monitored_repos": [],
    "exclude_dir_names": [
        ".git",
        ".svn",
        ".hg",
        ".Trash",
        ".cache",
        ".npm",
        ".pnpm-store",
        ".yarn",
        ".next",
        ".nuxt",
        ".turbo",
        ".idea",
        ".vscode",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        "DerivedData",
        "Library",
        "Applications",
    ],
    "use_ai_commit": True,
    "openai_model": "gpt-5",
    "openai_base_url": "https://api.openai.com/v1/responses",
    "openai_reasoning_effort": "minimal",
    "diff_char_limit": 12000,
    "include_clean_repos": True,
}


@dataclass
class RepoInfo:
    path: Path
    name: str
    branch: str
    status_output: str
    upstream: str
    push_url: str
    change_count: int

    @property
    def dirty(self) -> bool:
        return self.change_count > 0


def load_config() -> dict:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"配置文件解析失败：{CONFIG_PATH} -> {exc}")
            sys.exit(1)
        if not isinstance(user_config, dict):
            print(f"配置文件格式错误：{CONFIG_PATH}")
            sys.exit(1)
        config.update(user_config)
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def expand_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def normalize_scan_roots(config: dict) -> list[Path]:
    roots: list[Path] = []
    for item in config.get("scan_roots", []):
        root = expand_path(item)
        if root.exists():
            roots.append(root)
    return roots or [Path.home()]


def is_within_roots(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def run_command(command: list[str], cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=check,
    )


def git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return run_command(["git", "-C", str(repo), *args], check=check)


def discover_repos_with_mdfind(scan_roots: list[Path]) -> dict[str, Path]:
    repos: dict[str, Path] = {}
    try:
        result = run_command(["mdfind", 'kMDItemFSName == ".git"'])
    except FileNotFoundError:
        return repos

    if result.returncode != 0:
        return repos

    for raw_line in result.stdout.splitlines():
        candidate = Path(raw_line.strip()).expanduser()
        if not candidate.name == ".git":
            continue
        repo_dir = candidate.parent
        if not is_within_roots(repo_dir.resolve(), scan_roots):
            continue
        root = resolve_git_root(repo_dir)
        if root is not None:
            repos[str(root)] = root
    return repos


def discover_repos_with_walk(scan_roots: list[Path], exclude_dir_names: set[str]) -> dict[str, Path]:
    repos: dict[str, Path] = {}
    for scan_root in scan_roots:
        for current_root, dir_names, file_names in os.walk(scan_root, topdown=True):
            current_path = Path(current_root)
            if ".git" in dir_names or ".git" in file_names:
                root = resolve_git_root(current_path)
                if root is not None:
                    repos[str(root)] = root
            dir_names[:] = [
                name for name in dir_names
                if name == ".git" or name not in exclude_dir_names
            ]
    return repos


def resolve_git_root(path: Path) -> Path | None:
    result = run_command(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def get_repo_info(repo_path: Path) -> RepoInfo | None:
    status_result = git(repo_path, "-c", "core.quotePath=false", "status", "--short", "--branch")
    if status_result.returncode != 0:
        return None

    status_lines = status_result.stdout.splitlines()
    if not status_lines:
        return None

    branch, upstream = parse_status_branch_line(status_lines[0])
    status_output = "\n".join(status_lines[1:]).strip()
    change_count = len([line for line in status_lines[1:] if line.strip()])
    push_url = ""
    if upstream:
        remote_name = upstream.split("/", 1)[0]
        remote_url_result = git(repo_path, "remote", "get-url", "--push", remote_name)
        if remote_url_result.returncode == 0:
            push_url = remote_url_result.stdout.strip()

    return RepoInfo(
        path=repo_path,
        name=repo_path.name,
        branch=branch,
        status_output=status_output,
        upstream=upstream,
        push_url=push_url,
        change_count=change_count,
    )


def parse_status_branch_line(branch_line: str) -> tuple[str, str]:
    if not branch_line.startswith("## "):
        return "(unknown)", ""

    raw = branch_line[3:].strip()
    raw = raw.split(" [", 1)[0]

    if raw.startswith("HEAD"):
        return "(detached)", ""

    if "..." in raw:
        branch, upstream = raw.split("...", 1)
        return branch.strip() or "(unknown)", upstream.strip()

    return raw.strip() or "(unknown)", ""


def remote_exists(repo_path: Path, remote_name: str) -> bool:
    result = git(repo_path, "remote")
    if result.returncode != 0:
        return False
    return remote_name in {line.strip() for line in result.stdout.splitlines() if line.strip()}


def set_repo_push_url(repo_path: Path, remote_name: str, push_url: str) -> tuple[bool, str]:
    if remote_exists(repo_path, remote_name):
        result = git(repo_path, "remote", "set-url", "--push", remote_name, push_url)
        if result.returncode != 0:
            return False, (result.stderr or result.stdout).strip() or "设置 push 地址失败"
        return True, f"已更新 {remote_name} 的 push 地址"

    result = git(repo_path, "remote", "add", remote_name, push_url)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip() or "新增 remote 失败"
    return True, f"已新增 remote {remote_name}"


def discover_repo_infos(config: dict) -> list[RepoInfo]:
    scan_roots = normalize_scan_roots(config)
    exclude_dir_names = set(config.get("exclude_dir_names", []))

    repos = discover_repos_with_mdfind(scan_roots)
    if not repos:
        repos = discover_repos_with_walk(scan_roots, exclude_dir_names)

    repo_infos: list[RepoInfo] = []
    for repo_path in repos.values():
        info = get_repo_info(repo_path)
        if info is None:
            continue
        if config.get("include_clean_repos", True) or info.dirty:
            repo_infos.append(info)

    repo_infos.sort(key=lambda item: (not item.dirty, str(item.path).lower()))
    return repo_infos


def normalize_monitored_repos(config: dict) -> list[Path]:
    repos: list[Path] = []
    seen: set[str] = set()
    for raw_path in config.get("monitored_repos", []):
        path = expand_path(raw_path)
        key = str(path)
        if key not in seen:
            repos.append(path)
            seen.add(key)
    return repos


def get_monitored_repo_infos(config: dict) -> tuple[list[RepoInfo], list[str]]:
    infos: list[RepoInfo] = []
    errors: list[str] = []

    for repo_path in normalize_monitored_repos(config):
        if not repo_path.exists():
            errors.append(f"{shorten_path(repo_path)} 不存在")
            continue
        info = get_repo_info(repo_path)
        if info is None:
            errors.append(f"{shorten_path(repo_path)} 不是可访问的 Git 仓库")
            continue
        infos.append(info)

    infos.sort(key=lambda item: (not item.dirty, str(item.path).lower()))
    return infos, errors


def shorten_path(path: Path) -> str:
    home = Path.home()
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def push_host(push_url: str) -> str:
    if not push_url:
        return "-"
    parsed = urlparse(push_url)
    if parsed.netloc:
        return parsed.netloc
    if "@" in push_url and ":" in push_url:
        host_part = push_url.split("@", 1)[1]
        return host_part.split(":", 1)[0]
    return push_url


def build_default_commit_message(status_output: str) -> str:
    lines = [line for line in status_output.splitlines() if line.strip()]
    if not lines:
        return "sync: 更新文件"

    first_path = re.sub(r"^.. ", "", lines[0]).strip()
    if len(lines) == 1 and first_path:
        return f"sync: 更新 {Path(first_path).name}"

    return f"sync: 更新 {len(lines)} 个文件"


def build_diff_payload(repo: Path, diff_char_limit: int) -> dict[str, str]:
    status_output = git(repo, "-c", "core.quotePath=false", "status", "--short").stdout.strip()
    diff_stat = git(repo, "diff", "--cached", "--stat").stdout.strip()
    diff_summary = git(repo, "diff", "--cached", "--summary").stdout.strip()
    diff_patch = git(repo, "diff", "--cached", "--unified=1", "--no-color").stdout

    if len(diff_patch) > diff_char_limit:
        diff_patch = diff_patch[:diff_char_limit] + "\n... [diff truncated]"

    return {
        "status_output": status_output,
        "diff_stat": diff_stat,
        "diff_summary": diff_summary,
        "diff_patch": diff_patch.strip(),
    }


def extract_response_text(payload: dict) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    output_items = payload.get("output", [])
    parts: list[str] = []
    if isinstance(output_items, list):
        for item in output_items:
            for content in item.get("content", []):
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
    return "\n".join(parts).strip()


def cleanup_commit_message(message: str) -> str:
    cleaned = " ".join(message.replace("\n", " ").split()).strip()
    cleaned = cleaned.strip('"').strip("'").strip("`")
    cleaned = re.sub(r"^(commit message[:：]\s*)", "", cleaned, flags=re.IGNORECASE)
    return cleaned[:72] if len(cleaned) > 72 else cleaned


def generate_ai_commit_message(repo_info: RepoInfo, config: dict) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key or not config.get("use_ai_commit", True):
        return None

    diff_payload = build_diff_payload(repo_info.path, int(config.get("diff_char_limit", 12000)))
    prompt = (
        f"仓库：{repo_info.name}\n"
        f"分支：{repo_info.branch}\n\n"
        f"Git 状态：\n{diff_payload['status_output'] or '(无)'}\n\n"
        f"变更统计：\n{diff_payload['diff_stat'] or '(无)'}\n\n"
        f"变更摘要：\n{diff_payload['diff_summary'] or '(无)'}\n\n"
        f"Diff：\n{diff_payload['diff_patch'] or '(无)'}"
    )

    body = {
        "model": config.get("openai_model", "gpt-5"),
        "instructions": (
            "你是一个 Git commit message 生成器。"
            "请根据给定的仓库改动生成 1 条中文 commit message。"
            "要求："
            "1. 只输出 commit message 本身；"
            "2. 单行；"
            "3. 简洁明确，不要空话；"
            "4. 不要引号、不要项目符号、不要解释；"
            "5. 优先概括用户真正改动的主题。"
        ),
        "input": prompt,
        "reasoning": {"effort": config.get("openai_reasoning_effort", "minimal")},
        "text": {"verbosity": "low"},
    }

    request = urllib.request.Request(
        config.get("openai_base_url", "https://api.openai.com/v1/responses"),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    message = cleanup_commit_message(extract_response_text(payload))
    return message or None


def parse_selection(raw_text: str, repo_count: int, dirty_indexes: list[int]) -> list[int]:
    text = raw_text.strip().lower()
    if not text:
        return dirty_indexes
    if text == "all":
        return list(range(1, repo_count + 1))

    selected: set[int] = set()
    tokens = [token for token in re.split(r"[\s,]+", text) if token]
    for token in tokens:
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            for index in range(start, end + 1):
                if 1 <= index <= repo_count:
                    selected.add(index)
            continue

        index = int(token)
        if 1 <= index <= repo_count:
            selected.add(index)

    return sorted(selected)


def interactive_supported() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def read_key() -> str:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        first = os.read(fd, 1)
        if first == b"\x1b":
            rest = os.read(fd, 2)
            sequence = first + rest
            if sequence == b"\x1b[A":
                return "up"
            if sequence == b"\x1b[B":
                return "down"
            if sequence == b"\x1b[C":
                return "right"
            if sequence == b"\x1b[D":
                return "left"
            return "escape"
        if first in (b"\r", b"\n"):
            return "enter"
        if first == b" ":
            return "space"
        if first == b"\x03":
            raise KeyboardInterrupt
        return first.decode("utf-8", errors="ignore").lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def activate_interactive_screen() -> None:
    global INTERACTIVE_SCREEN_ACTIVE
    if INTERACTIVE_SCREEN_ACTIVE or not interactive_supported():
        return
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()
    INTERACTIVE_SCREEN_ACTIVE = True


def deactivate_interactive_screen() -> None:
    global INTERACTIVE_SCREEN_ACTIVE
    if not INTERACTIVE_SCREEN_ACTIVE:
        return
    sys.stdout.write("\033[?25h\033[?1049l")
    sys.stdout.flush()
    INTERACTIVE_SCREEN_ACTIVE = False


atexit.register(deactivate_interactive_screen)


def clear_screen() -> None:
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def reverse_video(text: str) -> str:
    return f"\033[7m{text}\033[0m"


def terminal_columns(default: int = 100) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def terminal_rows(default: int = 24) -> int:
    return shutil.get_terminal_size((100, default)).lines


def ellipsize(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if len(text) <= max_width:
        return text
    if max_width <= 1:
        return text[:max_width]
    return text[: max_width - 1] + "…"


def repo_status_label(info: RepoInfo) -> str:
    return f"{info.change_count}改动" if info.dirty else "clean"


def divider_line() -> str:
    return "─" * DECORATION_WIDTH


def section_title(title: str) -> str:
    width = DECORATION_WIDTH
    text = f" {title} "
    if len(text) >= width:
        return text.strip()
    remaining = width - len(text)
    left = remaining // 2
    right = remaining - left
    return f"{'─' * left}{text}{'─' * right}"


def build_repo_picker_lines(
    repo_infos: list[RepoInfo],
    selected_indexes: set[int],
    cursor_index: int,
    start_index: int,
    max_visible: int,
) -> list[str]:
    total = len(repo_infos)
    end_index = min(total, start_index + max_visible)
    columns = terminal_columns()
    name_width = max(12, min(28, columns - 28))
    lines = [section_title("选择要监控的仓库"), ""]
    for index in range(start_index, end_index):
        info = repo_infos[index]
        cursor = ">" if index == cursor_index else " "
        checked = "[x]" if index in selected_indexes else "[ ]"
        dirty = "*" if info.dirty else " "
        status_text = repo_status_label(info)
        name_text = ellipsize(info.name, name_width + 4)
        line = f"{cursor} {checked} {dirty} {name_text:<{name_width + 4}}  {status_text:<7}  {info.branch}"
        if index == cursor_index:
            line = reverse_video(line)
        lines.append(line)

    current = repo_infos[cursor_index]
    lines.extend(
        [
            "",
            section_title("当前项详情"),
            f"名称：{current.name}",
            f"状态：{'有改动' if current.dirty else '无改动'}（{repo_status_label(current)}）",
            f"分支：{current.branch}",
            f"上游：{current.upstream or '无上游'}",
            f"Push：{current.push_url or '无 push 地址'}",
            f"路径：{shorten_path(current.path)}",
        ]
    )
    lines.extend(
        [
            "",
            f"已选中 {len(selected_indexes)} / {total} 个仓库",
            "",
            divider_line(),
            "[↑/↓]移动  [空格]勾选  [回车]确认  [a]全选  [c]全不选  [p]设置push  [q]取消",
        ]
    )
    return lines


def prompt_text(message: str, default: str = "") -> str | None:
    prompt = f"{message}"
    if default:
        prompt += f" [{default}]"
    prompt += "："
    try:
        value = input(prompt + " ").strip()
    except EOFError:
        return None
    if not value:
        return default if default else None
    return value


def prompt_yes_no(message: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        value = input(f"{message} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not value:
        return default
    return value in ("y", "yes")


def configure_push_url_interactively(repo_info: RepoInfo) -> RepoInfo | None:
    clear_screen()
    print("设置当前仓库的 push 地址\n")
    print(f"仓库：{repo_info.name}")
    print(f"路径：{shorten_path(repo_info.path)}")
    print(f"当前上游：{repo_info.upstream or '无上游'}")
    print(f"当前 Push：{repo_info.push_url or '无 push 地址'}")
    print()

    if repo_info.push_url:
        if not prompt_yes_no("当前仓库已有 push 地址，是否覆盖？", default=False):
            return repo_info

    default_remote = repo_info.upstream.split("/", 1)[0] if repo_info.upstream else "origin"
    remote_name = prompt_text("请输入 remote 名称", default=default_remote)
    if not remote_name:
        return repo_info

    push_url = prompt_text("请输入 push 地址")
    if not push_url:
        return repo_info

    success, message = set_repo_push_url(repo_info.path, remote_name, push_url)
    print()
    print(message)
    if not success:
        pause_if_needed()
        return repo_info

    refreshed = get_repo_info(repo_info.path)
    pause_if_needed()
    return refreshed or repo_info


def interactive_pick_repos(repo_infos: list[RepoInfo], default_indexes: list[int]) -> list[int] | None:
    if not repo_infos:
        return []

    selected_indexes = {index - 1 for index in default_indexes if 1 <= index <= len(repo_infos)}
    cursor_index = min(selected_indexes) if selected_indexes else 0
    viewport_start = 0

    def render() -> list[str]:
        nonlocal viewport_start
        reserved_rows = 12
        available_rows = max(6, terminal_rows() - reserved_rows)
        max_visible = max(4, available_rows)

        if cursor_index < viewport_start:
            viewport_start = cursor_index
        if cursor_index >= viewport_start + max_visible:
            viewport_start = cursor_index - max_visible + 1

        return build_repo_picker_lines(
            repo_infos=repo_infos,
            selected_indexes=selected_indexes,
            cursor_index=cursor_index,
            start_index=viewport_start,
            max_visible=max_visible,
        )

    def handle_key(key: str) -> tuple[bool, list[int] | None]:
        nonlocal cursor_index, selected_indexes, repo_infos
        if key in ("up", "k"):
            cursor_index = (cursor_index - 1) % len(repo_infos)
        elif key in ("down", "j"):
            cursor_index = (cursor_index + 1) % len(repo_infos)
        elif key == "space":
            if cursor_index in selected_indexes:
                selected_indexes.remove(cursor_index)
            else:
                selected_indexes.add(cursor_index)
        elif key == "a":
            selected_indexes = set(range(len(repo_infos)))
        elif key == "c":
            selected_indexes.clear()
        elif key == "p":
            refreshed = configure_push_url_interactively(repo_infos[cursor_index])
            if refreshed is not None:
                repo_infos[cursor_index] = refreshed
        elif key in ("q", "escape"):
            return True, None
        elif key == "enter":
            return True, sorted(index + 1 for index in selected_indexes)
        return False, None

    return run_interactive_loop(render, handle_key)


def build_monitored_repo_lines(
    repo_infos: list[RepoInfo],
    errors: list[str],
    numbered: bool,
) -> list[str]:
    lines = [section_title("已监控仓库"), ""]

    if not repo_infos and not errors:
        lines.append("（当前为空）")
        return lines

    name_width = max((len(info.name) for info in repo_infos), default=8)
    host_width = max((len(push_host(info.push_url)) for info in repo_infos), default=6)

    for index, info in enumerate(repo_infos, start=1):
        prefix = f"[{index:>2}] " if numbered else ""
        lines.append(
            f"{prefix}{info.name:<{name_width}}  {push_host(info.push_url):<{host_width}}  {shorten_path(info.path)}"
        )

    if errors:
        lines.extend(["", section_title("无效监控项")])
        for message in errors:
            lines.append(f"- {message}")

    return lines


def build_main_menu_lines(
    repo_infos: list[RepoInfo],
    errors: list[str],
    options: list[tuple[str, str]],
    cursor_index: int,
) -> list[str]:
    lines = build_monitored_repo_lines(repo_infos, errors, numbered=False)
    lines.extend([
        "",
        section_title("主菜单"),
    ])

    for index, (_, label) in enumerate(options):
        cursor = ">" if index == cursor_index else " "
        line = f"{cursor} {label}"
        if index == cursor_index:
            line = reverse_video(line)
        lines.append(line)

    lines.extend([
        "",
        divider_line(),
        "[↑/↓]移动  [回车]确认  [q]退出",
    ])

    return lines


def interactive_choose_main_action(
    repo_infos: list[RepoInfo],
    errors: list[str],
    options: list[tuple[str, str]],
) -> str | None:
    cursor_index = 0

    def render() -> list[str]:
        return build_main_menu_lines(
            repo_infos=repo_infos,
            errors=errors,
            options=options,
            cursor_index=cursor_index,
        )

    def handle_key(key: str) -> tuple[bool, str | None]:
        nonlocal cursor_index
        if key in ("up", "k"):
            cursor_index = (cursor_index - 1) % len(options)
        elif key in ("down", "j"):
            cursor_index = (cursor_index + 1) % len(options)
        elif key in ("enter", "space"):
            return True, options[cursor_index][0]
        elif key in ("q", "escape"):
            return True, None
        return False, None

    return run_interactive_loop(render, handle_key)


def run_interactive_loop(render, handle_key):
    try:
        while True:
            clear_screen()
            sys.stdout.write("\n".join(render()) + "\n")
            sys.stdout.flush()

            done, result = handle_key(read_key())
            if done:
                return result
    except KeyboardInterrupt:
        return None


def print_repo_list(repo_infos: list[RepoInfo]) -> list[int]:
    dirty_indexes: list[int] = []
    print("\n发现以下 Git 仓库：\n")
    for index, info in enumerate(repo_infos, start=1):
        marker = "*" if info.dirty else " "
        if info.dirty:
            dirty_indexes.append(index)
        status_text = f"{info.change_count} 项改动" if info.dirty else "无改动"
        upstream_text = info.upstream if info.upstream else "无上游"
        push_text = info.push_url if info.push_url else "无 push 地址"
        print(f"[{index:>2}] {marker} {info.name}")
        print(f"     分支：{info.branch} | 状态：{status_text} | 上游：{upstream_text}")
        print(f"     Push：{push_text}")
        print(f"     路径：{shorten_path(info.path)}")
    return dirty_indexes


def print_monitored_repo_list(repo_infos: list[RepoInfo], errors: list[str]) -> None:
    print()
    print("\n".join(build_monitored_repo_lines(repo_infos, errors, numbered=True)))


def confirm_selection(selected_infos: list[RepoInfo]) -> bool:
    print("\n将处理以下仓库：")
    for info in selected_infos:
        print(f"- {info.name} ({shorten_path(info.path)})")
    answer = input("\n继续执行提交和推送？[Y/n] ").strip().lower()
    return answer in ("", "y", "yes")


def choose_menu_action() -> str:
    print("\n操作选项：")
    print("1. 修改监控仓库列表")
    print("2. 提交所有已监控仓库")
    print("q. 退出")
    return input("\n请输入选项： ").strip().lower()


def update_monitored_repos(config: dict) -> None:
    repo_infos = discover_repo_infos(config)
    if not repo_infos:
        print("\n未扫描到 Git 仓库。")
        return

    dirty_indexes = [
        index for index, info in enumerate(repo_infos, start=1)
        if info.dirty
    ]
    monitored_paths = {str(path) for path in normalize_monitored_repos(config)}
    current_indexes = [
        index for index, info in enumerate(repo_infos, start=1)
        if str(info.path) in monitored_paths
    ]
    default_indexes = current_indexes or dirty_indexes

    if interactive_supported():
        selected_indexes = interactive_pick_repos(repo_infos, default_indexes)
        if selected_indexes is None:
            print("已取消。")
            return
    else:
        print_repo_list(repo_infos)
        print("\n输入编号（支持 1 3 5 / 2-4 / all，直接回车默认选择全部有改动仓库）：")
        while True:
            try:
                raw_text = input("> ").strip()
                selected_indexes = parse_selection(raw_text, len(repo_infos), default_indexes)
                break
            except ValueError:
                print("输入格式不正确，请重新输入。")

    selected_paths = [str(repo_infos[index - 1].path) for index in selected_indexes]
    config["monitored_repos"] = selected_paths
    save_config(config)

    print("\n已更新监控仓库列表：")
    if not selected_paths:
        print("（当前为空）")
        return

    for path in selected_paths:
        print(f"- {shorten_path(Path(path))}")


def pull_rebase(repo_info: RepoInfo) -> tuple[bool, str]:
    if not repo_info.upstream:
        return True, "未配置上游，跳过拉取"

    result = git(repo_info.path, "pull", "--rebase", "--autostash")
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "git pull 失败"
        return False, message
    return True, "远程同步完成"


def sync_repo(repo_info: RepoInfo, config: dict, enable_ai: bool) -> tuple[bool, str]:
    latest_info = get_repo_info(repo_info.path)
    if latest_info is None:
        return False, "无法读取仓库状态"

    if not latest_info.dirty:
        return True, "无改动，已跳过"

    ok, pull_message = pull_rebase(latest_info)
    if not ok:
        return False, pull_message

    latest_info = get_repo_info(repo_info.path)
    if latest_info is None:
        return False, "拉取后无法读取仓库状态"

    if not latest_info.dirty:
        return True, "拉取后已无本地改动"

    add_result = git(latest_info.path, "add", "-A")
    if add_result.returncode != 0:
        return False, (add_result.stderr or add_result.stdout).strip() or "git add 失败"

    cached_diff = git(latest_info.path, "diff", "--cached", "--quiet")
    if cached_diff.returncode == 0:
        return True, "暂存区无变化，已跳过"
    if cached_diff.returncode not in (0, 1):
        return False, (cached_diff.stderr or cached_diff.stdout).strip() or "git diff --cached --quiet 执行失败"

    commit_message = None
    if enable_ai:
        commit_message = generate_ai_commit_message(latest_info, config)
    if not commit_message:
        commit_message = build_default_commit_message(latest_info.status_output)

    commit_result = git(latest_info.path, "commit", "-m", commit_message)
    if commit_result.returncode != 0:
        message = (commit_result.stderr or commit_result.stdout).strip() or "git commit 失败"
        return False, f"{message} | 提交信息：{commit_message}"

    push_result = git(latest_info.path, "push")
    if push_result.returncode != 0:
        message = (push_result.stderr or push_result.stdout).strip() or "git push 失败"
        return False, f"{message} | 提交信息：{commit_message}"

    return True, f"提交并推送成功 | {commit_message}"


def commit_all_monitored_repos(config: dict, enable_ai: bool) -> int:
    repo_infos, errors = get_monitored_repo_infos(config)

    if errors:
        print("\n以下监控仓库无效，请先修正：")
        for message in errors:
            print(f"- {message}")
        return 1

    if not repo_infos:
        print("\n当前没有已监控仓库，请先执行“修改监控仓库列表”。")
        return 0

    if enable_ai:
        print(f"\nAI 提交信息：已启用（模型 {config.get('openai_model', 'gpt-5')}）")
    else:
        print("\nAI 提交信息：未启用，将使用规则生成的默认提交信息")

    if not confirm_selection(repo_infos):
        print("已取消。")
        return 0

    print("\n开始执行...\n")
    results: list[tuple[RepoInfo, bool, str]] = []
    for info in repo_infos:
        print(f"==> {info.name} [{shorten_path(info.path)}]")
        success, message = sync_repo(info, config, enable_ai=enable_ai)
        results.append((info, success, message))
        prefix = "成功" if success else "失败"
        print(f"{prefix}：{message}\n")

    success_count = len([item for item in results if item[1]])
    failure_count = len(results) - success_count
    print("执行完成：")
    print(f"- 成功：{success_count}")
    print(f"- 失败：{failure_count}")

    if failure_count:
        print("\n失败明细：")
        for info, success, message in results:
            if not success:
                print(f"- {info.name}: {message}")

    return 0 if failure_count == 0 else 1


def pause_if_needed() -> None:
    if sys.stdin.isatty():
        try:
            input("\n按回车退出...")
        except EOFError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="管理监控仓库并批量提交")
    parser.add_argument("--list", action="store_true", help="只列出已监控仓库")
    parser.add_argument("--refresh-monitored", action="store_true", help="扫描仓库并重设监控列表")
    parser.add_argument("--commit-monitored", action="store_true", help="提交所有已监控仓库")
    parser.add_argument("--no-ai", action="store_true", help="禁用 AI 提交信息")
    args = parser.parse_args()

    config = load_config()
    repo_infos, errors = get_monitored_repo_infos(config)
    if args.list:
        print_monitored_repo_list(repo_infos, errors)
        pause_if_needed()
        return 0

    enable_ai = bool(os.environ.get("OPENAI_API_KEY")) and config.get("use_ai_commit", True) and not args.no_ai

    if args.refresh_monitored:
        try:
            activate_interactive_screen()
            update_monitored_repos(config)
        finally:
            deactivate_interactive_screen()
        pause_if_needed()
        return 0

    if args.commit_monitored:
        exit_code = commit_all_monitored_repos(config, enable_ai=enable_ai)
        pause_if_needed()
        return exit_code

    menu_options = [
        ("1", "修改监控仓库列表"),
        ("2", "提交所有已监控仓库"),
        ("q", "退出"),
    ]

    try:
        if interactive_supported():
            activate_interactive_screen()

        while True:
            if interactive_supported():
                action = interactive_choose_main_action(
                    repo_infos=repo_infos,
                    errors=errors,
                    options=menu_options,
                ) or "q"
            else:
                print_monitored_repo_list(repo_infos, errors)
                action = choose_menu_action()

            if action == "1":
                update_monitored_repos(config)
            elif action == "2":
                exit_code = commit_all_monitored_repos(config, enable_ai=enable_ai)
                pause_if_needed()
                return exit_code
            elif action in ("q", "quit", "exit"):
                pause_if_needed()
                return 0
            else:
                print("无效选项，请重新输入。")

            repo_infos, errors = get_monitored_repo_infos(config)
    finally:
        deactivate_interactive_screen()


if __name__ == "__main__":
    sys.exit(main())
