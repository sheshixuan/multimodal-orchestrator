#!/usr/bin/env python3
"""触发模式解析：auto / manual（按宿主配置；纯标准库，无第三方依赖）。

配置来源：config.toml（本 skill 目录内）
  mode = "auto"                 # 全局默认（缺失视为 auto）
  [hosts.codex]
  mode = "auto"
  [hosts.workbudy]
  mode = "manual"

解析顺序：hosts.<宿主>.mode → 顶层 mode → "auto"

用法示例：
  python3 mode.py --host codex      # 输出 codex 宿主的生效模式
  python3 mode.py --list            # 列出全局默认与各宿主生效模式
  python3 mode.py --config /path/config.toml --host workbudy
  python3 mode.py --set manual      # 全局切换为 manual（行级写入，保留注释与其他键）
  python3 mode.py --set auto --host codex          # 仅 codex 宿主切换为 auto
  python3 mode.py --unset-host codex               # 移除 codex 覆盖，回退全局 mode
"""
import argparse
import re
import sys
from pathlib import Path

from call_model import ConfigError, load_config

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config.toml"

_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")


def resolve_mode(cfg, host=None):
    """返回生效模式：hosts.<host>.mode → 顶层 mode → 'auto'。"""
    if host:
        host_cfg = cfg.get("hosts", {}).get(host)
        if isinstance(host_cfg, dict) and host_cfg.get("mode"):
            return host_cfg["mode"]
    return cfg.get("mode", "auto")


def _key_of(line):
    match = _KEY_RE.match(line)
    return match.group(1) if match else None


def _is_section(line):
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _first_section_index(lines):
    for i, line in enumerate(lines):
        if _is_section(line):
            return i
    return None


def _find_section(lines, section):
    for i, line in enumerate(lines):
        if _is_section(line) and line.strip()[1:-1].strip() == section:
            return i
    return None


def _find_key_in_section(lines, section_idx, key):
    """在 section_idx 起始的 section 内找 key 行（到下一个 section 头为止）。"""
    for i in range(section_idx + 1, len(lines)):
        if _is_section(lines[i]):
            break
        if _key_of(lines[i]) == key:
            return i
    return None


def _find_top_level_key(lines, key):
    """在首个 section 头之前找顶层 key 行。"""
    for i, line in enumerate(lines):
        if _is_section(line):
            break
        if _key_of(line) == key:
            return i
    return None


def _require_config(path):
    if not Path(path).exists():
        raise ConfigError(
            "config.toml 不存在：请先运行首次引导（说『重新配置 multimodal-orchestrator』）"
            "配置模式与模型，或参考 config.example.toml 手工创建。"
        )


def set_mode(path, mode, host=None):
    """行级更新 config.toml 的 mode（保留注释与其他键）。host 为空更新顶层 mode。"""
    path = Path(path)
    _require_config(path)
    if mode not in ("auto", "manual"):
        raise ConfigError(f"非法 mode：{mode!r}（仅支持 auto/manual）")
    lines = path.read_text(encoding="utf-8").splitlines()
    if host:
        section = f"hosts.{host}"
        idx = _find_section(lines, section)
        if idx is not None:
            j = _find_key_in_section(lines, idx, "mode")
            if j is not None:
                lines[j] = f'mode = "{mode}"'
            else:
                lines.insert(idx + 1, f'mode = "{mode}"')
        else:
            lines.extend(["", f"[{section}]", f'mode = "{mode}"'])
        where = f"hosts.{host}"
    else:
        j = _find_top_level_key(lines, "mode")
        if j is not None:
            lines[j] = f'mode = "{mode}"'
        else:
            insert_at = _first_section_index(lines)
            if insert_at is None:
                insert_at = len(lines)
            lines.insert(insert_at, f'mode = "{mode}"')
        where = "顶层"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return where


def unset_host_mode(path, host):
    """移除某宿主的模式覆盖，回退全局 mode；返回说明文本。"""
    path = Path(path)
    _require_config(path)
    section = f"hosts.{host}"
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _find_section(lines, section)
    if idx is None:
        return f"hosts.{host}：未配置覆盖，继续使用全局 mode"
    j = _find_key_in_section(lines, idx, "mode")
    if j is not None:
        del lines[j]
        # 删除 mode 行后若位于 section 内，索引保持有效；重查 section 内是否还有内容
        idx = _find_section(lines, section)
    next_header = None
    for i in range(idx + 1, len(lines)):
        if _is_section(lines[i]):
            next_header = i
            break
    end = next_header if next_header is not None else len(lines)
    has_content = any(
        lines[i].strip() and not lines[i].strip().startswith("#")
        for i in range(idx + 1, end)
    )
    if not has_content:
        del lines[idx]
        while idx < len(lines) and not lines[idx].strip():
            del lines[idx]
        if idx > 0 and not lines[idx - 1].strip():
            del lines[idx - 1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"hosts.{host}：已移除覆盖，回退全局 mode"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", help="宿主名（如 codex/workbudy/claude/opencode）")
    parser.add_argument("--list", action="store_true", help="列出全局默认与各宿主生效模式")
    parser.add_argument("--set", choices=["auto", "manual"], help="设置生效模式（可配合 --host）")
    parser.add_argument("--unset-host", help="移除某宿主的模式覆盖，回退全局 mode")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args(argv)

    try:
        if args.set:
            if args.unset_host:
                print("--set 与 --unset-host 不能同时使用", file=sys.stderr)
                return 3
            where = set_mode(args.config, args.set, args.host)
            effective = resolve_mode(load_config(args.config), args.host)
            print(
                f"已设置：{where} mode = {args.set}；"
                f"{args.host or '全局'}生效模式 = {effective}"
            )
            return 0
        if args.unset_host:
            print(unset_host_mode(args.config, args.unset_host))
            return 0
        cfg = load_config(args.config)
        if not args.list:
            print(resolve_mode(cfg, args.host))
            return 0
        print(f"全局默认: {resolve_mode(cfg)}")
        hosts = cfg.get("hosts", {})
        if isinstance(hosts, dict) and hosts:
            for name, host_cfg in hosts.items():
                mode = host_cfg.get("mode", resolve_mode(cfg))
                print(f"hosts.{name}: {mode}")
        else:
            print("（未配置任何 [hosts.xxx]）")
        return 0
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
