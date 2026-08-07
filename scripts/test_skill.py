#!/usr/bin/env python3
"""multimodal-orchestrator 自测：配置解析、模型预设匹配、分派路由、调用与健康检查分支。

运行：python3 scripts/test_skill.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
import urllib.error
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import call_model
import mode
import route


def make_http_error(code, body):
    fp = io.BytesIO(body.encode("utf-8"))
    return urllib.error.HTTPError("https://example.test/", code, "err", {}, fp)


class TestTomlParser(unittest.TestCase):
    def test_parse_basic_and_sections(self):
        text = '''
# comment
vision_model = "gemini-3.5-flash"
review_model = 'gemini-3.1-pro'
core = "self"

[providers.myproxy]
base_url = "https://proxy.example.com/v1"
env = "MY_KEY"
'''
        cfg = call_model.parse_toml_simple(text)
        self.assertEqual(cfg["vision_model"], "gemini-3.5-flash")
        self.assertEqual(cfg["review_model"], "gemini-3.1-pro")
        self.assertEqual(cfg["core"], "self")
        self.assertEqual(cfg["providers"]["myproxy"]["base_url"], "https://proxy.example.com/v1")
        self.assertEqual(cfg["providers"]["myproxy"]["env"], "MY_KEY")


class TestProviderResolution(unittest.TestCase):
    def setUp(self):
        self.providers = call_model.build_providers({})

    def test_go_default(self):
        # Go 订阅是默认 provider：Go 目录模型自动匹配到 opencode-go
        for model in (
            "deepseek-v4-flash", "deepseek-v4-pro", "qwen3.8-max", "qwen3.7-max",
            "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus", "minimax-m3",
            "minimax-m2.7", "minimax-m2.5", "kimi-k3", "kimi-k2.7-code",
            "kimi-k2.6", "kimi-k2.5", "glm-5.2", "glm-5.1", "glm-5",
            "mimo-v2.5", "mimo-v2.5-pro", "mimo-v2-pro", "mimo-v2-omni",
            "gpt-5.6-luna", "grok-4.5", "hy3", "hy3-preview",
        ):
            self.assertEqual(
                call_model.resolve_provider(model, self.providers),
                ("opencode-go", model),
            )

    def test_zen_prefix(self):
        # gemini/claude/gpt-5 等 Zen 目录模型仍走 opencode-zen
        self.assertEqual(
            call_model.resolve_provider("gemini-3.5-flash", self.providers),
            ("opencode-zen", "gemini-3.5-flash"),
        )
        self.assertEqual(
            call_model.resolve_provider("claude-opus-5", self.providers),
            ("opencode-zen", "claude-opus-5"),
        )
        self.assertEqual(
            call_model.resolve_provider("gpt-5.5", self.providers),
            ("opencode-zen", "gpt-5.5"),
        )

    def test_dashscope_prefix(self):
        self.assertEqual(
            call_model.resolve_provider("qwen-vl-max", self.providers),
            ("dashscope", "qwen-vl-max"),
        )
        self.assertEqual(
            call_model.resolve_provider("qwen3-vl-plus", self.providers),
            ("dashscope", "qwen3-vl-plus"),
        )

    def test_gemini_official_fallback(self):
        # gemini-2.x 不在 zen 预设，回退到官方 gemini provider
        self.assertEqual(
            call_model.resolve_provider("gemini-2.5-flash", self.providers),
            ("gemini", "gemini-2.5-flash"),
        )

    def test_explicit_provider_syntax(self):
        self.assertEqual(
            call_model.resolve_provider("gemini:gemini-2.5-flash", self.providers),
            ("gemini", "gemini-2.5-flash"),
        )
        self.assertEqual(
            call_model.resolve_provider("myproxy:foo", self.providers),
            ("myproxy", "foo"),
        )

    def test_unknown_model(self):
        with self.assertRaises(call_model.ConfigError):
            call_model.resolve_provider("totally-unknown-model-xyz", self.providers)


class TestRoute(unittest.TestCase):
    def test_image_prompt(self):
        result = route.classify("帮我看看这张截图，然后给出方案", [])
        self.assertTrue(result["needs_vision"])
        self.assertFalse(result["needs_review"])

    def test_review_request(self):
        result = route.classify("请评审一下我的方案", [])
        self.assertTrue(result["needs_review"])

    def test_plain_text(self):
        result = route.classify("帮我写一份周报", [])
        self.assertFalse(result["needs_vision"])
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["core"], "self")

    def test_image_file_forces_vision(self):
        result = route.classify("直接回答即可", ["/tmp/a.png"])
        self.assertTrue(result["needs_vision"])

    def test_review_negation(self):
        result = route.classify("不用评审，直接写方案", [])
        self.assertFalse(result["needs_review"])

    def test_vision_negation(self):
        result = route.classify("不用识图，直接回答", [])
        self.assertFalse(result["needs_vision"])

    def test_both(self):
        result = route.classify("看图后给方案，并评审", ["/tmp/a.png"])
        self.assertTrue(result["needs_vision"])
        self.assertTrue(result["needs_review"])


class TestCallModel(unittest.TestCase):
    def test_build_messages_text(self):
        msgs = call_model.build_messages("sys", "hi", [])
        self.assertEqual(msgs[0]["content"], "sys")
        self.assertEqual(msgs[1]["content"], "hi")

    def test_build_messages_image(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG fake")
            path = f.name
        try:
            msgs = call_model.build_messages("sys", "转写", [path])
            content = msgs[1]["content"]
            self.assertEqual(content[0]["type"], "text")
            self.assertEqual(content[1]["type"], "image_url")
            self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        finally:
            os.unlink(path)

    def test_extract_text(self):
        body = {"choices": [{"message": {"content": "评审意见"}}]}
        self.assertEqual(call_model.extract_text(body), "评审意见")

    def test_get_key_precedence(self):
        saved = {name: os.environ.get(name) for name in ("VISION_API_KEY", "OPENCODE_API_KEY")}
        os.environ["VISION_API_KEY"] = "vision-key"
        os.environ["OPENCODE_API_KEY"] = "zen-key"
        try:
            providers = call_model.build_providers({})
            self.assertEqual(call_model.get_key("opencode-zen", providers, "vision"), "vision-key")
            self.assertEqual(call_model.get_key("opencode-zen", providers, "review"), "zen-key")
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def test_missing_key(self):
        providers = call_model.build_providers({})
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                call_model.get_key("opencode-zen", providers, "custom")

    def test_format_credits_error(self):
        err = make_http_error(
            401,
            '{"type":"error","error":{"type":"CreditsError","message":"Insufficient balance."}}',
        )
        self.assertIn("余额不足", call_model.format_http_error(err))

    def test_format_http_error(self):
        err = make_http_error(404, '{"error":"model not found"}')
        self.assertIn("404", call_model.format_http_error(err))

    def test_api_success(self):
        body = {"choices": [{"message": {"content": "OK"}}], "usage": {}}
        with unittest.mock.patch("call_model.urllib.request.urlopen") as mock:
            mock.return_value.__enter__.return_value.read.return_value = json.dumps(body).encode("utf-8")
            resp = call_model.api_post(
                "https://x/chat/completions",
                {"Authorization": "Bearer k"},
                {"model": "m"},
                30,
            )
        self.assertEqual(call_model.extract_text(resp), "OK")
        request = mock.call_args.args[0]
        header_keys = {key.lower() for key in request.headers}
        self.assertIn("user-agent", header_keys)

    def test_plan_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("方案内容")
            path = f.name
        try:
            args = types.SimpleNamespace(prompt=None, prompt_file=None, plan=path)
            self.assertEqual(call_model.build_prompt("review", args), "方案内容")
        finally:
            os.unlink(path)

    def test_missing_config(self):
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, "config.toml")
            rc = call_model.main(["--role", "vision", "--prompt", "x", "--config", config])
            self.assertEqual(rc, 3)

    def test_missing_key_exit2(self):
        with tempfile.TemporaryDirectory() as d:
            config = os.path.join(d, "config.toml")
            Path(config).write_text('vision_model = "gemini-3.5-flash"\n', encoding="utf-8")
            with unittest.mock.patch.dict(os.environ, {}, clear=True):
                rc = call_model.main(["--role", "vision", "--prompt", "x", "--config", config])
            self.assertEqual(rc, 2)


class TestCheckKey(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("OPENCODE_API_KEY")
        os.environ["OPENCODE_API_KEY"] = "test-key"

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("OPENCODE_API_KEY", None)
        else:
            os.environ["OPENCODE_API_KEY"] = self.saved

    def _args(self):
        return types.SimpleNamespace(provider=None, model=None, timeout=30)

    @staticmethod
    def _http_resp(body):
        resp = unittest.mock.MagicMock()
        resp.__enter__ = unittest.mock.MagicMock(return_value=resp)
        resp.__exit__ = unittest.mock.MagicMock(return_value=False)
        resp.status = 200
        resp.read.return_value = body
        return resp

    def test_balance_fail(self):
        def fake_urlopen(req, timeout=None):
            if req.method == "GET":
                return self._http_resp(
                    json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode("utf-8")
                )
            raise make_http_error(
                401,
                '{"type":"error","error":{"type":"CreditsError","message":"Insufficient balance."}}',
            )

        providers = call_model.build_providers({})
        stderr = io.StringIO()
        with unittest.mock.patch("call_model.urllib.request.urlopen", side_effect=fake_urlopen):
            with contextlib.redirect_stderr(stderr):
                rc = call_model.run_check_key(self._args(), {}, providers)
        self.assertEqual(rc, 1)
        self.assertIn("余额不足", stderr.getvalue())

    def test_success(self):
        def fake_urlopen(req, timeout=None):
            if req.method == "GET":
                return self._http_resp(json.dumps({"data": [{"id": "a"}]}).encode("utf-8"))
            return self._http_resp(
                json.dumps({"choices": [{"message": {"content": "ping"}}]}).encode("utf-8")
            )

        providers = call_model.build_providers({})
        stdout = io.StringIO()
        with unittest.mock.patch("call_model.urllib.request.urlopen", side_effect=fake_urlopen):
            with contextlib.redirect_stdout(stdout):
                rc = call_model.run_check_key(self._args(), {}, providers)
        self.assertEqual(rc, 0)
        self.assertIn("鉴权成功", stdout.getvalue())
        self.assertIn("订阅探测通过：OpenCode Go 订阅有效", stdout.getvalue())

    def test_missing_key_exit2(self):
        providers = call_model.build_providers({})
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            rc = call_model.run_check_key(self._args(), {}, providers)
        self.assertEqual(rc, 2)


class TestModeResolution(unittest.TestCase):
    def test_default_auto_no_config(self):
        self.assertEqual(mode.resolve_mode({}, "codex"), "auto")
        self.assertEqual(mode.resolve_mode({}, None), "auto")

    def test_host_override_precedence(self):
        cfg = {"mode": "auto", "hosts": {"workbudy": {"mode": "manual"}}}
        self.assertEqual(mode.resolve_mode(cfg, "workbudy"), "manual")
        self.assertEqual(mode.resolve_mode(cfg, "codex"), "auto")  # 未列出 → 顶层
        self.assertEqual(mode.resolve_mode(cfg, None), "auto")

    def test_top_level_manual_fallback(self):
        cfg = {"mode": "manual"}
        self.assertEqual(mode.resolve_mode(cfg, "codex"), "manual")
        self.assertEqual(mode.resolve_mode(cfg, None), "manual")

    def test_old_config_without_mode(self):
        cfg = {"vision_model": "qwen3.8-max", "review_model": "glm-5.2", "core": "self"}
        self.assertEqual(mode.resolve_mode(cfg, "codex"), "auto")


class TestRouteMode(unittest.TestCase):
    def _run(self, config_text, prompt, extra=None):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "config.toml")
            if config_text:
                Path(cfg_path).write_text(config_text, encoding="utf-8")
            argv = ["--prompt", prompt, "--config", cfg_path] + (extra or [])
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = route.main(argv)
            return rc, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_manual_not_explicit_disabled(self):
        rc, result, err = self._run('mode = "manual"\n', "帮我看看这张截图并给方案")
        self.assertEqual(rc, 0)
        self.assertEqual(result["mode"], "manual")
        self.assertFalse(result["enabled"])
        self.assertIn("manual 模式且未显式点名", err)

    def test_manual_explicit_enabled(self):
        rc, result, err = self._run(
            'mode = "manual"\n', "用 multimodal-orchestrator 看这张截图", ["--explicit"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["mode"], "manual")
        self.assertTrue(result["explicit"])
        self.assertTrue(result["enabled"])
        self.assertNotIn("enabled=false", err)

    def test_auto_enabled(self):
        rc, result, err = self._run('mode = "auto"\n', "帮我看看这张截图并给方案")
        self.assertEqual(rc, 0)
        self.assertEqual(result["mode"], "auto")
        self.assertTrue(result["enabled"])
        self.assertTrue(result["needs_vision"])

    def test_host_override_in_route(self):
        cfg = 'mode = "manual"\n\n[hosts.codex]\nmode = "auto"\n'
        _, result_codex, _ = self._run(cfg, "看图", ["--host", "codex"])
        self.assertEqual(result_codex["mode"], "auto")
        self.assertTrue(result_codex["enabled"])
        _, result_wb, _ = self._run(cfg, "看图", ["--host", "workbudy"])
        self.assertEqual(result_wb["mode"], "manual")
        self.assertFalse(result_wb["enabled"])

    def test_backward_compat_no_mode_no_host(self):
        rc, result, err = self._run(None, "帮我写一份周报")
        self.assertEqual(rc, 0)
        self.assertEqual(result["mode"], "auto")
        self.assertTrue(result["enabled"])
        self.assertFalse(result["needs_vision"])
        self.assertFalse(result["needs_review"])
        self.assertEqual(result["core"], "self")

    def test_old_config_format_no_mode(self):
        rc, result, _ = self._run(
            'vision_model = "qwen3.8-max"\nreview_model = "glm-5.2"\ncore = "self"\n',
            "看图给方案",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(result["mode"], "auto")
        self.assertTrue(result["enabled"])

    def test_manual_mention_enabled(self):
        rc, result, err = self._run('mode = "manual"\n', "用 @multimodal-orchestrator 看这张截图")
        self.assertEqual(rc, 0)
        self.assertTrue(result["mention_detected"])
        self.assertTrue(result["explicit"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["clean_prompt"], "用 multimodal-orchestrator 看这张截图")
        self.assertNotIn("enabled=false", err)

    def test_manual_plain_name_enabled(self):
        rc, result, _ = self._run('mode = "manual"\n', "用 multimodal-orchestrator 写个方案")
        self.assertEqual(rc, 0)
        self.assertTrue(result["mention_detected"])
        self.assertTrue(result["enabled"])

    def test_manual_negated_mention_disabled(self):
        rc, result, err = self._run('mode = "manual"\n', "不要用 multimodal-orchestrator 处理")
        self.assertEqual(rc, 0)
        self.assertFalse(result["mention_detected"])
        self.assertFalse(result["explicit"])
        self.assertFalse(result["enabled"])
        self.assertIn("manual 模式且未显式点名", err)


class TestMentionDetection(unittest.TestCase):
    def test_symbol_mentions(self):
        for prompt in (
            "用 $multimodal-orchestrator 看这张图",
            "用 @multimodal-orchestrator 看这张图",
            "用 @ multimodal-orchestrator 看这张图",
            "用 $  multimodal-orchestrator 看这张图",
            "用 @Multimodal-Orchestrator 看这张图",
        ):
            self.assertTrue(route.detect_mention(prompt), prompt)

    def test_chinese_mention(self):
        self.assertTrue(route.detect_mention("用多模态编排处理这张图"))

    def test_plain_name_mention(self):
        self.assertTrue(route.detect_mention("用 multimodal-orchestrator 看这张图"))

    def test_boundary_and_unrelated_not_mention(self):
        self.assertFalse(route.detect_mention("xmultimodal-orchestratory 处理一下"))
        self.assertFalse(route.detect_mention("看看这张图，给个方案"))
        self.assertFalse(route.detect_mention(""))

    def test_negation_not_mention(self):
        for prompt in (
            "不要用 multimodal-orchestrator 处理",
            "不用 multimodal-orchestrator 了",
            "别用 multimodal-orchestrator",
            "别再用 multimodal-orchestrator",
            "不需要 multimodal-orchestrator",
        ):
            self.assertFalse(route.detect_mention(prompt), prompt)

    def test_discussion_not_mention(self):
        for prompt in (
            "什么是 multimodal-orchestrator？",
            "介绍一下 multimodal-orchestrator",
            "讲讲 multimodal-orchestrator",
            "说说 multimodal-orchestrator",
            "评价一下 multimodal-orchestrator",
        ):
            self.assertFalse(route.detect_mention(prompt), prompt)

    def test_clean_mentions(self):
        self.assertEqual(
            route.clean_mentions("用 @multimodal-orchestrator 看这张图"),
            "用 multimodal-orchestrator 看这张图",
        )
        self.assertEqual(
            route.clean_mentions("用 $ multimodal-orchestrator 处理"),
            "用 multimodal-orchestrator 处理",
        )
        self.assertEqual(route.clean_mentions("帮我看看这张图"), "帮我看看这张图")
        self.assertEqual(route.clean_mentions(""), "")


if __name__ == "__main__":
    unittest.main()
