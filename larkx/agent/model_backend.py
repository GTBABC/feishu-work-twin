"""CLI and API adapters behind the same structured-generation interface."""

import asyncio
import base64
import json
import mimetypes
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import requests
from jsonschema import ValidationError, validate

from .context import bounded_text, context_budget
from .backend import hidden_process_options
from .model_connections import executable


def structured(text, schema):
    text = str(text or "").strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    elif text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()
    if schema:
        try:
            value = json.loads(text)
            validate(value, schema)
        except (ValueError, TypeError, ValidationError):
            raise RuntimeError("模型返回的数据格式不符合要求，未执行后续操作") from None
        return json.dumps(value, ensure_ascii=False)
    return text


def image_data(path):
    path = Path(path)
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return mime, base64.b64encode(path.read_bytes()).decode()


def api_request(config, text, images, schema, output_tokens=8192):
    protocol, model = config["protocol"], config["model"]
    instruction = "只处理提供的对话资料，不调用工具。"
    if schema:
        instruction += (
            "严格输出符合以下 JSON Schema 的 JSON 对象，不加 Markdown："
            + json.dumps(schema, ensure_ascii=False)
        )
    headers = {"Content-Type": "application/json"}
    if protocol == "openai":
        headers["Authorization"] = "Bearer " + config["api_key"]
        content = [{"type": "text", "text": text}]
        for path in images:
            mime, data = image_data(path)
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{data}"},
                }
            )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": content if images else text},
            ],
            "stream": False,
        }
        output_key = (
            "max_completion_tokens" if config["provider"] == "openai" else "max_tokens"
        )
        payload[output_key] = output_tokens
        # JSON object mode is more portable than strict schema mode. All results
        # are validated locally before business code can apply model actions.
        if schema:
            payload["response_format"] = {"type": "json_object"}
        endpoint = "/chat/completions"
    elif protocol == "anthropic":
        headers.update(
            {"x-api-key": config["api_key"], "anthropic-version": "2023-06-01"}
        )
        content = [{"type": "text", "text": text}]
        for path in images:
            mime, data = image_data(path)
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                }
            )
        payload = {
            "model": model,
            "system": instruction,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": output_tokens,
        }
        endpoint = "/messages"
    else:
        headers["x-goog-api-key"] = config["api_key"]
        parts = [{"text": text}]
        for path in images:
            mime, data = image_data(path)
            parts.append({"inline_data": {"mime_type": mime, "data": data}})
        payload = {
            "system_instruction": {"parts": [{"text": instruction}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"maxOutputTokens": output_tokens},
        }
        if schema:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        endpoint = (
            "/models/"
            + quote(model.removeprefix("models/"), safe="")
            + ":generateContent"
        )
    return config["base_url"] + endpoint, headers, payload


def response_text(data, protocol):
    try:
        if protocol == "openai":
            message = data["choices"][0]["message"]
            if message.get("tool_calls"):
                raise ValueError()
            return message["content"]
        if protocol == "anthropic":
            if any(part.get("type") == "tool_use" for part in data["content"]):
                raise ValueError()
            return "".join(
                part.get("text", "")
                for part in data["content"]
                if part.get("type") == "text"
            )
        return "".join(
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
            if not part.get("thought")
        )
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("模型未返回可用结果，请检查模型和接口配置") from None


async def generate_api(config, prompt, images, schema, settings):
    if not config["api_key"] and config["provider"] != "custom":
        raise RuntimeError("请先设置此服务商的 API Key")
    url, headers, payload = api_request(
        config, prompt, images, schema, settings["api_output_tokens"]
    )
    deadline = time.monotonic() + settings["codex_timeout"]
    for attempt in range(settings["network_retries"] + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("模型生成超时，未发送消息")

        def perform():
            with requests.Session() as session:
                # Refuse redirects: a configured endpoint cannot forward its key
                # to a different host without an explicit configuration change.
                return session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(min(10, remaining), remaining),
                    allow_redirects=False,
                )

        try:
            response = await asyncio.wait_for(asyncio.to_thread(perform), remaining)
        except (requests.RequestException, asyncio.TimeoutError):
            if (
                attempt < settings["network_retries"]
                and deadline - time.monotonic() > settings["retry_delay"]
            ):
                await asyncio.sleep(settings["retry_delay"])
                continue
            raise RuntimeError("模型接口连接失败或超时，请检查地址和网络") from None
        if (
            response.status_code in (408, 429, 500, 502, 503, 504)
            and attempt < settings["network_retries"]
            and deadline - time.monotonic() > settings["retry_delay"]
        ):
            await asyncio.sleep(settings["retry_delay"])
            continue
        if not 200 <= response.status_code < 300:
            detail = {
                401: "密钥无效或已过期",
                403: "没有访问权限",
                404: "接口或模型不存在",
                429: "额度或请求频率受限",
            }.get(response.status_code, "请求未成功")
            raise RuntimeError(f"模型接口返回 HTTP {response.status_code}：{detail}")
        try:
            data = response.json()
        except ValueError:
            raise RuntimeError("模型接口返回内容不是 JSON") from None
        return structured(response_text(data, config["protocol"]), schema)


def cli_arguments(config, folder):
    provider = config["provider"]
    if provider == "claude":
        args = [
            "-p",
            "--output-format",
            "json",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--no-session-persistence",
        ]
    elif provider == "cursor":
        cursor = folder / ".cursor"
        cursor.mkdir()
        (cursor / "cli.json").write_text(
            json.dumps(
                {"permissions": {"deny": ["Shell(*)", "Read(*)", "Write(*)", "Mcp(*)"]}}
            ),
            encoding="utf-8",
        )
        args = ["-p", "--mode", "ask", "--output-format", "json"]
    elif provider == "qwen":
        args = [
            "-p",
            "只处理标准输入中的请求",
            "--output-format",
            "json",
            "--safe-mode",
            "--max-tool-calls",
            "0",
        ]
    elif provider == "gemini":
        policy = folder / "deny-tools.toml"
        policy.write_text(
            '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n',
            encoding="utf-8",
        )
        args = [
            "-p",
            "只处理标准输入中的请求",
            "--output-format",
            "json",
            "--policy",
            str(policy),
        ]
    else:
        (folder / "opencode.json").write_text(
            json.dumps({"permission": {"*": "deny"}, "mcp": {}}), encoding="utf-8"
        )
        # A file avoids placing private message content in the process command line.
        args = [
            "run",
            "--format",
            "json",
            "--file",
            str(folder / "input.txt"),
            "只处理附带的请求文本，不使用工具",
        ]
    if config["model"]:
        args += ["--model", config["model"]]
    return args


def cli_text(stdout):
    text = stdout.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        # OpenCode emits typed JSONL events.
        try:
            events = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            raise RuntimeError("CLI 返回格式无法识别，请检查版本") from None
        if any(event.get("type") == "error" for event in events):
            raise RuntimeError("CLI 调用失败，请检查工具登录状态")
        return "".join(
            event.get("part", {}).get("text", "")
            for event in events
            if event.get("type") == "text"
        )
    if isinstance(data, dict):
        if data.get("is_error") or data.get("error"):
            raise RuntimeError("CLI 调用失败，请检查工具登录状态")
        if isinstance(data.get("structured_output"), dict):
            return json.dumps(data["structured_output"], ensure_ascii=False)
        for key in ("result", "response"):
            if isinstance(data.get(key), str):
                return data[key]
        if data.get("type") == "text":
            return data.get("part", {}).get("text", "")
    raise RuntimeError("CLI 未返回可用文本，请检查工具版本")


async def communicate(args, text, cwd, timeout, env=None):
    if os.name == "nt" and Path(args[0]).suffix.lower() in (".cmd", ".bat"):
        # Only executable/adapter-controlled flags go through cmd. Private prompts
        # stay on stdin or in a temporary file. Reject shell metacharacters.
        if any(any(c in arg for c in "&|<>^%!\r\n") for arg in args):
            raise RuntimeError("此 Windows CLI 启动参数需要原生可执行程序")
        args = [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(args),
        ]
    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name != "nt",
        **hidden_process_options(),
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(text.encode()), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    **hidden_process_options(),
                )
                await killer.wait()
                if process.returncode is None:
                    process.kill()
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError("CLI 调用失败，请检查登录状态、模型和工具版本")
    return stdout


async def generate_cli(config, prompt, schema, settings):
    program = executable(config)
    if not program:
        raise RuntimeError("未找到所选 CLI，请安装或指定程序路径")
    instruction = "只生成回答，不调用工具、不执行命令。"
    if schema:
        instruction += "严格输出符合以下 JSON Schema 的 JSON 对象：" + json.dumps(
            schema, ensure_ascii=False
        )
    with tempfile.TemporaryDirectory(prefix="larkx-model-") as temporary:
        folder = Path(temporary)
        text = instruction + "\n" + prompt
        if config["provider"] == "opencode":
            (folder / "input.txt").write_text(text, encoding="utf-8")
        args = [program, *cli_arguments(config, folder)]
        env = dict(os.environ)
        if config["provider"] == "opencode":
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
                {"permission": {"*": "deny"}, "mcp": {}}
            )
        try:
            output = await communicate(
                args, text, temporary, settings["codex_timeout"], env
            )
        except asyncio.TimeoutError:
            raise RuntimeError("CLI 生成超时，未发送消息") from None
        except OSError:
            raise RuntimeError("无法启动 CLI，请检查程序路径") from None
    return structured(cli_text(output), schema)


class ModelRouter:
    name = "models"
    returns_reply = True

    def __init__(self, connections):
        self.connections = connections
        self.codex = {}
        self.locks = {}
        self.history = {}

    def reset_session(self, session_key):
        for backend in self.codex.values():
            backend.reset_session(session_key)
        for key in list(self.history):
            if key[-1] == session_key:
                self.history.pop(key, None)

    async def run(
        self,
        session_key,
        prompt,
        image_paths=(),
        response_schema=None,
        history_text=None,
        settings=None,
        **kwargs,
    ):
        from .codex_backend import CodexBackend
        from ..settings import resolve_settings

        settings = dict(settings if settings is not None else resolve_settings()[0])
        active = self.connections.active()
        config = self.connections.profile(**active, include_secret=True)
        # Capture configuration and secrets before the first await. A UI switch
        # cannot change the provider of an already-running invocation.
        identity = (config["kind"], config["provider"], session_key)
        lock = self.locks.setdefault(identity, asyncio.Lock())
        async with lock:
            if config["kind"] == "cli" and config["provider"] == "codex":
                path = executable(config)
                if not path:
                    raise RuntimeError("未找到 Codex CLI，请安装或指定程序路径")
                backend = self.codex.get(path)
                if backend is None:
                    backend = self.codex[path] = CodexBackend(executable=path)
                settings.update(
                    codex_model=config["model"],
                    codex_reasoning_effort=config["reasoning"],
                )
                return await backend.run(
                    session_key,
                    prompt,
                    image_paths=image_paths,
                    response_schema=response_schema,
                    history_text=history_text,
                    settings=settings,
                    **kwargs,
                )
            images = (
                list(image_paths)[-settings["model_image_limit"] :]
                if settings["model_image_limit"]
                else []
            )
            if images and not config["vision"]:
                raise RuntimeError(
                    "当前连接未启用图片理解，请选择支持图片的 API 或 Codex"
                )
            history = self.history.get(identity, []) if history_text is not None else []
            budget = (
                context_budget(settings)
                - 512
                - (
                    len(json.dumps(response_schema, ensure_ascii=False))
                    if response_schema
                    else 0
                )
            )
            turns = settings["control_history_turns"]
            history = list(history[-turns:]) if turns else []
            history_budget = min(
                settings["control_history_chars"], budget - len(prompt) - 100
            )
            while (
                history
                and len(json.dumps(history, ensure_ascii=False)) > history_budget
            ):
                history.pop(0)
            if history:
                prompt = (
                    "此前的简短对话："
                    + json.dumps(history, ensure_ascii=False)
                    + "\n当前请求："
                    + prompt
                )
            if len(prompt) > budget:
                raise RuntimeError("输入超过上下文预算，请缩减背景或提高字数上限")
            result = (
                await generate_api(config, prompt, images, response_schema, settings)
                if config["kind"] == "api"
                else await generate_cli(config, prompt, response_schema, settings)
            )
            if history_text is not None and turns:
                parsed = json.loads(result) if response_schema else {}
                self.history[identity] = (
                    history
                    + [
                        {
                            "input": bounded_text(history_text, 2000),
                            "answer": bounded_text(parsed.get("answer", result), 2000),
                        }
                    ]
                )[-turns:]
            elif not turns:
                self.history.pop(identity, None)
            return result

    async def list_models(self, body):
        config = self.connections.prepare(
            body, include_secret=True, require_model=False
        )
        if config["kind"] != "api":
            raise ValueError("CLI 可填写模型名称或留空使用工具默认模型")
        protocol = config["protocol"]
        headers = (
            {"x-goog-api-key": config["api_key"]}
            if protocol == "gemini"
            else {"x-api-key": config["api_key"], "anthropic-version": "2023-06-01"}
            if protocol == "anthropic"
            else {"Authorization": "Bearer " + config["api_key"]}
        )

        def perform():
            with requests.Session() as session:
                response = session.get(
                    config["base_url"].rstrip("/") + "/models",
                    headers=headers,
                    timeout=(5, 15),
                    allow_redirects=False,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        "无法获取模型列表，请手动填写模型 ID 或接入点 ID"
                    )
                try:
                    data = response.json()
                    rows = data.get("models" if protocol == "gemini" else "data", [])
                    values = [
                        row.get(
                            "name" if protocol == "gemini" else "id", ""
                        ).removeprefix("models/")
                        for row in rows
                    ]
                    return {
                        "models": sorted(
                            {
                                value
                                for value in values
                                if isinstance(value, str) and 0 < len(value) <= 200
                            }
                        )[:500]
                    }
                except (ValueError, TypeError, AttributeError):
                    raise RuntimeError("模型列表格式无法识别，可手动填写") from None

        try:
            return await asyncio.wait_for(asyncio.to_thread(perform), 20)
        except (requests.RequestException, asyncio.TimeoutError):
            raise RuntimeError(
                "获取模型列表失败，请检查连接或手动填写模型名称"
            ) from None

    async def check(self, body):
        config = self.connections.prepare(body, include_secret=True)
        if config["kind"] == "api":
            return {
                "installed": None,
                "login": "已设置密钥" if config["api_key"] else "未设置密钥",
                "message": "点击测试调用验证接口和模型",
            }
        program = executable(config)
        if not program:
            return {
                "installed": False,
                "login": "未检测",
                "message": "未找到程序，请安装或填写路径",
            }
        login = "无法确认"
        with tempfile.TemporaryDirectory(prefix="larkx-check-") as temporary:
            try:
                await communicate([program, "--version"], "", temporary, 10)
                args = {
                    "codex": ["login", "status"],
                    "cursor": ["status"],
                    "claude": ["auth", "status", "--json"],
                }.get(config["provider"])
                if args:
                    await communicate([program, *args], "", temporary, 10)
                    login = "已登录"
            except RuntimeError:
                login = "请检查安装或登录"
            except (OSError, asyncio.TimeoutError):
                login = "检查未完成"
        return {
            "installed": True,
            "path": program,
            "login": login,
            "message": "测试调用可进一步验证所选模型",
        }

    async def test(self, body, settings):
        config = self.connections.prepare(body, include_secret=True)
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean", "enum": [True]}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        prompt = '只回复 JSON：{"ok":true}'
        settings = {
            **settings,
            "codex_timeout": min(settings["codex_timeout"], 30),
            "network_retries": 0,
        }
        if config["kind"] == "cli" and config["provider"] == "codex":
            from .codex_backend import CodexBackend

            path = executable(config)
            if not path:
                raise ValueError("未找到 Codex CLI")
            backend = CodexBackend(executable=path)
            settings.update(
                codex_model=config["model"], codex_reasoning_effort=config["reasoning"]
            )
            result = await backend.run(
                "connection-test", prompt, response_schema=schema, settings=settings
            )
            structured(result, schema)
        elif config["kind"] == "cli":
            await generate_cli(config, prompt, schema, settings)
        else:
            await generate_api(config, prompt, [], schema, settings)
        return {"ok": True, "message": "测试成功：模型返回了有效的结构化结果"}
