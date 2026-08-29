#!/usr/bin/env python3
"""Codex Plan 收尾评审门（Plan Review Gate）辅助脚本。

在 Codex Plan 模式提交最终计划前：
  1. 纯本地评估计划的难度、不确定性和风险
  2. 从用户已配置的 provider/model 中匹配评审者、推理强度和等待时间
  3. 流式执行结构化评审，必要时扩容、分片或进行一次终局仲裁

用法示例：
  python3 review_plan.py --list-models
  python3 review_plan.py --list-models --json
  python3 review_plan.py --assess 计划.md --json
  python3 review_plan.py --set-model kimi-k3
  python3 review_plan.py --review 计划.md
  python3 review_plan.py --review 计划.md --confirm-slow
  python3 review_plan.py --review 计划.md --model opencode-zen:gemini-3.1-pro --image 原图.png

退出码：0=成功 1=API/网络/评审错误 2=缺少 API key 3=配置/参数/待定事项 4=需要慢任务确认
"""
import argparse
import copy
import json
import os
import re
import sys
import urllib.error
from dataclasses import replace
from pathlib import Path

import call_model
from call_model import ConfigError, build_providers, load_config, resolve_provider
import review_engine
import review_router

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config.toml"
DEFAULT_STATE = SKILL_ROOT / ".local" / "review-runtime.json"
DEFAULT_CATALOG_URL = "https://models.dev/api.json"

REVIEW_MODELS = [
    {
        "model": "glm-5.2",
        "recommended": True,
        "note": "OpenCode Go 订阅，评审稳健",
    },
    {
        "model": "kimi-k3",
        "recommended": False,
        "note": "OpenCode Go 订阅，备选",
    },
    {
        "model": "deepseek-v4-pro",
        "recommended": False,
        "note": "OpenCode Go 订阅，备选",
    },
    {
        "model": "opencode-zen:gemini-3.1-pro",
        "recommended": False,
        "note": "OpenCode Zen 按量，备选",
    },
]

PLAN_MODES = ("off", "ask", "auto")

_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=")


def _is_section(line):
    stripped = line.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _key_of(line):
    match = _KEY_RE.match(line)
    return match.group(1) if match else None


def _first_section_index(lines):
    for i, line in enumerate(lines):
        if _is_section(line):
            return i
    return None


def _find_top_level_key(lines, key):
    for i, line in enumerate(lines):
        if _is_section(line):
            break
        if _key_of(line) == key:
            return i
    return None


def _key_ready(model, providers):
    try:
        provider, _ = resolve_provider(model, providers)
        envs = ["REVIEW_API_KEY", providers[provider]["env"]]
        return any(os.environ.get(name) for name in envs)
    except (ConfigError, KeyError):
        return False


def resolve_plan_mode(cfg):
    """返回 plan_review 生效时机：off / ask / auto。

    缺失默认 ask；旧配置 true/false 分别兼容为 ask/off。
    """
    value = cfg.get("plan_review", "ask")
    if isinstance(value, bool):
        return "ask" if value else "off"
    if value in PLAN_MODES:
        return value
    if value == "true":
        return "ask"
    if value == "false":
        return "off"
    if value == "always":
        return "ask"
    raise ConfigError(
        f"非法 plan_review：{value!r}（仅支持 off/ask/auto，旧值 true/false 也兼容）"
    )


def list_models(cfg, providers):
    current = cfg.get("review_model", "")
    presets = list(REVIEW_MODELS)
    seen = {preset["model"] for preset in presets}
    configured = cfg.get("review_models", {})
    if not isinstance(configured, dict):
        raise ConfigError("review_models 必须使用 [review_models] 配置段")
    for name, model in configured.items():
        if not isinstance(model, str):
            raise ConfigError(f"review_models.{name} 必须是模型名字符串")
        model = model.strip()
        if not model or model in seen:
            continue
        presets.append(
            {
                "model": model,
                "recommended": False,
                "note": f"本地配置：{name}",
            }
        )
        seen.add(model)
    items = []
    for index, preset in enumerate(presets, start=1):
        model = preset["model"]
        provider, resolved = resolve_provider(model, providers)
        items.append(
            {
                "index": index,
                "model": model,
                "resolved_model": resolved,
                "provider": provider,
                "recommended": preset["recommended"],
                "note": preset["note"],
                "key_ready": _key_ready(model, providers),
                "current": bool(current) and model == current,
            }
        )
    return items


def render_list(items, current, plan_mode):
    lines = [f"当前 review_model: {current or '（未配置）'}"]
    lines.append(f"当前 plan_review: {plan_mode}")
    lines.append("可用评审模型（推荐项优先）：")
    for item in items:
        flags = []
        if item["recommended"]:
            flags.append("推荐")
        if item["current"]:
            flags.append("当前")
        flags.append("API key 已配置" if item["key_ready"] else "缺少 API key")
        tag = f"[{'/'.join(flags)}] " if flags else ""
        lines.append(f"{item['index']}. {item['model']:<34} {tag}{item['note']}")
    lines.append(
        "Codex 流程：向用户展示列表并等待选择；选中后运行 --set-model 与 --review。"
    )
    return "\n".join(lines)


def set_review_model(path, model):
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            "config.toml 不存在：请先运行首次引导"
            "（说『重新配置 multimodal-orchestrator』）或参考 config.example.toml 创建。"
        )
    providers = build_providers(load_config(path))
    resolve_provider(model, providers)
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _find_top_level_key(lines, "review_model")
    line = f'review_model = "{model}"'
    if idx is not None:
        lines[idx] = line
    else:
        insert_at = _first_section_index(lines)
        if insert_at is None:
            insert_at = len(lines)
        lines.insert(insert_at, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return model


def set_plan_mode(path, mode):
    """行级更新 config.toml 的 plan_review（保留注释与其他键）。"""
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            "config.toml 不存在：请先运行首次引导"
            "（说『重新配置 multimodal-orchestrator』）或参考 config.example.toml 创建。"
        )
    if mode not in PLAN_MODES:
        raise ConfigError(f"非法 plan_review：{mode!r}（仅支持 {'/'.join(PLAN_MODES)}）")
    lines = path.read_text(encoding="utf-8").splitlines()
    idx = _find_top_level_key(lines, "plan_review")
    line = f'plan_review = "{mode}"'
    if idx is not None:
        lines[idx] = line
    else:
        insert_at = _first_section_index(lines)
        if insert_at is None:
            insert_at = len(lines)
        lines.insert(insert_at, line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mode


def _cached_metadata(state):
    raw = state.get("capabilities", {}) if isinstance(state, dict) else {}
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in raw.items():
        if not isinstance(key, str) or ":" not in key or not isinstance(value, dict):
            continue
        provider, model = key.split(":", 1)
        clean = dict(value)
        clean.setdefault("source", "cache")
        clean.setdefault("confidence", "medium")
        if isinstance(clean.get("effort_levels"), list):
            clean["effort_levels"] = tuple(clean["effort_levels"])
        result[(provider, model)] = clean
    return result


def _apply_latency(profiles, state, tier):
    updated = []
    for profile in profiles:
        p50, p90 = review_router.telemetry_latency(
            state, profile.provider, profile.model, tier
        )
        updated.append(
            replace(
                profile,
                p50_seconds=p50 or profile.p50_seconds,
                p90_seconds=p90 or profile.p90_seconds,
            )
        )
    return updated


def assess_review(plan, cfg, providers, state=None):
    """纯本地评估；不会访问 provider、目录或模型 API。"""
    state = state or {}
    assessment = review_router.assess_plan(plan)
    profiles = review_router.build_capability_profiles(cfg, providers)
    profiles = review_router.apply_capability_metadata(profiles, _cached_metadata(state))
    profiles = _apply_latency(profiles, state, assessment.tier)
    route = review_router.select_route(assessment, profiles, cfg)
    return assessment, profiles, route


def _cache_profiles(state, profiles):
    result = copy.deepcopy(state) if state else {}
    cache = result.setdefault("capabilities", {})
    for profile in profiles:
        if profile.source == "config":
            continue
        cache[f"{profile.provider}:{profile.model}"] = {
            "family": profile.family,
            "effort_levels": list(profile.effort_levels),
            "context_limit": profile.context_limit,
            "output_limit": profile.output_limit,
            "token_semantics": profile.token_semantics,
            "streaming": profile.streaming,
            "quality": profile.quality,
            "confidence": profile.confidence,
            "source": profile.source,
        }
    return result


def refresh_profiles(
    profiles,
    providers,
    state_path,
    *,
    timeout=30,
    catalog_url=DEFAULT_CATALOG_URL,
):
    """在用户收到路由通知后补全能力元数据；失败时保留已有档案。"""
    metadata = {}
    for provider in sorted({item.provider for item in profiles if item.key_ready}):
        try:
            key = call_model.get_key(provider, providers, "review")
            status, raw = call_model.api_get(
                f"{providers[provider]['base_url'].rstrip('/')}/models",
                {"Authorization": f"Bearer {key}"},
                timeout,
            )
            if status == 200:
                body = json.loads(raw)
                metadata.update(review_router.provider_metadata(provider, body, profiles))
        except (KeyError, OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            continue
    refreshed = review_router.apply_capability_metadata(profiles, metadata)

    if catalog_url:
        try:
            status, raw = call_model.api_get(catalog_url, {}, timeout)
            if status == 200:
                catalog = json.loads(raw)
                catalog_metadata = review_router.catalog_metadata_for_profiles(catalog, refreshed)
                refreshed = review_router.apply_capability_metadata(refreshed, catalog_metadata)
        except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
            pass

    state = review_router.load_runtime_state(state_path)
    review_router.save_runtime_state(state_path, _cache_profiles(state, refreshed))
    return refreshed


def _explicit_config(cfg, models):
    if not models:
        return cfg
    result = copy.deepcopy(cfg)
    existing = result.get("review_models", {})
    aliases = {}
    for index, model in enumerate(models, 1):
        alias = next(
            (name for name, value in existing.items() if value == model),
            f"manual_{index}",
        ) if isinstance(existing, dict) else f"manual_{index}"
        aliases[alias] = model
    result["review_models"] = aliases
    result.pop("review_model", None)
    return result


def _override_route(route, profiles, effort=None, timeout=None):
    reviewers = []
    for selected in route.reviewers:
        profile = next(item for item in profiles if item.alias == selected.alias)
        chosen_effort = effort or selected.effort
        if chosen_effort and chosen_effort not in profile.effort_levels:
            raise ConfigError(
                f"模型 {profile.configured_model} 不支持 reasoning_effort={chosen_effort}；"
                f"可用：{', '.join(profile.effort_levels)}"
            )
        reviewers.append(
            replace(
                selected,
                effort=chosen_effort,
                timeout_seconds=timeout or selected.timeout_seconds,
            )
        )
    return replace(route, reviewers=tuple(reviewers))


def render_route_notice(route):
    assessment = route.assessment
    lines = [
        f"Plan Review 评估：{route.tier}（难度 {assessment.difficulty} / "
        f"不确定性 {assessment.uncertainty} / 风险 {assessment.risk} / 综合 {assessment.score}）"
    ]
    for evidence in assessment.evidence:
        lines.append(f"- {evidence}")
    for item in route.reviewers:
        low, high = item.estimated_seconds
        lines.append(
            f"- {item.alias}: {item.provider}:{item.model}，推理 {item.effort or 'none'}，"
            f"预算 {item.output_budget}，预计 {int(low)}–{int(high)} 秒，"
            f"硬超时 {item.timeout_seconds} 秒，能力置信度 {item.confidence}"
        )
    lines.append(
        f"最坏情况：{route.max_calls} 次模型调用、{route.max_total_output_tokens} 输出 token、"
        f"{route.max_wall_seconds} 秒。"
    )
    if route.confirmation_reasons:
        lines.append("需要确认：" + "；".join(route.confirmation_reasons))
    return "\n".join(lines)


def run_review(args):
    cfg = load_config(args.config)
    requested = list(args.reviewer)
    if args.model:
        if requested:
            print("--model 与 --reviewer 不能同时使用", file=sys.stderr)
            return 3
        requested = [args.model]
    cfg = _explicit_config(cfg, requested)
    try:
        providers = build_providers(cfg)
        plan = call_model.read_plan_source(args.review)
        if args.prompt:
            plan = args.prompt.strip() + "\n\n" + plan
        state = review_router.load_runtime_state(args.state)
        assessment, profiles, route = assess_review(plan, cfg, providers, state)
        route = _override_route(route, profiles, args.effort, args.timeout)
    except (ConfigError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 3

    print(render_route_notice(route), file=sys.stderr)
    if route.tier == "blocked":
        payload = {
            "status": "blocked",
            "assessment": assessment.to_dict(),
            "route": route.to_dict(),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("计划存在关键待定事项，请先确认后再评审。")
        return 3
    if route.requires_confirmation and not args.confirm_slow:
        payload = {"status": "confirmation_required", "route": route.to_dict()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("该评审需要确认；确认后使用 --confirm-slow 重新运行。")
        return 4

    routing = review_router.routing_config(cfg)
    catalog_url = routing.get("catalog_url", DEFAULT_CATALOG_URL)
    profiles = refresh_profiles(
        profiles,
        providers,
        args.state,
        timeout=min(args.timeout or 30, 30),
        catalog_url=catalog_url if isinstance(catalog_url, str) else DEFAULT_CATALOG_URL,
    )
    try:
        route = review_router.select_route(assessment, profiles, cfg)
        route = _override_route(route, profiles, args.effort, args.timeout)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if route.requires_confirmation and not args.confirm_slow:
        payload = {"status": "confirmation_required", "route": route.to_dict()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("能力信息更新后该评审需要确认；请使用 --confirm-slow 重新运行。")
        return 4

    for image_path in args.image:
        if not Path(image_path).exists():
            print(f"图片不存在: {image_path}", file=sys.stderr)
            return 3

    def caller(profile, selected, prompt, system):
        key = call_model.get_key(profile.provider, providers, "review")
        messages = call_model.build_messages(system, prompt, args.image)

        def heartbeat(elapsed, has_content):
            stage = "正文生成中" if has_content else "推理中"
            print(
                f"{profile.alias} 仍在运行：{int(elapsed)} 秒（{stage}）",
                file=sys.stderr,
                flush=True,
            )

        try:
            return call_model.call_chat(
                base_url=providers[profile.provider]["base_url"],
                api_key=key,
                model=profile.model,
                messages=messages,
                timeout=selected.timeout_seconds,
                output_budget=selected.output_budget,
                token_semantics=profile.token_semantics,
                reasoning_effort=selected.effort,
                reasoning_field=profile.reasoning_field,
                temperature=0.2,
                stream=profile.streaming,
                heartbeat=heartbeat if profile.streaming else None,
                heartbeat_seconds=route.heartbeat_seconds,
            )
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            if call_model.classify_api_error(getattr(exc, "code", 0), raw) == "input_context_overflow":
                return call_model.ModelCallResult(
                    text="",
                    status="input_context_overflow",
                    finish_reason=None,
                    completion_tokens=0,
                    reasoning_tokens=0,
                    saw_reasoning=False,
                )
            raise urllib.error.HTTPError(
                exc.url, exc.code, exc.msg, exc.hdrs, call_model.io.BytesIO(raw.encode("utf-8"))
            )

    try:
        output = review_engine.execute_review(
            plan, route, profiles, caller, confirmed=args.confirm_slow
        )
    except KeyError as exc:
        print(f"缺少 API key：请设置环境变量 {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(call_model.format_http_error(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc.reason}", file=sys.stderr)
        return 1

    state = review_router.load_runtime_state(args.state)
    for record in output.get("calls", []):
        observed_semantics = record.get("observed_token_semantics")
        if observed_semantics:
            capability = state.setdefault("capabilities", {}).setdefault(
                f"{record['provider']}:{record['model']}", {}
            )
            capability.update(
                {
                    "token_semantics": observed_semantics,
                    "source": "observation",
                    "confidence": "medium",
                }
            )
        if record.get("status") != "success":
            continue
        state = review_router.record_telemetry(
            state,
            provider=record["provider"],
            model=record["model"],
            tier=route.tier,
            elapsed_seconds=record.get("elapsed_seconds", 0),
            completion_tokens=record.get("completion_tokens", 0),
            reasoning_tokens=record.get("reasoning_tokens", 0),
            status="success",
        )
    review_router.save_runtime_state(args.state, state)

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    elif output.get("status") == "success":
        print(json.dumps(output["review"], ensure_ascii=False, indent=2))
        if output.get("adjudicated_by"):
            print(f"\n仲裁者：{output['adjudicated_by']}")
    else:
        print(json.dumps(output, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if output.get("status") == "success" else 1


def run_assess(args, cfg, providers):
    try:
        requested = list(args.reviewer)
        if args.model:
            if requested:
                raise ConfigError("--model 与 --reviewer 不能同时使用")
            requested = [args.model]
        cfg = _explicit_config(cfg, requested)
        providers = build_providers(cfg)
        plan = call_model.read_plan_source(args.assess)
        state = review_router.load_runtime_state(args.state)
        assessment, profiles, route = assess_review(plan, cfg, providers, state)
        route = _override_route(route, profiles, args.effort, args.timeout)
    except (ConfigError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 3
    payload = {
        "assessment": assessment.to_dict(),
        "route": route.to_dict(),
        "capabilities": [item.to_dict() for item in profiles],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render_route_notice(route))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list-models", action="store_true", help="列出可用评审模型")
    parser.add_argument("--set-model", metavar="MODEL", help="把 review_model 写入 config.toml")
    parser.add_argument("--plan-mode", action="store_true", help="输出当前 plan_review 生效时机")
    parser.add_argument(
        "--set-plan-mode",
        choices=PLAN_MODES,
        help="写入 plan_review：off / ask / auto",
    )
    parser.add_argument("--review", metavar="PLAN", help="评审方案文件（可配合 --model 指定模型）")
    parser.add_argument("--assess", metavar="PLAN", help="纯本地评估并输出建议路由，不调用外部 API")
    parser.add_argument("--model", help="本次评审使用的模型；缺省读 config.toml 的 review_model")
    parser.add_argument("--reviewer", action="append", default=[], help="显式评审模型，可重复")
    parser.add_argument(
        "--effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
        help="覆盖自动推理档位；必须受所选模型支持",
    )
    parser.add_argument("--timeout", type=int, help="覆盖每个模型的硬超时秒数")
    parser.add_argument("--confirm-slow", action="store_true", help="确认慢任务、多模型或扩容调用")
    parser.add_argument("--image", action="append", default=[], help="原图路径，可多次")
    parser.add_argument("--prompt", help="追加的评审提示词")
    parser.add_argument("--json", action="store_true", help="列表/评审输出为 JSON")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.toml 路径")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="本地能力与延迟缓存路径")
    args = parser.parse_args(argv)
    if args.timeout is not None and args.timeout <= 0:
        print("--timeout 必须是正整数", file=sys.stderr)
        return 3

    actions = [
        bool(args.list_models),
        bool(args.set_model),
        bool(args.plan_mode),
        bool(args.set_plan_mode),
        bool(args.review),
        bool(args.assess),
    ]
    if sum(actions) != 1:
        parser.error(
            "请从 --list-models / --set-model / --plan-mode / "
            "--set-plan-mode / --assess / --review 中选择一个操作"
        )

    cfg = load_config(args.config)
    try:
        providers = build_providers(cfg)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.plan_mode:
        try:
            print(resolve_plan_mode(cfg))
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        return 0

    if args.set_plan_mode:
        try:
            mode = set_plan_mode(args.config, args.set_plan_mode)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(f"已更新 config.toml：plan_review = {mode}")
        return 0

    if args.list_models:
        try:
            items = list_models(cfg, providers)
            plan_mode = resolve_plan_mode(cfg)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            print(render_list(items, cfg.get("review_model", ""), plan_mode))
        return 0

    if args.assess:
        return run_assess(args, cfg, providers)

    if args.set_model:
        try:
            model = set_review_model(args.config, args.set_model)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        print(f"已更新 config.toml：review_model = {model}")
        return 0

    return run_review(args)


if __name__ == "__main__":
    raise SystemExit(main())
