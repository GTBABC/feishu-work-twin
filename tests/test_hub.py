import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient
from larkx.storage import Storage
from larkx.workhub.store import HubStore
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app

ANALYSIS = {
    "should_reply": True,
    "reply_reason": "有明确工作请求",
    "reply": "我记下了，会跟进。",
    "overview": "同事需要一份排期，等待我准备。",
    "facts": ["同事需要一份排期。"],
    "tasks": [
        {
            "title": "准备排期",
            "details": "整理上线安排",
            "priority": "high",
            "due_at": "",
            "assignee": "self",
            "action_required": True,
            "evidence_ids": ["m1"],
        }
    ],
    "needs_attention": False,
    "attention_reason": "",
}


class FakeBackend:
    model = "fake"
    returns_reply = True

    async def run(self, *args, **kwargs):
        self.prompt = args[1]
        result = json.loads(json.dumps(ANALYSIS))
        schema = kwargs.get("response_schema", {})
        items = schema.get("properties", {}).get("tasks", {}).get("items", {})
        ids = (
            items.get("properties", {})
            .get("evidence_ids", {})
            .get("items", {})
            .get("enum", ["m1"])
        )
        result["tasks"][0]["evidence_ids"] = ids[:1]
        return json.dumps(result, ensure_ascii=False)


class HubTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.backend = FakeBackend()
        self.runtime = HubRuntime(self.storage, self.backend)
        self.runtime.hub.update_settings({"default_private_reply": "auto"})
        self.msg = {
            "msg_id": "m1",
            "chat_id": "c1",
            "from_id": "u1",
            "chat_type": 1,
            "content": "请准备上线排期",
            "anchor": "",
        }
        self.runtime.hub.observe(self.msg, "测试同事", "测试同事")

    def tearDown(self):
        self.storage.engine.dispose()
        self.temp.cleanup()

    async def test_analysis_saves_tasks_memory_and_uses_role(self):
        h = self.runtime.hub
        h.update_persona(
            "user:u1", {"role": "产品负责人", "description": "关心上线节奏"}
        )
        sent = []

        async def send(*args):
            sent.append(args)

        await self.runtime.engine.process("c1:", self.msg, "请准备排期", [], send)
        await self.runtime.engine.process(
            "c1:", self.msg, "请准备排期", [], send, send_reply=False
        )
        self.assertEqual(len(h.tasks()), 1)
        self.assertEqual(len(h.memories("c1")), 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("产品负责人", self.backend.prompt)
        self.assertIn("关心上线节奏", self.backend.prompt)
        self.assertTrue(h.context("c1")["overview"])

    async def test_group_observation_does_not_send(self):
        sent = []

        async def send(*args):
            sent.append(args)

        await self.runtime.engine.process(
            "c1:", self.msg, "message", [], send, send_reply=False
        )
        self.assertFalse(sent)
        self.assertEqual(len(self.runtime.hub.tasks()), 1)

    async def test_draft_mode_never_sends(self):
        self.runtime.hub.update_persona("user:u1", {"mode": "draft"})

        async def send(*args):
            self.fail("draft must not send")

        await self.runtime.engine.process("c1:", self.msg, "message", [], send)
        self.assertEqual(
            self.runtime.hub.reply_draft("c1")["content"], ANALYSIS["reply"]
        )
        self.assertFalse(
            any(t["title"].startswith("审核回复") for t in self.runtime.hub.tasks())
        )

    async def test_send_idempotency_and_uncertain_result(self):
        calls = []

        def send(text, chat_id, anchor):
            calls.append(text)
            return "sent-id"

        self.runtime.client = SimpleNamespace(
            auth=SimpleNamespace(user_id="me"), send_msg=send
        )
        self.runtime.connection = "connected"
        a = await self.runtime.send("c1", "测试", "request-1")
        b = await self.runtime.send("c1", "测试", "request-1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(a["status"], "sent")
        self.runtime.client.send_msg = lambda *args: None
        c = await self.runtime.send("c1", "不确定", "request-2")
        self.assertEqual(c["status"], "uncertain")
        self.assertFalse(self.runtime.hub.tasks())
        self.assertEqual(self.runtime.hub.uncertain_send_count(), 1)

    async def test_restart_does_not_replay_pending_sends(self):
        self.runtime.hub.claim_send("crash-request", "c1", "message")
        self.runtime.hub.new_job("j", "control", "message")
        self.runtime.hub.recover_interrupted()
        self.assertEqual(self.runtime.hub.actions()[0]["status"], "uncertain")
        self.assertEqual(self.runtime.hub.job("j")["status"], "failed")

    async def test_suffix_is_configurable_and_added_once(self):
        sent = []

        def send(text, *args):
            sent.append(text)
            return "s" + str(len(sent))

        self.runtime.client = SimpleNamespace(
            auth=SimpleNamespace(user_id="me"), send_msg=send
        )
        self.runtime.connection = "connected"
        await self.runtime.send("c1", "你好", "suffix-1")
        await self.runtime.send("c1", "你好\n\nreply by Bot", "suffix-2")
        self.assertEqual(sent[0], "你好\n\nreply by Bot")
        self.assertEqual(sent[1].count("reply by Bot"), 1)
        self.runtime.hub.set_setting("reply_suffix", "陈进的工作数字人")
        await self.runtime.send("c1", "收到", "suffix-3")
        self.assertTrue(sent[-1].endswith("陈进的工作数字人"))
        self.runtime.hub.set_setting("reply_suffix", "")
        await self.runtime.send("c1", "不加尾缀", "suffix-4")
        self.assertEqual(sent[-1], "不加尾缀")
        with self.assertRaises(ValueError):
            await self.runtime.send("c1", "   ", "suffix-empty")

    async def test_polishing_saves_draft_without_sending_or_completing_task(self):
        task = self.runtime.hub.create_task({"title": "准备排期", "chat_id": "c1"})
        self.runtime.hub.update_persona("user:u1", {"reply_style": "简洁直接"})
        payload = json.dumps(
            {
                "task_id": task["id"],
                "text": "我先看看再给你排期",
                "instruction": "语气自然",
            }
        )
        self.runtime.hub.new_job("draft-job", "task_draft", payload)

        async def no_send(*args, **kwargs):
            self.fail("polishing must not send")

        self.runtime.send = no_send
        await self.runtime.run_job("draft-job", "task_draft", payload, "")
        self.assertEqual(self.runtime.hub.job("draft-job")["status"], "done")
        updated = self.runtime.hub.tasks()[0]
        self.assertEqual(updated["draft_reply"], ANALYSIS["reply"])
        self.assertEqual(updated["status"], "open")
        self.assertIn("简洁直接", self.backend.prompt)
        self.assertIn("我先看看再给你排期", self.backend.prompt)

    async def test_read_only_control_cannot_send_even_if_model_suggests_it(self):
        action = {
            "type": "send_message",
            "chat_id": "c1",
            "content": "not allowed",
            "task_id": 0,
        }

        async def generate(*args, **kwargs):
            return json.dumps({"answer": "查询结果", "actions": [action]})

        self.runtime.backend.run = generate

        async def send(*args, **kwargs):
            self.fail("read only control must not send")

        self.runtime.send = send
        result = await self.runtime.engine.control(
            "查看待办，不要发送消息", "readonly-job"
        )
        self.assertFalse(result["actions"][0]["ok"])
        self.assertIn("不发送", result["actions"][0]["error"])

    async def test_conversation_polish_includes_context_and_never_sends(self):
        self.storage.save_message(self.msg, sender_name="测试同事")
        self.runtime.hub.update_persona(
            "user:u1", {"role": "产品负责人", "reply_style": "简洁直接"}
        )
        self.runtime.hub.apply_analysis(self.msg, ANALYSIS)

        async def no_send(*args, **kwargs):
            self.fail("conversation polishing must not send")

        self.runtime.send = no_send
        payload = json.dumps({"text": "我核对一下", "instruction": ""})
        self.runtime.hub.new_job(
            "conversation-draft-job", "conversation_draft", payload
        )
        before = self.runtime.hub.tasks()
        await self.runtime.run_job(
            "conversation-draft-job", "conversation_draft", payload, "c1"
        )
        self.assertEqual(
            self.runtime.hub.job("conversation-draft-job")["status"], "done"
        )
        for content in (
            "产品负责人",
            "简洁直接",
            "请准备上线排期",
            ANALYSIS["overview"],
            "准备排期",
            "我核对一下",
        ):
            self.assertIn(content, self.backend.prompt)
        self.assertEqual(self.runtime.hub.tasks(), before)
        self.assertFalse(self.runtime.hub.actions())

    async def test_human_handoff_rolls_per_chat_and_survives_restart(self):
        h = self.runtime.hub
        with patch("larkx.workhub.store.time.time", return_value=1000):
            h.touch_handoff("c1", manual=True)
            self.assertTrue(h.human_paused("c1"))
            self.assertFalse(h.human_paused("other"))
        with patch("larkx.workhub.store.time.time", return_value=1100):
            h.touch_handoff("c1")
            self.assertEqual(h.handoff("c1")["until"], 1220)
            h.touch_handoff("other")
            self.assertFalse(h.human_paused("other"))
        with patch("larkx.workhub.store.time.time", return_value=1210):
            h.touch_handoff("c1", manual=True)
            restarted = HubStore(self.storage)
            self.assertEqual(restarted.handoff("c1")["until"], 1330)
        with patch("larkx.workhub.store.time.time", return_value=1330):
            h.touch_handoff("c1")
            self.assertFalse(h.human_paused("c1"))

    async def test_handoff_blocks_inflight_reply_and_updates_memory(self):
        async def generate(*args, **kwargs):
            self.runtime.hub.touch_handoff("c1", manual=True)
            return json.dumps(ANALYSIS)

        self.runtime.backend.run = generate

        async def no_reply(*args):
            self.fail("owner intervention must block an in-flight reply")

        await self.runtime.engine.process("c1:", self.msg, "排期", [], no_reply)
        self.assertTrue(self.runtime.hub.tasks())
        self.assertTrue(self.runtime.hub.memories("c1"))
        self.assertFalse(
            any(t["title"].startswith("审核回复") for t in self.runtime.hub.tasks())
        )

    async def test_paused_reply_is_not_delayed_until_expiry_and_new_messages_resume(
        self,
    ):
        h = self.runtime.hub
        with patch("larkx.workhub.store.time.time", return_value=1000):
            h.touch_handoff("c1", manual=True)

            async def generate(*args, **kwargs):
                # The model finishes after the window expires.
                clock.return_value = 1121
                return json.dumps(ANALYSIS)

            self.runtime.backend.run = generate

            async def no_reply(*args):
                self.fail("paused messages must not be sent after expiry")

            with patch("larkx.workhub.store.time.time", return_value=1100) as clock:
                await self.runtime.engine.process("c1:", self.msg, "排期", [], no_reply)
        self.runtime.backend = FakeBackend()
        sent = []

        async def reply(*args):
            sent.append(args)

        with patch("larkx.workhub.store.time.time", return_value=1121):
            await self.runtime.engine.process("c1:", self.msg, "新的消息", [], reply)
        self.assertEqual(len(sent), 1)

    async def test_manual_web_send_pauses_but_bot_send_does_not(self):
        self.runtime.client = SimpleNamespace(
            auth=SimpleNamespace(user_id="me"), send_msg=lambda *args: "sent-manual"
        )
        self.runtime.connection = "connected"
        await self.runtime.send("c1", "自动回复", "bot-send")
        self.assertFalse(self.runtime.hub.human_paused("c1"))
        await self.runtime.send("c1", "人工回复", "manual-send", manual=True)
        self.assertTrue(self.runtime.hub.human_paused("c1"))

    async def test_feishu_manual_echo_and_queued_messages_respect_handoff(self):
        self.runtime.spawn = lambda coro: coro.close()
        own = dict(self.msg, msg_id="own-1", create_time=1000)
        with patch("larkx.workhub.store.time.time", return_value=1000):
            self.runtime.on_saved(own, "测试同事", "我", True)
            self.assertTrue(self.runtime.hub.human_paused("c1"))
        inbound = dict(self.msg, create_time=1100)
        with patch("larkx.workhub.store.time.time", return_value=1100):
            self.runtime.on_saved(inbound, "测试同事", "测试同事", False)
            self.assertTrue(inbound["_human_handled"])
            self.assertEqual(self.runtime.hub.handoff("c1")["until"], 1220)

        async def no_reply(*args):
            self.fail(
                "messages received during handoff must not reply from a delayed queue"
            )

        with patch("larkx.workhub.store.time.time", return_value=1221):
            await self.runtime.engine.process("c1:", inbound, "排期", [], no_reply)
        self.assertTrue(self.runtime.hub.memories("c1"))

    async def test_tracked_bot_echo_does_not_trigger_handoff(self):
        self.runtime.spawn = lambda coro: coro.close()
        a, _ = self.runtime.hub.claim_send("echo-test", "c1", "自动回复")
        self.runtime.hub.finish_action(a["id"], "sent", "echo-id")
        self.runtime.on_saved(dict(self.msg, msg_id="echo-id"), "测试同事", "我", True)
        self.assertFalse(self.runtime.hub.human_paused("c1"))

    async def test_contextual_no_reply_still_saves_work_and_creates_no_review(self):
        async def generate(*args, **kwargs):
            return json.dumps(
                {
                    **ANALYSIS,
                    "should_reply": False,
                    "reply_reason": "对话结束",
                    "reply": "多余的确认",
                }
            )

        self.runtime.backend.run = generate
        self.runtime.hub.update_persona("user:u1", {"mode": "draft"})

        async def no_reply(*args):
            self.fail("AI should_reply=false must not send")

        result = await self.runtime.engine.process(
            "c1:", self.msg, "收到", [], no_reply
        )
        self.assertEqual(result["reply"], "")
        self.assertTrue(self.runtime.hub.memories("c1"))
        self.assertEqual(len(self.runtime.hub.tasks()), 1)
        self.assertFalse(
            any(t["title"].startswith("审核回复") for t in self.runtime.hub.tasks())
        )

    async def test_person_switch_blocks_only_that_user_and_blocks_inflight_reply(self):
        async def generate(*args, **kwargs):
            self.runtime.hub.update_persona("user:u1", {"mode": "off"})
            return json.dumps(ANALYSIS)

        self.runtime.backend.run = generate

        async def no_reply(*args):
            self.fail("disabled contact must not auto reply")

        await self.runtime.engine.process("c1:", self.msg, "请求", [], no_reply)
        self.runtime.hub.observe(
            {"chat_id": "c2", "from_id": "u2", "chat_type": 1}, "另一同事", "另一同事"
        )
        self.runtime.backend = FakeBackend()
        sent = []

        async def reply(*args):
            sent.append(args)

        await self.runtime.engine.process(
            "c2:", {**self.msg, "chat_id": "c2", "from_id": "u2"}, "请求", [], reply
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "c2")

    async def test_disabled_person_does_not_reply_in_enabled_group(self):
        msg = {**self.msg, "chat_id": "g1", "chat_type": 2}
        self.runtime.hub.observe(msg, "测试群", "测试同事")
        second = {**msg, "msg_id": "m2", "from_id": "u2"}
        self.runtime.hub.observe(second, "测试群", "另一同事")
        self.runtime.hub.update_persona("group:g1", {"group_reply_policy": "all"})

        async def generate(*args, **kwargs):
            self.runtime.hub.update_persona("user:u1", {"mode": "off"})
            return json.dumps(ANALYSIS)

        self.runtime.backend.run = generate

        async def no_reply(*args):
            self.fail("disabled person must not get automatic group replies")

        for batch in ([msg], [msg, second]):
            with self.subTest(senders=[m["from_id"] for m in batch]):
                self.runtime.hub.update_persona("user:u1", {"mode": "auto"})
                with patch.object(
                    self.runtime.hub, "context", wraps=self.runtime.hub.context
                ) as context:
                    await self.runtime.engine.process(
                        "g1:", dict(batch[-1], _batch=batch), "请求", [], no_reply
                    )
                self.assertEqual(context.call_count, 2)


class ApiTests(unittest.TestCase):
    def test_edit_tasks_profiles_and_cross_origin_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            storage = Storage("sqlite:///" + str(Path(folder) / "api.db"))
            runtime = HubRuntime(storage, FakeBackend())
            runtime.hub.observe(
                {"chat_id": "c", "from_id": "u", "chat_type": 1}, "测试", "测试"
            )
            with TestClient(create_app(runtime, start_bridge=False)) as client:
                self.assertEqual(client.get("/api/state").status_code, 200)
                self.assertEqual(
                    client.post("/api/tasks", json={"title": "测试"}).status_code, 403
                )
                headers = {"X-Workhub": "1"}
                self.assertEqual(
                    client.post(
                        "/api/tasks",
                        json={"title": "测试"},
                        headers={**headers, "Origin": "https://evil.invalid"},
                    ).status_code,
                    403,
                )
                task = client.post(
                    "/api/tasks", json={"title": "测试"}, headers=headers
                ).json()
                updated = client.patch(
                    f"/api/tasks/{task['id']}", json={"status": "done"}, headers=headers
                )
                self.assertEqual(updated.json()["status"], "done")
                profile = client.patch(
                    "/api/personas/user:u", json={"role": "同事"}, headers=headers
                )
                self.assertEqual(profile.json()["role"], "同事")
            storage.engine.dispose()


if __name__ == "__main__":
    unittest.main()
