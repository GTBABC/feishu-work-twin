import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event

from larkx.storage import Storage
from larkx.workhub.models import Action, Conversation, Persona, Task
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from tests.test_hub import FakeBackend


class ListPageTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage("sqlite:///" + str(Path(folder) / "test.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        hub = self.runtime.hub
        hub.update_settings({"completed_task_limit": 10, "actions_limit": 10})
        with self.storage.Session() as session:
            stamp = datetime(2026, 1, 1, 12)
            for i in range(31):
                session.add(
                    Task(
                        title=f"事项 {i}", status="done", created_at=stamp, chat_id="c0"
                    )
                )
                session.add(
                    Action(
                        request_id=f"a{i}",
                        content=f"记录 {i}",
                        status="sent" if i % 2 else "failed",
                        chat_id="c0",
                        created_at=stamp,
                    )
                )
            session.add(Task(title="100%_进度", status="needs_attention"))
            for i in range(13):
                kind = "private" if i % 2 else "group"
                session.add(
                    Conversation(
                        chat_id=f"c{i}",
                        name=f"联系 {i}",
                        kind=kind,
                        persona_key=f"p{i}",
                        updated_at=stamp,
                    )
                )
                session.add(
                    Persona(
                        key=f"p{i}",
                        name=f"联系 {i}",
                        kind="person" if kind == "private" else "group",
                        chat_id=f"c{i}",
                    )
                )
            session.commit()
        self.client = self.enterContext(
            TestClient(create_app(self.runtime, start_bridge=False))
        )

    def page(self, key, **params):
        response = self.client.get(f"/api/lists/{key}", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_all_completed_tasks_and_actions_remain_accessible_beyond_legacy_caps(self):
        for key, params in (("tasks", {"status": "done"}), ("actions", {})):
            ids = []
            for number in range(1, 6):
                data = self.page(key, page=number, size=7, **params)
                self.assertEqual(data["total"], 31)
                self.assertLessEqual(len(data["items"]), 7)
                ids.extend(row["id"] for row in data["items"])
            self.assertEqual(len(set(ids)), 31)
            self.assertEqual(ids, sorted(ids, reverse=True))
            self.assertEqual(self.page(key, page=999, size=7, **params)["page"], 5)

    def test_search_filters_and_literal_wildcards_apply_before_pagination(self):
        data = self.page("tasks", query="%_", status="open", size=1)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["title"], "100%_进度")
        for key in ("personas", "conversations"):
            data = self.page(key, kind="private", size=2)
            self.assertEqual(data["total"], 6)
            self.assertEqual(len(data["items"]), 2)
        data = self.page(
            "actions",
            query="联系 0",
            status="failed",
            start="2026-01-01",
            end="2026-01-01",
        )
        self.assertEqual(data["total"], 16)
        self.assertEqual(data["items"][0]["chat_name"], "联系 0")
        self.assertEqual(self.page("actions", start="2026-01-02")["total"], 0)

    def test_web_snapshot_contains_counts_and_four_recent_contacts_not_full_lists(self):
        with (
            patch.object(
                self.runtime.hub, "tasks", side_effect=AssertionError("full tasks")
            ),
            patch.object(
                self.runtime.hub, "actions", side_effect=AssertionError("full actions")
            ),
        ):
            data = self.client.get("/api/updates?scope=web").json()
        changes = data["changes"]
        self.assertEqual(changes["tasks"]["upsert"], [])
        self.assertEqual(changes["personas"]["upsert"], [])
        self.assertEqual(len(changes["conversations"]["upsert"]), 4)
        self.assertEqual(changes["status"]["totals"]["completed"], 31)
        self.assertEqual(changes["status"]["totals"]["attention"], 1)
        self.assertEqual(changes["status"]["totals"]["contacts"], 6)
        statements = []

        def capture(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(self.storage.engine, "before_cursor_execute", capture)
        try:
            delta = self.client.get(
                "/api/updates",
                params={
                    "scope": "web",
                    "since": data["revision"],
                    "epoch": data["epoch"],
                },
            ).json()
        finally:
            event.remove(self.storage.engine, "before_cursor_execute", capture)
        self.assertEqual(delta["changes"], {})
        self.assertEqual(statements, ["PRAGMA data_version"])

    def test_page_queries_are_bounded_in_sql_and_revision_changes_after_edit(self):
        first = self.page("personas", size=3)
        statements = []

        def capture(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(self.storage.engine, "before_cursor_execute", capture)
        try:
            second = self.page("personas", size=3, page=2)
        finally:
            event.remove(self.storage.engine, "before_cursor_execute", capture)
        self.assertFalse(
            set(p["key"] for p in first["items"])
            & set(p["key"] for p in second["items"])
        )
        self.assertTrue(
            any(
                "LIMIT" in statement and "OFFSET" in statement
                for statement in statements
            )
        )
        self.runtime.hub.update_persona(first["items"][0]["key"], {"role": "更新"})
        self.assertGreater(self.page("personas")["revision"], first["revision"])

    def test_invalid_page_filter_and_dates_are_rejected(self):
        for params in ({"size": 0}, {"size": 101}, {"page": -1}, {"kind": "invalid"}):
            self.assertGreaterEqual(
                self.client.get("/api/lists/conversations", params=params).status_code,
                400,
            )
        for params in (
            {"status": "invalid"},
            {"start": "bad"},
            {"start": "2026-02-02", "end": "2026-01-01"},
        ):
            self.assertEqual(
                self.client.get("/api/lists/actions", params=params).status_code, 400
            )

    def test_older_messages_are_disjoint_and_stop_at_the_beginning(self):
        for i in range(105):
            self.storage.save_message(
                {
                    "msg_id": f"m{i}",
                    "chat_id": "c0",
                    "from_id": "u",
                    "content": str(i),
                    "create_time": 1000 + i % 17,
                }
            )
        initial = self.client.get("/api/conversations/c0").json()
        before = initial["history"]["next_before_id"]
        self.assertIsNotNone(before)
        older = self.client.get(
            "/api/conversations/c0/history", params={"before_id": before}
        ).json()
        self.assertEqual(len(older["messages"]) + len(initial["messages"]), 105)
        self.assertIsNone(older["next_before_id"])
        self.assertFalse(
            set(m["id"] for m in initial["messages"])
            & set(m["id"] for m in older["messages"])
        )
