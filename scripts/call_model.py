#!/usr/bin/env python3
"""通用 OpenAI 兼容模型调用器（vision / review 共用；纯标准库，无第三方依赖）。

本脚本是 multimodal-orchestrator skill 的模型调用层：
  - 根据模型名自动匹配 provider（默认 OpenCode Go 订阅），支持 "provider:model" 显式指定
  - API key 只从环境变量读取（不落盘、不写入任何配置）
  - 支持图片输入（base64 data URL），供视觉模块使用
  - 内置 review 角色的评审提示词，供评审模块使用
  - --check-key 做只读健康检查（鉴权 + 余额探测），用于首次配置引导

用法示例：
  export OPENCODE_API_KEY=你的key
  python3 call_model.py --role vision --image a.png --prompt "请转写这张图"
  python3 call_model.py --role review --plan 方案.md --image a.png
  python3 call_model.py --model gemini-3.5-flash --prompt "你好"
  python3 call_model.py --check-key
  python3 call_model.py --list-presets

退出码：0=成功 1=API/网络错误 2=缺少 API key 3=配置/参数错误
"""
from __future__ import annotations

import argparse
import ast
import base64
import io
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_ROOT / "config.toml"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

BILLING_HINT = (
    "OpenCode Zen 余额不足：请先到 opencode 工作区充值后重试 "
    "（https://opencode.ai）"
)

PRESETS = {
    "opencode-go": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "env": "OPENCODE_API_KEY",
        "note": "OpenCode Go（订阅制，默认 provider；OpenAI 兼容，一份 key 驱动 vision+review）",
        "prefixes": [
            "deepseek-v4-", "qwen3.8-", "qwen3.7-", "qwen3.6-", "qwen3.5-",
            "minimax-", "kimi-", "glm-", "mimo-v2", "gpt-5.6-", "grok-", "hy3",
        ],
    },
    "opencode-zen": {
        "base_url": "https://opencode.ai/zen/v1",
        "env": "OPENCODE_API_KEY",
        "note": "OpenCode Zen（OpenAI 兼容，默认 provider）",
        "prefixes": [
            "gemini-3.6", "gemini-3.5", "gemini-3.1", "gemini-3",
            "claude-", "gpt-5", "gpt-4", "deepseek-v", "glm-", "kimi-",
            "grok-", "minimax-", "mimo", "north-", "ling-", "nemotron-",
            "longcat-", "big-pickle", "laguna-", "qwen3",
        ],
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env": "GEMINI_API_KEY",
        "note": "Google Gemini 官方 OpenAI 兼容端点",
        "prefixes": ["gemini-"],
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": "DASHSCOPE_API_KEY",
        "note": "阿里云百炼（qwen-vl 系列识图）",
        "prefixes": [
            "qwen-vl", "qwen2.5-vl", "qwen3-vl", "qwq-",
            "qwen-max", "qwen-plus", "qwen-turbo",
        ],
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
        "note": "OpenAI 官方",
        "prefixes": [],
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "note": "DeepSeek 官方（无视觉，仅文本）",
        "prefixes": [],
    },
}

PROBE_MODELS = {"opencode-go": "deepseek-v4-flash", "opencode-zen": "gemini-3.5-flash"}

MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}

DEFAULT_REVIEW_SYSTEM = (
    "你是一位严格的方案评审专家。请审查给定方案的：正确性、完整性、可执行性、"
    "风险与遗漏，指出具体错误并给出修改建议。若提供了图片，请结合图片核验方案中"
    "涉及的事实是否与图片一致（如数值、文字、布局、状态），指出不一致之处。"
    "用中文回答，先给结论，再分条列出问题，最后给出修订建议。"
)


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ModelCallResult:
    text: str
    status: str
    finish_reason: str | None
    completion_tokens: int
    reasoning_tokens: int
    saw_reasoning: bool
    first_reasoning_seconds: float | None = None
    first_content_seconds: float | None = None
    elapsed_seconds: float = 0.0

    def to_dict(self):
        return asdict(self)


# ---------- 配置 ----------

def _strip_toml_comment(value):
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in "\"'":
            quote = None if quote == char else (char if quote is None else quote)
        elif char == "#" and quote is None:
            return value[:index].rstrip()
    return value.strip()


def _parse_toml_value(value):
    value = _strip_toml_comment(value)
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    if value == "true":
        return True
    if value == "false":
        return False
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][+-]?\d+)?", value):
        return float(value)
    if value.startswith("[") and value.endswith("]"):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    return value


def parse_toml_simple(text):
    """解析本 skill 使用的 TOML 子集，支持嵌套段、标量与简单列表。"""
    cfg = {}
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = _parse_toml_value(val.strip())
        node = cfg
        if section:
            for part in section.split("."):
                node = node.setdefault(part, {})
        node[key] = val
    return cfg


def load_config(path=None):
    path = Path(path) if path else DEFAULT_CONFIG
    if not path.exists():
        return {}
    return parse_toml_simple(path.read_text(encoding="utf-8"))


def build_providers(cfg):
    providers = {name: dict(p) for name, p in PRESETS.items()}
    configured = cfg.get("providers", {})
    if not isinstance(configured, dict):
        raise ConfigError("providers 必须使用 [providers.<name>] 配置段")
    for name, opts in configured.items():
        if not isinstance(opts, dict):
            raise ConfigError(f"providers.{name} 必须是配置段")
        base_url = opts.get("base_url", "")
        env = opts.get("env", "OPENCODE_API_KEY")
        if not isinstance(base_url, str) or not base_url:
            raise ConfigError(f"providers.{name}.base_url 必须是非空字符串")
        if not isinstance(env, str) or not env:
            raise ConfigError(f"providers.{name}.env 必须是非空字符串")
        providers[name] = {
            "base_url": base_url,
            "env": env,
            "note": "自定义 provider",
            "prefixes": [],
        }
    return providers


# ---------- 模型解析 ----------

def resolve_provider(model, providers, explicit=None):
    """返回 (provider_name, model)。支持 'provider:model'、显式 provider、前缀自动匹配。"""
    if not model or any(not (ch.isalnum() or ch in "._:/-") for ch in model):
        raise ConfigError(f"非法模型名: {model!r}")
    if ":" in model:
        provider_name, _, rest = model.partition(":")
        if provider_name and rest:
            if provider_name not in providers:
                raise ConfigError(f"未知 provider: {provider_name}")
            return provider_name, rest
    if explicit:
        if explicit not in providers:
            raise ConfigError(f"未知 provider: {explicit}")
        return explicit, model
    candidates = []
    for name, pres in providers.items():
        for prefix in pres.get("prefixes", []):
            candidates.append((len(prefix), prefix, name))
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, prefix, name in candidates:
        if model.startswith(prefix):
            return name, model
    raise ConfigError(
        f"无法自动匹配模型 '{model}' 的 provider：请在 config.toml 中写 'provider:model'"
        f"（如 'opencode-zen:{model}'），或运行 --list-presets 查看预设"
    )


def get_key(provider, providers, role="custom"):
    envs = []
    if role == "vision":
        envs.append("VISION_API_KEY")
    elif role == "review":
        envs.append("REVIEW_API_KEY")
    envs.append(providers[provider]["env"])
    for env_name in envs:
        value = os.environ.get(env_name)
        if value:
            return value
    raise KeyError("、".join(envs))


# ---------- 请求构造 ----------

def data_url(path):
    suffix = Path(path).suffix.lower()
    mime = MIME_BY_EXT.get(suffix) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_messages(system_text, prompt_text, image_paths):
    if image_paths:
        content = [{"type": "text", "text": prompt_text}]
        for path in image_paths:
            content.append({"type": "image_url", "image_url": {"url": data_url(path)}})
        user = {"role": "user", "content": content}
    else:
        user = {"role": "user", "content": prompt_text}
    return [{"role": "system", "content": system_text}, user]


def budget_fields(token_semantics, budget):
    if not budget:
        return {}
    if token_semantics == "answer_only":
        return {"max_tokens": budget}
    if token_semantics == "separate":
        return {"max_tokens": max(1, budget // 3), "max_completion_tokens": budget}
    return {"max_completion_tokens": budget}


def build_payload(
    model,
    messages,
    temperature,
    max_tokens,
    *,
    max_completion_tokens=None,
    reasoning_effort=None,
    reasoning_field="reasoning_effort",
    stream=False,
):
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if max_completion_tokens:
        payload["max_completion_tokens"] = max_completion_tokens
    if reasoning_effort:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", reasoning_field or ""):
            raise ConfigError(f"非法 reasoning_field：{reasoning_field!r}")
        payload[reasoning_field] = reasoning_effort
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return payload


def read_text_source(value):
    """支持字面文本、'@文件路径'、'-'（stdin）。"""
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        return Path(value[1:]).read_text(encoding="utf-8")
    return value


def read_plan_source(value):
    """--plan 取值：'-'（stdin）、已存在文件路径、或直接文本。"""
    if value == "-":
        return sys.stdin.read()
    if Path(value).exists():
        return Path(value).read_text(encoding="utf-8")
    return value


def resolve_system(role, args):
    if args.system:
        return read_text_source(args.system)
    if role == "vision":
        prompt_file = SKILL_ROOT / "references" / "vision_subagent_prompt.md"
        if prompt_file.exists():
            return prompt_file.read_text(encoding="utf-8")
    if role == "review":
        return DEFAULT_REVIEW_SYSTEM
    return ""


def build_prompt(role, args):
    parts = []
    if args.prompt:
        parts.append(args.prompt)
    if args.prompt_file:
        parts.append(read_text_source(args.prompt_file))
    if args.plan:
        parts.append(read_plan_source(args.plan))
    prompt = "\n\n".join(parts).strip()
    if role == "vision" and not prompt:
        prompt = "请按系统提示词要求，穷尽式转写这张/这些图片。"
    return prompt


def extract_text(body):
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def _reasoning_present(value):
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, list):
        return bool(value)
    return value is not None


def _usage_counts(usage):
    usage = usage if isinstance(usage, dict) else {}
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
    details = usage.get("completion_tokens_details", usage.get("output_tokens_details", {}))
    details = details if isinstance(details, dict) else {}
    reasoning = details.get("reasoning_tokens", usage.get("reasoning_tokens", 0))
    return int(completion or 0), int(reasoning or 0)


def _result_status(text, finish_reason, saw_reasoning, completion_tokens, requested_budget):
    if finish_reason == "length":
        if text:
            return "incomplete_review"
        near_budget = not requested_budget or completion_tokens >= int(requested_budget * 0.9)
        if saw_reasoning and near_budget:
            return "reasoning_budget_exhausted"
        return "output_budget_exhausted"
    return "success" if text else "empty_response"


def normalize_response(body, *, requested_budget=None, elapsed_seconds=0.0):
    choice = body.get("choices", [{}])[0] if isinstance(body, dict) else {}
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    text = extract_text(body) if isinstance(body, dict) else ""
    reasoning = None
    if isinstance(message, dict):
        reasoning = next(
            (message.get(name) for name in ("reasoning_content", "reasoning", "thinking") if message.get(name) is not None),
            None,
        )
    completion, reasoning_tokens = _usage_counts(body.get("usage", {}) if isinstance(body, dict) else {})
    saw_reasoning = _reasoning_present(reasoning) or reasoning_tokens > 0
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    return ModelCallResult(
        text=text,
        status=_result_status(text, finish, saw_reasoning, completion, requested_budget),
        finish_reason=finish,
        completion_tokens=completion,
        reasoning_tokens=reasoning_tokens,
        saw_reasoning=saw_reasoning,
        elapsed_seconds=round(float(elapsed_seconds), 3),
    )


def classify_api_error(status_code, body):
    lowered = (body or "").lower()
    context_markers = (
        "maximum context length", "context length exceeded", "context_length_exceeded",
        "too many input tokens", "prompt is too long", "input tokens exceed",
    )
    if status_code in (400, 413, 422) and any(marker in lowered for marker in context_markers):
        return "input_context_overflow"
    return "api_error"


def _delta_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(
            item.get("text", "") for item in value if isinstance(item, dict)
        )
    return ""


def parse_stream_response(
    lines,
    *,
    requested_budget=None,
    started_at=None,
    clock=time.monotonic,
    heartbeat=None,
    heartbeat_seconds=30,
):
    started = clock() if started_at is None else float(started_at)
    last_heartbeat = started
    first_reasoning = None
    first_content = None
    saw_reasoning = False
    content_parts = []
    finish_reason = None
    usage = {}
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", "replace").strip()
        else:
            line = str(raw_line).strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        now = clock()
        choices = event.get("choices", []) if isinstance(event, dict) else []
        if choices:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            delta = choice.get("delta", {}) if isinstance(choice.get("delta", {}), dict) else {}
            reasoning = next(
                (delta.get(name) for name in ("reasoning_content", "reasoning", "thinking") if delta.get(name) is not None),
                None,
            )
            if _reasoning_present(reasoning):
                saw_reasoning = True
                if first_reasoning is None:
                    first_reasoning = now - started
            content = _delta_text(delta.get("content"))
            if content:
                content_parts.append(content)
                if first_content is None:
                    first_content = now - started
            if choice.get("finish_reason") is not None:
                finish_reason = choice.get("finish_reason")
        if isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if heartbeat and now - last_heartbeat >= heartbeat_seconds:
            heartbeat(now - started, bool(content_parts))
            last_heartbeat = now
    ended = clock()
    completion, reasoning_tokens = _usage_counts(usage)
    saw_reasoning = saw_reasoning or reasoning_tokens > 0
    text = "".join(content_parts)
    return ModelCallResult(
        text=text,
        status=_result_status(text, finish_reason, saw_reasoning, completion, requested_budget),
        finish_reason=finish_reason,
        completion_tokens=completion,
        reasoning_tokens=reasoning_tokens,
        saw_reasoning=saw_reasoning,
        first_reasoning_seconds=None if first_reasoning is None else round(first_reasoning, 3),
        first_content_seconds=None if first_content is None else round(first_content, 3),
        elapsed_seconds=round(ended - started, 3),
    )


# ---------- HTTP ----------

def api_post(url, headers, payload, timeout):
    headers = {**headers, "User-Agent": USER_AGENT}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_chat(
    *,
    base_url,
    api_key,
    model,
    messages,
    timeout,
    output_budget=None,
    token_semantics="unknown",
    reasoning_effort=None,
    reasoning_field="reasoning_effort",
    temperature=0.3,
    stream=True,
    heartbeat=None,
    heartbeat_seconds=30,
):
    """执行一次规范化 chat/completions 调用，不返回原始隐藏推理。"""
    fields = budget_fields(token_semantics, output_budget)
    payload = build_payload(
        model,
        messages,
        temperature,
        fields.get("max_tokens"),
        max_completion_tokens=fields.get("max_completion_tokens"),
        reasoning_effort=reasoning_effort,
        reasoning_field=reasoning_field,
        stream=stream,
    )
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if not stream:
        started = time.monotonic()
        body = post_payload(url, headers, payload, timeout)
        return normalize_response(
            body,
            requested_budget=output_budget,
            elapsed_seconds=time.monotonic() - started,
        )
    def open_stream(stream_payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(stream_payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(request, timeout=timeout)

    started = time.monotonic()
    try:
        response_context = open_stream(payload)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        if "temperature" in raw and payload.get("temperature") not in (None, 1):
            response_context = open_stream(dict(payload, temperature=1))
        else:
            raise urllib.error.HTTPError(
                exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(raw.encode("utf-8"))
            )
    with response_context as response:
        return parse_stream_response(
            response,
            requested_budget=output_budget,
            started_at=started,
            heartbeat=heartbeat,
            heartbeat_seconds=heartbeat_seconds,
        )


def api_get(url, headers, timeout):
    headers = {**headers, "User-Agent": USER_AGENT}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def post_payload(url, headers, payload, timeout):
    """POST chat/completions；kimi 等模型仅接受 temperature=1 时自动重试一次。"""
    try:
        return api_post(url, headers, payload, timeout)
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        temp = payload.get("temperature")
        if "temperature" in raw and temp not in (None, 1):
            retry = dict(payload, temperature=1)
            return api_post(url, headers, retry, timeout)
        rebuilt = urllib.error.HTTPError(
            exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(raw.encode("utf-8"))
        )
        raise rebuilt


def format_http_error(exc):
    body = ""
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        pass
    if "CreditsError" in body or "Insufficient balance" in body:
        return BILLING_HINT
    return f"API 错误 {getattr(exc, 'code', '?')}: {body[:500] or exc}"


# ---------- 健康检查 ----------

def run_check_key(args, cfg, providers):
    provider = args.provider or "opencode-go"
    if provider not in providers:
        print(f"未知 provider: {provider}（可用：{', '.join(providers)}）", file=sys.stderr)
        return 3
    pres = providers[provider]
    try:
        key = get_key(provider, providers, "custom")
    except KeyError as exc:
        print(f"缺少 API key：请先设置环境变量 {exc}", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {key}"}
    try:
        status, raw = api_get(f"{pres['base_url']}/models", headers, args.timeout)
    except urllib.error.HTTPError as exc:
        print(format_http_error(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc.reason}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"鉴权失败 HTTP {status}: {raw[:300]}", file=sys.stderr)
        return 1
    try:
        count = len(json.loads(raw).get("data", []))
    except Exception:
        count = 0
    print(f"鉴权成功：provider={provider}（{pres['base_url']}），可见模型 {count} 个")
    probe = args.model or cfg.get("vision_model") or PROBE_MODELS.get(provider)
    if not probe:
        print("未指定 --model，跳过余额探测（如需探测请加 --model <模型名>）")
        return 0
    try:
        payload = build_payload(probe, [{"role": "user", "content": "ping"}], 0.0, 1)
        post_payload(
            f"{pres['base_url']}/chat/completions",
            {**headers, "Content-Type": "application/json"},
            payload,
            args.timeout,
        )
    except urllib.error.HTTPError as exc:
        print(format_http_error(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc.reason}", file=sys.stderr)
        return 1
    if provider == "opencode-go":
        print("订阅探测通过：OpenCode Go 订阅有效，可以正常调用")
    else:
        print("余额探测通过：可以正常调用")
    return 0


# ---------- 入口 ----------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--role", choices=["vision", "review", "custom"], default="custom")
    parser.add_argument("--model", help="模型名；缺省读 config.toml 的 vision_model/review_model")
    parser.add_argument("--prompt", help="用户提示词文本")
    parser.add_argument("--prompt-file", help="提示词文件路径或 '-'（stdin）")
    parser.add_argument("--plan", help="方案文件路径、'-'（stdin）或直接文本（review 用）")
    parser.add_argument("--image", action="append", default=[], help="图片路径，可多次")
    parser.add_argument("--system", help="系统提示词：字面文本、'@文件' 或 '-'")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--max-tokens", type=int, default=None)
    parser.add_argument("--max-completion-tokens", type=int, default=None)
    parser.add_argument(
        "--token-semantics",
        choices=["answer_only", "combined", "separate", "unknown"],
        help="输出预算语义；未指定时按所用预算字段保守推断",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh", "max"],
    )
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream", action="store_true")
    stream_group.add_argument("--no-stream", dest="stream", action="store_false")
    parser.set_defaults(stream=None)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--provider", help="显式指定 provider（--check-key 用）")
    parser.add_argument("--json", action="store_true", help="输出不含隐藏推理的规范化 JSON")
    parser.add_argument("--check-key", action="store_true", help="只做健康检查（鉴权+余额探测）")
    parser.add_argument("--list-presets", action="store_true", help="列出内置 provider 预设")
    args = parser.parse_args(argv)
    if args.max_tokens and args.max_completion_tokens:
        parser.error("--max-tokens 与 --max-completion-tokens 不能同时指定")

    cfg = load_config(args.config)
    try:
        providers = build_providers(cfg)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3

    if args.list_presets:
        print(f"{'provider':<14}{'base_url':<52}{'env':<22}自动匹配前缀")
        for name, pres in providers.items():
            prefixes = "、".join(pres.get("prefixes", []))[:60]
            print(f"{name:<14}{pres['base_url']:<52}{pres['env']:<22}{prefixes}")
        return 0

    if args.check_key:
        return run_check_key(args, cfg, providers)

    role = args.role
    model = args.model
    if not model:
        model_key = {"vision": "vision_model", "review": "review_model"}.get(role)
        if role == "custom" or not model_key:
            print("custom 模式必须提供 --model", file=sys.stderr)
            return 3
        model = cfg.get(model_key)
        if not model:
            print(f"config.toml 缺少 {model_key}：请先运行首次引导（见 references/onboarding.md）", file=sys.stderr)
            return 3

    try:
        provider, model = resolve_provider(model, providers)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    if provider not in providers:
        print(f"未知 provider: {provider}", file=sys.stderr)
        return 3

    try:
        api_key = get_key(provider, providers, role)
    except KeyError as exc:
        print(f"缺少 API key：请设置环境变量 {exc}", file=sys.stderr)
        return 2

    system_text = resolve_system(role, args)
    prompt_text = build_prompt(role, args)
    if not prompt_text:
        print("缺少提示词：请提供 --prompt/--prompt-file/--plan", file=sys.stderr)
        return 3
    for image_path in args.image:
        if not Path(image_path).exists():
            print(f"图片不存在: {image_path}", file=sys.stderr)
            return 3

    messages = build_messages(system_text, prompt_text, args.image)
    pres = providers[provider]
    if args.max_completion_tokens:
        output_budget = args.max_completion_tokens
        token_semantics = args.token_semantics or "combined"
    elif args.max_tokens:
        output_budget = args.max_tokens
        token_semantics = args.token_semantics or "answer_only"
    elif role == "review":
        output_budget = 8192
        token_semantics = args.token_semantics or "combined"
    else:
        output_budget = None
        token_semantics = args.token_semantics or "unknown"
    use_stream = role == "review" if args.stream is None else args.stream

    def heartbeat(elapsed, has_content):
        stage = "正文生成中" if has_content else "推理中"
        print(f"评审仍在运行：{int(elapsed)} 秒（{stage}）", file=sys.stderr, flush=True)

    try:
        result = call_chat(
            base_url=pres["base_url"],
            api_key=api_key,
            model=model,
            messages=messages,
            timeout=args.timeout,
            output_budget=output_budget,
            token_semantics=token_semantics,
            reasoning_effort=args.reasoning_effort,
            temperature=args.temperature,
            stream=use_stream,
            heartbeat=heartbeat if use_stream else None,
        )
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        category = classify_api_error(getattr(exc, "code", 0), raw)
        if category == "input_context_overflow":
            print(f"input_context_overflow: {raw[:500]}", file=sys.stderr)
        else:
            rebuilt = urllib.error.HTTPError(
                exc.url, exc.code, exc.msg, exc.hdrs, io.BytesIO(raw.encode("utf-8"))
            )
            print(format_http_error(rebuilt), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc.reason}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"响应解析失败: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    elif result.status == "success":
        print(result.text)
    else:
        hints = {
            "reasoning_budget_exhausted": "推理已耗尽组合输出预算，未生成正文；这不是输入上下文溢出",
            "incomplete_review": "正文未完整生成，结果不能作为最终评审",
            "output_budget_exhausted": "输出预算已耗尽",
            "empty_response": "响应中无文本内容",
        }
        print(f"{result.status}: {hints.get(result.status, '评审调用失败')}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
