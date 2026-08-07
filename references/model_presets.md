# 模型预设表（Model Presets）

`scripts/call_model.py` 内置的 provider 预设。模型名自动匹配规则：按前缀长度从长到短匹配；
未命中时在 `config.toml` 用 `provider:model` 显式指定。运行 `python3 scripts/call_model.py --list-presets` 可查看实时表。

## 内置 provider

| provider | base_url | 环境变量 | 自动匹配前缀 | 说明 |
|---|---|---|---|---|
| `opencode-go` | `https://opencode.ai/zen/go/v1` | `OPENCODE_API_KEY` | `deepseek-v4-`、`qwen3.8-`、`qwen3.7-`、`qwen3.6-`、`qwen3.5-`、`minimax-`、`kimi-`、`glm-`、`mimo-v2`、`gpt-5.6-`、`grok-`、`hy3` | **默认 provider（订阅制）**；一份 key 覆盖 vision+review；`--check-key` 探测订阅有效性 |
| `opencode-zen` | `https://opencode.ai/zen/v1` | `OPENCODE_API_KEY` | `gemini-3.6/3.5/3.1/3`、`claude-`、`gpt-5`、`gpt-4`、`deepseek-v`、`glm-`、`kimi-`、`grok-`、`minimax-`、`mimo`、`north-`、`ling-`、`nemotron-`、`longcat-`、`big-pickle`、`laguna-`、`qwen3` | Zen 按量计费；需在 opencode 工作区充值。Go 订阅用户如需用 gemini/claude 等需走此端点 |
| `gemini` | `https://generativelanguage.googleapis.com/v1beta/openai` | `GEMINI_API_KEY` | `gemini-`（zen 之外的 gemini-2.x 等） | Google Gemini 官方 OpenAI 兼容端点 |
| `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `DASHSCOPE_API_KEY` | `qwen-vl`、`qwen2.5-vl`、`qwen3-vl`、`qwq-`、`qwen-max`、`qwen-plus`、`qwen-turbo` | 阿里云百炼（qwen-vl 系列识图） |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` | （显式指定） | OpenAI 官方 |
| `deepseek` | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | （显式指定） | DeepSeek 官方（无视觉，仅文本） |

## OpenCode Go 订阅可用模型（25 个，2026-08-07 实测）

- 文本/推理：`glm-5.2`、`glm-5.1`、`glm-5`、`kimi-k3`、`kimi-k2.7-code`、`kimi-k2.6`、`kimi-k2.5`、
  `deepseek-v4-pro`、`deepseek-v4-flash`、`minimax-m3`、`minimax-m2.7`、`minimax-m2.5`
- 通义：`qwen3.8-max`、`qwen3.7-max`、`qwen3.7-plus`、`qwen3.6-plus`、`qwen3.5-plus`
- 小米 mimo：`mimo-v2.5`、`mimo-v2.5-pro`、`mimo-v2-pro`、`mimo-v2-omni`（已废弃，勿用）
- 其他：`gpt-5.6-luna`、`grok-4.5`、`hy3`、`hy3-preview`

## 视觉能力实测结论（Go 端点，2026-08-07）

- ✅ **可识图**：`qwen3.8-max`（推荐，响应快）、`mimo-v2.5`（reasoning 型，需给足 `--max-tokens`）
- ❌ 不可识图：`glm-5.x`（明确无多模态）、`mimo-v2.5-pro`（无图片输入端点）、`gpt-5.6-luna`（静默忽略图片）、`deepseek-v4-*`
- 若配置的 vision 模型不支持图片，脚本会报错并提示换模型；请优先选 `qwen3.8-max`。

## 常用模型建议（OpenCode Go 订阅）

- 视觉（vision）：`qwen3.8-max`（推荐，实测可识图）、`mimo-v2.5`（备选，需加大 max_tokens）
- 评审（review）：`glm-5.2`（推荐）、`kimi-k3`（备选）、`deepseek-v4-pro`（备选）
- 健康检查默认用 `deepseek-v4-flash` 探测订阅有效性（成本为 0，订阅制不限量）。
- Zen 用户（按量）：vision 推荐 `gemini-3.5-flash`，review 推荐 `gemini-3.1-pro`。

## 自定义 provider

在 `config.toml` 增加：

```toml
[providers.myproxy]
base_url = "https://your-proxy.example.com/v1"
env = "MY_API_KEY"
```

然后在模型名里写 `myproxy:模型名`。新增 provider 也可在 `scripts/call_model.py` 的 `PRESETS` 中补充前缀，
并把本表同步更新。
