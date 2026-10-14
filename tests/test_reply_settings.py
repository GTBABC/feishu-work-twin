import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from larkx.agent.dispatcher import SessionWorker
from larkx.workhub.server import create_app
from larkx.workhub.store import HubStore
from tests import test_message_context
from tests.test_hub import ANALYSIS


class ReplySettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_message_context.MessageContextTests.setUp(self)

    def test_timing_saved_and_persists(self):
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            response = client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={
                    "batch_delay": 2.5,
                    "batch_max_wait": 8,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["batch_delay"], 2.5)
            self.assertEqual(response.json()["batch_max_wait"], 8)
        self.assertEqual(
            HubStore(self.storage).batch_timing(),
            {"batch_delay": 2.5, "batch_max_wait": 8},
        )

    def test_invalid_timing_cannot_partially_change_other_settings(self):
        self.runtime.hub.set_setting("own_rules", "original")
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            for fields in (
                {"batch_delay": None},
                {"batch_delay": True},
                {"batch_delay": "2"},
                {"batch_delay": 0},
                {"batch_delay": 31},
                {"batch_max_wait": 61},
                {"batch_delay": 6, "batch_max_wait": 5},
            ):
                with self.subTest(fields=fields):
                    response = client.patch(
                        "/api/settings",
                        headers={"X-Workhub": "1"},
                        json={
                            "own_rules": "should not change",
                            **fields,
                        },
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(self.runtime.hub.setting("own_rules"), "original")
            client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={"batch_delay": 4, "batch_max_wait": 8},
            )
            response = client.patch(
                "/api/settings", headers={"X-Workhub": "1"}, json={"batch_max_wait": 3}
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.runtime.hub.batch_timing()["batch_max_wait"], 8)

    async def test_worker_uses_saved_timing_instead_of_environment(self):
        self.runtime.hub.set_setting("batch_delay", "0.12")
        self.runtime.hub.set_setting("batch_max_wait", "0.2")
        finished = asyncio.Event()
        received = []

        async def handle(key, msg):
            received.extend(m["content"] for m in msg.get("_batch") or [msg])
            finished.set()

        with patch.dict("os.environ", {"LARKX_BATCH_DELAY": "0.01"}):
            worker = SessionWorker(
                SimpleNamespace(
                    backend=self.runtime.backend,
                    workflow=self.runtime.engine,
                    handle_message=handle,
                ),
                "g:",
            )
            try:
                await worker.queue.put({"content": "第一句"})
                await asyncio.sleep(0.06)
                await worker.queue.put({"content": "第二句"})
                await asyncio.wait_for(finished.wait(), 1)
                self.assertEqual(received, ["第一句", "第二句"])
            finally:
                worker.task.cancel()
                await asyncio.gather(worker.task, return_exceptions=True)

    async def test_casual_mode_can_choose_silence_and_gets_participation_rules(self):
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "all"})

        async def run(*args, **kwargs):
            self.runtime.backend.prompt = args[1]
            return json.dumps({**ANALYSIS, "should_reply": False, "reply": ""})

        self.runtime.backend.run = run
        sent = []

        async def reply(*args):
            sent.append(args)

        result = await self.runtime.engine.process(
            "g:",
            {"chat_id": "g", "chat_type": 2, "from_id": "li"},
            "其他同事彼此闲聊",
            [],
            reply,
        )
        self.assertFalse(result["should_reply"])
        self.assertFalse(sent)
        self.assertIn("闲聊模式", self.runtime.backend.prompt)
        self.assertIn("不是对我的请求", self.runtime.backend.prompt)
        self.assertIn("我刚插过一句时通常先保持安静", self.runtime.backend.prompt)
