#!/usr/bin/env python3
"""Plan Review 的结构化执行、合并、扩容与终局仲裁。"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any, Callable

import call_model
import review_router


DIMENSIONS = (
    "correctness", "completeness", "executability", "scope", "risk", "testability"
)
DIMENSION_LEVEL = {"pass": 0, "warn": 1, "block": 2}
VERDICT_LEVEL = {"pass": 0, "revise": 1, "block": 2}
SEVERITY_LEVEL = {"low": 0, "medium": 1, "high": 2, "critical": 3}
HARD_MAX_CALLS = 3

STRUCTURED_REVIEW_SYSTEM = """你是方案评审器。只输出一个 JSON 对象，不要 Markdown 围栏，也不要展示推理过程。
JSON 必须包含：
{
  "verdict": "pass|revise|block",
  "dimensions": {
    "correctness": {"status": "pass|warn|block", "summary": "..."},
    "completeness": {"status": "pass|warn|block", "summary": "..."},
    "executability": {"status": "pass|warn|block", "summary": "..."},
    "scope": {"status": "pass|warn|block", "summary": "..."},
    "risk": {"status": "pass|warn|block", "summary": "..."},
    "testability": {"status": "pass|warn|block", "summary": "..."}
  },
  "issues": [{"severity":"low|medium|high|critical","evidence":"...","recommendation":"..."}],
  "unresolved_questions": ["..."],
  "confidence": 0.0
}
若多条建议是在同一决策上互斥，可在 issue 增加 decision_key 和 recommended_option。
先核验事实与约束，再给结论；不要因为缺少信息而臆测。"""


def parse_review(text: str) -> dict[str, Any]:
    candidate = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(f"评审没有返回有效 JSON：{exc}") from exc
    validate_review(value)
    return value


def validate_review(review: Any) -> None:
    if not isinstance(review, dict):
        raise ValueError("评审必须是 JSON 对象")
    if review.get("verdict") not in VERDICT_LEVEL:
        raise ValueError("verdict 必须是 pass/revise/block")
    dimensions = review.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("dimensions 必须是对象")
    for name in DIMENSIONS:
        item = dimensions.get(name)
        if not isinstance(item, dict) or item.get("status") not in DIMENSION_LEVEL:
            raise ValueError(f"dimensions.{name}.status 非法或缺失")
        if not isinstance(item.get("summary", ""), str):
            raise ValueError(f"dimensions.{name}.summary 必须是字符串")
    issues = review.get("issues")
    if not isinstance(issues, list):
        raise ValueError("issues 必须是列表")
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") not in SEVERITY_LEVEL:
            raise ValueError("issue.severity 非法或缺失")
        if not isinstance(issue.get("evidence"), str) or not isinstance(issue.get("recommendation"), str):
            raise ValueError("issue 必须包含 evidence 和 recommendation")
    questions = review.get("unresolved_questions")
    if not isinstance(questions, list) or any(not isinstance(item, str) for item in questions):
        raise ValueError("unresolved_questions 必须是字符串列表")
    confidence = review.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        raise ValueError("confidence 必须在 0..1")


def detect_conflict(reviews: list[dict[str, Any]]) -> bool:
    if len(reviews) < 2:
        return False
    verdicts = [VERDICT_LEVEL[item["verdict"]] for item in reviews]
    if max(verdicts) - min(verdicts) >= 2:
        return True
    for name in DIMENSIONS:
        statuses = [DIMENSION_LEVEL[item["dimensions"][name]["status"]] for item in reviews]
        if max(statuses) - min(statuses) >= 2:
            return True
    decisions: dict[str, set[str]] = {}
    for review in reviews:
        for issue in review.get("issues", []):
            key = issue.get("decision_key")
            option = issue.get("recommended_option")
            if isinstance(key, str) and key and isinstance(option, str) and option:
                decisions.setdefault(key, set()).add(option)
    return any(len(options) > 1 for options in decisions.values())


def aggregate_reviews(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    if not reviews:
        raise ValueError("没有可合并的有效评审")
    verdict = max((item["verdict"] for item in reviews), key=lambda value: VERDICT_LEVEL[value])
    dimensions = {}
    for name in DIMENSIONS:
        worst = max(
            (item["dimensions"][name] for item in reviews),
            key=lambda value: DIMENSION_LEVEL[value["status"]],
        )
        summaries = []
        for review in reviews:
            summary = review["dimensions"][name].get("summary", "").strip()
            if summary and summary not in summaries:
                summaries.append(summary)
        dimensions[name] = {"status": worst["status"], "summary": "；".join(summaries)}
    issues = []
    seen_issues = set()
    for review in reviews:
        for issue in review.get("issues", []):
            identity = (issue.get("severity"), issue.get("evidence"), issue.get("recommendation"))
            if identity not in seen_issues:
                seen_issues.add(identity)
                issues.append(issue)
    issues.sort(key=lambda item: -SEVERITY_LEVEL[item["severity"]])
    questions = []
    for review in reviews:
        for question in review.get("unresolved_questions", []):
            if question not in questions:
                questions.append(question)
    return {
        "verdict": verdict,
        "dimensions": dimensions,
        "issues": issues,
        "unresolved_questions": questions,
        "confidence": round(min(item["confidence"] for item in reviews), 3),
    }


def _sections(plan: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", plan))
    if not matches:
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", plan) if item.strip()]
        return [(f"片段 {index}", paragraph) for index, paragraph in enumerate(paragraphs, 1)]
    sections = []
    prefix = plan[:matches[0].start()].strip()
    if prefix:
        sections.append(("前言", prefix))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(plan)
        sections.append((match.group(2).strip(), plan[match.start():end].strip()))
    return sections


def extract_global_constraints(plan: str) -> str:
    keywords = (
        "目标", "非目标", "范围", "约束", "关键接口", "接口", "验收标准", "验收",
        "已确定", "决定", "non-goal", "scope", "constraint", "acceptance", "interface",
    )
    selected = [body for title, body in _sections(plan) if any(key.lower() in title.lower() for key in keywords)]
    if not selected:
        selected = [plan[: min(len(plan), 2000)].strip()]
    return "## 不可变全局约束\n" + "\n\n".join(item for item in selected if item)


def _split_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("chunk_limit_too_small")
    remaining = text.strip()
    parts = []
    while len(remaining) > limit:
        window = remaining[:limit]
        minimum = max(1, limit // 2)
        positions = []
        for marker in ("\n\n", "\n", "。", "！", "？", ". "):
            position = window.rfind(marker, minimum)
            if position >= minimum:
                positions.append(position + len(marker))
        cut = max(positions, default=0)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def chunk_plan(plan: str, max_chars: int = 12000) -> list[str]:
    constraints = extract_global_constraints(plan)
    label_reserve = 64
    target = max_chars - len(constraints) - label_reserve
    if target <= 0:
        raise ValueError("global_constraints_exceed_chunk_limit")
    constraint_bodies = set()
    for _, body in _sections(constraints):
        constraint_bodies.add(body.strip())
    variable = []
    for title, body in _sections(plan):
        if body.strip() not in constraint_bodies and body.strip() not in constraints:
            variable.append(body)
    if not variable:
        variable = [plan]
    pieces = []
    for body in variable:
        pieces.extend(_split_text(body, target))
    groups = []
    current = []
    current_size = 0
    for body in pieces:
        if current and current_size + len(body) + 2 > target:
            groups.append("\n\n".join(current))
            current, current_size = [], 0
        current.append(body)
        current_size += len(body) + 2
    if current:
        groups.append("\n\n".join(current))
    chunks = [
        f"{constraints}\n\n## 当前评审分片 {index}/{len(groups)}\n{body}"
        for index, body in enumerate(groups, 1)
    ]
    if any(len(chunk) > max_chars for chunk in chunks):
        raise ValueError("chunk_exceeds_hard_character_limit")
    return chunks


def _chunks_for_profile(plan, item, selected):
    safe_input_tokens = int(item.context_limit * 0.85) - selected.output_budget
    if safe_input_tokens <= 0:
        raise ValueError("output_reservation_exceeds_context_safe_line")
    max_chars = int(max(0, safe_input_tokens / 1.2 - 768) / 1.2)
    if max_chars <= 0:
        raise ValueError("global_protocol_overhead_exceeds_context_safe_line")
    chunks = chunk_plan(plan, max_chars=max_chars)
    if any(review_router.estimate_tokens(chunk) > safe_input_tokens for chunk in chunks):
        raise ValueError("chunk_token_estimate_exceeds_context_safe_line")
    return chunks


def _review_prompt(plan: str) -> str:
    return "请按固定 JSON schema 评审以下实施计划。证据必须引用计划中的具体约束：\n\n" + plan


def _judge_prompt(plan: str, reviews: list[dict[str, Any]]) -> str:
    return (
        "你是终局仲裁者。以下两个评审存在实质冲突。根据原计划和证据给出一次终局评审；"
        "只输出同一 JSON schema，不要再次请求其他模型。\n\n原计划：\n"
        + plan
        + "\n\n待仲裁评审：\n"
        + json.dumps(reviews, ensure_ascii=False, indent=2)
    )


def execute_review(
    plan: str,
    route: review_router.ReviewRoute,
    profiles: list[review_router.CapabilityProfile],
    caller: Callable[[review_router.CapabilityProfile, review_router.ReviewerChoice, str, str], call_model.ModelCallResult],
    *,
    confirmed: bool,
) -> dict[str, Any]:
    if route.tier == "blocked":
        return {"status": "blocked", "assessment": route.assessment.to_dict(), "call_count": 0}
    if route.requires_confirmation and not confirmed:
        return {"status": "confirmation_required", "route": route.to_dict(), "call_count": 0}

    profiles_by_alias = {item.alias: item for item in profiles}
    lock = threading.Lock()
    call_count = 0
    requested_tokens = 0
    actual_tokens = 0
    call_records = []
    started_at = time.monotonic()
    effective_max_calls = min(max(1, route.max_calls), HARD_MAX_CALLS)

    def invoke(item, selected, prompt):
        nonlocal call_count, requested_tokens, actual_tokens
        with lock:
            if call_count >= effective_max_calls:
                return None
            remaining_wall = route.max_wall_seconds - (time.monotonic() - started_at)
            if remaining_wall <= 0:
                return None
            remaining = route.max_total_output_tokens - requested_tokens
            if remaining <= 0:
                return None
            budget = min(selected.output_budget, remaining, item.output_limit)
            if budget <= 0:
                return None
            selected = replace(
                selected,
                output_budget=budget,
                timeout_seconds=min(selected.timeout_seconds, max(0.001, remaining_wall)),
            )
            call_count += 1
            requested_tokens += budget
        response = caller(item, selected, prompt, STRUCTURED_REVIEW_SYSTEM)
        if time.monotonic() - started_at > route.max_wall_seconds:
            response = replace(response, text="", status="wall_time_exceeded")
        with lock:
            actual_tokens += max(0, response.completion_tokens)
            observed_semantics = (
                "combined"
                if response.status == "reasoning_budget_exhausted"
                and response.reasoning_tokens >= int(budget * 0.9)
                else None
            )
            call_records.append(
                {
                    "alias": item.alias,
                    "provider": item.provider,
                    "model": item.model,
                    "status": response.status,
                    "output_budget": budget,
                    "completion_tokens": response.completion_tokens,
                    "reasoning_tokens": response.reasoning_tokens,
                    "elapsed_seconds": response.elapsed_seconds,
                    "first_content_seconds": response.first_content_seconds,
                    "observed_token_semantics": observed_semantics,
                }
            )
        return response

    if route.needs_chunking:
        if not route.reviewers:
            return {"status": "failed", "reason": "no_reviewer_for_chunking", "call_count": 0}
        selected = route.reviewers[0]
        item = profiles_by_alias[selected.alias]
        try:
            chunks = _chunks_for_profile(plan, item, selected)
        except ValueError as exc:
            return {
                "status": "resource_limit",
                "reason": str(exc),
                "call_count": 0,
            }
        if len(chunks) > effective_max_calls:
            return {
                "status": "resource_limit",
                "reason": "chunk_count_exceeds_max_calls",
                "chunk_count": len(chunks),
                "call_count": 0,
            }
        chunk_reviews = []
        invalid_chunks = []
        for index, chunk in enumerate(chunks, 1):
            response = invoke(item, selected, _review_prompt(chunk))
            if response is None or response.status != "success":
                invalid_chunks.append(
                    {"chunk": index, "status": response.status if response else "resource_limit"}
                )
                continue
            try:
                chunk_reviews.append(parse_review(response.text))
            except ValueError as exc:
                invalid_chunks.append({"chunk": index, "status": "invalid_review", "error": str(exc)})
        if invalid_chunks:
            return {
                "status": "resource_limit" if any(
                    item["status"] == "wall_time_exceeded" for item in invalid_chunks
                ) else "failed",
                "reason": "wall_time_exceeded" if any(
                    item["status"] == "wall_time_exceeded" for item in invalid_chunks
                ) else "required_chunk_failed",
                "call_count": call_count,
                "calls": call_records,
                "invalid_reviews": invalid_chunks,
            }
        conflict = detect_conflict(chunk_reviews)
        return {
            "status": "success",
            "review": aggregate_reviews(chunk_reviews),
            "adjudicated_by": "core" if conflict else None,
            "chunk_count": len(chunks),
            "call_count": call_count,
            "requested_output_tokens": requested_tokens,
            "completion_tokens": actual_tokens,
            "calls": call_records,
            "invalid_reviews": invalid_chunks,
        }

    initial_results: dict[str, tuple[review_router.ReviewerChoice, call_model.ModelCallResult | None]] = {}
    prompt = _review_prompt(plan)
    if len(route.reviewers) > 1:
        with ThreadPoolExecutor(max_workers=len(route.reviewers)) as pool:
            futures = {
                pool.submit(invoke, profiles_by_alias[selected.alias], selected, prompt): selected
                for selected in route.reviewers
            }
            for future in as_completed(futures):
                selected = futures[future]
                initial_results[selected.alias] = (selected, future.result())
    else:
        for selected in route.reviewers:
            initial_results[selected.alias] = (
                selected,
                invoke(profiles_by_alias[selected.alias], selected, prompt),
            )

    final_responses: dict[str, call_model.ModelCallResult] = {}
    retry_statuses = {"reasoning_budget_exhausted", "incomplete_review"}
    # Resource arbitration follows route priority, never concurrent completion order.
    for routed in route.reviewers:
        alias = routed.alias
        selected, response = initial_results[alias]
        item = profiles_by_alias[alias]
        semantics_confirmed_by_response = bool(
            response is not None
            and (
                (
                    response.status == "reasoning_budget_exhausted"
                    and response.reasoning_tokens >= int(selected.output_budget * 0.9)
                )
                or (
                    response.status == "incomplete_review"
                    and response.completion_tokens >= int(selected.output_budget * 0.9)
                )
            )
        )
        semantics_confirmed_by_metadata = bool(
            item.source != "name_heuristic" and item.token_semantics != "unknown"
        )
        if (
            response is not None
            and response.status in retry_statuses
            and confirmed
            and (semantics_confirmed_by_metadata or semantics_confirmed_by_response)
            and call_count < effective_max_calls
        ):
            expanded_budget = min(item.output_limit, selected.output_budget * 2)
            if expanded_budget > selected.output_budget:
                expanded = replace(selected, output_budget=expanded_budget)
                retried = invoke(item, expanded, prompt)
                if retried is not None:
                    response = retried
        if response is not None:
            final_responses[alias] = response

    adaptive_statuses = retry_statuses | {"input_context_overflow"}
    if any(response.status == "wall_time_exceeded" for response in final_responses.values()):
        return {
            "status": "resource_limit",
            "reason": "wall_time_exceeded",
            "call_count": call_count,
            "requested_output_tokens": requested_tokens,
            "completion_tokens": actual_tokens,
            "calls": call_records,
        }
    if not confirmed and any(
        response.status in adaptive_statuses for response in final_responses.values()
    ):
        return {
            "status": "confirmation_required",
            "reason": "runtime_retry_or_chunking_requires_confirmation",
            "call_count": call_count,
            "requested_output_tokens": requested_tokens,
            "completion_tokens": actual_tokens,
            "calls": call_records,
        }

    parsed = []
    parsed_by_alias = {}
    invalid = []
    for alias, response in final_responses.items():
        if response.status != "success":
            invalid.append({"alias": alias, "status": response.status})
            continue
        try:
            review = parse_review(response.text)
        except ValueError as exc:
            invalid.append({"alias": alias, "status": "invalid_review", "error": str(exc)})
            continue
        parsed.append(review)
        parsed_by_alias[alias] = review

    chunk_fallback_used = False
    retry_exhausted = any(
        response.status in retry_statuses for response in final_responses.values()
    )
    runtime_overflow = any(
        response.status == "input_context_overflow" for response in final_responses.values()
    )
    if not parsed and (retry_exhausted or runtime_overflow) and call_count < effective_max_calls and route.reviewers:
        selected = route.reviewers[0]
        item = profiles_by_alias[selected.alias]
        try:
            chunks = _chunks_for_profile(plan, item, selected)
        except ValueError as exc:
            return {
                "status": "resource_limit",
                "reason": str(exc),
                "call_count": call_count,
                "requested_output_tokens": requested_tokens,
                "completion_tokens": actual_tokens,
                "calls": call_records,
                "invalid_reviews": invalid,
            }
        remaining_calls = effective_max_calls - call_count
        if len(chunks) <= remaining_calls:
            chunk_fallback_used = True
            runtime_chunk_invalid = []
            for index, chunk in enumerate(chunks, 1):
                response = invoke(item, selected, _review_prompt(chunk))
                if response is None or response.status != "success":
                    entry = {
                        "chunk": index,
                        "status": response.status if response else "resource_limit",
                    }
                    invalid.append(entry)
                    runtime_chunk_invalid.append(entry)
                    continue
                try:
                    parsed.append(parse_review(response.text))
                except ValueError as exc:
                    entry = {"chunk": index, "status": "invalid_review", "error": str(exc)}
                    invalid.append(entry)
                    runtime_chunk_invalid.append(entry)
            if runtime_chunk_invalid:
                wall_exceeded = any(
                    entry["status"] == "wall_time_exceeded" for entry in runtime_chunk_invalid
                )
                return {
                    "status": "resource_limit" if wall_exceeded else "failed",
                    "reason": "wall_time_exceeded" if wall_exceeded else "required_chunk_failed",
                    "call_count": call_count,
                    "requested_output_tokens": requested_tokens,
                    "completion_tokens": actual_tokens,
                    "calls": call_records,
                    "invalid_reviews": invalid,
                }
        else:
            return {
                "status": "resource_limit",
                "reason": "adaptive_retry_left_too_few_calls_for_chunking",
                "chunk_count": len(chunks),
                "remaining_calls": remaining_calls,
                "call_count": call_count,
                "requested_output_tokens": requested_tokens,
                "completion_tokens": actual_tokens,
                "calls": call_records,
                "invalid_reviews": invalid,
            }

    if not parsed:
        return {
            "status": "failed",
            "call_count": call_count,
            "requested_output_tokens": requested_tokens,
            "completion_tokens": actual_tokens,
            "calls": call_records,
            "invalid_reviews": invalid,
        }

    adjudicated_by = None
    final_review = aggregate_reviews(parsed)
    if detect_conflict(parsed):
        if chunk_fallback_used:
            return {
                "status": "success",
                "review": final_review,
                "adjudicated_by": "core",
                "call_count": call_count,
                "requested_output_tokens": requested_tokens,
                "completion_tokens": actual_tokens,
                "calls": call_records,
                "invalid_reviews": invalid,
            }
        used = set(parsed_by_alias)
        judge = review_router.select_judge(profiles, used, route.assessment) if route.conflict_judge else None
        if judge is not None and call_count < effective_max_calls:
            estimate = review_router.TIER_LATENCY["critical"]
            judge_choice = review_router.ReviewerChoice(
                alias=judge.alias,
                provider=judge.provider,
                model=judge.model,
                configured_model=judge.configured_model,
                family=judge.family,
                effort=judge.effort_levels[-1] if judge.effort_levels != ("none",) else None,
                output_budget=min(32768, judge.output_limit),
                token_semantics=judge.token_semantics,
                confidence=judge.confidence,
                estimated_seconds=estimate,
                timeout_seconds=min(route.max_wall_seconds, 900),
            )
            judged = invoke(judge, judge_choice, _judge_prompt(plan, parsed))
            if judged is not None and judged.status == "success":
                try:
                    final_review = parse_review(judged.text)
                    adjudicated_by = judge.alias
                except ValueError:
                    adjudicated_by = "core"
                    final_review = aggregate_reviews(parsed)
            else:
                adjudicated_by = "core"
        else:
            adjudicated_by = "core"

    return {
        "status": "success",
        "review": final_review,
        "adjudicated_by": adjudicated_by,
        "call_count": call_count,
        "requested_output_tokens": requested_tokens,
        "completion_tokens": actual_tokens,
        "calls": call_records,
        "invalid_reviews": invalid,
    }
