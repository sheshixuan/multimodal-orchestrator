# Plan Review 智能路由

在 Plan 收尾评审、手动方案评审、推理模型长时间无正文或返回 `finish_reason=length` 时读取本参考。

## 标准流程

1. 本地评估，不访问 API：

   ```bash
   python3 scripts/review_plan.py --assess plan.md --json
   ```

2. 告知用户评分依据、模型/provider、推理档位、预计 P50–P90、硬超时、调用/token 上限和能力置信度。
3. 普通单模型评审可在告知后执行。多模型、预计超过 180 秒、分片或预算扩容必须先确认：

   ```bash
   python3 scripts/review_plan.py --review plan.md --confirm-slow
   ```

4. 用户可覆盖自动结果：

   ```bash
   python3 scripts/review_plan.py --review plan.md --model provider:model
   python3 scripts/review_plan.py --review plan.md \
     --reviewer provider-a:model-a --reviewer provider-b:model-b
   ```

`--model` 强制单评审者；可重复的 `--reviewer` 固定首轮评审者。`--effort` 和 `--timeout`
只覆盖本次调用；推理档位必须存在于模型能力档案中。需要确认但未传 `--confirm-slow` 时退出码为 4，且不调用外部 API。

Plan Review Gate 的 hook 只负责触发本流程，不等于用户授权慢调用。`plan_review=auto` 也只自动放行已告知的普通单模型评审；
多模型、慢任务、分片和重试始终需要用户本次明确确认。手动触发模式不会覆盖这些确认边界。

## 评估与选择

评分为 `0.45 × difficulty + 0.35 × uncertainty + 0.20 × risk`：

- 缺少会改变方向的用户决定或关键事实：`blocked`，先询问。例如尚未决定数据是否允许破坏性迁移、认证边界由谁负责，
  或两个互斥架构目标没有优先级。仅仅存在可由评审验证的未知依赖、外部服务行为或实现假设会提高 `uncertainty`，不会自动阻塞。
- 任一单项达到 70 或综合达到 70：`critical`。
- 综合达到 40：`complex`。
- 其余：`routine`。

`routine` 选择一个速度优先的合格模型；`complex` 选择一个质量优先模型；`critical` 选择两个不同家族、
优先不同 provider 的高质量模型并行评审。关键任务的通知会明确说明首轮并行调用占用两个调用名额；若第三次被扩容占用，
冲突只能由主代理裁决。路由代码只读取能力字段，不按模型名或 provider 名分支。
关键任务先筛选不同模型家族，再在其中优先不同 provider；只有没有同时满足条件的候选时才按可用能力降级。

能力来源优先级为：

1. `[review_capabilities.<alias>]` 本地覆盖。
2. provider `/models` 的有效扩展字段。
3. 可信模型目录。
4. 本地缓存。
5. 名称启发式推断。

只含模型 ID 的 `/models` 响应不会被当成高置信度能力数据，也不会阻止更完整的目录元数据。名称推断标记为
`confidence=low`，未知 token 语义按 `combined` 保守处理；真实评审响应会校验行为并更新本地缓存。若真实响应显示
`finish_reason=length`、正文为空且推理 token 达到预算的 90%，该次响应即可把组合预算语义提升为已观测事实，允许一次已获确认的扩容；
系统不为此单独消耗探测调用。

## 配置

```toml
[review_routing]
mode = "auto"
slow_confirm_seconds = 180
max_calls = 3
max_total_output_tokens = 131072
max_wall_seconds = 1800
heartbeat_seconds = 30
long_plan_strategy = "adaptive_then_chunk"
conflict_judge = true

[review_models]
primary = "provider-a:model-a"
secondary = "provider-b:model-b"

[review_capabilities.primary]
family = "family-a"
modalities = ["text"]
quality = 92
reasoning_levels = ["high", "max"]
reasoning_field = "reasoning_effort"
context_limit = 1000000
output_limit = 384000
token_semantics = "combined"
streaming = true
```

能力覆盖是可选的。未配置 `[review_routing]` 时仍默认自动路由，旧 `review_model` 会作为候选继续工作。
`plan_review=ask|auto|off` 只决定评审门何时触发，不决定模型能力。

## Token 与超时

默认上限：`routine=8K`、`complex=24K`、`critical=32K/评审者`、仲裁 32K；整项任务最多 3 次调用、
累计 128K 输出 token 和 30 分钟。字段按能力档案发送：

`max_calls` 可以调低但不能配置为大于 3；执行器自身也会再次施加三次硬上限，避免其他调用方绕过配置校验。

- `answer_only`：发送 `max_tokens`。
- `combined` 或 `unknown`：发送 `max_completion_tokens`。
- `separate`：同时限制答案和总 completion。

只发送模型声明支持的 `reasoning_effort`。流式调用记录首次推理、首次正文和总耗时，每 30 秒报告进度，
但不会输出或缓存 `reasoning_content`。

延迟统计只保留每个 `provider:model:tier` 最近 20 次、30 天内的成功样本。少于 3 个样本时使用档位默认值；
否则展示 P50–P90，硬超时取 `max(P90 × 1.5, 档位下限)`，且不能超过任务总时限。

## 失败分类与长方案

- `reasoning_budget_exhausted`：`length`、正文为空、推理已接近组合预算。这不是输入上下文溢出。
- `incomplete_review`：已有部分正文但被长度截断，不能合并为最终评审。
- `input_context_overflow`：本地估算超过 85% 安全线，或 provider 明确报告输入过长。
- `empty_response` / `api_error`：分别表示空响应和其他接口错误。

任何层级的已确认评审发生 `reasoning_budget_exhausted` 或 `incomplete_review` 时，只允许一次预算翻倍；低置信度名称推断
只有在本次真实响应已观测到上述 90% 组合预算特征时才允许自动扩容；`incomplete_review` 也可由 completion 总量达到预算 90%
确认已触顶。扩容后的预算必须严格增大，否则直接进入有效分片或返回资源不足。扩容后仍失败或输入过长时，按 Markdown 章节切分。
若普通评审是在未传 `--confirm-slow` 的情况下才发现需要扩容或分片，系统会在首个响应后停止并以退出码 4 返回；它不会先行发起第二次调用。
用户确认后重跑才允许自适应调用。
预检已经判断输入过长时，分片会直接替代关键任务的双模型策略，并只使用路由排名最高的首位评审者；运行时失败后的分片也只使用该评审者。
每个分片原样包含不可变全局约束：目标、非目标、范围、
关键接口、验收标准和已确定决策。分片数仍受 3 次调用总上限约束；无法在上限内完成时返回 `resource_limit`，
不静默增加成本。单个超长章节会继续按自然边界或固定窗口拆分，并对每个最终分片重新执行 token 安全线校验；
不可变约束本身无法装入安全线时直接返回资源不足。任一必需分片失败或无效时，整体不得返回成功。

第三次调用的资源仲裁顺序固定，不受并发返回先后影响：首轮先占两个名额；如有评审失败，按原路由排名给最高优先失败者一次扩容重试；
只有不需要重试且两个有效首轮意见存在实质冲突时，第三次才用于独立仲裁。若调用余额不足以完成全部分片，则直接返回资源不足，
不会在双模型、分片或仲裁之间隐式增加第四次调用。

## 合并与仲裁

模型必须返回固定 JSON：`verdict`、六个评审维度、带 severity/evidence/recommendation 的 issues、
unresolved questions 和 confidence。

只有两个有效首轮评审存在实质冲突时才触发仲裁：`pass` 对 `block`、同维度相差两级，或同一 `decision_key` 推荐不同选项。
仅覆盖范围不同不算冲突。仲裁使用未参加首轮且质量最高的合格模型；没有第三模型、额度不足或第三次调用已用于扩容时，由主代理裁决并标记
`adjudicated_by=core`。仲裁结果是终局，不再触发递归评审。

## 本地数据与密钥

能力和延迟缓存位于 `.local/review-runtime.json`，目录被 Git 忽略，文件权限为 `0600`。缓存不含计划正文、模型回答、
API key、Authorization 头或隐藏推理。API key 只从角色级、provider 级环境变量或宿主 Keychain 注入，禁止写入仓库配置。
