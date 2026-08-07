# multimodal-orchestrator

一个 Codex skill：三模块流水线编排 **vision 图像识别 → core 核心处理 → review 方案评审**，
让每个模块调用不同的模型或 API。当主代理不具备视觉能力（如 DeepSeek 文本模型）、需要转写图片/截图/图表、
需要基于图片产出方案、或需要外部模型评审方案/交叉核验时使用。

## 工作原理

- **vision**：调用外部视觉模型（默认 OpenCode Go 上的 `qwen3.8-max`）把图片穷尽式转写为文本，充当主代理的"眼睛"。
- **core**：主代理自己完成推理与产出（`core = self`，不产生额外 API 调用）。
- **review**：调用外部模型（默认 `glm-5.2`）审查方案的正确性、完整性、可执行性与风险，可附原图交叉核验。
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

## 首次使用

1. 设置 API key 环境变量（不落盘、不写入任何文件）：

   ```
   export OPENCODE_API_KEY="你的 OpenCode Go/Zen key"
   ```

2. 首次触发 skill 时会按 vision / review / core 三模块引导配置，自动生成 `config.toml`；
   也可以参考仓库里的 `config.example.toml` 手工创建。
3. 引导会运行 `scripts/call_model.py --check-key` 做健康检查（Go 订阅探测 / Zen 余额探测）。
4. 想重跑引导：直接说"重新配置 multimodal-orchestrator"。

## 使用示例

```
# 分派（可选，用于确认模块组合）
python3 <skill_dir>/scripts/route.py --image /path/a.png --prompt "看图后给方案并评审"

# vision：转写图片
python3 <skill_dir>/scripts/call_model.py --role vision --image /path/a.png --prompt "转写这张图"

# review：评审方案（可附原图交叉核验）
python3 <skill_dir>/scripts/call_model.py --role review --plan 方案.md [--image /path/a.png]

# 健康检查
python3 <skill_dir>/scripts/call_model.py --check-key

# 查看内置 provider 预设
python3 <skill_dir>/scripts/call_model.py --list-presets
```

## 模型配置

- 默认 provider 为 **OpenCode Go**（订阅制，`https://opencode.ai/zen/go/v1`），一份 key 驱动 vision+review。
- 模型名自动匹配 provider（如 `qwen3.8-max` → opencode-go），也支持 `provider:model` 显式指定
  （如 `opencode-zen:gemini-3.5-flash`）与自定义 `[providers.xxx]`。
- 完整预设表、Go 可用模型清单与视觉能力实测结论见 `references/model_presets.md`。

## 安全

- API key 只从环境变量读取（默认 `OPENCODE_API_KEY`，可用 `VISION_API_KEY`/`REVIEW_API_KEY` 覆盖），
  **绝不写入 skill 文件或仓库**。
- 仓库不包含任何密钥；`config.toml`（本地运行时配置）已在 `.gitignore` 中排除。

## 测试

```
python3 scripts/test_skill.py        # 离线单测，无需 API key
python3 scripts/call_model.py --list-presets   # 只读预设列表，无需 API key
```

## License

[MIT](LICENSE) © 2026 sheshixuan
