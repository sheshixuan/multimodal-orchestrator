#!/usr/bin/env python3
"""review_plan 智能路由 CLI 集成测试。"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import call_model
import review_plan


CRITICAL_PLAN = """
# 生产数据迁移
跨模块修改公开 API，并发迁移数据库，需要兼容、回滚、安全审计和灾难恢复。
外部依赖行为尚未验证，存在数据丢失与权限扩大风险。
"""


def review_json(verdict="pass"):
    return {
        "verdict": verdict,
        "dimensions": {
            name: {"status": "pass", "summary": "ok"}
            for name in (
                "correctness", "completeness", "executability", "scope", "risk", "testability"
            )
        },
        "issues": [],
        "unresolved_questions": [],
        "confidence": 0.9,
    }


class TestReviewPlanCli(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.config = root / "config.toml"
        self.plan = root / "plan.md"
        self.state = root / "runtime.json"
        self.plan.write_text(CRITICAL_PLAN, encoding="utf-8")
        self.config.write_text(
            '''
plan_review = "ask"
[providers.alpha]
base_url = "https://alpha.example/v1"
env = "ALPHA_KEY"
[providers.beta]
base_url = "https://beta.example/v1"
env = "BETA_KEY"
[review_models]
first = "alpha:reasoner-pro"
second = "beta:thinker-max"
[review_capabilities.first]
family = "family-a"
quality = 95
reasoning_levels = ["high", "max"]
context_limit = 1000000
output_limit = 384000
token_semantics = "combined"
[review_capabilities.second]
family = "family-b"
quality = 93
reasoning_levels = ["high", "max"]
context_limit = 1000000
output_limit = 384000
token_semantics = "combined"
''',
            encoding="utf-8",
        )

    def tearDown(self):
        self.directory.cleanup()

    def _main(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = review_plan.main(args)
            except SystemExit as exc:
                self.fail(f"CLI exited through argparse instead of returning: {exc}")
        return code, stdout.getvalue(), stderr.getvalue()

    def test_assess_is_local_and_returns_selected_route(self):
        self.assertTrue(hasattr(review_plan, "assess_review"))
        with unittest.mock.patch.dict(
            os.environ, {"ALPHA_KEY": "x", "BETA_KEY": "x"}, clear=True
        ):
            with unittest.mock.patch("call_model.api_get") as api_get:
                code, stdout, stderr = self._main(
                    [
                        "--assess", str(self.plan), "--json",
                        "--config", str(self.config), "--state", str(self.state),
                    ]
                )

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["assessment"]["tier"], "critical")
        self.assertEqual(len(payload["route"]["reviewers"]), 2)
        api_get.assert_not_called()

    def test_slow_multi_model_review_requires_confirmation_before_api(self):
        self.assertTrue(hasattr(review_plan, "assess_review"))
        with unittest.mock.patch.dict(
            os.environ, {"ALPHA_KEY": "x", "BETA_KEY": "x"}, clear=True
        ):
            with unittest.mock.patch("call_model.call_chat") as call_chat:
                with unittest.mock.patch("call_model.api_get") as api_get:
                    code, stdout, stderr = self._main(
                        [
                            "--review", str(self.plan), "--json",
                            "--config", str(self.config), "--state", str(self.state),
                        ]
                    )

        self.assertEqual(code, 4)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "confirmation_required")
        call_chat.assert_not_called()
        api_get.assert_not_called()

    def test_confirmed_review_executes_selected_models(self):
        self.assertTrue(hasattr(review_plan, "refresh_profiles"))
        response = call_model.ModelCallResult(
            text=json.dumps(review_json()),
            status="success",
            finish_reason="stop",
            completion_tokens=100,
            reasoning_tokens=50,
            saw_reasoning=True,
            elapsed_seconds=2,
        )
        with unittest.mock.patch.dict(
            os.environ, {"ALPHA_KEY": "x", "BETA_KEY": "x"}, clear=True
        ):
            with unittest.mock.patch("review_plan.refresh_profiles", side_effect=lambda p, *a, **k: p):
                with unittest.mock.patch("call_model.call_chat", return_value=response) as caller:
                    code, stdout, stderr = self._main(
                        [
                            "--review", str(self.plan), "--confirm-slow", "--json",
                            "--config", str(self.config), "--state", str(self.state),
                        ]
                    )

        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["call_count"], 2)
        self.assertEqual(caller.call_count, 2)


if __name__ == "__main__":
    unittest.main()
