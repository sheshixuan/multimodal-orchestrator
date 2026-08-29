#!/usr/bin/env python3
"""Plan Review 的本地评估、能力档案与通用模型路由。"""

from __future__ import annotations

import math
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable

from call_model import ConfigError, resolve_provider


DEFAULT_ROUTING = {
    "mode": "auto",
    "slow_confirm_seconds": 180,
    "max_calls": 3,
    "max_total_output_tokens": 131072,
    "max_wall_seconds": 1800,
    "heartbeat_seconds": 30,
    "long_plan_strategy": "adaptive_then_chunk",
    "conflict_judge": True,
}

TIER_BUDGETS = {"routine": 8192, "complex": 24576, "critical": 32768}
TIER_LATENCY = {
    "routine": (30.0, 120.0),
    "complex": (120.0, 360.0),
    "critical": (240.0, 900.0),
}
EFFORT_ORDER = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
VALID_TOKEN_SEMANTICS = {"answer_only", "combined", "separate", "unknown"}


@dataclass(frozen=True)
class Assessment:
    difficulty: int
    uncertainty: int
    risk: int
    score: float
    tier: str
    evidence: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    input_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapabilityProfile:
    alias: str
    configured_model: str
    provider: str
    model: str
    family: str
    modalities: tuple[str, ...]
    effort_levels: tuple[str, ...]
    reasoning_field: str
    context_limit: int
    output_limit: int
    token_semantics: str
    streaming: bool
    quality: float
    confidence: str
    source: str
    key_ready: bool
    p50_seconds: float = 0.0
    p90_seconds: float = 0.0
    success_rate: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["modalities"] = list(self.modalities)
        data["effort_levels"] = list(self.effort_levels)
        return data


@dataclass(frozen=True)
class ReviewerChoice:
    alias: str
    provider: str
    model: str
    configured_model: str
    family: str
    effort: str | None
    output_budget: int
    token_semantics: str
    confidence: str
    estimated_seconds: tuple[float, float]
    timeout_seconds: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["estimated_seconds"] = list(self.estimated_seconds)
        return data


@dataclass(frozen=True)
class ReviewRoute:
    tier: str
    reviewers: tuple[ReviewerChoice, ...]
    requires_confirmation: bool
    confirmation_reasons: tuple[str, ...]
    max_calls: int
    max_total_output_tokens: int
    max_wall_seconds: int
    heartbeat_seconds: int
    long_plan_strategy: str
    conflict_judge: bool
    assessment: Assessment
    needs_chunking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "reviewers": [item.to_dict() for item in self.reviewers],
            "requires_confirmation": self.requires_confirmation,
            "confirmation_reasons": list(self.confirmation_reasons),
            "max_calls": self.max_calls,
            "max_total_output_tokens": self.max_total_output_tokens,
            "max_wall_seconds": self.max_wall_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "long_plan_strategy": self.long_plan_strategy,
            "conflict_judge": self.conflict_judge,
            "needs_chunking": self.needs_chunking,
            "assessment": self.assessment.to_dict(),
        }


def estimate_tokens(text: str, protocol_overhead: int = 768) -> int:
    ascii_count = sum(ord(char) < 128 for char in text)
    non_ascii_count = len(text) - ascii_count
    raw = ascii_count / 4.0 + non_ascii_count * 1.2 + protocol_overhead
    return int(math.ceil(raw * 1.2))


def _matched(text: str, terms: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term.lower() in lowered]


def assess_plan(plan: str) -> Assessment:
    text = plan or ""
    blocking_patterns = (
        r"\bTBD\b",
        r"待用户(?:选择|确认|决定)",
        r"需要用户(?:选择|确认|决定)",
        r"(?:关键|必须).{0,16}(?:尚未决定|待定)",
    )
    blocking = [pattern for pattern in blocking_patterns if re.search(pattern, text, re.IGNORECASE)]

    difficulty_terms = (
        "跨模块", "跨服务", "多个服务", "公开 api", "接口变更", "迁移", "并发",
        "兼容", "回滚", "重构", "状态机", "数据库", "部署", "多 provider",
    )
    uncertainty_terms = (
        "未知", "尚未验证", "待验证", "不确定", "外部依赖", "假设", "可能",
        "分支方案", "取决于", "缺少信息",
    )
    risk_terms = (
        "生产", "数据丢失", "权限", "安全", "隐私", "密钥", "不可逆", "灾难",
        "回滚", "兼容", "资金", "合规", "破坏性",
    )
    difficulty_hits = _matched(text, difficulty_terms)
    uncertainty_hits = _matched(text, uncertainty_terms)
    risk_hits = _matched(text, risk_terms)
    heading_count = len(re.findall(r"(?m)^#{1,6}\s+", text))
    length_points = min(20, len(text) // 1500 * 5)

    difficulty = min(100, 10 + len(difficulty_hits) * 12 + min(12, heading_count * 2) + length_points)
    uncertainty = min(100, 5 + len(uncertainty_hits) * 20 + min(10, length_points // 2))
    risk = min(100, 5 + len(risk_hits) * 15)
    score = round(0.45 * difficulty + 0.35 * uncertainty + 0.20 * risk, 1)

    evidence = []
    if difficulty_hits:
        evidence.append("复杂度信号：" + "、".join(difficulty_hits[:6]))
    if uncertainty_hits:
        evidence.append("不确定性信号：" + "、".join(uncertainty_hits[:6]))
    if risk_hits:
        evidence.append("风险信号：" + "、".join(risk_hits[:6]))
    if length_points:
        evidence.append(f"长方案输入：约 {estimate_tokens(text)} tokens")

    if blocking:
        tier = "blocked"
        reasons = ["存在会改变方案方向的待定事项，需先由用户确认"]
    elif difficulty >= 70 or uncertainty >= 70 or risk >= 70 or score >= 70:
        tier = "critical"
        reasons = []
    elif score >= 40:
        tier = "complex"
        reasons = []
    else:
        tier = "routine"
        reasons = []
    return Assessment(
        difficulty=difficulty,
        uncertainty=uncertainty,
        risk=risk,
        score=score,
        tier=tier,
        evidence=evidence,
        blocking_reasons=reasons,
        input_tokens=estimate_tokens(text),
    )


def routing_config(cfg: dict[str, Any]) -> dict[str, Any]:
    raw = cfg.get("review_routing", {})
    if not isinstance(raw, dict):
        raise ConfigError("review_routing 必须使用 [review_routing] 配置段")
    result = dict(DEFAULT_ROUTING)
    result.update(raw)
    integer_keys = (
        "slow_confirm_seconds", "max_calls", "max_total_output_tokens",
        "max_wall_seconds", "heartbeat_seconds",
    )
    for key in integer_keys:
        value = result[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"review_routing.{key} 必须是正整数")
    if result["max_calls"] > 3:
        raise ConfigError("review_routing.max_calls 不能超过硬上限 3")
    if result["mode"] not in ("auto", "manual"):
        raise ConfigError("review_routing.mode 仅支持 auto/manual")
    if result["long_plan_strategy"] != "adaptive_then_chunk":
        raise ConfigError("review_routing.long_plan_strategy 仅支持 adaptive_then_chunk")
    if not isinstance(result["conflict_judge"], bool):
        raise ConfigError("review_routing.conflict_judge 必须是布尔值")
    return result


def _as_string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [item.strip() for item in value.split(",") if item.strip()]
    elif isinstance(value, list):
        values = value
    else:
        raise ConfigError(f"{field_name} 必须是字符串列表")
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ConfigError(f"{field_name} 必须包含非空字符串")
    return tuple(item.strip() for item in values)


def _infer_family(model: str) -> str:
    parts = [part for part in re.split(r"[-_.]+", model.lower()) if part]
    tier_words = {"pro", "max", "mini", "lite", "flash", "turbo", "preview", "latest"}
    stable = [part for part in parts if part not in tier_words]
    return "-".join((stable or parts)[:2]) or "unknown"


def _infer_efforts(model: str) -> tuple[str, ...]:
    tokens = set(re.split(r"[-_.]+", model.lower()))
    reasoning_hints = {"reasoner", "reasoning", "think", "thinking", "pro", "max", "r1"}
    if tokens & reasoning_hints:
        # 名称只能提示“可能支持推理”，不能证明厂商是否把最高档命名为 max/xhigh。
        return ("high",)
    return ("none",)


def _infer_quality(model: str, efforts: tuple[str, ...]) -> float:
    tokens = set(re.split(r"[-_.]+", model.lower()))
    score = 50.0
    if any(item != "none" for item in efforts):
        score += 20
    if any(item in {"xhigh", "max"} for item in efforts):
        score += 15
    if tokens & {"pro", "max", "opus", "ultra"}:
        score += 10
    if tokens & {"flash", "mini", "lite", "nano"}:
        score -= 15
    return max(0.0, min(100.0, score))


def _positive_int(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{field_name} 必须是正整数")
    return value


def _number(value: Any, default: float, field_name: str) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{field_name} 必须是数字")
    return float(value)


def build_capability_profiles(cfg: dict[str, Any], providers: dict[str, dict[str, Any]]) -> list[CapabilityProfile]:
    configured = cfg.get("review_models", {})
    if configured and not isinstance(configured, dict):
        raise ConfigError("review_models 必须使用 [review_models] 配置段")
    candidates: dict[str, str] = dict(configured or {})
    current = cfg.get("review_model")
    if current and current not in candidates.values():
        candidates = {"current": current, **candidates}
    if not candidates:
        raise ConfigError("缺少 review_model 或 [review_models] 候选模型")

    overrides = cfg.get("review_capabilities", {})
    if overrides and not isinstance(overrides, dict):
        raise ConfigError("review_capabilities 必须使用 [review_capabilities.<alias>] 配置段")

    profiles = []
    for alias, configured_model in candidates.items():
        if not isinstance(configured_model, str):
            raise ConfigError(f"review_models.{alias} 必须是模型名字符串")
        provider, model = resolve_provider(configured_model, providers)
        raw = overrides.get(alias, {}) if isinstance(overrides, dict) else {}
        if not isinstance(raw, dict):
            raise ConfigError(f"review_capabilities.{alias} 必须是配置段")

        inferred_efforts = _infer_efforts(model)
        efforts = _as_string_list(
            raw.get("reasoning_levels", list(inferred_efforts)),
            f"review_capabilities.{alias}.reasoning_levels",
        )
        invalid_efforts = [item for item in efforts if item not in EFFORT_ORDER]
        if invalid_efforts:
            raise ConfigError(f"review_capabilities.{alias}.reasoning_levels 含非法值：{invalid_efforts[0]}")
        efforts = tuple(sorted(set(efforts), key=lambda item: EFFORT_ORDER[item]))
        semantics = raw.get("token_semantics") or (
            "combined" if any(item != "none" for item in efforts) else "unknown"
        )
        if semantics not in VALID_TOKEN_SEMANTICS:
            raise ConfigError(f"review_capabilities.{alias}.token_semantics 非法：{semantics}")
        modalities = _as_string_list(
            raw.get("modalities", ["text"]), f"review_capabilities.{alias}.modalities"
        )
        quality = _number(
            raw.get("quality"), _infer_quality(model, efforts), f"review_capabilities.{alias}.quality"
        )
        if not 0 <= quality <= 100:
            raise ConfigError(f"review_capabilities.{alias}.quality 必须在 0..100")

        explicit_fields = bool(raw)
        reasoning_field = str(raw.get("reasoning_field") or "reasoning_effort")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", reasoning_field):
            raise ConfigError(f"review_capabilities.{alias}.reasoning_field 非法")
        env_names = ("REVIEW_API_KEY", providers[provider]["env"])
        key_ready = any(os.environ.get(name) for name in env_names)
        profiles.append(
            CapabilityProfile(
                alias=alias,
                configured_model=configured_model,
                provider=provider,
                model=model,
                family=str(raw.get("family") or _infer_family(model)),
                modalities=modalities,
                effort_levels=efforts,
                reasoning_field=reasoning_field,
                context_limit=_positive_int(
                    raw.get("context_limit"), 128000, f"review_capabilities.{alias}.context_limit"
                ),
                output_limit=_positive_int(
                    raw.get("output_limit"), 32768, f"review_capabilities.{alias}.output_limit"
                ),
                token_semantics=semantics,
                streaming=bool(raw.get("streaming", True)),
                quality=quality,
                confidence=str(raw.get("confidence") or ("high" if explicit_fields else "low")),
                source=str(raw.get("source") or ("config" if explicit_fields else "name_heuristic")),
                key_ready=key_ready,
                p50_seconds=_number(raw.get("p50_seconds"), 0.0, f"review_capabilities.{alias}.p50_seconds"),
                p90_seconds=_number(raw.get("p90_seconds"), 0.0, f"review_capabilities.{alias}.p90_seconds"),
                success_rate=_number(raw.get("success_rate"), 1.0, f"review_capabilities.{alias}.success_rate"),
            )
        )
    return profiles


def _effort_for_tier(profile: CapabilityProfile, tier: str) -> str | None:
    levels = profile.effort_levels
    if not levels or levels == ("none",):
        return None
    if tier == "routine":
        return levels[0]
    if tier == "critical":
        return levels[-1]
    return levels[min(len(levels) - 1, max(0, len(levels) // 2))]


def _latency_for(profile: CapabilityProfile, tier: str) -> tuple[float, float]:
    default_p50, default_p90 = TIER_LATENCY[tier]
    return (profile.p50_seconds or default_p50, profile.p90_seconds or default_p90)


def _quality_rank(profile: CapabilityProfile, tier: str) -> tuple[float, int, float, int, float]:
    max_effort = max((EFFORT_ORDER.get(item, 0) for item in profile.effort_levels), default=0)
    return (
        profile.quality,
        max_effort,
        profile.success_rate,
        CONFIDENCE_ORDER.get(profile.confidence, 0),
        -_latency_for(profile, tier)[1],
    )


def _choice(profile: CapabilityProfile, tier: str, routing: dict[str, Any]) -> ReviewerChoice:
    estimate = _latency_for(profile, tier)
    tier_budget = TIER_BUDGETS[tier]
    budget = min(profile.output_limit, tier_budget)
    floor = {"routine": 120, "complex": 600, "critical": 900}[tier]
    timeout = min(routing["max_wall_seconds"], max(floor, int(math.ceil(estimate[1] * 1.5))))
    return ReviewerChoice(
        alias=profile.alias,
        provider=profile.provider,
        model=profile.model,
        configured_model=profile.configured_model,
        family=profile.family,
        effort=_effort_for_tier(profile, tier),
        output_budget=budget,
        token_semantics=profile.token_semantics,
        confidence=profile.confidence,
        estimated_seconds=estimate,
        timeout_seconds=timeout,
    )


def select_route(assessment: Assessment, profiles: list[CapabilityProfile], cfg: dict[str, Any]) -> ReviewRoute:
    routing = routing_config(cfg)
    if assessment.tier == "blocked":
        selected: list[CapabilityProfile] = []
        needs_chunking = False
    else:
        key_ready = [
            profile for profile in profiles
            if profile.key_ready
            and "text" in profile.modalities
        ]
        if not key_ready:
            raise ConfigError("没有 API key 就绪的文本评审模型")
        eligible = [
            profile for profile in key_ready
            if assessment.input_tokens + min(profile.output_limit, TIER_BUDGETS[assessment.tier])
            <= int(profile.context_limit * 0.85)
        ]
        needs_chunking = not eligible
        if needs_chunking:
            eligible = key_ready
        if assessment.tier == "routine":
            selected = [min(eligible, key=lambda item: ((_latency_for(item, "routine")[1]), -item.quality))]
        else:
            ranked = sorted(
                eligible,
                key=lambda item: tuple(-value for value in _quality_rank(item, assessment.tier)) + (item.alias,),
            )
            selected = [ranked[0]]
            if assessment.tier == "critical" and len(ranked) > 1 and not needs_chunking:
                first = ranked[0]
                diverse_family = [item for item in ranked[1:] if item.family != first.family]
                candidates = diverse_family or ranked[1:]
                diverse_provider = [item for item in candidates if item.provider != first.provider]
                candidates = diverse_provider or candidates
                selected.append(
                    max(
                        candidates,
                        key=lambda item: (
                            *_quality_rank(item, "critical")[:-1],
                            int(item.provider != first.provider),
                            _quality_rank(item, "critical")[-1],
                        ),
                    )
                )

    choices = tuple(_choice(item, assessment.tier, routing) for item in selected)
    reasons = []
    if len(choices) > 1:
        reasons.append("需要并行调用多个外部评审模型")
    if needs_chunking:
        reasons.append("输入超过上下文安全线，需要分片评审")
    if choices and max(item.estimated_seconds[1] for item in choices) > routing["slow_confirm_seconds"]:
        reasons.append(f"预计耗时超过 {routing['slow_confirm_seconds']} 秒")
    return ReviewRoute(
        tier=assessment.tier,
        reviewers=choices,
        requires_confirmation=bool(reasons),
        confirmation_reasons=tuple(reasons),
        max_calls=routing["max_calls"],
        max_total_output_tokens=routing["max_total_output_tokens"],
        max_wall_seconds=routing["max_wall_seconds"],
        heartbeat_seconds=routing["heartbeat_seconds"],
        long_plan_strategy=routing["long_plan_strategy"],
        conflict_judge=routing["conflict_judge"],
        assessment=assessment,
        needs_chunking=needs_chunking,
    )


def select_judge(
    profiles: list[CapabilityProfile], used_aliases: set[str], assessment: Assessment
) -> CapabilityProfile | None:
    eligible = [
        item for item in profiles
        if item.alias not in used_aliases and item.key_ready and "text" in item.modalities
    ]
    if not eligible:
        return None
    used_providers = {item.provider for item in profiles if item.alias in used_aliases}
    return max(
        eligible,
        key=lambda item: (
            item.quality,
            CONFIDENCE_ORDER.get(item.confidence, 0),
            int(item.provider not in used_providers),
            -(item.p90_seconds or TIER_LATENCY.get(assessment.tier, TIER_LATENCY["critical"])[1]),
        ),
    )


def catalog_metadata_for_profiles(
    catalog: dict[str, Any], profiles: list[CapabilityProfile]
) -> dict[tuple[str, str], dict[str, Any]]:
    """把第三方目录的模型条目归一化；未知 schema 只会被忽略。"""
    if not isinstance(catalog, dict):
        return {}
    by_model: dict[str, list[dict[str, Any]]] = {}
    for provider_data in catalog.values():
        if not isinstance(provider_data, dict):
            continue
        models = provider_data.get("models", {})
        if isinstance(models, list):
            iterable = ((item.get("id"), item) for item in models if isinstance(item, dict))
        elif isinstance(models, dict):
            iterable = models.items()
        else:
            continue
        for key, item in iterable:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or key
            if isinstance(model_id, str) and model_id:
                by_model.setdefault(model_id, []).append(item)

    normalized: dict[tuple[str, str], dict[str, Any]] = {}
    for profile in profiles:
        matches = by_model.get(profile.model, [])
        if len(matches) != 1:
            continue
        item = matches[0]
        limits = item.get("limit", {}) if isinstance(item.get("limit", {}), dict) else {}
        efforts = None
        options = item.get("reasoning_options", {})
        if isinstance(options, dict):
            efforts = options.get("effort")
        metadata: dict[str, Any] = {
            "source": "catalog",
            "confidence": "medium",
        }
        if isinstance(item.get("family"), str) and item["family"]:
            metadata["family"] = item["family"]
        if isinstance(efforts, list) and efforts:
            valid = [value for value in efforts if value in EFFORT_ORDER]
            if valid:
                metadata["effort_levels"] = tuple(sorted(set(valid), key=lambda value: EFFORT_ORDER[value]))
        if isinstance(limits.get("context"), int) and limits["context"] > 0:
            metadata["context_limit"] = limits["context"]
        if isinstance(limits.get("output"), int) and limits["output"] > 0:
            metadata["output_limit"] = limits["output"]
        if item.get("reasoning") is True:
            metadata["token_semantics"] = "combined"
        normalized[(profile.provider, profile.model)] = metadata
    return normalized


def provider_metadata(
    provider: str, body: dict[str, Any], profiles: list[CapabilityProfile]
) -> dict[tuple[str, str], dict[str, Any]]:
    """归一化 OpenAI-compatible /models 扩展字段。"""
    data = body.get("data", []) if isinstance(body, dict) else []
    if not isinstance(data, list):
        return {}
    wanted = {item.model for item in profiles if item.provider == provider}
    result = {}
    for item in data:
        if not isinstance(item, dict) or item.get("id") not in wanted:
            continue
        model = item["id"]
        metadata: dict[str, Any] = {"source": "provider", "confidence": "high"}
        for target, candidates in (
            ("context_limit", ("context_limit", "context_window", "max_context_tokens")),
            ("output_limit", ("output_limit", "max_output_tokens", "max_completion_tokens")),
        ):
            value = next((item.get(name) for name in candidates if isinstance(item.get(name), int)), None)
            if value and value > 0:
                metadata[target] = value
        if isinstance(item.get("family"), str):
            metadata["family"] = item["family"]
        efforts = item.get("reasoning_levels") or item.get("reasoning_efforts")
        if isinstance(efforts, list):
            valid = [value for value in efforts if value in EFFORT_ORDER]
            if valid:
                metadata["effort_levels"] = tuple(sorted(set(valid), key=lambda value: EFFORT_ORDER[value]))
        semantics = item.get("token_semantics")
        if semantics in VALID_TOKEN_SEMANTICS:
            metadata["token_semantics"] = semantics
        if any(key not in {"source", "confidence"} for key in metadata):
            result[(provider, model)] = metadata
    return result


def apply_capability_metadata(
    profiles: list[CapabilityProfile],
    metadata: dict[tuple[str, str], dict[str, Any]],
) -> list[CapabilityProfile]:
    result = []
    allowed = {
        "family", "effort_levels", "context_limit", "output_limit", "token_semantics",
        "streaming", "quality", "confidence", "source", "p50_seconds", "p90_seconds",
        "success_rate",
    }
    source_rank = {
        "name_heuristic": 0,
        "cache": 1,
        "catalog": 2,
        "observation": 3,
        "provider": 3,
        "config": 4,
    }
    for profile in profiles:
        update = metadata.get((profile.provider, profile.model))
        incoming_source = update.get("source", "cache") if update else "cache"
        if (
            not update
            or profile.source == "config"
            or source_rank.get(incoming_source, 0) < source_rank.get(profile.source, 0)
        ):
            result.append(profile)
            continue
        clean = {key: value for key, value in update.items() if key in allowed}
        result.append(replace(profile, **clean))
    return result


def load_runtime_state(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_runtime_state(path: Path | str, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def record_telemetry(
    state: dict[str, Any],
    *,
    provider: str,
    model: str,
    tier: str,
    elapsed_seconds: float,
    completion_tokens: int,
    reasoning_tokens: int,
    status: str,
    now: float | None = None,
) -> dict[str, Any]:
    current = float(time.time() if now is None else now)
    result = json.loads(json.dumps(state)) if state else {}
    telemetry = result.setdefault("telemetry", {})
    key = f"{provider}:{model}:{tier}"
    samples = telemetry.setdefault(key, [])
    cutoff = current - 30 * 86400
    samples[:] = [
        sample for sample in samples
        if isinstance(sample, dict) and float(sample.get("timestamp", 0)) >= cutoff
    ]
    if status == "success":
        samples.append(
            {
                "timestamp": current,
                "elapsed_seconds": round(float(elapsed_seconds), 3),
                "completion_tokens": max(0, int(completion_tokens)),
                "reasoning_tokens": max(0, int(reasoning_tokens)),
                "status": "success",
            }
        )
    samples.sort(key=lambda sample: sample["timestamp"], reverse=True)
    del samples[20:]
    return result


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def telemetry_latency(
    state: dict[str, Any], provider: str, model: str, tier: str, *, now: float | None = None
) -> tuple[float, float]:
    current = float(time.time() if now is None else now)
    cutoff = current - 30 * 86400
    key = f"{provider}:{model}:{tier}"
    samples = state.get("telemetry", {}).get(key, []) if isinstance(state, dict) else []
    values = [
        float(sample["elapsed_seconds"])
        for sample in samples
        if isinstance(sample, dict)
        and sample.get("status") == "success"
        and float(sample.get("timestamp", 0)) >= cutoff
    ][:20]
    if len(values) < 3:
        return (0.0, 0.0)
    return (_percentile(values, 0.5), _percentile(values, 0.9))
