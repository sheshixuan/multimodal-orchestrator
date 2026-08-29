#!/usr/bin/env python3
"""结构化评审、冲突仲裁和长方案切分测试。"""

import importlib
import json
import sys
import time
import unittest
from dataclasses import replace
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import call_model
import review_router


def structured_review(verdict, dimension_status="pass", option=None):
    issue = {
        "severity": "high" if verdict == "block" else "medium",
        "evidence": "evidence",
        "recommendation": "recommendation",
    }
    if option:
        issue.update({"decision_key": "deployment", "recommended_option": option})
    return {
        "verdict": verdict,
        "dimensions": {
            name: {"status": dimension_status, "summary": name}
            for name in (
                "correctness", "completeness", "executability", "scope", "risk", "testability"
            )
        },
        "issues": [issue],
        "unresolved_questions": [],
        "confidence": 0.9,
    }


def profile(alias, provider, family, quality=90, source="catalog"):
    return review_router.CapabilityProfile(
        alias=alias,
        configured_model=f"{provider}:{alias}-model",
        provider=provider,
        model=f"{alias}-model",
        family=family,
        modalities=("text",),
        effort_levels=("high", "max"),
        reasoning_field="reasoning_effort",
        context_limit=1000000,
        output_limit=384000,
        token_semantics="combined",
        streaming=True,
        quality=quality,
        confidence="medium",
        source=source,
        key_ready=True,
    )


def choice(item, budget=32000):
    return review_router.ReviewerChoice(
        alias=item.alias,
        provider=item.provider,
        model=item.model,
        configured_model=item.configured_model,
        family=item.family,
        effort="max",
        output_budget=budget,
        token_semantics=item.token_semantics,
        confidence=item.confidence,
        estimated_seconds=(240, 600),
        timeout_seconds=900,
    )


def route(items, tier="critical", max_calls=3):
    assessment = review_router.Assessment(80, 75, 90, 80, tier, input_tokens=2000)
    return review_router.ReviewRoute(
        tier=tier,
        reviewers=tuple(choice(item) for item in items),
        requires_confirmation=True,
        confirmation_reasons=("slow",),
        max_calls=max_calls,
        max_total_output_tokens=131072,
        max_wall_seconds=1800,
        heartbeat_seconds=30,
        long_plan_strategy="adaptive_then_chunk",
        conflict_judge=True,
        assessment=assessment,
    )


def result(review=None, status="success", tokens=1000):
    return call_model.ModelCallResult(
        text=json.dumps(review, ensure_ascii=False) if review else "",
        status=status,
        finish_reason="stop" if status == "success" else "length",
        completion_tokens=tokens,
        reasoning_tokens=tokens if status == "reasoning_budget_exhausted" else tokens // 2,
        saw_reasoning=True,
        elapsed_seconds=5,
    )


class TestStructuredReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.engine = importlib.import_module("review_engine")
        except ImportError:
            cls.engine = None

    def test_conflict_requires_verdict_or_dimension_gap_not_coverage_difference(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        passing = structured_review("pass", "pass")
        blocking = structured_review("block", "block")
        another_pass = structured_review("pass", "pass")
        another_pass["issues"].append(
            {"severity": "low", "evidence": "extra", "recommendation": "extra"}
        )

        self.assertTrue(self.engine.detect_conflict([passing, blocking]))
        self.assertFalse(self.engine.detect_conflict([passing, another_pass]))

    def test_mutually_exclusive_structured_recommendations_conflict(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        left = structured_review("revise", "warn", option="blue-green")
        right = structured_review("revise", "warn", option="in-place")
        self.assertTrue(self.engine.detect_conflict([left, right]))

    def test_chunking_repeats_immutable_global_constraints(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        plan = """
# 目标
上线新接口。
# 非目标
不迁移旧数据。
# 关键接口
POST /v2/items。
# 验收标准
旧客户端继续工作。
# 实现 A
细节 A。
# 实现 B
细节 B。
"""
        chunks = self.engine.chunk_plan(plan, max_chars=90)
        self.assertGreaterEqual(len(chunks), 2)
        constraints = chunks[0].split("\n\n## 当前评审分片", 1)[0]
        for chunk in chunks:
            self.assertTrue(chunk.startswith(constraints))
            self.assertIn("旧客户端继续工作", chunk)


class TestReviewExecution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.engine = importlib.import_module("review_engine")
        except ImportError:
            cls.engine = None

    def test_confirmed_reasoning_exhaustion_retries_once_with_double_budget(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        primary = profile("primary", "provider-a", "family-a")
        calls = []

        def caller(item, selected, prompt, system):
            calls.append(selected.output_budget)
            if len(calls) == 1:
                return result(status="reasoning_budget_exhausted", tokens=selected.output_budget)
            return result(structured_review("pass"))

        output = self.engine.execute_review(
            "plan", route([primary], tier="complex"), [primary], caller, confirmed=True
        )

        self.assertEqual(calls, [32000, 64000])
        self.assertEqual(output["status"], "success")
        self.assertEqual(output["call_count"], 2)

    def test_observed_reasoning_exhaustion_confirms_low_confidence_semantics(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        inferred = profile(
            "inferred", "provider-a", "family-a", source="name_heuristic"
        )
        calls = []

        def caller(item, selected, prompt, system):
            calls.append(selected.output_budget)
            if len(calls) == 1:
                return result(status="reasoning_budget_exhausted", tokens=selected.output_budget)
            return result(structured_review("pass"))

        output = self.engine.execute_review(
            "plan", route([inferred], tier="complex"), [inferred], caller, confirmed=True
        )

        self.assertEqual(calls, [32000, 64000])
        self.assertEqual(output["status"], "success")

    def test_conflict_uses_one_unused_model_as_terminal_judge(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        first = profile("first", "provider-a", "family-a", 95)
        second = profile("second", "provider-b", "family-b", 93)
        judge = profile("judge", "provider-c", "family-c", 99)
        calls = []

        def caller(item, selected, prompt, system):
            calls.append(item.alias)
            reviews = {
                "first": structured_review("pass", "pass"),
                "second": structured_review("block", "block"),
                "judge": structured_review("revise", "warn"),
            }
            return result(reviews[item.alias])

        output = self.engine.execute_review(
            "plan", route([first, second]), [first, second, judge], caller, confirmed=True
        )

        self.assertCountEqual(calls[:2], ["first", "second"])
        self.assertEqual(calls[2], "judge")
        self.assertEqual(len(calls), 3)
        self.assertEqual(output["adjudicated_by"], "judge")
        self.assertEqual(output["review"]["verdict"], "revise")

    def test_third_call_retries_highest_ranked_failed_reviewer_deterministically(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        first = profile("first", "provider-a", "family-a", 95)
        second = profile("second", "provider-b", "family-b", 93)
        calls = []
        attempts = {"first": 0, "second": 0}

        def caller(item, selected, prompt, system):
            calls.append(item.alias)
            attempts[item.alias] += 1
            if attempts[item.alias] == 1:
                if item.alias == "first":
                    time.sleep(0.05)
                return result(
                    status="reasoning_budget_exhausted",
                    tokens=selected.output_budget,
                )
            return result(structured_review("pass"))

        output = self.engine.execute_review(
            "plan", route([first, second]), [first, second], caller, confirmed=True
        )

        self.assertCountEqual(calls[:2], ["first", "second"])
        self.assertEqual(calls[2], "first")
        self.assertEqual(output["call_count"], 3)

    def test_no_unused_judge_uses_core_without_recursive_call(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        first = profile("first", "provider-a", "family-a")
        second = profile("second", "provider-b", "family-b")
        calls = []

        def caller(item, selected, prompt, system):
            calls.append(item.alias)
            verdict = "pass" if item.alias == "first" else "block"
            return result(structured_review(verdict, verdict))

        output = self.engine.execute_review(
            "plan", route([first, second]), [first, second], caller, confirmed=True
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(output["adjudicated_by"], "core")
        self.assertEqual(output["review"]["verdict"], "block")

    def test_preflight_chunking_reviews_each_chunk_with_same_constraints(self):
        self.assertIsNotNone(self.engine, "review_engine module is required")
        if self.engine is None:
            return
        primary = replace(
            profile("primary", "provider-a", "family-a"),
            context_limit=900,
            output_limit=200,
        )
        selected_route = replace(
            route([primary], tier="critical"),
            reviewers=(choice(primary, budget=200),),
            needs_chunking=True,
        )
        plan = (
            "# 目标\n安全上线。\n# 验收标准\n旧客户端继续工作。\n"
            "# 模块 A\n" + "A 细节。" * 150 + "\n"
            "# 模块 B\n" + "B 细节。" * 150 + "\n"
        )
        prompts = []

        def caller(item, selected, prompt, system):
            prompts.append(prompt)
            return result(structured_review("pass"))

        output = self.engine.execute_review(
            plan, selected_route, [primary], caller, confirmed=True
        )

        self.assertEqual(output["status"], "success")
        self.assertGreaterEqual(output["call_count"], 2)
        self.assertLessEqual(output["call_count"], 3)
        for prompt in prompts:
            self.assertIn("旧客户端继续工作", prompt)


if __name__ == "__main__":
    unittest.main()
