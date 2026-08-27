---
name: multimodal-orchestrator
description: 三模块流水线编排（vision 图像识别 → core 核心处理 → review 方案评审），让不同模块调用不同模型或 API。当主代理不具备视觉能力、需要转写图片/截图/图表、需要基于图片产出方案、或需要外部模型（如 OpenCode Go/Zen、Gemini）评审方案或交叉核验时使用。支持 auto（命中场景自动触发）与 manual（需提及式调用）两种触发模式，按宿主（Codex/WorkBuddy 等）分别配置；可为 Codex Plan 模式安装收尾评审门（plan_review=off/ask/auto）；首次使用会先选择模式再引导配置 API；用户说"重新配置 multimodal-orchestrator"时重跑引导。
---

# Multimodal Orchestrator

把任务拆成三个可配置模块，用不同模型/API 处理：

1. **vision** — 图像识别：把图片转写为穷尽文本，是主代理（无视觉能力）的"眼睛"。
2. **core** — 核心流程：由主代理自己完成推理与产出（`core = self`，读取 `~/.codex/config.toml` 主模型）。
3. **review** — 方案评审：调用外部模型审查方案的正确性、完整性、可执行性与风险，可附原图交叉核验。

> 下文 `<skill_dir>` 指 skill 安装目录：Codex 默认 `~/.codex/skills/multimodal-orchestrator/`，
> 也可位于 `~/.claude/skills/` 或 `~/.config/opencode/skills/` 等任意位置；脚本本身全部用相对路径动态定位，可整体迁移。

## 触发模式（auto / manual）

生效模式由 `config.toml` 决定，解析顺序：`[hosts.<宿主>].mode` → 顶层 `mode` → 默认 `auto`。
以 `python3 <skill_dir>/scripts/route.py --host <宿主> [--explicit] --prompt "..."` 输出的 `mode` 与 `enabled` 为准。

- **宿主映射**：Codex → `codex`；WorkBuddy → `workbudy`；Claude → `claude`；opencode → `opencode`；
  不确定当前宿主时不传 `--host`（使用全局默认 mode）。
- **随时切换**：无需重跑引导。用户说"切换到 manual/auto 模式"或"把 Codex 切成 manual"时，
  运行 `python3 <skill_dir>/scripts/mode.py --set <auto|manual> [--host <宿主>]` 即时生效
  （行级写入，保留注释与其他配置）；`--unset-host <宿主>` 可移除某宿主覆盖回退全局。
  此类模式管理请求即使当前为 manual 模式也直接执行（只改本地配置，不调用外部模型）。
- **auto 模式**（默认）：`enabled=true`，命中场景即按下方工作流执行。
- **manual 模式**：仅当用户**提及**本 skill 时才执行。`route.py` 自动识别提及
  （输出 `mention_detected: true` → `explicit=true`），主代理无需再传 `--explicit`（仅作兜底）：
  - **提及形式**：Codex 用 `$multimodal-orchestrator`（也可用 `/skills` 选择）；ChatGPT/WorkBuddy 用
    `@multimodal-orchestrator`（`$`/`@` 后容忍空格、大小写不敏感）；纯名称 `multimodal-orchestrator`
    或中文名 `多模态编排` 也算点名；
  - **不算点名**：否定/讨论性提及（如"不要用 multimodal-orchestrator""什么是 multimodal-orchestrator"、
    "介绍一下 multimodal-orchestrator"）以及模糊说法（如"用多模态处理一下""看图给方案"）；
  - 未点名时 **绝对禁止调用任何外部模型**（包括 vision/review 的 `call_model.py`），
    提示"需要时请提及：Codex 用 $multimodal-orchestrator，ChatGPT/WorkBuddy 用 @multimodal-orchestrator"
    后停止，不消耗任何 API。

## 工作流

1. **分派**：运行 `python3 <skill_dir>/scripts/route.py --host <当前宿主> [--explicit] --prompt "<用户提示词>" [--image <路径>]`
   得到 JSON 分派计划（含 `mode`/`enabled`/`mention_detected`/`clean_prompt`），再结合实际上下文做最终判断；
   `enabled=false` 时停止，不执行任何模块；构造 vision/review 提示词时使用 `clean_prompt`
   （已剥离 `$`/`@` 符号）而非含符号的原始文本。
   规则：含图必走 vision；显式要求评审必走 review；其余仅 core。
2. **vision（如需要）**：`python3 <skill_dir>/scripts/call_model.py --role vision --image <绝对路径> [--image ...] --prompt "转写需求"`，
   拿到文本转写后继续。模型取自 `config.toml` 的 `vision_model`。
3. **core**：主代理基于转写/提示词自行推理产出方案；需要落盘时写入文件。
4. **review（如需要）**：`python3 <skill_dir>/scripts/call_model.py --role review --plan <方案文件或-> [--image <原图>]`，
   把评审意见并入最终回答（先结论、再问题、后修订建议）。模型取自 `config.toml` 的 `review_model`。

## Codex Plan 模式集成（Plan Review Gate）

在 Codex Plan 模式中，计划草稿完成、准备提交最终计划前，按 `plan_review`
配置执行本评审门。触发由 `~/.codex/hooks.json` 中的 `UserPromptSubmit` hook
完成（新安装/重新配置时在首次引导里询问是否安装；也可手动运行
`python3 <skill_dir>/scripts/install_plan_hook.py --install`）。hook 只在
Codex 的 Plan 权限模式下注入指令，不会在普通任务里打扰。

1. **完成实施计划**：先把计划草稿落盘为文件（如 `<项目>/.codex/plan-review.md` 或
   `work/plan-review.md`），不要在计划完成前先询问用户选择评审模型。
2. **准备 review 计划**：运行 `python3 <skill_dir>/scripts/review_plan.py --list-models`，
   整理一份简短 review 计划，说明本次评审重点、推荐/备选模型与 API key 状态；本步骤不调用外部模型。
3. **最后一步同时展示**：把实施计划和 review 计划一起展示给用户，
   询问"用哪个模型评审？也可以跳过"；用户也可以先审阅实施计划再决定。
4. **持久化选择**：所选模型与 `config.toml` 不同时，运行
   `python3 <skill_dir>/scripts/review_plan.py --set-model <模型>`。
5. **评审计划**：运行 `python3 <skill_dir>/scripts/review_plan.py --review <计划文件> [--image <原图>] [--model <模型>]`。
6. **修订并提交**：按输出约定把评审意见并入计划（先结论、再问题、后修订建议），修订后再提交最终计划。

控制与边界：
- 顶层 `plan_review` 支持三档（缺失视为 `ask`；旧值 `true/false` 分别兼容为 `ask/off`）：
  - `ask`（默认）：先完成实施计划并准备 review 计划，最后一步同时展示后等待用户选择，用户也可跳过。
  - `auto`：不询问模型，直接用当前 `review_model` 自动评审并修订后提交。
  - `off`：完全关闭本门，hook 不注入任何指令。
- 随时切换（无需重跑引导）：`python3 <skill_dir>/scripts/review_plan.py --set-plan-mode off|ask|auto`；
  查看当前值用 `--plan-mode`。
- 本门与 auto/manual 无关：向用户询问模型选择是发起 review 的显式步骤；用户未选择模型前
  不调用任何外部模型，因此 manual 模式同样适用。
- 安装 hook 后需在 Codex 里运行 `/hooks` 审查并信任该 hook；信任后重启 Codex 或开新任务生效
  （当前已打开的会话不会重新加载 hook）。
- `~/.codex/AGENTS.md` 保留一份相同规则作为未安装 hook 时的回退；已安装 hook 时以 hook 注入为准。
- 在非 Codex 宿主（WorkBuddy/Claude/opencode）中，仅当宿主流程明确处于等价"计划收尾"阶段时按本节执行。

## 首次使用（配置引导）

若 `<skill_dir>/config.toml` 不存在，按 `references/onboarding.md` 执行：

1. 询问 vision 模型（推荐 `qwen3.8-max`，实测可识图）与 review 模型（推荐 `glm-5.2`）；core 固定 `self`。
2. 确认 API key 环境变量（默认 `OPENCODE_API_KEY`），运行 `scripts/call_model.py --check-key` 校验；
   默认走 OpenCode Go 订阅探测（订阅无效则中止写入）；若用 Zen 端点，余额不足会提示充值。
3. 询问是否安装 Codex Plan 收尾评审门 hook（默认是）：运行
   `python3 <skill_dir>/scripts/install_plan_hook.py --install`，然后提示用户
   在 Codex 里运行 `/hooks` 信任并重启/新开任务；`plan_review` 写入 `ask`（默认）。
4. 把 `vision_model`/`review_model`/`core`/`plan_review` 写入 `config.toml`
   （key 不落盘），然后继续原任务。

用户说"重新配置 multimodal-orchestrator"时，重跑上述引导。
仅切换 auto/manual 模式不用重跑引导，用上面的 `mode.py --set` 即可。

## 配置与预设

- `config.toml`（skill 目录内）：`vision_model`、`review_model`、`core = "self"`，
  可选 `plan_review = "off"|"ask"|"auto"`（Codex Plan 收尾评审门时机，缺失视为 `ask`；
  旧值 `true/false` 兼容为 `ask/off`）。
- 模型名可写 `provider:model`（如 `opencode-zen:gemini-3.5-flash`）；支持 `[providers.<name>]` 自定义 base_url/env，
  详见 `references/model_presets.md`。
- API key 只从环境变量读取：默认 `OPENCODE_API_KEY`；vision/review 可分别用 `VISION_API_KEY`/`REVIEW_API_KEY`
  覆盖；兼容 `GEMINI_API_KEY`、`DASHSCOPE_API_KEY` 等 provider 级变量。
- 其他参考：`references/agent_registry.md`（分派表与角色）、`references/vision_subagent_prompt.md`（vision 系统提示词）、
  `references/onboarding.md`（引导脚本）。

## 输出约定

- 涉及图片时注明"已由视觉模块转写"，并附关键转写信息。
- 涉及评审时先给结论，再列问题，最后给修订建议。
