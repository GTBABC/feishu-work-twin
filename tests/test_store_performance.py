import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import event

from larkx.storage import Storage
from larkx.storage.models import Message
from larkx.workhub.models import Conversation, Persona, Setting, Task
from larkx.workhub.store import HubStore


class StorePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "store.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.hub = HubStore(self.storage)

    @contextmanager
    def queries(self):
        statements = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(self.storage.engine, "before_cursor_execute", capture)
        try:
            yield statements
        finally:
            event.remove(self.storage.engine, "before_cursor_execute", capture)

    def history(self, rows):
        with self.storage.Session() as session:
            session.bulk_insert_mappings(
                Message,
                [
                    {
                        "msg_id": str(i),
                        "direction": "in",
                        "chat_id": "c",
                        "sender_id": "u",
                        "chat_type": 1,
                        "chat_name": "联系人",
                        "sender_name": "姓名",
                        **row,
                    }
                    for i, row in enumerate(rows)
                ],
            )
            session.commit()

    def test_conversation_list_has_fixed_query_count_and_preserves_fields(self):
        now = datetime.now()
        with self.storage.Session() as session:
            for i in range(25):
                group = i % 2 == 0
                key = f"group:c{i}" if group else f"user:u{i}"
                session.add(
                    Persona(
                        key=key, name=f"角色{i}", kind="group" if group else "person"
                    )
                )
                session.add(
                    Conversation(
                        chat_id=f"c{i}",
                        name=f"会话{i}",
                        persona_key=key,
                        updated_at=now + timedelta(seconds=i),
                    )
                )
                session.add_all(
                    [
                        Message(
                            msg_id="old",
                            chat_id=f"c{i}",
                            content="旧消息",
                            create_time=9999999,
                        ),
                        Message(
                            msg_id="new",
                            chat_id=f"c{i}",
                            content="新" * 150,
                            direction="out",
                            create_time=1,
                        ),
                        Task(title="普通", chat_id=f"c{i}", status="open"),
                        Task(title="进行中", chat_id=f"c{i}", status="in_progress"),
                        Task(title="待决定", chat_id=f"c{i}", status="needs_attention"),
                        Task(title="已完成", chat_id=f"c{i}", status="done"),
                        Setting(
                            key=f"human_pause:c{i}", value=json.dumps({"until": 1234})
                        ),
                    ]
                )
                if group:
                    session.add(Setting(key=f"group_reply_policy:c{i}", value="at_me"))
            session.add(
                Conversation(
                    chat_id="empty", name="无消息", updated_at=now - timedelta(days=1)
                )
            )
            session.commit()
        with self.queries() as queries:
            rows = self.hub.conversations()
        self.assertLessEqual(len(queries), 3)
        self.assertEqual(
            [r["chat_id"] for r in rows[:-1]], [f"c{i}" for i in reversed(range(25))]
        )
        for i, row in enumerate(rows[:-1]):
            self.assertEqual(row["last_message"], "新" * 120)
            self.assertEqual(row["open_tasks"], 3)
            self.assertEqual(row["human_pause_until"], 1234)
            self.assertEqual(row["persona"]["name"], f"角色{24 - i}")
            if row["persona"]["kind"] == "group":
                self.assertEqual(row["persona"]["group_reply_policy"], "at_me")
        self.assertIsNone(rows[-1]["persona"])
        self.assertEqual(rows[-1]["last_message"], "")
        self.assertEqual(rows[-1]["open_tasks"], 0)
        self.assertEqual(rows[-1]["human_pause_until"], 0)

    def test_persona_list_batches_group_policy_queries(self):
        with self.storage.Session() as session:
            session.add_all(
                Persona(key=f"group:g{i}", chat_id=f"g{i}", kind="group", name=f"群{i}")
                for i in range(20)
            )
            session.add(Setting(key="group_reply_policy:g1", value="all"))
            session.commit()
        with self.queries() as queries:
            profiles = self.hub.personas()
        self.assertLessEqual(len(queries), 2)
        policies = {p["chat_id"]: p["group_reply_policy"] for p in profiles}
        self.assertEqual(policies["g1"], "all")
        self.assertEqual(policies["g0"], "inherit")
        self.assertEqual(profiles[0]["effective_group_reply_policy"], "off")

    def test_seed_compresses_history_without_loading_message_objects(self):
        self.history([{"content": "大量文本" * 100} for _ in range(100)])
        loaded = []

        def capture(session, instance):
            if isinstance(instance, Message):
                loaded.append(instance)

        event.listen(self.storage.Session, "loaded_as_persistent", capture)
        try:
            with (
                self.queries() as queries,
                patch.object(self.hub, "observe", wraps=self.hub.observe) as observe,
            ):
                self.hub.seed_existing()
        finally:
            event.remove(self.storage.Session, "loaded_as_persistent", capture)
        self.assertFalse(loaded)
        self.assertEqual(observe.call_count, 0)
        self.assertLessEqual(len(queries), 15)
        self.assertEqual(len(self.hub.conversations()), 1)
        self.assertEqual(len(self.hub.personas()), 1)

    def test_seed_preserves_latest_nonempty_names_and_private_chat_mapping(self):
        times = [datetime(2026, 1, 1) + timedelta(seconds=i) for i in range(6)]
        self.history(
            [
                {
                    "chat_id": "p",
                    "sender_name": "旧姓名",
                    "chat_name": "私聊",
                    "stored_at": times[0],
                },
                {
                    "chat_id": "g",
                    "chat_type": 2,
                    "sender_name": "新姓名",
                    "chat_name": "项目群",
                    "stored_at": times[1],
                },
                {
                    "chat_id": "g",
                    "chat_type": 2,
                    "sender_id": "v",
                    "sender_name": "另一人",
                    "chat_name": "",
                    "stored_at": times[2],
                },
                {
                    "chat_id": "g",
                    "chat_type": 2,
                    "sender_name": "",
                    "chat_name": "",
                    "stored_at": times[3],
                },
                {
                    "chat_id": "p",
                    "sender_name": "",
                    "chat_name": "",
                    "stored_at": times[4],
                },
                {
                    "chat_id": "out",
                    "sender_id": "me",
                    "direction": "out",
                    "stored_at": times[5],
                },
            ]
        )
        self.hub.seed_existing()
        profiles = {p["key"]: p for p in self.hub.personas()}
        self.assertEqual(set(profiles), {"user:u", "user:v", "group:g"})
        self.assertEqual(profiles["user:u"]["name"], "新姓名")
        self.assertEqual(profiles["user:u"]["chat_id"], "p")
        self.assertEqual(profiles["group:g"]["name"], "项目群")
        conversations = {c["chat_id"]: c for c in self.hub.conversations()}
        self.assertEqual(set(conversations), {"p", "g"})
        self.assertEqual(conversations["p"]["name"], "私聊")
        self.assertEqual(conversations["g"]["name"], "项目群")
        self.assertEqual(conversations["p"]["updated_at"], times[4].isoformat())

    def test_seed_is_incremental_persistent_and_keeps_owner_configuration(self):
        self.history([{"stored_at": datetime(2026, 1, 1)}])
        self.hub.seed_existing()
        self.hub.update_persona(
            "user:u", {"role": "负责人", "reply_style": "简洁", "mode": "draft"}
        )
        with self.storage.Session() as session:
            session.get(Conversation, "c").overview = "已有概览"
            session.commit()
        before = self.hub.conversations()
        reopened = HubStore(self.storage)
        with self.queries() as queries:
            reopened.seed_existing()
        self.assertLessEqual(len(queries), 3)
        self.assertFalse(
            any(
                q.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
                for q in queries
            )
        )
        self.assertEqual(reopened.conversations(), before)
        self.history(
            [
                {
                    "msg_id": "next",
                    "sender_name": "更新姓名",
                    "chat_name": "新会话名",
                    "stored_at": datetime(2026, 1, 2),
                }
            ]
        )
        reopened.seed_existing()
        profile = reopened.personas()[0]
        self.assertEqual(profile["name"], "更新姓名")
        self.assertEqual(profile["role"], "负责人")
        self.assertEqual(profile["reply_style"], "简洁")
        self.assertEqual(profile["mode"], "draft")
        self.assertEqual(reopened.conversations()[0]["overview"], "已有概览")
        self.assertEqual(reopened.conversations()[0]["name"], "新会话名")

    def test_failed_seed_does_not_advance_progress_or_leave_partial_metadata(self):
        self.history([{}])
        with patch.object(
            self.storage.Session.class_,
            "commit",
            side_effect=RuntimeError("failed commit"),
        ):
            with self.assertRaises(RuntimeError):
                self.hub.seed_existing()
        self.assertEqual(self.hub.conversations(), [])
        self.assertEqual(self.hub.personas(), [])
        self.hub.seed_existing()
        self.assertEqual(len(self.hub.conversations()), 1)

    def test_empty_history_then_first_message_and_invalid_progress_recover(self):
        with self.queries() as queries:
            self.hub.seed_existing()
        self.assertLessEqual(len(queries), 2)
        self.assertEqual(self.hub.conversations(), [])
        self.history([{}])
        for progress in ("invalid", "999999"):
            with self.subTest(progress=progress):
                self.hub.set_setting("seed_last_message_id", progress)
                self.hub.seed_existing()
                self.assertEqual(len(self.hub.conversations()), 1)
                self.assertEqual(len(self.hub.personas()), 1)
                self.assertEqual(self.hub.setting("seed_last_message_id"), "1")

    def test_incremental_seed_keeps_group_name_and_all_configured_modes(self):
        self.history([{"chat_id": "g", "chat_type": 2, "chat_name": "项目群"}])
        self.hub.seed_existing()
        self.hub.update_persona(
            "group:g", {"group_reply_policy": "at_me", "role": "项目协作群"}
        )
        before = self.hub.conversations()[0]
        self.history(
            [
                {
                    "msg_id": "next",
                    "chat_id": "g",
                    "chat_type": 2,
                    "chat_name": "",
                    "sender_id": "v",
                    "sender_name": "另一人",
                }
            ]
        )
        with patch.object(
            self.storage.Session.class_,
            "commit",
            side_effect=RuntimeError("failed commit"),
        ):
            with self.assertRaises(RuntimeError):
                self.hub.seed_existing()
        self.assertEqual(self.hub.setting("seed_last_message_id"), "1")
        self.assertEqual(self.hub.conversations(), [before])
        self.hub.seed_existing()
        group = self.hub.conversations()[0]
        self.assertEqual(group["name"], "项目群")
        self.assertEqual(group["persona"]["role"], "项目协作群")
        self.assertEqual(group["persona"]["mode"], "auto")
        self.assertEqual(group["persona"]["group_reply_policy"], "at_me")
        self.assertEqual(
            {p["key"] for p in self.hub.personas()}, {"group:g", "user:u", "user:v"}
        )
