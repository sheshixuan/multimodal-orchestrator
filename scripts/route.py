#!/usr/bin/env python3
"""提示词分派分类器：把用户提示词映射为三模块分派计划。

混合路由规则：
  - 有图片（--image 或提示词提及图片/截图/图表等）→ needs_vision = true
  - 显式要求评审/复查/质检 → needs_review = true
  - 否定短语可强制关闭对应模块（图片已实际传入时不会关闭 vision）
  - 其余情况 → 仅 core（主代理自处理）

触发模式（auto / manual，按宿主配置，见 scripts/mode.py）：
  - 生效模式：hosts.<宿主>.mode → 顶层 mode → auto（读取 config.toml）
  - 提及检测：route.py 自动识别提示词中的显式点名（$multimodal-orchestrator /
    @multimodal-orchestrator / 纯名称 multimodal-orchestrator / 中文名 多模态编排），
    命中即 explicit=true（输出 mention_detected=true）；--explicit 仅作兜底
  - 否定/讨论性提及（如"不要用/什么是 multimodal-orchestrator"）不算点名
  - enabled = (mode == "auto") or explicit
  - 最后防线：manual 且未点名时强制 enabled=false 并告警（即使关键词命中）

输出 JSON：{"needs_vision": bool, "needs_review": bool, "core": "self",
           "mode": "auto|manual", "explicit": bool, "enabled": bool,
           "mention_detected": bool, "clean_prompt": str, "reason": "..."}
主代理可结合上下文对结果做最终判断；enabled=false 时不得调用任何外部模型。

用法示例：
  python3 route.py --host codex --prompt "帮我看看这张截图并给出方案"
  python3 route.py --host workbudy --explicit --image /path/a.png --prompt "写个方案并评审"
  echo "用 @multimodal-orchestrator 看这张截图" | python3 route.py --host codex
  echo "帮我写周报" | python3 route.py
"""
import argparse
import json
import re
import sys
from pathlib import Path

from call_model import load_config
from mode import resolve_mode

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config.toml"

VISION_KEYWORDS = [
    "图片", "截图", "图表", "图像", "照片", "识图", "OCR", "图中", "图里",
    "截图里", "这张图", "这个图", "下图", "上图", "看图",
]
VISION_NEGATIONS = ["不用识图", "不需要识图", "不要识图", "不用看图", "不看图"]
REVIEW_KEYWORDS = [
    "评审", "复查", "质检", "交叉核验", "复核", "审阅", "把关",
    "核对一下", "检查一下方案", "方案评审", "再检查", "review", "挑刺",
]
REVIEW_NEGATIONS = ["不用评审", "不需要评审", "不要评审", "免评审", "不用review"]

# 提及式点名检测（大小写不敏感）：
#   $/@ + 可选空格 + 名称（Codex 用 $，ChatGPT/WorkBuddy 用 @）
MENTION_SYMBOL_RE = re.compile(r"[$@]\s*multimodal-orchestrator", re.IGNORECASE)
# 纯名称：要求两侧单词边界，避免 xmultimodal-orchestratory 之类子串误命中
MENTION_NAME_RE = re.compile(
    r"(?<![a-z0-9])multimodal-orchestrator(?![a-z0-9])", re.IGNORECASE
)
# 纯名称前 12 字符内出现以下否定/讨论短语时不算点名
DISMISS_PHRASES = [
    "不要用", "不用", "别用", "别再用", "不需要", "不用了",
    "什么是", "介绍一下", "讲讲", "说说", "评价一下",
]


def detect_mention(prompt):
    """检测提示词是否显式提及本 skill（$/@/纯名称/中文名），返回 bool。

    优先级：$/@ 符号提及、中文名 多模态编排 直接算点名；纯名称受单词边界约束，
    且提及前 12 字符内出现否定/讨论短语（如"不要用/什么是 multimodal-orchestrator"）不算点名。
    """
    prompt = prompt or ""
    if MENTION_SYMBOL_RE.search(prompt) or "多模态编排" in prompt:
        return True
    match = MENTION_NAME_RE.search(prompt)
    if not match:
        return False
    before = prompt[max(0, match.start() - 12): match.start()]
    return not any(phrase in before for phrase in DISMISS_PHRASES)


def clean_mentions(prompt):
    """把 $/@ 提及符号剥离为纯名称（如 @multimodal-orchestrator -> multimodal-orchestrator），
    供传给 vision/review 等下游模型时使用，避免符号干扰。"""
    prompt = prompt or ""
    return MENTION_SYMBOL_RE.sub("multimodal-orchestrator", prompt)


def classify(prompt, images=None, mode="auto", explicit=False):
    images = images or []
    prompt = prompt or ""
    prompt_lower = prompt.lower()
    reasons = []
    enabled = (mode == "auto") or bool(explicit)

    needs_vision = bool(images)
    if needs_vision:
        reasons.append("传入图片")
    for keyword in VISION_KEYWORDS:
        if keyword in prompt:
            needs_vision = True
            reasons.append(f"提示词含「{keyword}」")
            break
    if not images:
        for negation in VISION_NEGATIONS:
            if negation in prompt:
                needs_vision = False
                break

    needs_review = False
    for keyword in REVIEW_KEYWORDS:
        if keyword in prompt_lower:
            needs_review = True
            reasons.append(f"提示词含「{keyword}」")
            break
    for negation in REVIEW_NEGATIONS:
        if negation in prompt_lower:
            needs_review = False
            break

    if not needs_vision and not needs_review:
        reasons.append("纯文本任务，仅 core")

    return {
        "needs_vision": needs_vision,
        "needs_review": needs_review,
        "core": "self",
        "mode": mode,
        "explicit": bool(explicit),
        "enabled": enabled,
        "reason": "；".join(reasons),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--prompt", help="提示词文本；缺省从 stdin 读取")
    parser.add_argument("--image", action="append", default=[], help="图片路径，可多次")
    parser.add_argument("--host", help="当前宿主名（如 codex/workbudy/claude/opencode）；缺省用全局 mode")
    parser.add_argument("--explicit", action="store_true", help="用户已显式点名本 skill")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.toml 路径")
    args = parser.parse_args(argv)
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    mode = resolve_mode(load_config(args.config), args.host)
    mention_detected = detect_mention(prompt)
    explicit = args.explicit or mention_detected
    result = classify(prompt, args.image, mode=mode, explicit=explicit)
    result["mention_detected"] = mention_detected
    result["clean_prompt"] = clean_mentions(prompt)
    if result["mode"] == "manual" and not result["explicit"]:
        print(
            "manual 模式且未显式点名 multimodal-orchestrator："
            "enabled=false，不执行任何模块、不调用外部模型。"
            "需要时请在提示词中提及：Codex 用 $multimodal-orchestrator，"
            "ChatGPT/WorkBuddy 用 @multimodal-orchestrator，或直接说名称/多模态编排。",
            file=sys.stderr,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
