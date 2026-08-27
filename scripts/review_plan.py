#!/usr/bin/env python3
"""Codex Plan 收尾评审门（Plan Review Gate）辅助脚本。

在 Codex Plan 模式提交最终计划前：
  1. 列出可用 review 模型（含推荐项与 API key 就绪状态）
  2. 用户选择模型后写入 config.toml 的 review_model
  3. 调用 call_model.py --role review 评审已落盘的方案

用法示例：
  python3 review_plan.py --list-models
  python3 review_plan.py --list-models --json
  python3 review_plan.py --set-model kimi-k3
  python3 review_plan.py --review 计划.md
  python3 review_plan.py --review 计划.md --model opencode-zen:gemini-3.1-pro --image 原图.png

退出码：0=成功 1=API/网络错误 2=缺少 API key 3=配置/参数错误
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import call_model
from call_model import ConfigError, build_providers, load_config, resolve_provider

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config.toml"

REVIEW_MODELS = [
    {
        "model": "glm-5.2",
        "recommended": True,
        "note": "OpenCode Go 订阅，评审稳健",
    },
    {
        "model": "kimi-k3",
        "recommended": False,
        "note": "OpenCode Go 订阅，备选",
    },
    {
        "model": "deepseek-v4-pro",
        "recommended": False,
        "note": "OpenCode Go 订阅，备选",
    },
    {
        "model": "opencode-zen:gemini-3.1-pro",
        "recommended": False,
        "note": "OpenCode Zen 按量，备选",
    },
]

PLAN_MODES = ("off", "ask", "auto")

_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")


def _is_section(line):
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _key_of(line):
    match = _KEY_RE.match(line)
    return match.group(1) if match else None


def _first_section_index(lines):
    for i, line in enumerate(lines):
        if _is_section(line):
            return i
    return None


def _find_top_level_key(lines, key):
    for i, line in enumerate(lines):
        if _is_section(line):
            break
        if _key_of(line) == key:
            return i
    return None


def _key_ready(model, providers):
    try:
        provider, _ = resolve_provider(model, providers)
        envs = ["REVIEW_API_KEY", providers[provider]["env"]]
        return any(os.environ.get(name) for name in envs)
    except (ConfigError, KeyError):
        return False


def resolve_plan_mode(cfg):
    """返回 plan_review 生效时机：off / ask / auto。

    缺失默认 ask；旧配置 true/false 分别兼容为 ask/off。
    """
    value = cfg.get("plan_review", "ask")
    if isinstance(value, bool):
        return "ask" if value else "off"
    if value in PLAN_MODES:
        return value
    if value == "true":
        return "ask"
    if value == "false":
        return "off"
    if value == "always":
        return "ask"
    raise ConfigError(
        f"非法 plan_review：{value!r}（仅支持 off/ask/auto，旧值 true/false 也兼容）"
    )


def list_models(cfg, providers):
    current = cfg.get("review_model", "")
    presets = list(REVIEW_MODELS)
    seen = {preset["model"] for preset in presets}
    configured = cfg.get("review_models", {})
    if not isinstance(configured, dict):
        raise ConfigError("review_models 必须使用 [review_models] 配置段")
    for name, model in configured.items():
        if not isinstance(model, str):
            raise ConfigError(f"review_models.{name} 必须是模型名字符串")
        model = model.strip()
        if not model or model in seen:
            continue
        presets.append(
            {
                "model": model,
                "recommended": False,
                "note": f"本地配置：{name}",
            }
        )
        seen.add(model)
    items = []
    for index, preset in enumerate(presets, start=1):
        model = preset["model"]
        provider, resolved = resolve_provider(model, providers)
        items.append(
            {
                "index": index,
                "model": model,
                "resolved_model": resolved,
                "provider": provider,
                "recommended": preset["recommended"],
                "note": preset["note"],
                "key_ready": _key_ready(model, providers),
                "current": bool(current) and model == current,
            }
        )
    return items


def render_list(items, current, plan_mode):
    lines = [f"当前 review_model: {current or '（未配置）'}"]
    lines.append(f"当前 plan_review: {plan_mode}")
    lines.append("可用评审模型（推荐项优先）：")
    for item in items:
        flags = []
        if item["recommended"]:
            flags.append("推荐")
        if item["current"]:
            flags.append("当前")
        flags.append("API key 已配置" if item["key_ready"] else "缺少 API key")
        tag = f"[{'/'.join(flags)}] " if flags else ""
        lines.append(f"{item['index']}. {item['model']:<34} {tag}{item['note']}")
    lines.append(
        "Codex 流程：向用户展示列表并等待选择；选中后运行 --set-model 与 --review。"
    )
    return "\n".join(lines)


def set_review_model(path, model):
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            "config.toml 不存在：请先运行首次引导"
            "（说『重新配置 multimodal-orchestrator』）或参考 config.example.toml 创建。"
        )
    providers = build_providers(load_config(path))
    resolve_provider(model, providers)
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _find_top_level_key(lines, "review_model")
    line = f'review_model = "{model}"'
    if idx is not None:
        lines[idx] = line
    else:
        insert_at = _first_section_index(lines)
        if insert_at is None:
            insert_at = len(lines)
        lines.insert(insert_at, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return model


def set_plan_mode(path, mode):
    """行级更新 config.toml 的 plan_review（保留注释与其他键）。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            "config.toml 不存在：请先运行首次引导"
            "（说『重新配置 multimodal-orchestrator』）或参考 config.example.toml 创建。"
        )
    if mode not in PLAN_MODES:
        raise ConfigError(f"非法 plan_review：{mode!r}（仅支持 {'/'.join(PLAN_MODES)}）")
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _find_top_level_key(lines, "plan_review")
    line = f'plan_review = "{mode}"'
    if idx is not None:
        lines[idx] = line
    else:
        insert_at = _first_section_index(lines)
        if insert_at is None:
            insert_at = len(lines)
        lines.insert(insert_at, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mode


def run_review(args):
    cfg = load_config(args.config)
    model = args.model or cfg.get("review_model")
    if not model:
        print(
            "未指定 --model，且 config.toml 缺少 review_model："
            "请先运行 --set-model <模型> 或参照 references/onboarding.md 配置。",
            file=sys.stderr,
        )
        return 3
    argv = [
        "--role",
        "review",
        "--plan",
        args.review,
        "--model",
        model,
        "--config",
        args.config,
    ]
    for image in args.image:
        argv.extend(["--image", image])
    if args.prompt:
        argv.extend(["--prompt", args.prompt])
    if args.json:
        argv.append("--json")
    return call_model.main(argv)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list-models", action="store_true", help="列出可用评审模型")
    parser.add_argument("--set-model", metavar="MODEL", help="把 review_model 写入 config.toml")
    parser.add_argument("--plan-mode", action="store_true", help="输出当前 plan_review 生效时机")
    parser.add_argument(
        "--set-plan-mode",
        choices=PLAN_MODES,
        help="写入 plan_review：off / ask / auto",
    )
    parser.add_argument("--review", metavar="PLAN", help="评审方案文件（可配合 --model 指定模型）")
    parser.add_argument("--model", help="本次评审使用的模型；缺省读 config.toml 的 review_model")
    parser.add_argument("--image", action="append", default=[], help="原图路径，可多次")
    parser.add_argument("--prompt", help="追加的评审提示词")
    parser.add_argument("--json", action="store_true", help="列表/评审输出为 JSON")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.toml 路径")
    args = parser.parse_args(argv)

    actions = [
        bool(args.list_models),
        bool(args.set_model),
        bool(args.plan_mode),
        bool(args.set_plan_mode),
        bool(args.review),
    ]
    if sum(actions) != 1:
        parser.error(
            "请从 --list-models / --set-model / --plan-mode / "
            "--set-plan-mode / --review 中选择一个操作"
        )

    cfg = load_config(args.config)
    try:
        providers = build_providers(cfg)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.plan_mode:
        try:
            print(resolve_plan_mode(cfg))
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        return 0

    if args.set_plan_mode:
        try:
            mode = set_plan_mode(args.config, args.set_plan_mode)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(f"已更新 config.toml：plan_review = {mode}")
        return 0

    if args.list_models:
        try:
            items = list_models(cfg, providers)
            plan_mode = resolve_plan_mode(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(render_list(items, cfg.get("review_model", ""), plan_mode))
        return 0

    if args.set_model:
        try:
            model = set_review_model(args.config, args.set_model)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(f"已更新 config.toml：review_model = {model}")
        return 0

    return run_review(args)


if __name__ == "__main__":
    raise SystemExit(main())
