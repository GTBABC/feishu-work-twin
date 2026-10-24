import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from larkx import startup
from larkx.agent.codex_backend import CodexBackend
from larkx.agent.model_backend import ModelRouter, api_request
from larkx.agent.model_connections import ModelConnections, SecretVault
from larkx.settings import resolve_settings
from larkx.storage import Storage
from larkx.workhub.login import BrowserLogin
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from tests.test_hub import FakeBackend


class StartupSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.temp / "bootstrap" / "startup.json"
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "LARKX_STARTUP_PATH": str(self.config),
                    "LARKX_HOME": str(self.temp / "original"),
                    "LARKX_STORAGE_URL": "",
                    "LARKX_PORT": "8765",
                },
            )
        )
        self.storage = Storage("sqlite:///" + str(self.temp / "current.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.addAsyncCleanup(self.runtime.stop)

    def target(self, **changes):
        return {
            "data_dir": str(self.temp / "new"),
            "storage_url": "sqlite:///" + str(self.temp / "new" / "messages.db"),
            "port": 9876,
            **changes,
        }

    def test_default_data_directory_is_next_to_program_and_created_on_storage_start(
        self,
    ):
        program = self.temp / "程序目录"
        with (
            patch.dict(os.environ, {"LARKX_HOME": ""}),
            patch.object(startup, "__file__", str(program / "larkx" / "startup.py")),
        ):
            values = startup.effective()
            self.assertEqual(values["data_dir"], str(program / "data"))
            self.assertEqual(
                make_url(values["storage_url"]).database,
                str(program / "data" / "messages.db"),
            )
            self.assertTrue(startup.check({})["ok"])
            self.assertFalse(program.exists())
            storage = Storage(values["storage_url"])
            self.addCleanup(storage.engine.dispose)
            self.assertTrue((program / "data" / "messages.db").is_file())
            self.assertIn("messages", inspect(storage.engine).get_table_names())

    def test_fresh_process_default_does_not_follow_working_directory(self):
        root = Path(startup.__file__).resolve().parents[1]
        env = {**os.environ, "LARKX_HOME": "", "PYTHONPATH": str(root)}
        output = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "from larkx.startup import effective; import json; print(json.dumps(effective()))",
            ],
            cwd=self.temp,
            env=env,
            text=True,
            encoding="utf-8",
        )
        values = json.loads(output)
        self.assertEqual(values["data_dir"], str(root / "data"))
        self.assertEqual(
            make_url(values["storage_url"]).database, str(root / "data" / "messages.db")
        )
        self.assertFalse((self.temp / "data").exists())

    def test_encrypted_persistence_redaction_and_password_only_restart(self):
        old = self.target(storage_url="mysql+pymysql://user:old-password@localhost/db")
        startup.save(old)
        startup.save({**startup.describe(old)["values"], "password": "new-password"})
        stored = self.config.read_text(encoding="utf-8")
        self.assertNotIn("password", stored)
        self.assertNotIn("mysql", stored)
        self.assertIn("new-password", startup.effective()["storage_url"])
        info = startup.describe(old)
        self.assertTrue(info["restart_required"])
        self.assertNotIn("new-password", json.dumps(info))
        startup.save(info["values"])
        self.assertIn("new-password", startup.effective()["storage_url"])
        self.assertNotIn("old-password", json.dumps(info))

    def test_storage_check_never_creates_or_modifies_a_database(self):
        body = self.target()
        self.assertTrue(startup.check(body)["ok"])
        self.assertFalse((self.temp / "new").exists())
        current = self.storage.engine.url.database
        before = Path(current).read_bytes()
        self.assertTrue(
            startup.check(self.target(storage_url="sqlite:///" + current))["ok"]
        )
        self.assertEqual(Path(current).read_bytes(), before)
        broken = self.temp / "broken.db"
        broken.write_text("not sqlite", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SQLite"):
            startup.check(self.target(storage_url="sqlite:///" + str(broken)))

    def test_sqlite_file_and_tables_are_created_only_when_storage_starts(self):
        database = self.temp / "新目录" / "messages.db"
        body = {
            "storage_kind": "sqlite",
            "sqlite_path": str(database),
            "data_dir": str(self.temp / "new"),
            "port": 9876,
        }
        self.assertTrue(startup.check(body)["ok"])
        startup.save(body)
        self.assertFalse(database.exists())
        storage = Storage(startup.effective()["storage_url"])
        self.addCleanup(storage.engine.dispose)
        self.assertTrue(database.is_file())
        self.assertIn("messages", inspect(storage.engine).get_table_names())

    def mysql_fields(self, **changes):
        return {
            "storage_kind": "mysql",
            "mysql_host": "db.example.com",
            "mysql_port": 3307,
            "mysql_database": "业务库",
            "mysql_user": "user@:/#?",
            "password": "p@ss:/#?%中文",
            **changes,
        }

    def test_mysql_fields_round_trip_and_password_is_encrypted_and_retained(self):
        body = self.mysql_fields()
        startup.save(body)
        current = startup.effective()
        url = make_url(current["storage_url"])
        self.assertEqual(url.host, body["mysql_host"])
        self.assertEqual(url.port, 3307)
        self.assertEqual(url.database, body["mysql_database"])
        self.assertEqual(url.username, body["mysql_user"])
        self.assertEqual(url.password, body["password"])
        info = startup.describe(current)
        self.assertEqual(info["storage"]["mysql_user"], body["mysql_user"])
        self.assertNotIn(body["password"], json.dumps(info, ensure_ascii=False))
        self.assertNotIn(body["password"], self.config.read_text(encoding="utf-8"))
        startup.save(self.mysql_fields(password="", mysql_port=3308))
        url = make_url(startup.effective()["storage_url"])
        self.assertEqual(url.password, body["password"])
        self.assertEqual(url.port, 3308)
        startup.save(self.mysql_fields(password="replacement"))
        self.assertEqual(
            make_url(startup.effective()["storage_url"]).password, "replacement"
        )

    def test_mysql_check_uses_separate_account_and_password_fields(self):
        with patch("sqlalchemy.create_engine") as create:
            self.assertTrue(startup.check(self.mysql_fields())["ok"])
            url = create.call_args.args[0]
            self.assertEqual(url.username, self.mysql_fields()["mysql_user"])
            self.assertEqual(url.password, self.mysql_fields()["password"])
            create.return_value.connect.return_value.__enter__.return_value.execute.assert_called_once()
            create.return_value.dispose.assert_called_once()

    def test_invalid_structured_storage_is_rejected_without_saving(self):
        startup.save(self.target())
        before = self.config.read_bytes()
        for changes in (
            {"mysql_port": True},
            {"mysql_port": 0},
            {"mysql_port": 65536},
            {"mysql_port": 3306.5},
            {"mysql_port": "3306"},
            {"mysql_user": ""},
            {"mysql_host": "host\ninvalid"},
            {"mysql_host": "localhost/path"},
            {"mysql_host": "mysql://localhost"},
            {"mysql_database": None},
            {"password": 123},
            {"storage_kind": "other"},
            {"storage_url": "sqlite:///conflict.db"},
            {"sqlite_path": "unexpected.db"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                startup.save(self.mysql_fields(**changes))
            self.assertEqual(self.config.read_bytes(), before)

    def test_invalid_settings_do_not_replace_saved_config(self):
        startup.save(self.target())
        before = self.config.read_bytes()
        for changes in (
            {"port": True},
            {"port": 80},
            {"port": 9000.5},
            {"data_dir": "relative"},
            {"data_dir": str(self.config)},
            {"storage_url": "bad"},
            {"storage_url": "sqlite:///relative.db"},
            {"storage_url": "postgresql://localhost/db"},
            {"storage_url": "mysql+pymysql://user@localhost:99999/db"},
            {"unknown": 1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                startup.save(self.target(**changes))
            self.assertEqual(self.config.read_bytes(), before)

    def test_saved_configuration_is_used_by_a_fresh_process(self):
        startup.save(self.target())
        script = "from larkx.config import DATA_DIR, load_config; from larkx.startup import effective; import json; print(json.dumps([str(DATA_DIR),load_config()['storage_url'],effective()['port']]))"
        output = subprocess.check_output(
            [sys.executable, "-c", script], text=True, encoding="utf-8"
        )
        values = json.loads(output)
        self.assertEqual(values, list(self.target().values()))
        # The bootstrap file stays discoverable after switching data directories.
        self.assertFalse((self.temp / "new" / "startup.json").exists())
        output = subprocess.check_output(
            [sys.executable, "-m", "larkx", "web", "--print-url"],
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(output.strip(), "http://127.0.0.1:9876")
        from larkx.workhub import mcp_server

        with (
            patch.object(sys, "argv", ["mcp"]),
            patch.object(mcp_server, "RemoteAccess") as remote,
            patch.object(mcp_server, "create_mcp"),
        ):
            mcp_server.main()
            remote.assert_called_once_with("http://127.0.0.1:9876")

    def test_startup_routes_are_guarded_and_do_not_hot_switch_storage(self):
        with TestClient(
            create_app(self.runtime, start_bridge=False),
            base_url="http://127.0.0.1:8765",
        ) as client:
            headers = {"X-Workhub": "1"}
            self.assertEqual(
                client.put("/api/settings/startup", json=self.target()).status_code, 403
            )
            response = client.put(
                "/api/settings/startup", headers=headers, json=self.target()
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["restart_required"])
            self.assertEqual(
                self.runtime.storage.url, "sqlite:///" + str(self.temp / "current.db")
            )
            restored = client.post(
                "/api/settings/startup/reset", headers=headers, json={}
            ).json()
            self.assertFalse(restored["restart_required"])
            self.assertEqual(
                restored["values"]["storage_url"], self.runtime.storage.url
            )

    def test_single_instance_lock_prevents_duplicate_receivers(self):
        with startup.server_lock():
            with self.assertRaisesRegex(RuntimeError, "已在运行"):
                with startup.server_lock():
                    self.fail("second receiver acquired lock")
        with startup.server_lock():
            pass

    async def test_reconnect_rejects_busy_runtime_and_restarts_idle_bridge(self):
        with patch.object(self.runtime, "bridge", new_callable=AsyncMock) as bridge:
            async with self.runtime.model_slots.slot():
                with self.assertRaises(ValueError):
                    await self.runtime.reconnect()
            await self.runtime.reconnect()
            await asyncio.sleep(0)
            bridge.assert_awaited_once()

    async def test_login_keeps_current_credentials_until_validated(self):
        login = BrowserLogin()
        self.addCleanup(login.clear)
        ql = Mock(qr_content="fixture login qr")
        ql.session.cookies = {"session": "fixture"}
        ql.poll.return_value = {"next_step": "done", "step_info": {"status": 0}}

        def finish(auth):
            auth.path.write_text("fixture-credentials", encoding="utf-8")

        ql.finish.side_effect = finish
        target = self.temp / "credentials.json"
        target.write_text("old-credentials", encoding="utf-8")
        with (
            patch("larkx.workhub.login.QrLogin", return_value=ql),
            patch("larkx.workhub.login.DATA_DIR", self.temp),
            patch("larkx.config.CREDENTIALS_PATH", target),
            patch.object(self.runtime, "bridge", new_callable=AsyncMock),
        ):
            started = await login.start("lark", self.runtime)
            self.assertEqual(target.read_text(), "old-credentials")
            with self.assertRaises(ValueError):
                await login.poll("wrong-id", self.runtime)
            result = await login.poll(started["id"], self.runtime)
            self.assertTrue(result["done"])
            self.assertEqual(target.read_text(), "fixture-credentials")
            self.assertFalse(list(self.temp.glob("credentials.login-*")))

    async def test_message_limits_apply_to_real_send_paths_before_any_send(self):
        r = self.runtime
        r.hub.observe({"chat_id": "c", "from_id": "u", "chat_type": 1}, "同事", "同事")
        r.hub.update_settings(
            {"message_max_chars": 200, "send_image_limit": 1, "reply_suffix": "tail"}
        )
        sender = Mock(return_value="sent")
        r.client = SimpleNamespace(
            auth=SimpleNamespace(user_id="me"), send_text=sender, send_rich=sender
        )
        r.connection = "connected"
        with self.assertRaises(ValueError):
            await r.send("c", "x" * 197)
        r.hub.set_setting(
            "upload:image", json.dumps({"key": "key", "width": 10, "height": 10})
        )
        with self.assertRaises(ValueError):
            await r.send_parts(
                "c", [{"type": "image", "upload_id": "image"}] * 2, "blocked"
            )
        sender.assert_not_called()
        r.hub.update_settings({"upload_max_mb": 1})
        r.hub.set_setting(
            "upload:large",
            json.dumps(
                {"key": "key", "width": 10, "height": 10, "size": 2 * 1024 * 1024}
            ),
        )
        with self.assertRaisesRegex(ValueError, "大小上限"):
            await r.send_parts(
                "c", [{"type": "image", "upload_id": "large"}], "too-large"
            )
        sender.assert_not_called()
        self.assertEqual(
            r.status()["limits"],
            {"send_images": 1, "upload_bytes": 1024 * 1024, "message_chars": 200},
        )

    async def test_api_history_uses_configured_turns_and_output_caps(self):
        manager = ModelConnections(self.runtime.hub, SecretVault(self.temp / "vault"))
        manager.save(
            {
                "kind": "api",
                "provider": "deepseek",
                "api_key": "fixture",
                "model": "fixture",
            }
        )
        router = ModelRouter(manager)
        settings = {**resolve_settings()[0], "control_history_turns": 1}
        prompts = []

        async def generate(config, prompt, *args):
            prompts.append(prompt)
            return '{"answer":"fixture answer"}'

        with patch("larkx.agent.model_backend.generate_api", side_effect=generate):
            for question in ("first-input", "second-input", "third-input"):
                await router.run(
                    "owner",
                    question,
                    history_text=question,
                    response_schema={"type": "object"},
                    settings=settings,
                )
            self.assertIn("second-input", prompts[2])
            self.assertNotIn("first-input", prompts[2])
            await router.run(
                "owner",
                "no-history",
                history_text="no-history",
                settings={**settings, "control_history_turns": 0},
            )
            self.assertNotIn("third-input", prompts[-1])
            self.assertFalse(router.history)
        for provider, protocol, key in (
            ("deepseek", "openai", "max_tokens"),
            ("openai", "openai", "max_completion_tokens"),
            ("claude", "anthropic", "max_tokens"),
            ("gemini", "gemini", "maxOutputTokens"),
        ):
            config = {
                "provider": provider,
                "protocol": protocol,
                "model": "fixture",
                "base_url": "https://example.test",
                "api_key": "fixture",
            }
            payload = api_request(config, "test", [], {}, 4096)[2]
            self.assertEqual(
                (payload["generationConfig"] if protocol == "gemini" else payload)[key],
                4096,
            )

    async def test_codex_history_uses_same_turn_count_and_expires_old_images(self):
        with (
            patch.dict(os.environ, {"LARKX_CODEX_PATH": sys.executable}),
            patch("larkx.agent.codex_backend.DATA_DIR", self.temp),
        ):
            backend = CodexBackend()
        image = self.temp / "fixture.png"
        image.write_bytes(b"fixture")
        backend._path("owner").write_text(
            json.dumps(
                [
                    {"role": "user", "content": "old-input", "compact": True},
                    {"role": "assistant", "content": "old-answer", "compact": True},
                    {
                        "role": "user",
                        "content": "recent-input",
                        "compact": True,
                        "images": [str(image)],
                        "image_time": 1,
                    },
                    {"role": "assistant", "content": "recent-answer", "compact": True},
                ]
            ),
            encoding="utf-8",
        )
        requests = []

        async def spawn(*args, **kwargs):
            output = Path(args[args.index("--output-last-message") + 1])
            self.assertNotIn("--image", args)

            async def communicate(data):
                requests.append(data.decode())
                output.write_text('{"answer":"new-answer"}', encoding="utf-8")
                return None, b""

            return SimpleNamespace(returncode=0, communicate=communicate)

        settings = {**resolve_settings()[0], "control_history_turns": 1}
        with patch("asyncio.create_subprocess_exec", side_effect=spawn):
            await backend.run(
                "owner",
                "current",
                history_text="current",
                response_schema={"type": "object"},
                settings=settings,
            )
        self.assertIn("recent-input", requests[0])
        self.assertNotIn("old-input", requests[0])
        history = json.loads(backend._path("owner").read_text(encoding="utf-8"))
        self.assertEqual(
            [entry["content"] for entry in history], ["current", "new-answer"]
        )

    async def test_failed_login_validation_leaves_old_credentials_untouched(self):
        login = BrowserLogin()
        self.addCleanup(login.clear)
        ql = Mock(qr_content="fixture login")
        ql.session.cookies = {"session": "fixture"}
        ql.poll.return_value = {"next_step": "done", "step_info": {"status": 0}}
        ql.finish.side_effect = RuntimeError("validation failed")
        target = self.temp / "credentials.json"
        target.write_text("old-credentials", encoding="utf-8")
        with (
            patch("larkx.workhub.login.QrLogin", return_value=ql),
            patch("larkx.workhub.login.DATA_DIR", self.temp),
            patch("larkx.config.CREDENTIALS_PATH", target),
        ):
            started = await login.start("feishu", self.runtime)
            with self.assertRaisesRegex(ValueError, "登录失败"):
                await login.poll(started["id"], self.runtime)
            self.assertEqual(target.read_text(), "old-credentials")
            self.assertFalse(list(self.temp.glob("credentials.login-*")))
