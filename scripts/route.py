#!/usr/bin/env python3
"""提示词分派分类器：把用户提示词映射为三模块分派计划。

混合路由规则：
  - 有图片（--image 或提示词提及图片/截图/图表等）→ needs_vision = true
  - 显式要求评审/复查/质检 → needs_review = true
  - 否定短语可强制关闭对应模块（图片已实际传入时不会关闭 vision）
  - 其余情况 → 仅 core（主代理自处理）

输出 JSON：{"needs_vision": bool, "needs_review": bool, "core": "self", "reason": "..."}
主代理可结合上下文对结果做最终判断。

用法示例：
  python3 route.py --prompt "帮我看看这张截图并给出方案"
  python3 route.py --image /path/a.png --prompt "写个方案并评审"
  echo "帮我写周报" | python3 route.py
"""
import argparse
import json
import sys

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


def classify(prompt, images=None):
    images = images or []
    prompt = prompt or ""
    prompt_lower = prompt.lower()
    reasons = []

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
        "reason": "；".join(reasons),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--prompt", help="提示词文本；缺省从 stdin 读取")
    parser.add_argument("--image", action="append", default=[], help="图片路径，可多次")
    args = parser.parse_args(argv)
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    result = classify(prompt, args.image)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
