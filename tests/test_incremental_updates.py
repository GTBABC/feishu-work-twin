import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event

from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from larkx.workhub.updates import Snapshot, StateUpdates
from tests.test_hub import FakeBackend


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.db"
        self.storage = Storage("sqlite:///" + str(self.path))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.runtime.hub.observe(
            {"chat_id": "c", "chat_type": 1, "from_id": "u"}, "同事", "同事"
        )
        self.app = create_app(self.runtime, start_bridge=False)
        self.client = self.enterContext(TestClient(self.app))

    def delta(self, previous):
        return self.client.get(
            "/api/updates",
            params={"since": previous["revision"], "epoch": previous["epoch"]},
        ).json()

    def test_idle_poll_only_reads_database_version(self):
        first = self.client.get("/api/updates").json()
        self.assertTrue(first["reset"])
        calls = []

        def query(conn, cursor, statement, *args):
            calls.append(statement)

        event.listen(self.storage.engine, "before_cursor_execute", query)
        try:
            with (
                patch.object(
                    self.runtime.hub,
                    "conversations",
                    side_effect=AssertionError("full reload"),
                ),
                patch.object(
                    self.runtime.hub,
                    "context",
                    side_effect=AssertionError("context reload"),
                ),
            ):
                for _ in range(3):
                    packet = self.delta(first)
                    self.assertEqual(packet["changes"], {})
                    self.assertEqual(packet["revision"], first["revision"])
        finally:
            event.remove(self.storage.engine, "before_cursor_execute", query)
        self.assertEqual(calls, ["PRAGMA data_version"] * 3)
        self.assertLess(len(json.dumps(packet)), 140)

    def test_task_update_and_view_deletion_are_deltas(self):
        task = self.runtime.hub.create_task({"title": "旧标题"})
        first = self.client.get("/api/updates").json()
        self.runtime.hub.update_task(task["id"], {"status": "done"})
        changed = self.delta(first)
        self.assertEqual(changed["changes"]["tasks"]["upsert"][0]["status"], "done")
        self.assertNotIn("personas", changed["changes"])
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("DELETE FROM hub_tasks WHERE id=?", (task["id"],))
        removed = self.delta(changed)
        self.assertEqual(removed["changes"]["tasks"]["removed"], [task["id"]])

    def test_external_connection_updates_and_status_only_changes(self):
        first = self.client.get("/api/updates").json()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("UPDATE hub_conversations SET name='新名称' WHERE chat_id='c'")
        packet = self.delta(first)
        self.assertEqual(
            packet["changes"]["conversations"]["upsert"][0]["name"], "新名称"
        )
        self.runtime.connection = "connected"
        with patch.object(
            self.runtime.hub, "tasks", side_effect=AssertionError("full reload")
        ):
            status = self.delta(packet)
        self.assertEqual(status["changes"], {"status": {"connection": "connected"}})

    def test_settings_and_persona_changes_are_visible_without_full_response(self):
        first = self.client.get("/api/updates").json()
        self.runtime.hub.set_setting("own_rules", "新规则")
        self.runtime.hub.update_persona("user:u", {"role": "负责人", "mode": "off"})
        packet = self.delta(first)
        self.assertEqual(packet["changes"]["status"], {"own_rules": "新规则"})
        self.assertEqual(packet["changes"]["personas"]["upsert"][0]["role"], "负责人")
        self.assertTrue(self.client.get("/api/updates?since=-1").status_code == 422)
        self.assertTrue(self.delta({"epoch": "restarted", "revision": 999})["reset"])

    def test_conversation_cache_and_existing_message_edit(self):
        msg = {
            "chat_id": "c",
            "msg_id": "m",
            "from_id": "u",
            "msg_type": 4,
            "content": "旧消息",
        }
        self.storage.save_message(msg)
        first = self.client.get("/api/conversations/c").json()

        def delta(previous):
            return self.client.get(
                "/api/conversations/c",
                params={"since": previous["revision"], "epoch": previous["epoch"]},
            ).json()

        with patch.object(
            self.runtime.hub, "messages", side_effect=AssertionError("full reload")
        ):
            self.assertEqual(delta(first)["changes"], {})
        self.storage.save_message(
            {**msg, "position": 1, "content": "服务器回读的新内容"}
        )
        packet = delta(first)
        self.assertEqual(
            packet["changes"]["messages"]["upsert"][0]["id"], first["messages"][0]["id"]
        )
        self.assertEqual(
            packet["changes"]["messages"]["upsert"][0]["content"], "服务器回读的新内容"
        )
        self.assertTrue(delta({"epoch": "old", "revision": 0})["reset"])

    def test_evicted_chat_resets_its_delta_epoch(self):
        first = self.client.get("/api/conversations/c").json()
        for n in range(8):
            self.client.get(f"/api/conversations/other-{n}")
        packet = self.client.get(
            "/api/conversations/c",
            params={"since": first["revision"], "epoch": first["epoch"]},
        ).json()
        self.assertTrue(packet["reset"])


class SnapshotTests(unittest.TestCase):
    def test_changes_across_missed_polls_preserve_order_and_deletions(self):
        s = Snapshot({"rows": "id"})
        s.update({"rows": [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}]})
        first = s.delta()
        s.update({"rows": [{"id": 2, "text": "new"}, {"id": 3, "text": "c"}]})
        s.update({"rows": [{"id": 3, "text": "c"}, {"id": 2, "text": "new"}]})
        packet = s.delta(first["revision"], first["epoch"])
        self.assertEqual(packet["changes"]["rows"]["removed"], [1])
        self.assertEqual(packet["changes"]["rows"]["order"], [3, 2])
        self.assertEqual(
            {row["id"] for row in packet["changes"]["rows"]["upsert"]}, {2, 3}
        )
        s.update({"rows": [{"id": 1, "text": "back"}]})
        self.assertNotIn(1, s.delta(0, s.epoch)["changes"]["rows"]["removed"])

    def test_tombstones_are_bounded_and_old_clients_reset(self):
        s = Snapshot({"rows": "id"})
        s.update({"rows": [{"id": n} for n in range(2100)]})
        first = s.delta()
        s.update({"rows": []})
        self.assertLessEqual(len(s.removed["rows"]), 2000)
        self.assertTrue(s.delta(first["revision"], first["epoch"])["reset"])

    def test_in_memory_database_commits_invalidate_cache(self):
        storage = Storage("sqlite:///:memory:")
        self.addCleanup(storage.engine.dispose)
        runtime = HubRuntime(storage, FakeBackend())
        updates = StateUpdates(runtime)
        self.addCleanup(updates.close)
        updates.state()
        runtime.hub.create_task({"title": "新的待办"})
        self.assertEqual(updates.state().data["tasks"][0]["title"], "新的待办")
