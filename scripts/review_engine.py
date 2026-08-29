#!/usr/bin/env python3
"""Plan Review 的结构化执行、合并、扩容与终局仲裁。"""

from __future__ import annotations

import json
import re
import threading
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


def chunk_plan(plan: str, max_chars: int = 12000) -> list[str]:
    constraints = extract_global_constraints(plan)
    constraint_bodies = set()
    for _, body in _sections(constraints):
        constraint_bodies.add(body.strip())
    variable = []
    for title, body in _sections(plan):
        if body.strip() not in constraint_bodies and body.strip() not in constraints:
            variable.append(body)
    if not variable:
        variable = [plan]
    groups = []
    current = []
    current_size = 0
    target = max(1, max_chars - len(constraints) - 32)
    for body in variable:
        if current and current_size + len(body) + 2 > target:
            groups.append("\n\n".join(current))
            current, current_size = [], 0
        current.append(body)
        current_size += len(body) + 2
    if current:
        groups.append("\n\n".join(current))
    return [f"{constraints}\n\n## 当前评审分片 {index}/{len(groups)}\n{body}" for index, body in enumerate(groups, 1)]


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

    def invoke(item, selected, prompt):
        nonlocal call_count, requested_tokens, actual_tokens
        with lock:
            if call_count >= route.max_calls:
                return None
            remaining = route.max_total_output_tokens - requested_tokens
            if remaining <= 0:
                return None
            budget = min(selected.output_budget, remaining, item.output_limit)
            if budget <= 0:
                return None
            selected = replace(selected, output_budget=budget)
            call_count += 1
            requested_tokens += budget
        response = caller(item, selected, prompt, STRUCTURED_REVIEW_SYSTEM)
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
        safe_input_tokens = max(1, int(item.context_limit * 0.5) - selected.output_budget)
        chunks = chunk_plan(plan, max_chars=max(1000, safe_input_tokens * 2))
        if len(chunks) > route.max_calls:
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
        if not chunk_reviews:
            return {
                "status": "failed",
                "reason": "all_chunks_failed",
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
            and response.status == "reasoning_budget_exhausted"
            and response.reasoning_tokens >= int(selected.output_budget * 0.9)
        )
        if (
            response is not None
            and response.status in retry_statuses
            and confirmed
            and (item.source != "name_heuristic" or semantics_confirmed_by_response)
            and item.token_semantics != "unknown"
            and call_count < route.max_calls
        ):
            expanded = replace(selected, output_budget=min(item.output_limit, selected.output_budget * 2))
            retried = invoke(item, expanded, prompt)
            if retried is not None:
                response = retried
        if response is not None:
            final_responses[alias] = response

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
    if not parsed and (retry_exhausted or runtime_overflow) and call_count < route.max_calls and route.reviewers:
        selected = route.reviewers[0]
        item = profiles_by_alias[selected.alias]
        safe_input_tokens = max(1, int(item.context_limit * 0.5) - selected.output_budget)
        chunks = chunk_plan(plan, max_chars=max(1000, safe_input_tokens * 2))
        remaining_calls = route.max_calls - call_count
        if len(chunks) <= remaining_calls:
            chunk_fallback_used = True
            for index, chunk in enumerate(chunks, 1):
                response = invoke(item, selected, _review_prompt(chunk))
                if response is None or response.status != "success":
                    invalid.append(
                        {"chunk": index, "status": response.status if response else "resource_limit"}
                    )
                    continue
                try:
                    parsed.append(parse_review(response.text))
                except ValueError as exc:
                    invalid.append({"chunk": index, "status": "invalid_review", "error": str(exc)})
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
        if judge is not None and call_count < route.max_calls:
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
