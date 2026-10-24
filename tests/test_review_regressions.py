import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from larkx.agent.dispatcher import AgentDispatcher
from larkx.media import image_messages, message_resource_url
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from tests.test_hub import FakeBackend


class ReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "review.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.msg = {
            "msg_id": "m",
            "chat_id": "c",
            "from_id": "u",
            "chat_type": 1,
            "content": "请求",
        }
        self.runtime.hub.observe(self.msg, "测试会话", "测试同事")

    async def test_workhub_context_stays_isolated_even_with_global_cli_config(self):
        dispatcher = AgentDispatcher(FakeBackend(), None, workflow=self.runtime.engine)
        dispatcher.cfg["context_scope"] = "global"
        self.assertNotEqual(
            dispatcher.session_key_of({"chat_id": "a"}),
            dispatcher.session_key_of({"chat_id": "b"}),
        )

    async def test_commands_cannot_bypass_workflow(self):
        sent = []

        async def reply(*args):
            sent.append(args)

        dispatcher = AgentDispatcher(FakeBackend(), reply, workflow=self.runtime.engine)
        await dispatcher.submit({**self.msg, "content": "/clear"}, my_user_id="me")
        self.assertFalse(sent)
        worker = next(iter(dispatcher.workers.values()))
        item = worker.queue.get_nowait()
        self.assertEqual(item["content"], "/clear")
        worker.task.cancel()
        await asyncio.gather(worker.task, return_exceptions=True)

    async def test_disabled_group_member_is_not_answered_in_mixed_batch(self):
        first = {**self.msg, "chat_id": "g", "chat_type": 2}
        last = {**first, "from_id": "u2", "msg_id": "m2"}
        self.runtime.hub.observe(first, "测试群", "测试同事")
        self.runtime.hub.observe(last, "测试群", "另一同事")
        self.runtime.hub.update_persona("user:u", {"mode": "off"})

        async def reply(*args):
            self.fail("mixed batch must respect the disabled member")

        await self.runtime.engine.process(
            "g:", {**last, "_batch": [first, last]}, "问题", [], reply
        )

    async def test_post_images_are_prepared_in_order(self):
        msg = {
            **self.msg,
            "msg_type": 2,
            "content_data": {
                "_display_parts": [
                    {"type": "text", "text": "a"},
                    {"type": "image", "key": "first"},
                    {"type": "image", "key": "second"},
                ]
            },
        }
        images = image_messages(msg)
        self.assertEqual(
            [m["content_data"]["image"]["origin"]["key"] for m in images],
            ["first", "second"],
        )
        captured = []

        async def prepare(item):
            return ["first.png", "second.png"]

        async def process(key, item, body, images, reply):
            captured.extend(images)

        dispatcher = AgentDispatcher(
            FakeBackend(),
            None,
            image_prepare=prepare,
            workflow=SimpleNamespace(process=process, runtime=self.runtime),
        )
        await dispatcher.handle_message("c:", msg)
        self.assertEqual(captured, ["first.png", "second.png"])

    async def test_rich_suffix_added_once_and_length_guard(self):
        r = self.runtime
        r.client = SimpleNamespace(
            auth=SimpleNamespace(user_id="me"), send_rich=lambda *args: "sent"
        )
        r.connection = "connected"
        r.hub.set_setting(
            "upload:a", json.dumps({"key": "img", "width": 100, "height": 50})
        )
        await r.send_parts(
            "c",
            [
                {"type": "text", "text": "hello\n\nreply by Bot"},
                {"type": "image", "upload_id": "a"},
            ],
            "r",
        )
        action = r.hub.actions()[0]
        self.assertEqual(action["content"].count("reply by Bot"), 1)
        with self.assertRaises(ValueError):
            await r.send_parts(
                "c",
                [
                    {"type": "text", "text": "x" * 12000},
                    {"type": "image", "upload_id": "a"},
                ],
                "too-long",
            )

    async def test_request_id_cannot_be_reused_for_different_message(self):
        h = self.runtime.hub
        h.claim_send("r", "c", "hello")
        with self.assertRaises(ValueError):
            h.claim_send("r", "c", "different")

    async def test_uncertain_owner_send_is_not_reported_successful(self):
        async def model(*args, **kwargs):
            return json.dumps(
                {
                    "answer": "发送",
                    "actions": [
                        {
                            "type": "send_message",
                            "chat_id": "c",
                            "content": "测试",
                            "task_id": 0,
                        }
                    ],
                }
            )

        async def send(*args, **kwargs):
            return {"status": "uncertain", "error": "未确认"}

        self.runtime.backend.run = model
        self.runtime.send = send
        result = await self.runtime.engine.control("发送一条测试消息", "job")
        self.assertFalse(result["actions"][0]["ok"])
        self.assertEqual(result["actions"][0]["error"], "未确认")

    async def test_invalid_settings_do_not_partially_save(self):
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            response = client.patch(
                "/api/settings",
                json={"own_rules": "changed", "reply_suffix": None},
                headers={"X-Workhub": "1"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertNotEqual(self.runtime.hub.setting("own_rules"), "changed")
            self.assertEqual(
                client.patch(
                    "/api/settings",
                    json={"mark_read": "false"},
                    headers={"X-Workhub": "1"},
                ).status_code,
                400,
            )

    async def test_invalid_task_status_and_unknown_chat_rejected(self):
        for fields in [
            {"title": "x", "status": "invalid"},
            {"title": "x", "chat_id": "unknown"},
        ]:
            with self.assertRaises(ValueError):
                self.runtime.hub.create_task(fields)

    async def test_resource_url_cannot_inject_path_or_query(self):
        url = message_resource_url(
            "id", "../other?x=1", "chat&x=1", "https://example.invalid"
        )
        self.assertIn("..%2Fother%3Fx%3D1", url)
        self.assertIn("chat_id=chat%26x%3D1", url)

    async def test_shutdown_awaits_workers(self):
        stopped = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()

        task = asyncio.create_task(work())
        await asyncio.sleep(0)
        self.runtime.dispatcher = SimpleNamespace(
            workers={"c": SimpleNamespace(task=task)}
        )
        await self.runtime.stop()
        self.assertTrue(stopped.is_set())
        self.assertTrue(task.done())


if __name__ == "__main__":
    unittest.main()
