import asyncio
import json
import tempfile
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from larkx.agent.model_backend import (
    ModelRouter,
    api_request,
    cli_arguments,
    cli_text,
    generate_api,
    generate_cli,
    response_text,
    structured,
)
from larkx.agent.model_connections import API, CLI, ModelConnections, SecretVault
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from larkx.workhub.store import HubStore
from tests.test_hub import FakeBackend, ANALYSIS

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class ModelConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage("sqlite:///" + str(Path(self.temp) / "models.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.hub = HubStore(self.storage)
        self.manager = ModelConnections(
            self.hub, SecretVault(Path(self.temp) / "vault")
        )
        self.settings = self.hub.settings()

    def api_config(self, provider="deepseek", **changes):
        return self.manager.prepare(
            {
                "kind": "api",
                "provider": provider,
                "model": "fixture-model",
                "api_key": "secret-fixture",
                **changes,
            },
            include_secret=True,
        )

    def test_legacy_codex_settings_and_provider_profiles_are_preserved(self):
        self.hub.update_settings(
            {"codex_model": "old-model", "codex_reasoning_effort": "medium"}
        )
        self.assertEqual(self.manager.active(), {"kind": "cli", "provider": "codex"})
        self.assertEqual(self.manager.profile("cli", "codex")["model"], "old-model")
        self.manager.save(
            {
                "kind": "cli",
                "provider": "codex",
                "model": "custom-codex",
                "reasoning": "high",
            }
        )
        self.manager.save(
            {
                "kind": "api",
                "provider": "deepseek",
                "model": "api-model",
                "api_key": "secret-fixture",
            }
        )
        restored = ModelConnections(self.hub, self.manager.vault)
        self.assertEqual(restored.active(), {"kind": "api", "provider": "deepseek"})
        self.assertEqual(restored.profile("cli", "codex")["model"], "custom-codex")
        self.assertEqual(restored.profile("cli", "codex")["reasoning"], "high")
        self.assertEqual(
            restored.profile("api", "deepseek", True)["api_key"], "secret-fixture"
        )
        serialized = self.hub.setting("model_profile:api:deepseek")
        self.assertNotIn("secret-fixture", serialized)
        self.assertNotIn("secret-fixture", json.dumps(restored.describe()))
        self.manager.save(
            {"kind": "api", "provider": "deepseek", "model": "changed", "api_key": ""}
        )
        self.assertEqual(
            restored.profile("api", "deepseek", True)["api_key"], "secret-fixture"
        )
        self.manager.save(
            {
                "kind": "api",
                "provider": "deepseek",
                "model": "changed",
                "clear_key": True,
            }
        )
        self.assertFalse(restored.profile("api", "deepseek")["key_set"])

    def test_invalid_save_cannot_change_active_connection_or_saved_secret(self):
        self.manager.save(
            {
                "kind": "api",
                "provider": "deepseek",
                "model": "valid",
                "api_key": "secret-fixture",
            }
        )
        for changes in (
            {"base_url": "https://secret@evil.example/v1"},
            {"base_url": "http://remote.example"},
            {"model": "-injected"},
            {"vision": "yes"},
            {"api_key": "bad\nkey"},
            {"provider": "unknown"},
        ):
            with self.assertRaises(ValueError):
                self.manager.save(
                    {
                        "kind": "api",
                        "provider": "deepseek",
                        "model": "invalid",
                        "api_key": "must-not-save",
                        **changes,
                    }
                )
        self.assertEqual(self.manager.profile("api", "deepseek")["model"], "valid")
        self.assertEqual(
            self.manager.profile("api", "deepseek", True)["api_key"], "secret-fixture"
        )

    def test_protocol_payloads_keep_keys_in_headers_and_validate_responses(self):
        for provider in API:
            config = self.api_config(
                provider,
                **(
                    {"base_url": "http://127.0.0.1:9999/v1"}
                    if provider == "custom"
                    else {}
                ),
            )
            url, headers, payload = api_request(config, "fixture input", [], SCHEMA)
            self.assertNotIn("secret-fixture", url)
            self.assertNotIn("secret-fixture", json.dumps(payload))
            self.assertIn("secret-fixture", headers.values()) if config[
                "protocol"
            ] != "openai" else self.assertIn("Bearer secret-fixture", headers.values())
        self.assertEqual(
            response_text({"choices": [{"message": {"content": "OK"}}]}, "openai"), "OK"
        )
        self.assertEqual(
            response_text({"content": [{"type": "text", "text": "OK"}]}, "anthropic"),
            "OK",
        )
        self.assertEqual(
            response_text(
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "private reasoning", "thought": True},
                                    {"text": "OK"},
                                ]
                            }
                        }
                    ]
                },
                "gemini",
            ),
            "OK",
        )
        for invalid in ('{"ok":"yes"}', '{"other":true}', "not json"):
            with self.assertRaises(RuntimeError):
                structured(invalid, SCHEMA)
        self.assertEqual(
            json.loads(structured('```json\n{"ok":true}\n```', SCHEMA)), {"ok": True}
        )

    async def test_api_errors_are_redacted_redirects_refused_and_success_validated(
        self,
    ):
        config = self.api_config()
        good = SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": '{"ok":true}'}}]},
        )
        with patch("requests.Session.post", return_value=good) as post:
            result = await generate_api(
                config, "fixture only", [], SCHEMA, self.settings
            )
            self.assertEqual(json.loads(result), {"ok": True})
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
        bad = SimpleNamespace(status_code=401, text="secret-fixture sensitive response")
        with patch("requests.Session.post", return_value=bad):
            with self.assertRaises(RuntimeError) as error:
                await generate_api(config, "fixture only", [], SCHEMA, self.settings)
            self.assertNotIn("secret-fixture", str(error.exception))
            self.assertIn("401", str(error.exception))

    async def test_switch_during_generation_cannot_redirect_inflight_call(self):
        self.manager.save(
            {
                "kind": "api",
                "provider": "deepseek",
                "model": "first",
                "api_key": "first-key",
            }
        )
        router = ModelRouter(self.manager)
        started, release = asyncio.Event(), asyncio.Event()
        captured = []

        async def call(config, *args):
            captured.append(dict(config))
            started.set()
            await release.wait()
            return '{"ok":true}'

        with patch("larkx.agent.model_backend.generate_api", side_effect=call):
            task = asyncio.create_task(
                router.run(
                    "chat", "fixture", response_schema=SCHEMA, settings=self.settings
                )
            )
            await started.wait()
            self.manager.save(
                {
                    "kind": "api",
                    "provider": "qwen",
                    "model": "second",
                    "api_key": "second-key",
                }
            )
            release.set()
            await task
            await router.run(
                "chat", "fixture", response_schema=SCHEMA, settings=self.settings
            )
        self.assertEqual(
            [(c["provider"], c["api_key"]) for c in captured],
            [("deepseek", "first-key"), ("qwen", "second-key")],
        )

    async def test_unavailable_cli_and_unsupported_images_fail_explicitly(self):
        router = ModelRouter(self.manager)
        with patch("larkx.agent.model_backend.executable", return_value=""):
            result = await router.check({"kind": "cli", "provider": "cursor"})
            self.assertFalse(result["installed"])
        self.manager.save(
            {
                "kind": "api",
                "provider": "deepseek",
                "model": "text",
                "api_key": "key",
                "vision": False,
            }
        )
        with self.assertRaisesRegex(RuntimeError, "图片"):
            await router.run(
                "chat", "fixture", image_paths=["test.png"], settings=self.settings
            )

    async def test_cli_envelopes_and_commands_run_without_message_in_arguments(self):
        for provider in CLI.keys() - {"codex"}:
            with tempfile.TemporaryDirectory() as temp:
                config = self.manager.profile("cli", provider)
                args = cli_arguments(config, Path(temp))
                self.assertNotIn("--force", args)
                self.assertNotIn("--yolo", args)
            with (
                patch(
                    "larkx.agent.model_backend.executable",
                    return_value="fixture-cli.exe",
                ),
                patch(
                    "larkx.agent.model_backend.communicate",
                    new_callable=AsyncMock,
                    return_value=json.dumps(
                        {"result": json.dumps({"ok": True})}
                    ).encode(),
                ) as communicate,
            ):
                await generate_cli(
                    config, "sensitive fixture text", SCHEMA, self.settings
                )
                self.assertNotIn(
                    "sensitive fixture text", " ".join(communicate.call_args.args[0])
                )
                self.assertIn("sensitive fixture text", communicate.call_args.args[1])
        self.assertEqual(cli_text(b'{"response":"hello"}'), "hello")
        self.assertEqual(
            cli_text(b'{"type":"text","part":{"text":"hi"}}\n{"type":"finish"}'), "hi"
        )

    async def test_model_discovery_works_before_model_is_selected(self):
        router = ModelRouter(self.manager)
        response = SimpleNamespace(
            status_code=200, json=lambda: {"data": [{"id": "a"}, {"id": "b"}]}
        )
        with patch("requests.Session.get", return_value=response):
            result = await router.list_models(
                {"kind": "api", "provider": "deepseek", "api_key": "fixture"}
            )
        self.assertEqual(result["models"], ["a", "b"])

    def test_configuration_routes_never_echo_api_keys_and_checks_do_not_activate(self):
        runtime = HubRuntime(self.storage, FakeBackend())
        self.addAsyncCleanup(runtime.stop)
        runtime.models = self.manager
        with TestClient(create_app(runtime, start_bridge=False)) as client:
            payload = {
                "kind": "api",
                "provider": "deepseek",
                "model": "fixture",
                "api_key": "secret-fixture",
            }
            response = client.put(
                "/api/models/connection", headers={"X-Workhub": "1"}, json=payload
            )
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("secret-fixture", response.text)
            self.assertNotIn("secret-fixture", client.get("/api/settings").text)
            self.assertNotIn("secret-fixture", client.get("/api/state").text)
            self.assertEqual(
                client.put("/api/models/connection", json=payload).status_code, 403
            )
            with patch(
                "larkx.agent.model_backend.generate_api",
                new_callable=AsyncMock,
                return_value='{"ok":true}',
            ) as call:
                response = client.post(
                    "/api/models/test",
                    headers={"X-Workhub": "1"},
                    json={**payload, "provider": "qwen"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(call.call_args.args[1], '只回复 JSON：{"ok":true}')
            self.assertEqual(self.manager.active()["provider"], "deepseek")

    async def test_loopback_api_runs_complete_analysis_and_preserves_actions(self):
        analysis = {
            **ANALYSIS,
            "reply_to_message_id": "",
            "mention_user_id": "",
            "attention_task_id": 0,
            "tasks": [
                {
                    **ANALYSIS["tasks"][0],
                    "task_id": 0,
                    "evidence_ids": ["fixture-message"],
                }
            ],
        }
        captured = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(
                    (
                        self.path,
                        json.loads(
                            self.rfile.read(int(self.headers["Content-Length"]))
                        ),
                    )
                )
                body = json.dumps(
                    {"choices": [{"message": {"content": json.dumps(analysis)}}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.manager.save(
            {
                "kind": "api",
                "provider": "custom",
                "model": "fixture",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        )
        runtime = HubRuntime(self.storage, ModelRouter(self.manager))
        self.addAsyncCleanup(runtime.stop)
        msg = {
            "chat_id": "fixture",
            "chat_type": 1,
            "from_id": "fixture-user",
            "msg_id": "fixture-message",
            "content": "fixture request",
        }
        self.hub.observe(msg)
        self.hub.update_settings({"default_private_reply": "auto"})
        sent = []

        async def send(*args):
            sent.append(args)

        await runtime.engine.process("fixture:", msg, msg["content"], [], send)
        self.assertEqual(captured[0][0], "/v1/chat/completions")
        self.assertEqual(captured[0][1]["response_format"], {"type": "json_object"})
        self.assertEqual(len(sent), 1)
        self.assertTrue(self.hub.memories("fixture"))
        self.assertEqual(self.hub.tasks()[0]["title"], ANALYSIS["tasks"][0]["title"])

    async def test_timed_out_cli_process_is_terminated(self):
        from larkx.agent.model_backend import communicate

        with self.assertRaises(asyncio.TimeoutError):
            await communicate(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                "",
                self.temp,
                0.1,
            )
