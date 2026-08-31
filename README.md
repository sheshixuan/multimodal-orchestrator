# multimodal-orchestrator

一个给Deepseek V4 Flash装上眼睛的 Codex skill：三模块流水线编排 **vision 图像识别 → core 核心处理 → review 方案评审**，
让每个模块调用不同的模型或 API。当主代理不具备视觉能力（如 DeepSeek 文本模型）、需要转写图片/截图/图表、
需要基于图片产出方案、或需要外部模型评审方案/交叉核验时使用。

查看版本变化和行为调整：[更新日志](CHANGELOG.md)。

## 工作原理

- **vision**：调用外部视觉模型（默认 OpenCode Go 上的 `qwen3.8-max`）把图片穷尽式转写为文本，充当主代理的"眼睛"。
- **core**：主代理自己完成推理与产出（`core = self`，不产生额外 API 调用）。
- **review**：按方案难度、不确定性和风险自动选择一个或多个已配置 provider/model，匹配推理强度、token 预算与超时，输出结构化评审并在冲突时终局仲裁。
- **分派**：`scripts/route.py` 按混合路由规则自动分类提示词（含图必走 vision、显式要求评审必走 review、其余仅 core），
  主代理结合实际上下文做最终判断。

## 安装

前提：Python 3.9+（纯标准库，零第三方依赖，无需 `pip install`）。

方式一（skill-installer，推荐）：

```
codex skill install <github 仓库地址> multimodal-orchestrator
```

方式二（git clone）：

```
git clone https://github.com/sheshixuan/multimodal-orchestrator ~/.codex/skills/multimodal-orchestrator
```

方式三（其他工具）：把仓库内容拷贝到 `~/.claude/skills/multimodal-orchestrator/` 或
`~/.config/opencode/skills/multimodal-orchestrator/` 即可（目录格式与 bash 脚本执行两者通用）。

## 触发模式（auto / manual）

- `auto`（默认）：命中图片/评审等场景时自动分派执行。
- `manual`：仅当用户**提及**本 skill 才执行（`route.py` 自动识别，输出 `mention_detected: true`）：
  Codex 用 `$multimodal-orchestrator`（或 `/skills` 选择）、ChatGPT/WorkBuddy 用 `@multimodal-orchestrator`，
  直接说名称 `multimodal-orchestrator` 或中文名 `多模态编排` 也算；否定/讨论性提及
  （如"不要用/什么是 multimodal-orchestrator"）不算点名。未点名时严格停止、不调用任何外部模型、不消耗 API。
- 按宿主配置：`config.toml` 顶层 `mode` 为全局默认，`[hosts.<宿主>]` 可单独覆盖
  （如 `[hosts.codex] mode = "auto"`、`[hosts.workbudy] mode = "manual"`）；宿主名对应
  Codex→`codex`、WorkBuddy→`workbudy`、Claude→`claude`、opencode→`opencode`。
- 切换模式（随时生效，无需重跑引导）：

  ```
  python3 <skill_dir>/scripts/mode.py --set manual              # 全局切 manual
  python3 <skill_dir>/scripts/mode.py --set auto --host codex   # 仅 Codex 切 auto
  python3 <skill_dir>/scripts/mode.py --unset-host codex        # 移除覆盖，回退全局
  ```

  想重跑完整引导（重新选模式/模型）再说"重新配置 multimodal-orchestrator"。

## 首次使用

1. 设置 API key 环境变量（不落盘、不写入任何文件）：

   ```
   export OPENCODE_API_KEY="你的 OpenCode Go/Zen key"
   ```

2. 首次触发 skill 时会先询问**触发模式**（统一或按宿主选择 auto/manual），
   再按 vision / review / core 三模块引导配置 API，自动生成 `config.toml`；
   也可以参考仓库里的 `config.example.toml` 手工创建。
3. 引导会询问是否安装 **Codex Plan 收尾评审门 hook**（默认是），运行
   `scripts/install_plan_hook.py --install` 写入 `~/.codex/hooks.json`；
   安装后先在 Codex 里运行 `/hooks` 信任该 hook，再重启 Codex 或新开任务生效。
4. 引导会运行 `scripts/call_model.py --check-key` 做健康检查（Go 订阅探测 / Zen 余额探测）。
5. 想重跑引导：直接说"重新配置 multimodal-orchestrator"。

## 使用示例

```
# 分派（可选，用于确认模块组合；manual 模式未点名时 enabled=false）
python3 <skill_dir>/scripts/route.py --host codex --image /path/a.png --prompt "看图后给方案并评审"

# vision：转写图片
python3 <skill_dir>/scripts/call_model.py --role vision --image /path/a.png --prompt "转写这张图"

# review：先纯本地评估，再执行建议路由
python3 <skill_dir>/scripts/review_plan.py --assess 方案.md --json
python3 <skill_dir>/scripts/review_plan.py --review 方案.md [--image /path/a.png]

# 健康检查
python3 <skill_dir>/scripts/call_model.py --check-key

# 查看内置 provider 预设
python3 <skill_dir>/scripts/call_model.py --list-presets
```

## Codex Plan 收尾评审（Plan Review Gate）

安装本 skill 并执行 `scripts/install_plan_hook.py --install` 后，会在
`~/.codex/hooks.json` 注册一个 `UserPromptSubmit` hook：每次使用 Codex Plan 模式，
在计划草稿完成、准备提交最终计划前，按 `plan_review` 配置进入评审门。
首次信任 hook：在 Codex 里运行 `/hooks`，然后重启 Codex 或新开任务。

```
# 纯本地评估：输出评分、动态推荐的评审模式、建议模型、预计耗时和资源上限
python3 <skill_dir>/scripts/review_plan.py --assess 计划.md --json

# 查看/切换 Plan 收尾评审时机：off / ask / auto
python3 <skill_dir>/scripts/review_plan.py --plan-mode
python3 <skill_dir>/scripts/review_plan.py --set-plan-mode ask

# 保存用户选择的评审模型（可跨任务复用）
python3 <skill_dir>/scripts/review_plan.py --set-model kimi-k3

# 用户选择单模型或多模型后执行；若退出码为 4，需先确认慢任务
python3 <skill_dir>/scripts/review_plan.py --review 计划.md --strategy single
python3 <skill_dir>/scripts/review_plan.py --review 计划.md --strategy multi --confirm-slow

# 用户选择跳过；不调用任何外部模型
python3 <skill_dir>/scripts/review_plan.py --review 计划.md --strategy skip

# 本次临时指定评审模型并附原图交叉核验
python3 <skill_dir>/scripts/review_plan.py --review 计划.md \
  --model opencode-zen:gemini-3.1-pro --image 原图.png

# 固定两个首轮评审者
python3 <skill_dir>/scripts/review_plan.py --review 计划.md \
  --strategy multi --reviewer provider-a:model-a --reviewer provider-b:model-b --confirm-slow
```

`plan_review` 三档：`ask`（默认，第一层只展示单模型、多模型、跳过，并按方案评分动态推荐）、
`auto`（普通评审告知后自动执行，慢任务仍确认）、`off`（关闭）。旧值 `true/false`
兼容为 `ask/off`。`--assess` 不访问 API；多模型、预计超过 180 秒、分片或扩容必须加
`--confirm-slow`。卸载 hook：`scripts/install_plan_hook.py --uninstall`。

自动路由按能力字段工作，不在路由代码中硬编码具体模型/provider。`routine` 速度优先单模型，
`complex` 质量优先单模型，`critical` 选择两个不同家族、优先不同 provider 的模型并行评审。
冲突时最多再调用一个未参与首轮的模型；无可用第三模型时标记 `adjudicated_by=core`。

可在本地 `config.toml` 中同时保留多个 provider 的评审候选；内置候选不会被替换，
重复模型会自动去重：

```toml
[review_models]
opencode_go = "glm-5.2"
private_proxy = "myproxy:review-model"

[review_routing]
max_calls = 3
max_total_output_tokens = 131072
max_wall_seconds = 1800
slow_confirm_seconds = 180

[providers.myproxy]
base_url = "https://your-proxy.example.com/v1"
env = "MY_PROXY_API_KEY"
```

这里只保存模型、能力、接口地址和环境变量名称。API key 本身仍只放环境变量。完整配置、
token 语义、失败分类和长方案分片见 `references/plan_review_routing.md`。

## 模型配置

- 默认 provider 为 **OpenCode Go**（订阅制，`https://opencode.ai/zen/go/v1`），一份 key 驱动 vision+review。
- 模型名自动匹配 provider（如 `qwen3.8-max` → opencode-go），也支持 `provider:model` 显式指定
  （如 `opencode-zen:gemini-3.5-flash`）与自定义 `[providers.xxx]`。
- 完整预设表、Go 可用模型清单与视觉能力实测结论见 `references/model_presets.md`。

## 安全

- API key 只从环境变量读取（默认 `OPENCODE_API_KEY`，可用 `VISION_API_KEY`/`REVIEW_API_KEY` 覆盖），
  **绝不写入 skill 文件或仓库**。
- 仓库不包含任何密钥；`config.toml`（本地运行时配置）已在 `.gitignore` 中排除。
- `.local/review-runtime.json` 只保存能力、耗时和 token 统计，不保存计划、回答、隐藏推理或鉴权头。

## 测试

```
python3 -m unittest discover -s scripts -p 'test_*.py'  # 离线单测，无需 API key
python3 scripts/call_model.py --list-presets   # 只读预设列表，无需 API key
```

## License

[MIT](LICENSE) © 2026 sheshixuan
