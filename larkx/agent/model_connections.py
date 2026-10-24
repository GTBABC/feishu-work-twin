"""Provider presets, per-provider configuration and a locally encrypted key vault."""

import json
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ..config import DATA_DIR
from ..secrets import SecretVault as LocalSecretVault
from ..workhub.models import Setting

CLI = {
    "codex": {"label": "Codex", "commands": ["codex"], "reasoning": True},
    "cursor": {"label": "Cursor CLI", "commands": ["agent", "cursor-agent"]},
    "claude": {"label": "Claude Code", "commands": ["claude"]},
    "qwen": {"label": "Qwen Code", "commands": ["qwen"]},
    "gemini": {"label": "Gemini CLI", "commands": ["gemini"]},
    "opencode": {"label": "OpenCode", "commands": ["opencode"]},
}
API = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "protocol": "openai",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "protocol": "openai",
        "key_env": "DASHSCOPE_API_KEY",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "protocol": "openai",
        "key_env": "ZAI_API_KEY",
    },
    "kimi": {
        "label": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "protocol": "openai",
        "key_env": "MOONSHOT_API_KEY",
    },
    "doubao": {
        "label": "豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "protocol": "openai",
        "key_env": "ARK_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "protocol": "openai",
        "key_env": "OPENAI_API_KEY",
    },
    "claude": {
        "label": "Claude",
        "base_url": "https://api.anthropic.com/v1",
        "protocol": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
    },
    "gemini": {
        "label": "Gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "protocol": "gemini",
        "key_env": "GEMINI_API_KEY",
    },
    "custom": {
        "label": "自定义兼容接口",
        "base_url": "",
        "protocol": "openai",
        "key_env": "",
    },
}
REASONING = {
    "minimal": "最少",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "最高",
}


class SecretVault(LocalSecretVault):
    def __init__(self, directory=DATA_DIR):
        super().__init__(directory)


def executable(config):
    path = config.get("path") or (
        os.environ.get("LARKX_CODEX_PATH", "") if config["provider"] == "codex" else ""
    )
    if path:
        candidate = Path(path).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else ""
    for command in CLI[config["provider"]]["commands"]:
        candidate = shutil.which(command)
        if candidate:
            return candidate
    return ""


class ModelConnections:
    def __init__(self, hub, vault=None):
        self.hub = hub
        self.vault = vault or SecretVault()

    def active(self):
        return json.loads(
            self.hub.setting("model_active", '{"kind":"cli","provider":"codex"}')
        )

    def profile(self, kind, provider, include_secret=False):
        presets = CLI if kind == "cli" else API if kind == "api" else {}
        if provider not in presets:
            raise ValueError("请选择有效的接入方式和服务商")
        legacy = self.hub.settings()
        defaults = {
            "kind": kind,
            "provider": provider,
            "model": legacy["codex_model"]
            if kind == "cli" and provider == "codex"
            else "",
            "path": "",
            "base_url": presets[provider].get("base_url", ""),
            "protocol": presets[provider].get("protocol", ""),
            "reasoning": legacy["codex_reasoning_effort"],
            "vision": kind == "cli" and provider == "codex",
        }
        saved = json.loads(self.hub.setting(f"model_profile:{kind}:{provider}", "{}"))
        encrypted = saved.pop("secret", "")
        config = {**defaults, **saved}
        config["key_set"] = bool(
            encrypted or os.environ.get(presets[provider].get("key_env", ""))
        )
        if include_secret:
            config["api_key"] = (
                self.vault.decrypt(encrypted)
                if encrypted
                else os.environ.get(presets[provider].get("key_env", ""), "")
            )
        return config

    def prepare(self, body, include_secret=False, require_model=True):
        if not isinstance(body, dict) or body.keys() - {
            "kind",
            "provider",
            "model",
            "path",
            "base_url",
            "reasoning",
            "vision",
            "api_key",
            "clear_key",
        }:
            raise ValueError("模型连接配置格式无效")
        kind, provider = body.get("kind"), body.get("provider")
        replacing_key = bool(body.get("api_key")) or body.get("clear_key") is True
        config = self.profile(
            kind, provider, include_secret=include_secret and not replacing_key
        )
        if include_secret:
            config.setdefault("api_key", "")
        for key in ("model", "path", "base_url", "reasoning", "api_key"):
            if key in body:
                if (
                    not isinstance(body[key], str)
                    or len(body[key]) > (4096 if key == "api_key" else 1000)
                    or any(c in body[key] for c in "\r\n\x00")
                ):
                    raise ValueError("模型连接字段格式无效")
                if key != "api_key" or body[key].strip():
                    config[key] = body[key].strip()
        for key in ("vision", "clear_key"):
            if key in body and not isinstance(body[key], bool):
                raise ValueError("模型能力配置格式无效")
        config["vision"] = body.get("vision", config["vision"])
        if kind == "cli" and provider != "codex":
            config["vision"] = (
                False  # CLI image formats differ; never silently drop attachments.
            )
        if config["reasoning"] not in REASONING:
            raise ValueError("推理强度选项无效")
        if body.get("clear_key"):
            config["api_key"] = ""
        if kind == "api":
            address = urlparse(config["base_url"])
            if (
                address.scheme not in ("https", "http")
                or not address.hostname
                or address.username
                or address.password
                or address.query
                or address.fragment
            ):
                raise ValueError("请填写不含密钥和查询参数的 API 基础地址")
            if address.scheme == "http" and address.hostname not in (
                "localhost",
                "127.0.0.1",
                "::1",
            ):
                raise ValueError("远程 API 请使用 HTTPS")
            if (require_model and not config["model"]) or config["model"].startswith(
                "-"
            ):
                raise ValueError("请填写模型名称")
            config["base_url"] = config["base_url"].rstrip("/")
        if config["model"].startswith("-"):
            raise ValueError("模型名称不能以 - 开头")
        return config

    def save(self, body):
        config = self.prepare(body)
        key = f"model_profile:{config['kind']}:{config['provider']}"
        saved = json.loads(self.hub.setting(key, "{}"))
        secret = saved.get("secret", "")
        if body.get("clear_key"):
            secret = ""
        elif body.get("api_key", "").strip():
            secret = self.vault.encrypt(body["api_key"].strip())
        stored = {
            name: config[name]
            for name in ("model", "path", "base_url", "reasoning", "vision")
        }
        if secret:
            stored["secret"] = secret
        with self.hub.storage.Session() as session:
            for name, value in (
                (key, stored),
                (
                    "model_active",
                    {"kind": config["kind"], "provider": config["provider"]},
                ),
            ):
                row = session.get(Setting, name) or Setting(key=name)
                row.value = json.dumps(value, ensure_ascii=False)
                session.add(row)
            session.commit()
        return self.profile(config["kind"], config["provider"])

    def describe(self):
        active = self.active()
        return {
            "active": active,
            "profiles": {
                kind: {provider: self.profile(kind, provider) for provider in presets}
                for kind, presets in (("cli", CLI), ("api", API))
            },
            "presets": {"cli": CLI, "api": API},
            "reasoning": REASONING,
        }
