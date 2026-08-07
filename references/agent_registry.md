# 分派与角色说明（Agent Registry）

> 由 `work/orchestrator_rules.md` 迁移并更新：视觉与评审改为外部 API 调用（`scripts/call_model.py`），
> core 由主代理自处理。

## 角色

总调度器（Orchestrator）= 主代理。收到提示词后先解析任务，再按分派表决定参与的模块及顺序，最后汇总结果。
不要在不需要时调用外部模型。

## 解析步骤（每次必做）

1. 判断任务类型：是否涉及图片/截图/图表？是否需要产出方案？是否需要质量评审？
2. 按分派表确定参与的模块及顺序。
3. 需要图片的，先跑 vision 拿到文本转写，再继续。
4. 全部模块结果就绪后，汇总成完整回答。

## 分派表

| 触发条件 | 动作 |
|---|---|
| 提示词含图片路径/截图/图表 | ① vision：`python3 <skill_dir>/scripts/call_model.py --role vision --image <路径> --prompt "转写需求"`，输出穷尽式文本转写 |
| 需要基于图片产出方案/分析 | ② 拿到①的转写后，core 由主代理自己完成推理并给出方案（读取 `~/.codex/config.toml` 主模型） |
| 方案需要评审/交叉核验 | ③ review：`python3 <skill_dir>/scripts/call_model.py --role review --plan <方案文件或-> [--image <原图>]`，把评审意见并入最终回答 |
| 纯文本推理、无需图片和评审 | 直接自己回答，不调用任何外部模块 |

## 模型分派（自动匹配）

- 模型名 → provider/base_url 由 `call_model.py` 自动匹配（默认 OpenCode Go 订阅）；`config.toml` 可用
  `provider:model` 显式覆盖。
- vision/review 模型在首次引导时配置；core 固定 `self`。
- 分派可先用 `scripts/route.py` 做客观分类，再结合实际上下文做最终判断。

## 输出约定

- 涉及图片时，回答里注明"已由视觉模块转写"，并附关键转写信息。
- 涉及评审时，先给结论再列问题，最后给出修订建议。
