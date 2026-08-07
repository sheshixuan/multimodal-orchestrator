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
"""
import argparse
import sys
from pathlib import Path

from call_model import load_config

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config.toml"


def resolve_mode(cfg, host=None):
    """返回生效模式：hosts.<host>.mode → 顶层 mode → 'auto'。"""
    if host:
        host_cfg = cfg.get("hosts", {}).get(host)
        if isinstance(host_cfg, dict) and host_cfg.get("mode"):
            return host_cfg["mode"]
    return cfg.get("mode", "auto")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", help="宿主名（如 codex/workbudy/claude/opencode）")
    parser.add_argument("--list", action="store_true", help="列出全局默认与各宿主生效模式")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.list:
        print(f"全局默认: {resolve_mode(cfg)}")
        hosts = cfg.get("hosts", {})
        if isinstance(hosts, dict) and hosts:
            for name, host_cfg in hosts.items():
                mode = host_cfg.get("mode", resolve_mode(cfg))
                print(f"hosts.{name}: {mode}")
        else:
            print("（未配置任何 [hosts.xxx]）")
        return 0
    print(resolve_mode(cfg, args.host))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
