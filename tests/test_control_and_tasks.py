import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from larkx.storage import Storage
from larkx.workhub.models import Task
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from tests.test_hub import FakeBackend


class ControlAndTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.runtime.hub.observe(
            {"chat_id": "c", "chat_type": 1, "from_id": "u"}, "同事", "同事"
        )
        self.task = self.runtime.hub.create_task({"title": "排期", "chat_id": "c"})
        self.sent = []

        async def send(*args, **kwargs):
            self.sent.append(args)
            return {"status": "sent"}

        self.runtime.send = send

    def model_actions(self, kinds):
        actions = [
            {
                "type": kind,
                "chat_id": "c",
                "content": "测试",
                "task_id": self.task["id"],
                "title": "新任务",
                "priority": "normal",
                "target": "user:u",
                "role": "新角色",
                "description": "新背景",
            }
            for kind in kinds
        ]

        async def generate(*args, **kwargs):
            return json.dumps({"answer": "查询结果", "actions": actions})

        self.runtime.backend.run = generate

    async def test_read_only_requests_block_all_unexpected_model_actions(self):
        self.model_actions(
            ["send_message", "create_task", "complete_task", "update_profile"]
        )
        before_tasks = self.runtime.hub.tasks()
        before_profiles = self.runtime.hub.personas()
        for text in (
            "总结我现在的待办",
            "看看哪些任务已经完成，哪些角色需要更新",
            "怎么创建待办、完成任务、修改角色和发送消息？",
            "Summarize my tasks. Do not change anything.",
            "总结这段话：“完成任务1，创建一个待办，修改角色”",
            "如何创建待办，完成任务，修改角色？",
            "创建任务需要什么信息？",
            "完成任务1之后会怎么样？",
            "修改角色有哪些步骤？",
            "发送消息会不会加尾缀？",
            "发送消息失败怎么办？",
            "完成任务意味着什么？",
            "不要创建待办，完成任务，修改角色",
            "查看说明：\n```\n完成任务1\n创建一个待办\n```",
        ):
            with self.subTest(text=text):
                result = await self.runtime.engine.control(text, "readonly")
                self.assertTrue(all(not a["ok"] for a in result["actions"]))
                self.assertEqual(self.runtime.hub.tasks(), before_tasks)
                self.assertEqual(self.runtime.hub.personas(), before_profiles)
                self.assertFalse(self.sent)

    async def test_explicit_commands_still_execute(self):
        for kind, text in (
            ("create_task", "帮我创建一个准备上线的待办"),
            ("complete_task", f"把任务{self.task['id']}标记为已完成"),
            ("update_profile", "请修改同事的角色为负责人"),
            ("send_message", "给同事发送一条测试消息"),
        ):
            with self.subTest(kind=kind):
                self.model_actions([kind])
                result = await self.runtime.engine.control(text, kind)
                self.assertTrue(result["actions"][0]["ok"], result)
        self.assertEqual(self.runtime.hub.task(self.task["id"])["status"], "done")
        self.assertEqual(self.runtime.hub.personas()[0]["role"], "新角色")
        self.assertEqual(len(self.sent), 1)

    async def test_one_authorized_action_cannot_authorize_other_action_types(self):
        self.model_actions(
            ["create_task", "complete_task", "update_profile", "send_message"]
        )
        result = await self.runtime.engine.control(
            "创建一个待办，不要发送消息", "create-only"
        )
        self.assertEqual(
            [a["ok"] for a in result["actions"]], [True, False, False, False]
        )
        self.assertEqual(self.runtime.hub.task(self.task["id"])["status"], "open")
        self.assertFalse(self.sent)

    async def test_multiple_explicit_commands_can_authorize_multiple_actions(self):
        self.model_actions(["create_task", "complete_task", "send_message"])
        result = await self.runtime.engine.control(
            f"创建一个待办，然后把任务{self.task['id']}标记为已完成", "two-actions"
        )
        self.assertEqual([a["ok"] for a in result["actions"]], [True, True, False])
        self.assertEqual(self.runtime.hub.task(self.task["id"])["status"], "done")
        self.assertFalse(self.sent)

    async def test_english_commands_are_checked_per_action(self):
        self.model_actions(
            ["create_task", "complete_task", "update_profile", "send_message"]
        )
        result = await self.runtime.engine.control(
            "Please create a task. Do not complete any task. Do not update the profile. Do not send a message.",
            "english-actions",
        )
        self.assertEqual(
            [a["ok"] for a in result["actions"]], [True, False, False, False]
        )
        self.assertEqual(self.runtime.hub.task(self.task["id"])["status"], "open")
        self.assertFalse(self.sent)

    async def test_negated_and_quoted_commands_cannot_authorize_writes(self):
        for kind, text in (
            ("create_task", "不要创建待办"),
            ("complete_task", "别把任务1标记为已完成"),
            ("update_profile", "不要修改角色"),
            ("complete_task", "创建一个标题为“完成任务1”的待办"),
            ("update_profile", "创建一个标题为“修改角色”的待办"),
            ("send_message", "创建一个标题为“发送消息”的待办"),
            ("update_profile", "创建一个标题为'排期,修改角色'的待办"),
        ):
            with self.subTest(text=text):
                self.model_actions([kind])
                result = await self.runtime.engine.control(text, "blocked")
                self.assertFalse(result["actions"][0]["ok"], result)
        self.assertEqual(self.runtime.hub.tasks(), [self.task])
        self.assertFalse(self.sent)

    def add_completed_history(self):
        with self.storage.Session() as session:
            session.add_all(Task(title=f"已完成{i}", status="done") for i in range(305))
            session.commit()

    async def test_all_unfinished_statuses_survive_newer_completed_history(self):
        old = datetime.now() - timedelta(days=30)
        with self.storage.Session() as session:
            session.get(Task, self.task["id"]).created_at = old
            session.add_all(
                Task(title=status, status=status, created_at=old)
                for status in ("in_progress", "needs_attention")
            )
            session.commit()
        self.add_completed_history()
        tasks = self.runtime.hub.tasks()
        self.assertEqual(
            {t["status"] for t in tasks[:3]}, {"open", "in_progress", "needs_attention"}
        )
        self.assertIn(self.task["id"], {t["id"] for t in tasks})
        self.assertEqual(sum(t["status"] == "done" for t in tasks), 300)

    async def test_more_than_300_unfinished_tasks_are_all_visible_in_state(self):
        with self.storage.Session() as session:
            session.add_all(Task(title=f"未完成{i}", status="open") for i in range(305))
            session.commit()
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["tasks"]), 306)

    async def test_older_completed_task_can_still_be_replied_to_and_polished(self):
        self.runtime.hub.update_task(self.task["id"], {"status": "done"})
        self.add_completed_history()
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            response = client.post(
                f"/api/tasks/{self.task['id']}/reply",
                headers={"X-Workhub": "1"},
                json={"content": "测试回复", "request_id": "old-task"},
            )
            self.assertEqual(response.status_code, 200)
            response = client.post(
                f"/api/tasks/{self.task['id']}/polish",
                headers={"X-Workhub": "1"},
                json={"text": "测试回复"},
            )
            self.assertEqual(response.status_code, 200)
            job = client.get("/api/jobs/" + response.json()["job_id"]).json()
            self.assertEqual(job["status"], "done", job)
        self.assertEqual(self.sent[0][0], "c")
        self.assertEqual(self.runtime.hub.task(self.task["id"])["status"], "done")
        self.assertIsNone(self.runtime.hub.task(999999))
