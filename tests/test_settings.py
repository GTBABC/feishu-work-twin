import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from larkx.agent.codex_backend import CodexBackend
from larkx.agent.dispatcher import SessionWorker
from larkx.settings import SETTINGS, resolve_settings
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.scheduling import ModelScheduler
from larkx.workhub.server import create_app
from larkx.workhub.store import HubStore
from tests.test_hub import FakeBackend


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage("sqlite:///" + str(Path(self.temp) / "settings.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.addAsyncCleanup(self.runtime.stop)
        self.hub = self.runtime.hub

    def test_existing_settings_override_environment_and_empty_values_survive(self):
        with patch.dict(
            "os.environ", {"LARKX_BATCH_SIZE": "7", "LARKX_CODEX_MODEL": "env-model"}
        ):
            self.assertEqual(self.hub.settings()["batch_size"], 7)
            self.hub.update_settings(
                {
                    "batch_size": 3,
                    "codex_model": "",
                    "reply_suffix": "",
                    "human_pause_seconds": 0,
                    "mark_read": False,
                }
            )
            values, defaults, sources = HubStore(self.storage).settings_info()
            self.assertEqual(values["batch_size"], 3)
            self.assertEqual(defaults["batch_size"], 7)
            self.assertEqual(defaults["codex_model"], "env-model")
            self.assertEqual(values["codex_model"], "")
            self.assertEqual(values["reply_suffix"], "")
            self.assertEqual(values["human_pause_seconds"], 0)
            self.assertFalse(values["mark_read"])
            self.assertEqual(sources["batch_size"], "已保存")
        with patch.dict("os.environ", {"LARKX_BATCH_SIZE": "invalid"}):
            self.assertEqual(
                resolve_settings()[0]["batch_size"], SETTINGS["batch_size"].default
            )
        with patch.dict(
            "os.environ",
            {"LARKX_MAX_CONCURRENCY": "4", "LARKX_BACKGROUND_CONCURRENCY": "2"},
        ):
            self.assertEqual(self.hub.settings()["max_concurrency"], 4)
            self.assertEqual(self.hub.settings()["background_concurrency"], 2)

    def test_group_schema_and_cross_field_validation_are_atomic(self):
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            info = client.get("/api/settings").json()
            self.assertEqual(len(info["groups"]), 5)
            self.assertEqual({field["key"] for field in info["fields"]}, set(SETTINGS))
            for invalid in (
                {"max_concurrency": 1, "background_concurrency": 2},
                {"batch_delay": 6, "batch_max_wait": 5},
                {"batch_size": True},
                {"codex_timeout": 1.5},
                {"poll_interval": float("inf")},
                {"storage_url": "other"},
            ):
                # JSON deliberately excludes infinity; validate its type directly.
                if "poll_interval" in invalid:
                    with self.assertRaises(ValueError):
                        self.hub.update_settings(invalid)
                    continue
                response = client.patch(
                    "/api/settings",
                    headers={"X-Workhub": "1"},
                    json={"own_rules": "must not persist", **invalid},
                )
                self.assertEqual(response.status_code, 400)
                self.assertNotEqual(
                    self.hub.settings()["own_rules"], "must not persist"
                )
            response = client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={"max_concurrency": 4, "background_concurrency": 2},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.runtime.model_slots.limit, 4)
            self.assertEqual(self.runtime.model_slots.background_limit, 2)

    def test_legacy_fractional_times_round_up_and_new_fractions_are_rejected(self):
        legacy = {
            "batch_delay": ("1.5", 2),
            "batch_max_wait": ("2.2", 3),
            "human_pause_seconds": ("90", 120),
            "background_interval": ("30.1", 31),
            "poll_interval": ("2.5", 3),
            "job_poll_interval": ("0.5", 1),
            "retry_delay": ("0.1", 1),
        }
        for key, (stored, expected) in legacy.items():
            self.hub.set_setting(key, stored)
        self.hub.update_settings({"own_rules": "keep existing rules"})
        resolved = HubStore(self.storage).settings()
        for key, (_, expected) in legacy.items():
            self.assertEqual(resolved[key], expected)
        with patch.dict("os.environ", {"LARKX_BATCH_DELAY": "1.5"}):
            self.assertEqual(resolve_settings()[0]["batch_delay"], 2)
        for invalid in (
            {"batch_delay": 1.5},
            {"poll_interval": 2.5},
            {"human_pause_seconds": 90},
        ):
            with self.assertRaises(ValueError):
                self.hub.update_settings({"own_rules": "must not persist", **invalid})
        self.assertEqual(self.hub.settings()["own_rules"], "keep existing rules")

    def test_database_failure_rolls_back_all_settings_and_handoffs(self):
        self.hub.update_settings({"own_rules": "original"})
        self.hub.touch_handoff("c", manual=True)
        before = self.hub.handoff("c")
        with patch(
            "sqlalchemy.orm.Session.commit",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.hub.update_settings(
                    {"own_rules": "changed", "human_pause_seconds": 480}
                )
        self.assertEqual(self.hub.settings()["own_rules"], "original")
        self.assertEqual(self.hub.handoff("c"), before)

    def test_context_limits_and_display_limits_preserve_source_data(self):
        self.hub.observe(
            {"chat_id": "c", "from_id": "u", "chat_type": 1}, "同事", "同事"
        )
        self.hub.update_settings(
            {
                "recent_message_limit": 2,
                "memory_limit": 0,
                "task_context_limit": 1,
                "completed_task_limit": 10,
            }
        )
        for n in range(12):
            self.storage.save_message(
                {
                    "msg_id": str(n),
                    "chat_id": "c",
                    "from_id": "u",
                    "content": str(n),
                    "create_time": n,
                }
            )
            task = self.hub.create_task({"title": str(n), "chat_id": "c"})
            self.hub.update_task(task["id"], {"status": "done"})
        first = self.hub.create_task({"title": "open A", "chat_id": "c"})
        self.hub.create_task({"title": "open B", "chat_id": "c"})
        context = self.hub.context("c")
        self.assertEqual(len(context["recent_messages"]), 2)
        self.assertEqual(len(context["tasks"]), 1)
        self.assertEqual(context["tasks"][0]["id"], first["id"])
        self.assertEqual(
            len(self.hub.reply_context({"chat_id": "c", "msg_id": "11"})), 2
        )
        self.assertEqual(len(self.hub.tasks()), 12)
        self.assertEqual(len(self.storage.get_messages("c", limit=100)), 12)

    async def test_frontend_and_background_share_configured_batch_size(self):
        self.hub.update_settings({"batch_size": 2, "batch_delay": 1})
        worker = SessionWorker.__new__(SessionWorker)
        worker.dispatcher = type("Dispatcher", (), {"workflow": self.runtime.engine})()
        worker.queue = asyncio.Queue()
        for n in range(1, 5):
            await worker.queue.put({"msg_id": str(n)})
        batch = await worker.collect_batch({"msg_id": "0"})
        self.assertEqual(len(batch), 2)
        self.assertEqual(worker.queue.qsize(), 3)
        key = ("c", "")
        self.runtime.observations[key] = [{"msg_id": str(n)} for n in range(5)]
        batches = []

        async def analyze(batch, **kwargs):
            batches.append(batch)
            if len(batches) == 1:
                self.hub.update_settings({"batch_size": 3})

        self.runtime.analyze_messages = analyze
        with patch.object(self.hub, "background_interval", return_value=0.01):
            await self.runtime.observe_batch(key)
        self.assertEqual([len(b) for b in batches], [2, 3])
        self.assertEqual(
            [m["msg_id"] for b in batches for m in b], list(map(str, range(5)))
        )

    async def test_model_options_and_disabled_images_reach_cli_without_real_calls(self):
        import sys

        with (
            patch.dict("os.environ", {"LARKX_CODEX_PATH": sys.executable}),
            patch("larkx.agent.codex_backend.DATA_DIR", Path(self.temp)),
        ):
            backend = CodexBackend()
        first = self.hub.update_settings(
            {
                "codex_model": "first-model",
                "codex_reasoning_effort": "high",
                "model_image_limit": 0,
                "network_retries": 0,
            }
        )
        calls = []

        async def spawn(*args, **kwargs):
            calls.append(args)
            output = Path(args[args.index("--output-last-message") + 1])

            class Process:
                returncode = 1 if len(calls) > 2 else 0

                async def communicate(self, data):
                    output.write_text('{"reply":"ok"}', encoding="utf-8")
                    return (
                        None,
                        b"ERROR: stream disconnected" if self.returncode else b"",
                    )

            return Process()

        image = Path(self.temp) / "image.png"
        image.write_bytes(b"fixture")
        with patch("asyncio.create_subprocess_exec", spawn):
            await backend.run("a", "first", image_paths=[image], settings=first)
            second = self.hub.update_settings({"codex_model": "second-model"})
            await backend.run("b", "second", settings=second)
            with self.assertRaisesRegex(RuntimeError, "连接"):
                await backend.run("c", "network failure", settings=second)
        self.assertEqual(len(calls), 3)
        self.assertIn("first-model", calls[0])
        self.assertIn('model_reasoning_effort="high"', calls[0])
        self.assertNotIn("--image", calls[0])
        self.assertIn("second-model", calls[1])

    async def test_lowering_concurrency_does_not_interrupt_running_calls(self):
        scheduler = ModelScheduler(2, 1)
        entered = asyncio.Event()

        async def wait():
            async with scheduler.slot():
                entered.set()

        async with scheduler.slot():
            async with scheduler.slot():
                scheduler.configure(1, 1)
                waiting = asyncio.create_task(wait())
                await asyncio.sleep(0)
                self.assertEqual(scheduler.active, 2)
            await asyncio.sleep(0)
            self.assertFalse(entered.is_set())
        await waiting
        self.assertTrue(entered.is_set())
