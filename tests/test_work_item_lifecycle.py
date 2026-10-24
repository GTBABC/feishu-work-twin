from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

from larkx.storage import Storage
from larkx.workhub.listing import page, totals
from larkx.workhub.models import Task, TaskArchive
from larkx.workhub.runtime import HubRuntime, SendUncertainError
from larkx.workhub.server import create_app
from larkx.workhub.store import HubStore
from tests.test_hub import ANALYSIS, FakeBackend


class WorkItemLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.storage = Storage("sqlite:///" + str(self.temp / "test.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.addAsyncCleanup(self.runtime.stop)
        self.hub = self.runtime.hub
        self.msg = {
            "msg_id": "m1",
            "chat_id": "c",
            "chat_type": 1,
            "from_id": "them",
            "content": "明天把报告发给我",
            "anchor": "",
        }
        self.hub.observe(self.msg, "同事", "同事")
        self.runtime.client = SimpleNamespace(auth=SimpleNamespace(user_id="me"))

    async def analyze(self, **changes):
        data = {**deepcopy(ANALYSIS), **changes}

        async def model(*args, **kwargs):
            return json.dumps(data)

        self.runtime.backend.run = model
        return await self.runtime.engine.process(
            "c:", self.msg, self.msg["content"], [], None, send_reply=False
        )

    async def test_new_work_requires_self_pending_action_and_real_message_evidence(
        self,
    ):
        task = deepcopy(ANALYSIS["tasks"][0])
        for patch in (
            {"assignee": "other"},
            {"assignee": "unclear"},
            {"action_required": False},
            {"evidence_ids": ["invented"]},
            {"evidence_ids": []},
        ):
            await self.analyze(tasks=[{**task, **patch}])
            self.assertFalse(self.hub.tasks())
        # Different messages avoid the intentional replay marker.
        self.msg["msg_id"] = "real"
        await self.analyze(tasks=[{**task, "evidence_ids": ["real"]}])
        self.assertEqual(len(self.hub.tasks()), 1)

    async def test_acknowledgments_and_emoji_do_not_create_phantom_work(self):
        for index, text in enumerate(("好的", "谢谢！", "收到", "🙂", "👍")):
            self.msg.update(msg_id=f"ack-{index}", content=text)
            task = {
                **deepcopy(ANALYSIS["tasks"][0]),
                "evidence_ids": [self.msg["msg_id"]],
            }
            await self.analyze(
                tasks=[task], needs_attention=True, attention_reason="缺少上下文"
            )
        self.assertFalse(self.hub.tasks())

    async def test_answered_question_and_bare_uncertainty_create_no_task(self):
        await self.analyze(
            tasks=[], needs_attention=True, attention_reason="不知道怎么回答"
        )
        self.assertFalse(self.hub.tasks())

    async def test_group_request_to_someone_else_is_not_assigned_to_owner(self):
        self.msg.update(chat_type=2, at_user_ids=["other-person"])
        await self.analyze()
        self.assertFalse(self.hub.tasks())

    async def test_reply_off_can_still_record_real_work_without_sending(self):
        self.hub.update_settings({"default_private_reply": "off"})
        await self.analyze()
        self.assertEqual(len(self.hub.tasks()), 1)
        self.assertFalse(self.hub.actions())

    def test_attention_updates_existing_work_and_never_creates_a_placeholder(self):
        self.hub.apply_analysis(self.msg, {"tasks": [], "needs_attention": True})
        self.assertFalse(self.hub.tasks())
        task = self.hub.create_task({"title": "确认报价", "chat_id": "c"})
        self.hub.apply_analysis(
            {**self.msg, "msg_id": "price"},
            {
                "tasks": [],
                "needs_attention": True,
                "attention_task_id": task["id"],
                "attention_reason": "需要确认最终价格",
            },
            allowed_task_ids=[task["id"]],
        )
        self.assertEqual(len(self.hub.tasks()), 1)
        self.assertEqual(self.hub.task(task["id"])["status"], "needs_attention")

    def test_incidents_merge_across_chats_and_recover_without_business_tasks(self):
        first = self.hub.report_incident(
            "Codex 生成回复超时", stage="reply", provider="cli:codex", chat_id="c"
        )
        second = self.hub.report_incident(
            "Codex timeout", stage="analysis", provider="cli:codex", chat_id="other"
        )
        self.assertEqual(first["id"], second["id"])
        self.assertIn("累计 2 次", second["content"])
        self.assertEqual(self.hub.incident_count(), 1)
        self.assertFalse(self.hub.tasks())
        self.hub.resolve_incidents("cli:codex", "other")
        self.assertEqual(self.hub.incident_count(), 0)
        self.assertEqual(self.hub.actions()[0]["status"], "resolved")
        again = self.hub.report_incident(
            "timeout", stage="reply", provider="cli:codex", chat_id="c"
        )
        self.assertNotEqual(again["id"], first["id"])
        self.assertEqual(len(self.hub.actions()), 2)

    def test_chat_local_failure_does_not_resolve_an_unrelated_chat(self):
        self.hub.report_incident(
            "解析结果结构错误", stage="analysis", provider="api:qwen", chat_id="c"
        )
        self.hub.resolve_incidents("api:qwen", "other")
        self.assertEqual(self.hub.incident_count(), 1)
        self.hub.resolve_incidents("api:qwen", "c")
        self.assertEqual(self.hub.incident_count(), 0)

    async def test_background_failure_is_one_incident_with_preserved_input(self):
        async def fail(*args, **kwargs):
            raise RuntimeError("model timeout")

        self.runtime.analyze_messages = fail
        self.hub.update_settings({"background_interval": 1})
        self.runtime.observations[("c", "")] = [self.msg]
        with unittest.mock.patch(
            "larkx.workhub.runtime.asyncio.sleep", return_value=None
        ):
            await self.runtime.observe_batch(("c", ""))
            await self.runtime.observe_batch(("c", ""))
        self.assertEqual(self.runtime.observations[("c", "")], [self.msg])
        self.assertEqual(self.hub.incident_count(), 1)
        self.assertFalse(self.hub.tasks())

    def test_uncertain_send_uses_one_execution_record_and_can_be_checked(self):
        action, _ = self.hub.claim_send("uncertain-1", "c", "回复正文")
        self.hub.finish_action(action["id"], "uncertain", error="结果未确认")
        self.runtime.report_processing_error(
            SendUncertainError("发送未确认"), self.msg, "reply"
        )
        self.assertEqual(len(self.hub.actions()), 1)
        self.assertEqual(self.hub.uncertain_send_count(), 1)
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            denied = client.post(f"/api/actions/{action['id']}/checked")
            self.assertEqual(denied.status_code, 403)
            response = client.post(
                f"/api/actions/{action['id']}/checked", headers={"X-Workhub": "1"}
            )
            self.assertEqual(response.json()["status"], "checked")
        self.assertEqual(self.hub.uncertain_send_count(), 0)
        self.assertFalse(self.hub.tasks())

    def test_drafts_survive_restart_and_clear_only_the_matching_version(self):
        draft = self.hub.save_reply_draft("c", "thread", "第一版", "m1")
        newer = self.hub.save_reply_draft("c", "thread", "第二版", "m2")
        self.assertFalse(
            self.hub.clear_reply_draft("c", draft["key"], draft["updated_at"])
        )
        self.assertFalse(
            self.hub.clear_reply_draft("other", newer["key"], newer["updated_at"])
        )
        restarted = HubStore(self.storage)
        self.assertEqual(restarted.reply_draft("c")["content"], "第二版")
        self.assertTrue(
            restarted.clear_reply_draft("c", newer["key"], newer["updated_at"])
        )
        self.assertIsNone(restarted.reply_draft("c"))
        self.assertFalse(self.hub.tasks())

    def test_migration_preserves_snapshots_backs_up_and_is_idempotent(self):
        technical = self.hub.create_task(
            {
                "title": "自动处理异常：同事",
                "details": "Codex 生成回复超时",
                "chat_id": "c",
            }
        )
        self.hub.update_task(technical["id"], {"status": "done"})
        before = self.hub.task(technical["id"])
        self.hub.create_task(
            {"title": "会话分析异常", "details": "JSON 解析失败", "chat_id": "c"}
        )
        self.hub.create_task(
            {"title": "审核回复：同事", "draft_reply": "请查看报告", "chat_id": "c"}
        )
        business = self.hub.create_task(
            {"title": "排查客户的超时问题", "details": "Codex 报错", "chat_id": "c"}
        )
        planned = self.hub.create_task(
            {
                "title": "会话分析异常",
                "details": "排查 Codex 超时",
                "due_at": "明天",
                "chat_id": "c",
            }
        )
        result = self.hub.migrate_work_items(force=True)
        self.assertEqual(result["archived"], 3)
        with self.storage.Session() as session:
            self.assertEqual(
                json.loads(session.get(TaskArchive, technical["id"]).snapshot), before
            )
            self.assertEqual(session.get(Task, technical["id"]).status, "archived")
        with closing(sqlite3.connect(result["backup"])) as connection:
            self.assertEqual(
                connection.execute(
                    "select status from hub_tasks where id=?", (technical["id"],)
                ).fetchone()[0],
                "done",
            )
        self.assertEqual(
            {t["id"] for t in self.hub.tasks()}, {business["id"], planned["id"]}
        )
        self.assertEqual(totals(self.hub)["completed"], 0)
        self.assertEqual(page(self.hub, "tasks", status="all")["total"], 2)
        self.assertEqual(page(self.hub, "actions", status="archived")["total"], 3)
        self.assertEqual(self.hub.reply_draft("c")["content"], "请查看报告")
        self.assertEqual(self.hub.migrate_work_items(force=True)["archived"], 0)
        self.assertEqual(len(list((self.temp / "backups").glob("*.db"))), 1)
        with self.assertRaises(ValueError):
            self.hub.update_task(technical["id"], {"status": "open"})

    def test_waiting_total_matches_pending_page_and_excludes_in_progress(self):
        for status in ("open", "needs_attention", "in_progress", "done"):
            self.hub.create_task({"title": status, "status": status})
        self.assertEqual(totals(self.hub)["waiting"], 2)
        self.assertEqual(
            totals(self.hub)["waiting"], page(self.hub, "tasks", status="open")["total"]
        )

    def test_draft_endpoint_rejects_old_version_and_keeps_the_new_draft(self):
        first = self.hub.save_reply_draft("c", "", "第一版", "m1")
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            self.assertEqual(
                client.get("/api/conversations/c").json()["draft"]["content"], "第一版"
            )
            newest = self.hub.save_reply_draft("c", "", "第二版", "m2")
            response = client.post(
                "/api/conversations/c/draft/clear",
                headers={"X-Workhub": "1"},
                json={"key": first["key"], "version": first["updated_at"]},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(self.hub.reply_draft("c")["content"], "第二版")
            response = client.post(
                "/api/conversations/c/draft/clear",
                headers={"X-Workhub": "1"},
                json={"key": newest["key"], "version": newest["updated_at"]},
            )
            self.assertTrue(response.json()["cleared"])
            self.assertEqual(client.get("/api/conversations/c").json()["draft"], {})

    def test_old_send_review_tasks_reuse_one_uncertain_record(self):
        action, _ = self.hub.claim_send("legacy-send", "c", "待核对的消息")
        self.hub.finish_action(action["id"], "uncertain", error="发送未确认")
        for _ in range(2):
            self.hub.create_task(
                {
                    "title": "核对消息是否发送成功",
                    "chat_id": "c",
                    "details": "待核对的消息",
                }
            )
        result = self.hub.migrate_work_items(force=True)
        self.assertEqual(result["archived"], 2)
        self.assertEqual(self.hub.uncertain_send_count(), 1)
        self.assertFalse(self.hub.tasks())
        self.assertEqual(self.hub.migrate_work_items(force=True)["archived"], 0)

    def test_migration_failure_rolls_back_instead_of_partially_archiving(self):
        from sqlalchemy.exc import IntegrityError

        first = self.hub.create_task(
            {"title": "会话分析异常", "details": "Codex timeout"}
        )
        second = self.hub.create_task(
            {"title": "会话分析异常", "details": "Codex timeout"}
        )
        with self.storage.Session() as session:
            session.add(
                TaskArchive(
                    task_id=second["id"], reason="technical_error", snapshot="{}"
                )
            )
            session.commit()
        with self.assertRaises(IntegrityError):
            self.hub.migrate_work_items(force=True)
        self.assertEqual(self.hub.task(first["id"])["status"], "open")
        self.assertEqual(self.hub.task(second["id"])["status"], "open")
        with self.storage.Session() as session:
            self.assertIsNone(session.get(TaskArchive, first["id"]))
        self.assertFalse(self.hub.actions())
        self.assertEqual(len(list((self.temp / "backups").glob("*.db"))), 1)

    def test_completed_upgrade_does_not_reclassify_new_manual_tasks_on_restart(self):
        task = self.hub.create_task(
            {"title": "会话分析异常", "details": "人工安排排查 Codex 超时"}
        )
        restarted = HubStore(self.storage)
        self.assertEqual(restarted.work_item_migration["archived"], 0)
        self.assertEqual(restarted.task(task["id"])["status"], "open")


if __name__ == "__main__":
    unittest.main()
