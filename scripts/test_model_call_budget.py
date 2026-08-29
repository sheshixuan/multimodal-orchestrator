#!/usr/bin/env python3
"""流式模型调用和推理预算分类回归测试。"""

import contextlib
import io
import json
import inspect
import os
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import asdict
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import call_model


class TestBudgetPayload(unittest.TestCase):
    def test_combined_budget_uses_max_completion_tokens_and_supported_effort(self):
        self.assertIn("max_completion_tokens", inspect.signature(call_model.build_payload).parameters)
        payload = call_model.build_payload(
            "reasoner",
            [{"role": "user", "content": "review"}],
            0.2,
            None,
            max_completion_tokens=8192,
            reasoning_effort="high",
            stream=True,
        )

        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertIs(payload["stream"], True)
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_separate_budget_caps_answer_and_total_completion(self):
        self.assertTrue(hasattr(call_model, "budget_fields"))
        fields = call_model.budget_fields("separate", 24000)
        self.assertEqual(fields, {"max_tokens": 8000, "max_completion_tokens": 24000})

    def test_custom_reasoning_field_is_used_from_capability_profile(self):
        payload = call_model.build_payload(
            "reasoner",
            [{"role": "user", "content": "review"}],
            0.2,
            None,
            reasoning_effort="high",
            reasoning_field="thinking_level",
        )
        self.assertEqual(payload["thinking_level"], "high")
        self.assertNotIn("reasoning_effort", payload)


class TestResultClassification(unittest.TestCase):
    def test_reasoning_only_length_is_not_context_overflow(self):
        self.assertTrue(hasattr(call_model, "normalize_response"))
        body = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "hidden reasoning"},
                }
            ],
            "usage": {
                "completion_tokens": 3000,
                "completion_tokens_details": {"reasoning_tokens": 3000},
            },
        }

        result = call_model.normalize_response(body, requested_budget=3000)

        self.assertEqual(result.status, "reasoning_budget_exhausted")
        self.assertEqual(result.reasoning_tokens, 3000)
        self.assertTrue(result.saw_reasoning)
        self.assertNotIn("hidden reasoning", json.dumps(asdict(result)))

    def test_partial_content_at_length_is_incomplete_review(self):
        self.assertTrue(hasattr(call_model, "normalize_response"))
        body = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "partial", "reasoning_content": "hidden"},
                }
            ],
            "usage": {"completion_tokens": 3000},
        }

        result = call_model.normalize_response(body, requested_budget=3000)

        self.assertEqual(result.status, "incomplete_review")
        self.assertEqual(result.text, "partial")

    def test_provider_input_error_is_classified_separately(self):
        self.assertTrue(hasattr(call_model, "classify_api_error"))
        self.assertEqual(
            call_model.classify_api_error(400, '{"error":{"message":"maximum context length exceeded"}}'),
            "input_context_overflow",
        )


class TestStreaming(unittest.TestCase):
    def test_stream_records_timings_without_exposing_reasoning_text(self):
        self.assertTrue(hasattr(call_model, "parse_stream_response"))
        events = [
            {"choices": [{"delta": {"reasoning_content": "secret chain"}}]},
            {"choices": [{"delta": {"content": "{\"verdict\":\"pass\"}"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "completion_tokens": 42,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            },
        ]
        lines = [f"data: {json.dumps(event)}\n".encode("utf-8") for event in events]
        lines.append(b"data: [DONE]\n")
        moments = iter([1.0, 4.0, 5.0, 6.0])

        result = call_model.parse_stream_response(
            lines,
            requested_budget=8192,
            started_at=0.0,
            clock=lambda: next(moments),
        )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.first_reasoning_seconds, 1.0)
        self.assertEqual(result.first_content_seconds, 4.0)
        self.assertEqual(result.elapsed_seconds, 6.0)
        self.assertEqual(result.text, '{"verdict":"pass"}')
        self.assertNotIn("secret chain", json.dumps(asdict(result)))

    def test_call_chat_streams_with_combined_budget(self):
        class Response:
            def __init__(self, lines):
                self.lines = lines

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def __iter__(self):
                return iter(self.lines)

        lines = [
            b'data: {"choices":[{"delta":{"content":"done"}}]}\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":10}}\n',
            b'data: [DONE]\n',
        ]
        self.assertTrue(hasattr(call_model, "call_chat"))
        with unittest.mock.patch(
            "call_model.urllib.request.urlopen", return_value=Response(lines)
        ) as urlopen:
            result = call_model.call_chat(
                base_url="https://provider.example/v1",
                api_key="x",
                model="reasoner",
                messages=[{"role": "user", "content": "review"}],
                timeout=30,
                output_budget=8192,
                token_semantics="combined",
                reasoning_effort="high",
                stream=True,
            )

        self.assertEqual(result.text, "done")
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(payload["reasoning_effort"], "high")


class TestCallModelCli(unittest.TestCase):
    def test_review_cli_passes_completion_budget_effort_and_streaming(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('review_model = "deep-reasoner"\n', encoding="utf-8")
            result = call_model.ModelCallResult(
                text="reviewed",
                status="success",
                finish_reason="stop",
                completion_tokens=10,
                reasoning_tokens=5,
                saw_reasoning=True,
            )
            with unittest.mock.patch.dict(os.environ, {"OPENCODE_API_KEY": "x"}, clear=True):
                with unittest.mock.patch("call_model.call_chat", return_value=result) as caller:
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc = call_model.main(
                            [
                                "--role", "review",
                                "--plan", "a plan",
                                "--model", "deepseek-v4-pro",
                                "--max-completion-tokens", "8192",
                                "--reasoning-effort", "high",
                                "--stream",
                                "--config", str(config),
                            ]
                        )

        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().strip(), "reviewed")
        kwargs = caller.call_args.kwargs
        self.assertEqual(kwargs["output_budget"], 8192)
        self.assertEqual(kwargs["token_semantics"], "combined")
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertIs(kwargs["stream"], True)

    def test_reasoning_budget_exhaustion_has_specific_error(self):
        result = call_model.ModelCallResult(
            text="",
            status="reasoning_budget_exhausted",
            finish_reason="length",
            completion_tokens=3000,
            reasoning_tokens=3000,
            saw_reasoning=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('review_model = "deepseek-v4-pro"\n', encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {"OPENCODE_API_KEY": "x"}, clear=True):
                with unittest.mock.patch("call_model.call_chat", return_value=result):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        rc = call_model.main(
                            ["--role", "review", "--plan", "a plan", "--config", str(config)]
                        )

        self.assertEqual(rc, 1)
        self.assertIn("reasoning_budget_exhausted", stderr.getvalue())
        self.assertNotIn("input_context_overflow", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
