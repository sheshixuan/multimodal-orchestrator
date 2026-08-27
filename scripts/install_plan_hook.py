#!/usr/bin/env python3
"""把 multimodal-orchestrator 的 Plan Review Gate 安装为 Codex hook。

写入 ~/.codex/hooks.json 的 UserPromptSubmit command hook，指向同目录的
plan_review_hook.py。幂等安装/卸载：只增删本 skill 的 handler，不破坏已有 hooks。

用法：
  python3 install_plan_hook.py --install
  python3 install_plan_hook.py --status
  python3 install_plan_hook.py --uninstall
  python3 install_plan_hook.py --install --hooks-file /tmp/hooks.json
"""
import argparse
import json
import os
import shlex
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
HOOK_EVENT = "UserPromptSubmit"
DEFAULT_HOOKS_FILE = (
    Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
)


class HookError(Exception):
    pass


def hook_command():
    return " ".join(
        shlex.quote(part)
        for part in ("python3", str(SKILL_ROOT / "scripts" / "plan_review_hook.py"))
    )


def handler():
    return {
        "type": "command",
        "command": hook_command(),
        "statusMessage": "加载 multimodal-orchestrator Plan Review Gate",
        "additionalContextLimit": 4000,
        "timeout": 10,
    }


def _load(path):
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HookError(f"无法读取 hooks 文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HookError(f"hooks 文件 {path} 顶层必须是 JSON 对象")
    return data


def _save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def is_installed(data):
    events = data.get("hooks", {})
    if not isinstance(events, dict):
        return False
    groups = events.get(HOOK_EVENT, [])
    if not isinstance(groups, list):
        return False
    target = hook_command()
    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            continue
        for item in handlers:
            if isinstance(item, dict) and item.get("command") == target:
                return True
    return False


def install(path):
    """返回 (changed, message)。幂等：已安装时不再重复添加。"""
    data = _load(path)
    if is_installed(data):
        return False, "Plan Review Gate hook 已安装，无需重复添加"

    events = data.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise HookError("hooks.json 的 hooks 字段必须是对象")
    groups = events.setdefault(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise HookError(f"hooks.{HOOK_EVENT} 必须是数组")
    groups.append({"hooks": [handler()]})
    _save(path, data)
    return True, f"已安装 Plan Review Gate hook：{Path(path)}"


def uninstall(path):
    """返回 (changed, message)。只移除本 skill 的 handler，保留其他 hooks。"""
    path = Path(path)
    if not path.exists():
        return False, f"hooks 文件不存在：{path}"
    data = _load(path)
    if not is_installed(data):
        return False, "未找到 Plan Review Gate hook，无需卸载"

    events = data.get("hooks", {})
    target = hook_command()
    groups = events.get(HOOK_EVENT, [])
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        handlers = group.get("hooks", [])
        kept = [
            item
            for item in handlers
            if not (isinstance(item, dict) and item.get("command") == target)
        ]
        if kept:
            group["hooks"] = kept
            kept_groups.append(group)
    if kept_groups:
        events[HOOK_EVENT] = kept_groups
    else:
        events.pop(HOOK_EVENT, None)
    if not events:
        data.pop("hooks", None)
    _save(path, data)
    return True, f"已卸载 Plan Review Gate hook：{path}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--install", action="store_true", help="安装 hook")
    group.add_argument("--uninstall", action="store_true", help="卸载 hook")
    group.add_argument("--status", action="store_true", help="查看安装状态")
    parser.add_argument(
        "--hooks-file",
        default=str(DEFAULT_HOOKS_FILE),
        help="hooks.json 路径（默认 ~/.codex/hooks.json）",
    )
    args = parser.parse_args(argv)

    try:
        path = args.hooks_file
        if args.install:
            changed, message = install(path)
            print(("已添加： " if changed else "未改动： ") + message)
            return 0
        if args.uninstall:
            changed, message = uninstall(path)
            print(("已卸载： " if changed else "未改动： ") + message)
            return 0
        data = _load(path)
        installed = is_installed(data)
        print(f"{'已安装' if installed else '未安装'}: {path}")
        print(f"hook 命令: {hook_command()}")
        return 0
    except HookError as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
