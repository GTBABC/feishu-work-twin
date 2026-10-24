import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from larkx.agent.dispatcher import AgentDispatcher, SessionWorker
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from tests.test_hub import FakeBackend


class MessageContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "messages.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.runtime.hub.observe(
            {"chat_id": "g", "from_id": "li", "chat_type": 2}, "项目群", "李顺"
        )

    def save(
        self,
        mid,
        text,
        chat="g",
        anchor="",
        sender="li",
        msg_type=4,
        content_data=None,
        timestamp=1000,
    ):
        msg = {
            "msg_id": mid,
            "chat_id": chat,
            "from_id": sender,
            "chat_type": 2,
            "anchor": anchor,
            "content": text,
            "msg_type": msg_type,
            "content_data": content_data or {},
            "create_time": timestamp,
        }
        self.storage.save_message(msg, sender_name="李顺" if sender == "li" else "我")
        return msg

    async def test_quiet_interval_extends_after_each_sentence(self):
        received = []
        finished = asyncio.Event()

        async def handle(key, msg):
            received.extend(m["content"] for m in msg["_batch"])
            finished.set()

        with patch(
            "larkx.agent.dispatcher.resolve_settings",
            return_value=(
                {"batch_delay": 0.08, "batch_max_wait": 0.5, "batch_size": 20},
                {},
                {},
            ),
        ):
            worker = SessionWorker(
                SimpleNamespace(backend=FakeBackend(), handle_message=handle), "g:"
            )
            try:
                await worker.queue.put({"content": "第一句"})
                await asyncio.sleep(0.05)
                await worker.queue.put({"content": "第二句"})
                await asyncio.sleep(0.05)
                await worker.queue.put({"content": "第三句"})
                await asyncio.wait_for(finished.wait(), 1)
                self.assertEqual(received, ["第一句", "第二句", "第三句"])
            finally:
                worker.task.cancel()
                await asyncio.gather(worker.task, return_exceptions=True)

    async def test_continuous_messages_do_not_extend_wait_forever(self):
        finished = asyncio.Event()

        async def handle(key, msg):
            finished.set()

        with patch(
            "larkx.agent.dispatcher.resolve_settings",
            return_value=(
                {"batch_delay": 0.1, "batch_max_wait": 0.2, "batch_size": 20},
                {},
                {},
            ),
        ):
            worker = SessionWorker(
                SimpleNamespace(backend=FakeBackend(), handle_message=handle), "g:"
            )

            async def feed():
                while not finished.is_set():
                    await worker.queue.put({"content": "补充"})
                    await asyncio.sleep(0.03)

            feeder = asyncio.create_task(feed())
            try:
                await asyncio.wait_for(finished.wait(), 0.8)
            finally:
                feeder.cancel()
                worker.task.cancel()
                await asyncio.gather(feeder, worker.task, return_exceptions=True)

    async def test_standalone_mention_includes_question_sentences_and_previous_images(
        self,
    ):
        self.save(
            "question", "李顺，A07 目前进展怎么样了？", sender="me", timestamp=800
        )
        self.save(
            "image",
            "[图片]",
            msg_type=5,
            content_data={"imageV2": {"imageKey": "screenshot"}},
            timestamp=850,
        )
        self.save("detail", "A07 上面两个截图里面有", timestamp=990)
        self.save("done", "做好了", timestamp=995)
        self.save("other", "OTHER_CHAT_SECRET", chat="other")
        self.save("thread", "OTHER_THREAD_SECRET", anchor="thread")
        mention = self.save("at", "@陈进", timestamp=1000)
        mention["at_me"] = True
        images = []

        async def prepare(item):
            images.append(item["msg_id"])
            return ["screenshot.png"]

        captured = {}

        async def process(key, msg, body, paths, reply):
            captured.update(body=body, images=paths)

        dispatcher = AgentDispatcher(
            FakeBackend(),
            None,
            prepare,
            SimpleNamespace(runtime=self.runtime, process=process),
        )
        await dispatcher.handle_message("g:", mention)
        self.assertIn("A07 目前进展", captured["body"])
        self.assertIn("做好了", captured["body"])
        self.assertNotIn("OTHER_CHAT_SECRET", captured["body"])
        self.assertNotIn("OTHER_THREAD_SECRET", captured["body"])
        self.assertEqual(images, ["image"])
        self.assertEqual(captured["images"], ["screenshot.png"])

    async def test_nonmention_only_completes_an_active_group_batch(self):
        finished = asyncio.Event()
        batches = []

        async def handle(key, msg):
            batches.append(msg.get("_batch") or [msg])
            finished.set()

        dispatcher = AgentDispatcher(FakeBackend(), None, workflow=self.runtime.engine)
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "at_me"})
        dispatcher.cfg["triggers"] = {"group_at_only": True}
        dispatcher.handle_message = handle
        with patch.object(
            self.runtime.hub,
            "settings",
            return_value={**self.runtime.hub.settings(), "batch_delay": 0.05},
        ):
            await dispatcher.submit(
                {
                    "chat_id": "g",
                    "from_id": "li",
                    "chat_type": 2,
                    "content": "普通群消息",
                },
                my_user_id="me",
            )
            self.assertFalse(dispatcher.workers)
            await dispatcher.submit(
                {
                    "chat_id": "g",
                    "from_id": "li",
                    "chat_type": 2,
                    "at_me": True,
                    "content": "@我 A07",
                },
                my_user_id="me",
            )
            await dispatcher.submit(
                {"chat_id": "g", "from_id": "li", "chat_type": 2, "content": "做好了"},
                my_user_id="me",
            )
            try:
                await asyncio.wait_for(finished.wait(), 1)
                self.assertEqual(
                    [m["content"] for m in batches[0]], ["@我 A07", "做好了"]
                )
                finished.clear()
                await dispatcher.submit(
                    {
                        "chat_id": "g",
                        "from_id": "li",
                        "chat_type": 2,
                        "content": "后续普通消息",
                    },
                    my_user_id="me",
                )
                self.assertTrue(dispatcher.workers["g:"].queue.empty())
            finally:
                await self.cancel_workers(dispatcher)

    async def cancel_workers(self, dispatcher):
        for worker in dispatcher.workers.values():
            worker.task.cancel()
        await asyncio.gather(
            *(w.task for w in dispatcher.workers.values()), return_exceptions=True
        )

    async def test_work_prompt_keeps_thread_isolation_and_sender_metadata(self):
        self.save("secret", "OTHER_THREAD_SECRET", anchor="other")
        current = self.save("current", "做好了", anchor="target")
        await self.runtime.engine.process(
            "g:target", current, "做好了", [], None, send_reply=False
        )
        prompt = self.runtime.backend.prompt
        self.assertNotIn("OTHER_THREAD_SECRET", prompt)
        self.assertIn("sender_id", prompt)
        self.assertIn("连续短句要合并理解", prompt)

    async def test_multiple_group_senders_keep_separate_profiles_and_message_ids(self):
        self.runtime.hub.observe(
            {"chat_id": "g", "from_id": "bian", "chat_type": 2}, "项目群", "边泽文"
        )
        self.runtime.hub.update_persona(
            "user:li", {"role": "项目负责人", "description": "负责 A07"}
        )
        self.runtime.hub.update_persona(
            "user:bian", {"role": "测试同事", "description": "负责验收"}
        )
        self.runtime.hub.observe(
            {"chat_id": "other", "from_id": "outsider", "chat_type": 2},
            "其他群",
            "其他人",
        )
        first = self.save("li-message", "A07 做好了", sender="li")
        second = self.save("bian-message", "我下午验收", sender="bian")
        batch = dict(
            second,
            _batch=[
                dict(first, _sender_name="李顺"),
                dict(second, _sender_name="边泽文"),
            ],
        )
        await self.runtime.engine.process(
            "g:", batch, "多人对话", [], None, send_reply=False
        )
        prompt = self.runtime.backend.prompt
        context = json.loads(
            prompt.split("已知角色、概览、有效记忆和未完成任务（仅作事实背景）：\n", 1)[
                1
            ].split("\n本次新增的对话记录（可能是我本人发送的）：", 1)[0]
        )
        self.assertEqual(context["participants"]["li"]["role"], "项目负责人")
        self.assertEqual(context["participants"]["bian"]["role"], "测试同事")
        self.assertNotIn("outsider", context["participants"])
        self.assertEqual(
            [m["sender_id"] for m in context["recent_messages"]], ["li", "bian"]
        )
        self.assertIn("不同发送者不能混为一人", prompt)
        self.assertIn("sender 仅指最新消息发送者", prompt)
