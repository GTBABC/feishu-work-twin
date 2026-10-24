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
                    "batch_delay": 3,
                    "batch_max_wait": 8,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["batch_delay"], 3)
            self.assertEqual(response.json()["batch_max_wait"], 8)
        self.assertEqual(
            HubStore(self.storage).batch_timing(),
            {"batch_delay": 3, "batch_max_wait": 8},
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
                {"batch_delay": 1.5},
                {"batch_max_wait": 2.5},
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
        self.runtime.hub.set_setting("batch_delay", "2")
        self.runtime.hub.set_setting("batch_max_wait", "3")
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
                await asyncio.sleep(1.1)
                await worker.queue.put({"content": "第二句"})
                await asyncio.wait_for(finished.wait(), 3)
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

    async def test_casual_direct_mention_requires_reply_even_when_model_selects_silence(
        self,
    ):
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "all"})
        self.runtime.client = SimpleNamespace(auth=SimpleNamespace(user_id="me"))
        captured = []

        async def run(*args, **kwargs):
            captured.append(kwargs["response_schema"])
            self.runtime.backend.prompt = args[1]
            return json.dumps({**ANALYSIS, "should_reply": False, "reply": ""})

        self.runtime.backend.run = run
        sent = []

        async def reply(*args):
            sent.append(args)

        first = {
            "chat_id": "g",
            "chat_type": 2,
            "from_id": "li",
            "at_me": True,
            "content": "@我",
        }
        last = {**first, "at_me": False, "content": "前面说的 A07"}
        result = await self.runtime.engine.process(
            "g:", dict(last, _batch=[first, last]), "连续消息", [], reply
        )
        self.assertTrue(result["should_reply"])
        self.assertEqual(sent[0][1], "我在。")
        self.assertEqual(captured[0]["properties"]["should_reply"]["enum"], [True])
        self.assertIn("必须回应", self.runtime.backend.prompt)
        sent.clear()
        await self.runtime.engine.process("g:", last, "普通聊天", [], reply)
        self.assertFalse(sent)
        self.assertNotIn("enum", captured[-1]["properties"]["should_reply"])

    async def test_required_mention_respects_human_pause_and_excludes_at_all_only(self):
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "all"})
        self.runtime.client = SimpleNamespace(auth=SimpleNamespace(user_id="me"))

        async def run(*args, **kwargs):
            return json.dumps({**ANALYSIS, "should_reply": False, "reply": ""})

        self.runtime.backend.run = run
        sent = []

        async def reply(*args):
            sent.append(args)

        msg = {
            "chat_id": "g",
            "chat_type": 2,
            "from_id": "li",
            "at_me": True,
            "at_all": True,
            "at_user_ids": ["all"],
        }
        result = await self.runtime.engine.process("g:", msg, "@全体", [], reply)
        self.assertFalse(result["should_reply"])
        self.assertFalse(sent)
        self.runtime.hub.touch_handoff("g", manual=True)
        result = await self.runtime.engine.process(
            "g:", dict(msg, at_all=False), "@我", [], reply
        )
        self.assertTrue(result["should_reply"])
        self.assertFalse(sent)

    def test_human_pause_setting_persists_and_rejects_invalid_input(self):
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            self.assertEqual(self.runtime.status()["human_pause_seconds"], 120)
            response = client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={"human_pause_seconds": 300},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["human_pause_seconds"], 300)
            self.assertEqual(HubStore(self.storage).human_pause_seconds(), 300)
            self.runtime.hub.set_setting("own_rules", "keep")
            for value in [None, True, "60", -1, 86401, 90, 1.5]:
                response = client.patch(
                    "/api/settings",
                    headers={"X-Workhub": "1"},
                    json={"human_pause_seconds": value, "own_rules": "invalid change"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.runtime.hub.setting("own_rules"), "keep")
            response = client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={"human_pause_seconds": 0},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["human_pause_seconds"], 0)

    def test_configured_pause_rolls_with_activity_and_does_not_affect_other_chats(self):
        h = self.runtime.hub
        h.configure_human_pause(60)
        with patch("larkx.workhub.store.time.time", return_value=1000):
            h.touch_handoff("g", manual=True)
            self.assertEqual(h.handoff("g")["until"], 1060)
        with patch("larkx.workhub.store.time.time", return_value=1020):
            h.touch_handoff("g", activity_at=1020)
            self.assertEqual(h.handoff("g")["until"], 1080)
            h.touch_handoff("other")
            self.assertFalse(h.human_paused("other"))
        with patch("larkx.workhub.store.time.time", return_value=1079):
            self.assertTrue(HubStore(self.storage).human_paused("g"))
        with patch("larkx.workhub.store.time.time", return_value=1081):
            self.assertFalse(h.human_paused("g"))
            h.touch_handoff("g")
            self.assertFalse(h.human_paused("g"))

    def test_changing_duration_recalculates_active_pauses_without_reviving_expired(
        self,
    ):
        h = self.runtime.hub
        with patch("larkx.workhub.store.time.time", return_value=1000):
            h.touch_handoff("expired", manual=True)
        with patch("larkx.workhub.store.time.time", return_value=1200):
            h.touch_handoff("active", manual=True)
        with patch("larkx.workhub.store.time.time", return_value=1270):
            h.configure_human_pause(300)
            self.assertEqual(h.handoff("active")["until"], 1500)
            self.assertFalse(h.human_paused("expired"))
            h.configure_human_pause(60)
            self.assertEqual(h.handoff("active")["until"], 1260)
            self.assertFalse(h.human_paused("active"))

    def test_zero_duration_disables_pause_and_handles_legacy_states(self):
        h = self.runtime.hub
        h.set_setting(
            "human_pause:g",
            json.dumps({"until": 1120, "last_manual": 1000, "generation": 1}),
        )
        with patch("larkx.workhub.store.time.time", return_value=1010):
            h.configure_human_pause(300)
            self.assertEqual(h.handoff("g")["until"], 1300)
            h.configure_human_pause(0)
            self.assertFalse(h.human_paused("g"))
            h.touch_handoff("g", manual=True)
            self.assertFalse(h.human_paused("g"))
            self.assertEqual(h.handoff("g")["generation"], 2)
        for value in ["nan", "inf", "oops", "-1", "86401"]:
            h.set_setting("human_pause_seconds", value)
            self.assertEqual(h.human_pause_seconds(), 120)
