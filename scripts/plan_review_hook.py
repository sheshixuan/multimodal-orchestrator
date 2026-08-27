#!/usr/bin/env python3
"""Codex UserPromptSubmit hook：在 Plan 模式下注入 Plan Review Gate 指令。

被 install_plan_hook.py 写入 ~/.codex/hooks.json；Codex 会把本脚本 stdout
的额外上下文注入当前会话。仅在 permission_mode == "plan" 且 config.toml 的
plan_review 开启时输出；其他情况完全静默，不消耗任何 API。

用法（Codex hook，stdin 传入 JSON）：
  python3 plan_review_hook.py

手动/测试用法：
  python3 plan_review_hook.py --simulate --permission-mode plan
"""
import argparse
import json
import sys
from pathlib import Path

from call_model import ConfigError
from review_plan import DEFAULT_CONFIG, resolve_plan_mode

SKILL_ROOT = Path(__file__).resolve().parent.parent


def _script(*names):
    return SKILL_ROOT.joinpath("scripts", *names)


def build_context(cfg, permission_mode="plan"):
    """返回应注入的 Plan Review Gate 文本；无需注入时返回 None。"""
    if permission_mode != "plan":
        return None
    try:
        mode = resolve_plan_mode(cfg)
    except ConfigError as exc:
        return (
            "multimodal-orchestrator 的 plan_review 配置无效，请先修复 config.toml："
            f"{exc}"
        )
    if mode == "off":
        return None

    review_script = _script("review_plan.py")
    if mode == "auto":
        return (
            "你正在 Codex Plan 模式，且 multimodal-orchestrator 配置了 "
            "plan_review=auto。在计划草稿完成、准备提交最终计划前，必须先执行：\n"
            f"1. 把计划落盘为文件（如 <项目>/.codex/plan-review.md）。\n"
            f"2. 运行 python3 {review_script} --review <计划文件>，"
            "使用 config.toml 的 review_model 自动评审；若缺少 review_model "
            "或 API key，停止并让用户先配置，不要静默跳过。\n"
            "3. 把评审意见并入计划（先结论、再问题、后修订建议），修订后再提交最终计划。"
        )
    return (
        "你正在 Codex Plan 模式，且 multimodal-orchestrator 配置了 "
        "plan_review=ask。在计划草稿完成、准备提交最终计划前，必须先执行 "
        "Plan Review Gate：\n"
        f"1. 先完成实施计划草稿并落盘为文件（如 <项目>/.codex/plan-review.md），"
        "不要在计划完成前先询问用户选择评审模型。\n"
        f"2. 运行 python3 {review_script} --list-models，准备一份简短 review 计划："
        "说明本次评审重点、推荐/备选模型与 API key 状态，不调用外部模型。\n"
        "3. 最后一步把实施计划和 review 计划同时展示给用户，并询问"
        "\"用哪个模型评审？也可以跳过\"；用户也可以先审阅实施计划再决定。\n"
        "4. 用户选择模型后，若与 config.toml 的 review_model 不同，运行 "
        f"python3 {review_script} --set-model <模型>；用户跳过则不调用任何外部模型。\n"
        f"5. 运行 python3 {review_script} --review <计划文件> "
        "[--image <原图>] [--model <模型>]，把评审意见并入计划"
        "（先结论、再问题、后修订建议），修订后再提交最终计划。"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--simulate", action="store_true", help="不读 stdin，模拟 hook 输入")
    parser.add_argument(
        "--permission-mode",
        default="plan",
        choices=["plan", "default", "acceptEdits", "dontAsk", "bypassPermissions"],
        help="模拟用的 permission_mode（仅配合 --simulate）",
    )
    args = parser.parse_args(argv)

    permission_mode = args.permission_mode
    if not args.simulate:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return 0
        permission_mode = payload.get("permission_mode", "")

    cfg = {}
    config_path = Path(args.config)
    if config_path.exists():
        from call_model import load_config

        cfg = load_config(config_path)

    text = build_context(cfg, permission_mode)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
