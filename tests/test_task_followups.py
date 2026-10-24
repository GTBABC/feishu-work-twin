import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from larkx.storage import Storage
from larkx.workhub.engine import WORK_SCHEMA
from larkx.workhub.runtime import HubRuntime
from tests.test_hub import ANALYSIS, FakeBackend


class TaskFollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(temp.name) / "tasks.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.hub = self.runtime.hub
        self.msg = {
            "chat_id": "c",
            "msg_id": "first",
            "from_id": "li",
            "anchor": "",
            "chat_type": 1,
        }
        self.hub.observe(self.msg, "李顺", "李顺")
        self.runtime.client = SimpleNamespace(auth=SimpleNamespace(user_id="me"))

    def analysis(self, **fields):
        return {**deepcopy(ANALYSIS), "attention_task_id": 0, **fields}

    def create_original(self):
        self.hub.apply_analysis(self.msg, self.analysis(needs_attention=True))
        return self.hub.tasks()[0]

    def test_attention_without_new_tasks_updates_original(self):
        task = self.create_original()
        self.hub.update_task(
            task["id"],
            {
                "title": "我的标题",
                "status": "in_progress",
                "draft_reply": "我手写的草稿",
            },
        )
        for i, content in enumerate(["改了三个任务那个", "本人补发了版本文件"]):
            self.hub.apply_analysis(
                dict(self.msg, msg_id=f"follow-{i}", from_id="me"),
                self.analysis(
                    tasks=[],
                    needs_attention=True,
                    attention_reason=content,
                    attention_task_id=task["id"],
                ),
                allowed_task_ids=[task["id"]],
            )
        tasks = self.hub.tasks()
        self.assertEqual(len(tasks), 1)
        self.assertIn("改了三个任务那个", tasks[0]["details"])
        self.assertIn("本人补发了版本文件", tasks[0]["details"])
        self.assertEqual(tasks[0]["origin_msg_id"], "first")
        self.assertEqual(tasks[0]["title"], "我的标题")
        self.assertEqual(tasks[0]["status"], "in_progress")
        self.assertEqual(tasks[0]["draft_reply"], "我手写的草稿")

    def test_different_request_creates_separate_task_and_replay_is_ignored(self):
        original = self.create_original()
        msg = dict(self.msg, msg_id="another")
        data = self.analysis(
            tasks=[{"task_id": 0, "title": "准备评审材料", "details": "另一个问题"}]
        )
        self.hub.apply_analysis(msg, data)
        tasks = self.hub.tasks()
        self.assertEqual(len(tasks), 2)
        created = next(t for t in tasks if t["id"] != original["id"])
        self.hub.update_task(created["id"], {"status": "done"})
        data["tasks"][0]["title"] = "模型重试时换了一个标题"
        self.hub.apply_analysis(msg, data)
        self.assertEqual(len(self.hub.tasks()), 2)

    def test_completed_task_during_generation_is_not_reopened(self):
        task = self.create_original()
        self.hub.update_task(task["id"], {"status": "done"})
        self.hub.apply_analysis(
            dict(self.msg, msg_id="later"),
            self.analysis(tasks=[], needs_attention=True, attention_task_id=task["id"]),
            allowed_task_ids=[task["id"]],
        )
        self.assertEqual(len(self.hub.tasks()), 1)
        self.assertEqual(self.hub.tasks()[0]["status"], "done")

    def test_cannot_update_another_chat_thread_or_unprovided_task(self):
        task = self.create_original()
        data = self.analysis(
            tasks=[], needs_attention=True, attention_task_id=task["id"]
        )
        for msg, allowed in [
            (dict(self.msg, chat_id="other", msg_id="bad-chat"), [task["id"]]),
            (dict(self.msg, anchor="thread", msg_id="bad-thread"), [task["id"]]),
            (dict(self.msg, msg_id="not-provided"), []),
        ]:
            with self.assertRaises(ValueError):
                self.hub.apply_analysis(msg, data, allowed_task_ids=allowed)
        self.assertEqual(len(self.hub.tasks()), 1)
        self.assertEqual(self.hub.context("c", anchor="thread")["tasks"], [])

    def test_same_title_in_different_threads_is_not_merged(self):
        self.create_original()
        self.hub.apply_analysis(
            dict(self.msg, anchor="thread", msg_id="thread-first"), self.analysis()
        )
        self.assertEqual(len(self.hub.tasks()), 2)

    async def test_own_observation_has_identity_and_no_phantom_attention(self):
        captured = {}

        async def generate(*args, **kwargs):
            captured["prompt"] = args[1]
            return json.dumps(
                self.analysis(
                    tasks=[],
                    needs_attention=True,
                    attention_reason="不知道自己为什么发文件",
                )
            )

        self.runtime.backend.run = generate
        await self.runtime.engine.process(
            "overview:c",
            dict(self.msg, from_id="me"),
            "本人发送文件",
            [],
            None,
            send_reply=False,
        )
        self.assertFalse(self.hub.tasks())
        self.assertIn('"self_user_id": "me"', captured["prompt"])
        self.assertIn('"analysis_mode": "observe"', captured["prompt"])

    async def test_reply_and_observation_serialize_and_share_task_context(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def generate(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 1:
                entered.set()
                await release.wait()
                data = self.analysis(needs_attention=True)
                data["tasks"][0]["evidence_ids"] = ["first"]
                return json.dumps(data)
            task = self.hub.tasks()[0]
            self.assertIn('"id": ' + str(task["id"]), args[1])
            enums = kwargs["response_schema"]["properties"]["attention_task_id"]["enum"]
            self.assertIn(task["id"], enums)
            return json.dumps(
                self.analysis(
                    tasks=[], needs_attention=True, attention_task_id=task["id"]
                )
            )

        self.runtime.backend.run = generate
        first = asyncio.create_task(
            self.runtime.engine.process(
                "c:", self.msg, "原问题", [], None, send_reply=False
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(
            self.runtime.engine.process(
                "overview:c",
                dict(self.msg, msg_id="file", from_id="me"),
                "补充文件",
                [],
                None,
                send_reply=False,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(len(calls), 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(self.hub.tasks()), 1)
        self.assertNotIn("enum", WORK_SCHEMA["properties"]["attention_task_id"])

    async def test_observation_preserves_all_batch_sources(self):
        captured = {}

        async def process(*args, **kwargs):
            captured["msg"] = args[1]

        self.runtime.engine.process = process
        batch = [self.msg, dict(self.msg, msg_id="second")]
        await self.runtime.analyze_messages(batch)
        self.assertEqual(captured["msg"]["_batch"], batch)

    async def test_background_batches_keep_threads_separate(self):
        pending = []
        self.runtime.spawn = lambda coro: pending.append(coro)
        try:
            for anchor in ["", "thread"]:
                self.runtime.on_saved(
                    dict(self.msg, anchor=anchor, from_id="me"), "李顺", "陈进", True
                )
            self.assertEqual(
                set(self.runtime.observations), {("c", ""), ("c", "thread")}
            )
        finally:
            for coro in pending:
                coro.close()

    def test_explicit_new_requests_with_same_title_stay_separate(self):
        self.create_original()
        self.hub.apply_analysis(
            dict(self.msg, msg_id="different-issue"),
            self.analysis(
                tasks=[{"task_id": 0, "title": "准备排期", "details": "另外项目的排期"}]
            ),
        )
        self.assertEqual(len(self.hub.tasks()), 2)
