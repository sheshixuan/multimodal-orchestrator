#!/usr/bin/env python3
"""Plan Review 智能路由与预算行为测试。"""

import importlib
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import call_model


class TestTypedConfig(unittest.TestCase):
    def test_numeric_and_list_values_are_parsed_without_breaking_old_values(self):
        cfg = call_model.parse_toml_simple(
            '''
review_model = "proxy:reasoner-pro"
plan_review = "ask"

[review_routing]
max_calls = 3
max_total_output_tokens = 131072
conflict_judge = true

[review_capabilities.primary]
reasoning_levels = ["high", "max"]
quality = 91.5
'''
        )

        self.assertEqual(cfg["review_model"], "proxy:reasoner-pro")
        self.assertEqual(cfg["review_routing"]["max_calls"], 3)
        self.assertEqual(cfg["review_routing"]["max_total_output_tokens"], 131072)
        self.assertIs(cfg["review_routing"]["conflict_judge"], True)
        self.assertEqual(cfg["review_capabilities"]["primary"]["reasoning_levels"], ["high", "max"])
        self.assertEqual(cfg["review_capabilities"]["primary"]["quality"], 91.5)


class TestProductionRoutingIsCapabilityDriven(unittest.TestCase):
    def test_router_and_engine_do_not_name_specific_provider_or_model_families(self):
        scripts_dir = Path(__file__).resolve().parent
        production_source = "\n".join(
            (scripts_dir / name).read_text(encoding="utf-8").lower()
            for name in ("review_router.py", "review_engine.py")
        )
        forbidden_names = (
            "opencode",
            "dashscope",
            "bailian",
            "qwen",
            "deepseek",
            "gemini",
            "glm",
        )
        for name in forbidden_names:
            with self.subTest(name=name):
                self.assertNotIn(name, production_source)


class TestAssessmentAndRouting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.router = importlib.import_module("review_router")
        except ImportError:
            cls.router = None

    def test_critical_plan_selects_two_diverse_high_quality_reviewers(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return
        plan = """
# 数据迁移方案
跨三个服务修改公开 API，并发迁移生产数据，需要兼容旧客户端、回滚、安全审计和灾难恢复。
外部依赖行为尚未验证，存在数据丢失和权限扩大风险。
"""
        cfg = call_model.parse_toml_simple(
            '''
[providers.alpha]
base_url = "https://alpha.example/v1"
env = "ALPHA_KEY"
[providers.beta]
base_url = "https://beta.example/v1"
env = "BETA_KEY"
[providers.gamma]
base_url = "https://gamma.example/v1"
env = "GAMMA_KEY"

[review_models]
first = "alpha:reasoner-pro"
second = "beta:thinker-max"
third = "gamma:quick-lite"

[review_capabilities.first]
family = "family-a"
quality = 94
reasoning_levels = ["high", "max"]
context_limit = 1000000
output_limit = 384000
token_semantics = "combined"

[review_capabilities.second]
family = "family-b"
quality = 92
reasoning_levels = ["medium", "high"]
context_limit = 200000
output_limit = 64000
token_semantics = "answer_only"

[review_capabilities.third]
family = "family-c"
quality = 60
reasoning_levels = ["low"]
'''
        )
        providers = call_model.build_providers(cfg)
        with unittest.mock.patch.dict(
            os.environ,
            {"ALPHA_KEY": "x", "BETA_KEY": "x", "GAMMA_KEY": "x"},
            clear=True,
        ):
            assessment = self.router.assess_plan(plan)
            profiles = self.router.build_capability_profiles(cfg, providers)
            route = self.router.select_route(assessment, profiles, cfg)

        self.assertEqual(assessment.tier, "critical")
        self.assertEqual([item.alias for item in route.reviewers], ["first", "second"])
        self.assertEqual([item.effort for item in route.reviewers], ["max", "high"])
        self.assertTrue(route.requires_confirmation)
        self.assertEqual(route.max_calls, 3)
        self.assertEqual(route.max_total_output_tokens, 131072)

    def test_single_strategy_limits_critical_plan_to_one_reviewer(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return

        def candidate(alias, provider, family, quality):
            return self.router.CapabilityProfile(
                alias=alias,
                configured_model=f"{provider}:{alias}",
                provider=provider,
                model=alias,
                family=family,
                modalities=("text",),
                effort_levels=("high", "max"),
                reasoning_field="reasoning_effort",
                context_limit=1000000,
                output_limit=384000,
                token_semantics="combined",
                streaming=True,
                quality=quality,
                confidence="high",
                source="config",
                key_ready=True,
            )

        assessment = self.router.Assessment(80, 75, 80, 78.2, "critical", input_tokens=1000)
        profiles = [
            candidate("first", "provider-a", "family-a", 96),
            candidate("second", "provider-b", "family-b", 94),
        ]

        route = self.router.select_route(assessment, profiles, {}, strategy="single")

        self.assertEqual([item.alias for item in route.reviewers], ["first"])

    def test_multi_strategy_expands_routine_plan_to_two_diverse_reviewers(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return

        def candidate(alias, provider, family, quality, p90):
            return self.router.CapabilityProfile(
                alias=alias,
                configured_model=f"{provider}:{alias}",
                provider=provider,
                model=alias,
                family=family,
                modalities=("text",),
                effort_levels=("low", "high"),
                reasoning_field="reasoning_effort",
                context_limit=1000000,
                output_limit=64000,
                token_semantics="combined",
                streaming=True,
                quality=quality,
                confidence="high",
                source="config",
                key_ready=True,
                p50_seconds=30,
                p90_seconds=p90,
            )

        assessment = self.router.Assessment(15, 5, 5, 9.5, "routine", input_tokens=1000)
        profiles = [
            candidate("fast", "provider-a", "family-a", 80, 60),
            candidate("diverse", "provider-b", "family-b", 78, 90),
            candidate("same-family", "provider-c", "family-a", 79, 70),
        ]

        route = self.router.select_route(assessment, profiles, {}, strategy="multi")

        self.assertEqual([item.alias for item in route.reviewers], ["fast", "diverse"])
        self.assertTrue(route.requires_confirmation)
        self.assertIn("需要并行调用多个外部评审模型", route.confirmation_reasons)

    def test_missing_decision_blocks_before_any_model_is_selected(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return
        assessment = self.router.assess_plan(
            "上线前必须确定认证协议，但该项仍是 TBD，且需要用户选择后才能继续。"
        )
        self.assertEqual(assessment.tier, "blocked")
        self.assertTrue(assessment.blocking_reasons)

    def test_critical_second_reviewer_must_use_a_different_family_when_available(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return

        def candidate(alias, provider, family, quality):
            return self.router.CapabilityProfile(
                alias=alias,
                configured_model=f"{provider}:{alias}",
                provider=provider,
                model=alias,
                family=family,
                modalities=("text",),
                effort_levels=("high",),
                reasoning_field="reasoning_effort",
                context_limit=1000000,
                output_limit=64000,
                token_semantics="combined",
                streaming=True,
                quality=quality,
                confidence="high",
                source="config",
                key_ready=True,
            )

        profiles = [
            candidate("first", "provider-a", "family-a", 100),
            candidate("same-family", "provider-b", "family-a", 99),
            candidate("different-family", "provider-a", "family-b", 90),
        ]
        assessment = self.router.Assessment(80, 80, 80, 80, "critical", input_tokens=1000)

        selected = self.router.select_route(assessment, profiles, {})

        self.assertEqual(
            [item.alias for item in selected.reviewers],
            ["first", "different-family"],
        )

    def test_critical_prefers_a_different_provider_within_diverse_families(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return

        def candidate(alias, provider, family, quality):
            return self.router.CapabilityProfile(
                alias=alias,
                configured_model=f"{provider}:{alias}",
                provider=provider,
                model=alias,
                family=family,
                modalities=("text",),
                effort_levels=("high",),
                reasoning_field="reasoning_effort",
                context_limit=1000000,
                output_limit=64000,
                token_semantics="combined",
                streaming=True,
                quality=quality,
                confidence="high",
                source="config",
                key_ready=True,
            )

        profiles = [
            candidate("first", "provider-a", "family-a", 100),
            candidate("same-provider", "provider-a", "family-b", 99),
            candidate("different-provider", "provider-b", "family-c", 90),
        ]
        assessment = self.router.Assessment(80, 80, 80, 80, "critical", input_tokens=1000)

        selected = self.router.select_route(assessment, profiles, {})

        self.assertEqual(
            [item.alias for item in selected.reviewers],
            ["first", "different-provider"],
        )

    def test_max_calls_above_hard_limit_is_rejected(self):
        with self.assertRaises(call_model.ConfigError):
            self.router.routing_config({"review_routing": {"max_calls": 4}})

    def test_name_inference_is_low_confidence_and_conservative(self):
        self.assertIsNotNone(self.router, "review_router module is required")
        if self.router is None:
            return
        cfg = call_model.parse_toml_simple(
            '''
[providers.private]
base_url = "https://private.example/v1"
env = "PRIVATE_KEY"
[review_models]
candidate = "private:unknown-reasoner-pro"
'''
        )
        providers = call_model.build_providers(cfg)
        with unittest.mock.patch.dict(os.environ, {"PRIVATE_KEY": "x"}, clear=True):
            profile = self.router.build_capability_profiles(cfg, providers)[0]

        self.assertEqual(profile.confidence, "low")
        self.assertEqual(profile.token_semantics, "combined")
        self.assertIn(profile.effort_levels[-1], {"high", "max"})

    def test_oversized_input_routes_to_confirmed_chunking_instead_of_failing(self):
        cfg = call_model.parse_toml_simple(
            '''
[providers.private]
base_url = "https://private.example/v1"
env = "PRIVATE_KEY"
[review_models]
candidate = "private:reviewer-pro"
[review_capabilities.candidate]
family = "family-a"
quality = 90
reasoning_levels = ["high", "max"]
context_limit = 12000
output_limit = 4000
token_semantics = "combined"
'''
        )
        providers = call_model.build_providers(cfg)
        assessment = self.router.Assessment(
            difficulty=80,
            uncertainty=60,
            risk=80,
            score=75,
            tier="critical",
            input_tokens=20000,
        )
        with unittest.mock.patch.dict(os.environ, {"PRIVATE_KEY": "x"}, clear=True):
            profiles = self.router.build_capability_profiles(cfg, providers)
            selected = self.router.select_route(assessment, profiles, cfg)

        self.assertTrue(selected.needs_chunking)
        self.assertTrue(selected.requires_confirmation)
        self.assertEqual(len(selected.reviewers), 1)


class TestDiscoveryAndTelemetry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = importlib.import_module("review_router")

    def _profile(self, *, source="name_heuristic"):
        return self.router.CapabilityProfile(
            alias="primary",
            configured_model="proxy:reasoner-pro",
            provider="proxy",
            model="reasoner-pro",
            family="reasoner",
            modalities=("text",),
            effort_levels=("high", "max"),
            reasoning_field="reasoning_effort",
            context_limit=128000,
            output_limit=32768,
            token_semantics="combined",
            streaming=True,
            quality=95,
            confidence="low" if source == "name_heuristic" else "high",
            source=source,
            key_ready=True,
        )

    def test_catalog_metadata_upgrades_heuristic_profile(self):
        self.assertTrue(hasattr(self.router, "catalog_metadata_for_profiles"))
        self.assertTrue(hasattr(self.router, "apply_capability_metadata"))
        catalog = {
            "catalog-provider": {
                "models": {
                    "reasoner-pro": {
                        "id": "reasoner-pro",
                        "family": "reasoner-thinking",
                        "reasoning": True,
                        "reasoning_options": {"effort": ["high", "max"]},
                        "limit": {"context": 1000000, "output": 384000},
                        "interleaved": {"field": "reasoning_content"},
                    }
                }
            }
        }

        metadata = self.router.catalog_metadata_for_profiles(catalog, [self._profile()])
        upgraded = self.router.apply_capability_metadata([self._profile()], metadata)[0]

        self.assertEqual(upgraded.context_limit, 1000000)
        self.assertEqual(upgraded.output_limit, 384000)
        self.assertEqual(upgraded.family, "reasoner-thinking")
        self.assertEqual(upgraded.confidence, "medium")
        self.assertEqual(upgraded.source, "catalog")

    def test_catalog_never_overrides_explicit_config_profile(self):
        self.assertTrue(hasattr(self.router, "apply_capability_metadata"))
        configured = self._profile(source="config")
        metadata = {
            ("proxy", "reasoner-pro"): {
                "context_limit": 1,
                "output_limit": 1,
                "source": "catalog",
                "confidence": "medium",
            }
        }

        actual = self.router.apply_capability_metadata([configured], metadata)[0]

        self.assertEqual(actual.context_limit, 128000)
        self.assertEqual(actual.output_limit, 32768)
        self.assertEqual(actual.source, "config")

    def test_id_only_provider_listing_does_not_block_richer_catalog_metadata(self):
        metadata = self.router.provider_metadata(
            "proxy",
            {"data": [{"id": "reasoner-pro", "object": "model"}]},
            [self._profile()],
        )
        self.assertEqual(metadata, {})

    def test_telemetry_keeps_recent_twenty_successes_without_prompt_data(self):
        self.assertTrue(hasattr(self.router, "record_telemetry"))
        self.assertTrue(hasattr(self.router, "telemetry_latency"))
        now = time.time()
        state = {}
        for index in range(25):
            state = self.router.record_telemetry(
                state,
                provider="proxy",
                model="reasoner-pro",
                tier="critical",
                elapsed_seconds=100 + index,
                completion_tokens=1000 + index,
                reasoning_tokens=900 + index,
                status="success",
                now=now - index * 60,
            )
        state = self.router.record_telemetry(
            state,
            provider="proxy",
            model="reasoner-pro",
            tier="critical",
            elapsed_seconds=999,
            completion_tokens=0,
            reasoning_tokens=0,
            status="success",
            now=now - 31 * 86400,
        )

        samples = state["telemetry"]["proxy:reasoner-pro:critical"]
        self.assertEqual(len(samples), 20)
        serialized = str(state)
        self.assertNotIn("prompt", serialized.lower())
        self.assertNotIn("authorization", serialized.lower())
        p50, p90 = self.router.telemetry_latency(state, "proxy", "reasoner-pro", "critical", now=now)
        self.assertGreaterEqual(p90, p50)

    def test_runtime_state_roundtrip_uses_private_permissions(self):
        self.assertTrue(hasattr(self.router, "save_runtime_state"))
        self.assertTrue(hasattr(self.router, "load_runtime_state"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review-runtime.json"
            self.router.save_runtime_state(path, {"telemetry": {"x": []}})
            self.assertEqual(self.router.load_runtime_state(path), {"telemetry": {"x": []}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
