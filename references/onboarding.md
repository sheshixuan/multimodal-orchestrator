# 首次配置引导（Onboarding）

在 `config.toml` 不存在，或用户明确要求"重新配置 multimodal-orchestrator"时，按以下步骤执行。
引导完成后继续处理用户最初的请求。默认 provider 为 OpenCode Go（订阅制，`OPENCODE_API_KEY`）。

## 步骤

1. **选择触发模式**（先于 API 配置）：
   - 先问用户："所有宿主统一用一种模式，还是按宿主（Codex/WorkBuddy/Claude/opencode）分别设置？"
   - 统一：确认一个模式（推荐 `auto`），写入顶层 `mode`；
   - 分别：逐个询问各宿主用 `auto` 还是 `manual`（如 Codex=auto、WorkBuddy=manual），
     写入 `[hosts.<宿主>]` 段；用户未提及的宿主用全局默认（推荐 `auto`）。
   - 说明：`manual` 模式下，只有用户显式点名本 skill 才会执行，未点名时绝不调用外部模型。
2. **确认三模块模型**（逐一询问，附推荐项）：
   - vision：推荐从 OpenCode Go 模型列表选 `qwen3.8-max`（实测可识图）或 `mimo-v2.5`（备选，reasoning 型需给足 max_tokens）。
   - review：推荐 `glm-5.2`（评审稳健）或 `kimi-k3` / `deepseek-v4-pro`。
   - core：固定 `self`，无需询问（主代理自处理，读取 `~/.codex/config.toml` 主模型）。
3. **确认 API key 来源**：默认使用环境变量 `OPENCODE_API_KEY`；用户也可改用
   `VISION_API_KEY`/`REVIEW_API_KEY`（模块级覆盖）或 `GEMINI_API_KEY` 等（provider 级）。
   让用户把 key 放进 shell 配置（如 `~/.zshrc` 的 `export`），**不要写入任何文件**。
4. **健康检查**：运行
   `python3 <skill_dir>/scripts/call_model.py --check-key`。
   - 成功（鉴权 OK + 用 `deepseek-v4-flash` 实测调用通过）→ Go 订阅有效，继续。
   - 提示缺少 API key → 让用户设置环境变量后重试。
   - Go 端点返回 `ModelError / 401` → 检查 key 是否对应有效订阅；Zen 端点返回
     `CreditsError / Insufficient balance` → 告知用户去 opencode billing 充值后重试，
     **中止写入配置**。
5. **写配置**：把 mode 段与三行模型配置写入 `<skill_dir>/config.toml`（按用户选择替换模式与模型名）：

   ```toml
   mode = "auto"

   [hosts.workbudy]
   mode = "manual"

   vision_model = "qwen3.8-max"
   review_model = "glm-5.2"
   core = "self"
   ```

   如需显式指定 provider，模型名写成 `provider:model`（如 `opencode-zen:gemini-3.5-flash`）；
   如需自定义 provider，按 `references/model_presets.md` 增加 `[providers.xxx]`。
6. **继续原任务**：回到分派流程（`scripts/route.py --host <当前宿主> [--explicit]` → 执行各模块）
   处理用户最初的请求。
