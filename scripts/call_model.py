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
import argparse
import base64
import io
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
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


# ---------- 配置 ----------

def parse_toml_simple(text):
    """解析本 skill 使用的极简 TOML 子集：注释、[section]、key = "value"。"""
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
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif val == "true":
            val = True
        elif val == "false":
            val = False
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


def build_payload(model, messages, temperature, max_tokens):
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
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
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--provider", help="显式指定 provider（--check-key 用）")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON 响应")
    parser.add_argument("--check-key", action="store_true", help="只做健康检查（鉴权+余额探测）")
    parser.add_argument("--list-presets", action="store_true", help="列出内置 provider 预设")
    args = parser.parse_args(argv)

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
    payload = build_payload(model, messages, args.temperature, args.max_tokens)
    pres = providers[provider]
    url = f"{pres['base_url']}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        body = post_payload(url, headers, payload, args.timeout)
    except urllib.error.HTTPError as exc:
        print(format_http_error(exc), file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc.reason}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"响应解析失败: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(body, ensure_ascii=False))
    else:
        text = extract_text(body)
        if not text:
            finish = body.get("choices", [{}])[0].get("finish_reason")
            hint = "（finish_reason=length：reasoning 模型吃满 token，请加大 --max-tokens）" if finish == "length" else ""
            print(f"响应中无文本内容{hint}: {json.dumps(body, ensure_ascii=False)[:300]}", file=sys.stderr)
            return 1
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
