import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from larkx.client import LarkClient
from larkx.proto.gateway import resolve_type
from larkx.proto.decoders import resolve_scope
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from tests.test_hub import ANALYSIS, FakeBackend


class AddressedReplyTests(unittest.IsolatedAsyncioTestCase):
    def test_inline_quote_stays_in_main_chat_and_thread_reply_keeps_thread(self):
        self.assertEqual(
            resolve_scope("m", 2, 2, "quoted", "quoted", thread_message_type=0),
            ("chat", ""),
        )
        self.assertEqual(
            resolve_scope("m", 2, 2, "root", "root", thread_message_type=2),
            ("topic", "root"),
        )
        self.assertEqual(
            resolve_scope(
                "m", 2, 2, "root", "root", thread_chat=True, thread_message_type=0
            ),
            ("topic", "root"),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "reply.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.runtime.hub.observe(
            {"chat_id": "g", "chat_type": 2, "from_id": "123"}, "群聊", "李顺"
        )
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "all"})
        self.msg = {
            "chat_id": "g",
            "msg_id": "source",
            "from_id": "123",
            "chat_type": 2,
            "msg_type": 4,
            "content": "A07 做好了",
            "anchor": "",
        }
        self.storage.save_message(self.msg, sender_name="李顺")
        self.storage.save_message(
            dict(self.msg, msg_id="other-person", from_id="456"), sender_name="李顺"
        )
        self.storage.save_message(
            dict(self.msg, msg_id="elsewhere", chat_id="other"), sender_name="李顺"
        )
        self.sent = []

        def send(*args):
            self.sent.append(args)
            return "sent"

        self.runtime.client = SimpleNamespace(
            auth=SimpleNamespace(user_id="999"), send_rich=send
        )
        self.runtime.connection = "connected"

    async def test_send_mentions_and_quotes_stays_idempotent_and_stores_relation(self):
        args = {"reply_to_message_id": "source", "mention_user_id": "123"}
        result = await self.runtime.send("g", "知道了", "request", **args)
        again = await self.runtime.send("g", "知道了", "request", **args)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["id"], again["id"])
        self.assertEqual(len(self.sent), 1)
        parts, chat, anchor, quoted, root = self.sent[0]
        self.assertEqual(
            parts[0], {"type": "mention", "user_id": "123", "text": "@李顺"}
        )
        self.assertTrue(parts[1]["text"].endswith("reply by Bot"))
        self.assertEqual((chat, quoted, root), ("g", "source", "source"))
        record = next(
            m for m in self.runtime.hub.messages("g") if m["direction"] == "out"
        )
        self.assertEqual(record["parent_id"], "source")
        self.assertEqual(record["msg_type"], 2)
        with self.assertRaises(ValueError):
            await self.runtime.send(
                "g",
                "知道了",
                "request",
                reply_to_message_id="source",
                mention_user_id="456",
            )
        self.assertEqual(len(self.sent), 1)

    async def test_cross_chat_quote_unknown_user_and_self_mention_rejected(self):
        for fields in (
            {"reply_to_message_id": "elsewhere"},
            {"mention_user_id": "789"},
            {"mention_user_id": "999"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                await self.runtime.send("g", "回复", **fields)
        self.assertFalse(self.sent)
        self.assertFalse(self.runtime.hub.actions())

    async def test_engine_passes_only_valid_targets_to_reply(self):
        async def run(*args, **kwargs):
            return json.dumps(
                {**ANALYSIS, "reply_to_message_id": "source", "mention_user_id": "123"}
            )

        self.runtime.backend.run = run
        selected = []

        async def reply(*args, **kwargs):
            selected.append(kwargs)

        await self.runtime.engine.process("g:", self.msg, "A07 做好了", [], reply)
        self.assertEqual(
            selected, [{"reply_to_message_id": "source", "mention_user_id": "123"}]
        )

        async def invalid(*args, **kwargs):
            return json.dumps(
                {**ANALYSIS, "reply_to_message_id": "elsewhere", "mention_user_id": ""}
            )

        self.runtime.backend.run = invalid
        with self.assertRaises(ValueError):
            await self.runtime.engine.process("g:", self.msg, "消息", [], reply)
        self.assertEqual(len(selected), 1)

    def test_real_mention_encoding_and_quote_not_forced_into_thread(self):
        client = object.__new__(LarkClient)
        client.is_thread_chat = lambda chat: False
        packets = []
        client._gateway_post = lambda packet: packets.append(packet) or b""
        parts = [
            {"type": "mention", "user_id": "123", "text": "@李顺"},
            {"type": "text", "text": " 知道了"},
        ]
        with patch(
            "larkx.client.builders.decode_put_message_response", return_value="sent"
        ):
            client.send_rich(parts, "g", reply_to_id="message", reply_root_id="root")
            client.send_rich(parts, "g", root_id="topic", reply_to_id="message")
        request = resolve_type("messages.PutMessageRequest")()
        request.ParseFromString(packets[0].payload)
        self.assertEqual(list(request.atUserIdList), [123])
        at_node = next(
            e
            for e in request.content.richText.elements.dictionary.values()
            if e.tag == 5
        )
        at = resolve_type("entities.RichTextElement.AtProperty")()
        at.ParseFromString(at_node.property)
        self.assertEqual(at.userId, "123")
        self.assertEqual((request.parentId, request.rootId), ("message", "root"))
        self.assertFalse(request.isReplyInThread)
        request.ParseFromString(packets[1].payload)
        self.assertTrue(request.isReplyInThread)
        self.assertEqual((request.parentId, request.rootId), ("message", "topic"))
