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
            f"2. 运行 python3 {review_script} --assess <计划文件> --json，"
            "把难度/不确定性/风险、建议路由、预计耗时、调用上限和能力置信度先告知用户；"
            "本步骤是纯本地评估。\n"
            f"3. 运行 python3 {review_script} --review <计划文件>。普通评审在告知后自动开始；"
            "若脚本返回退出码 4，说明任务预计超过三分钟、多模型或需要分片/扩容，必须停止并等待用户确认，"
            f"确认后才可加 --confirm-slow 重跑。\n"
            "4. 把结构化评审意见并入计划；若缺少模型或 API key，停止并提示配置，不要静默跳过。"
        )
    return (
        "你正在 Codex Plan 模式，且 multimodal-orchestrator 配置了 "
        "plan_review=ask。在计划草稿完成、准备提交最终计划前，必须先执行 "
        "Plan Review Gate：\n"
        f"1. 先完成实施计划草稿并落盘为文件（如 <项目>/.codex/plan-review.md），"
        "不要在计划完成前先询问评审选择。\n"
        f"2. 运行 python3 {review_script} --assess <计划文件> --json；本步骤不得调用外部 API。\n"
        "3. 若评估结果为 blocked，先询问会改变方案方向的关键信息，不展示评审选择，也不调用外部模型。"
        "否则，第一层只展示且必须恰好展示三个选项：单模型评审、多模型交叉评审、跳过评审；"
        "不要把具体模型或自定义模型作为同层选项。根据 review_strategy.recommended 移动推荐标记，"
        "并展示评分依据、建议路由、预计耗时和调用上限；推荐项不得写死。\n"
        "4. 用户选择单模型或多模型后，第二层再展示自动匹配的具体模型/provider、推理档位、能力置信度，"
        "允许用户接受或调整；分别使用 --strategy single 或 --strategy multi，必要时再配合 --model/可重复的 --reviewer。"
        f"运行 python3 {review_script} --review <计划文件> --strategy <single|multi>。"
        "返回退出码 4 时，必须再次等待慢任务确认；"
        "确认后才可加 --confirm-slow。用户跳过时禁止调用任何外部模型。\n"
        "5. 把结构化评审意见并入计划，修订后再提交最终计划。"
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
