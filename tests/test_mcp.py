import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import requests
from sqlalchemy import event

from fastapi.testclient import TestClient
from mcp import Client

from larkx.storage import Storage
from larkx.workhub.mcp_server import HubAccess, RemoteAccess, create_mcp
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from tests.test_hub import FakeBackend


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "mcp.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.runtime.connection = "connected"
        self.sent = []

        def send(text, chat, anchor):
            self.sent.append((text, chat, anchor))
            return "sent-message"

        self.runtime.client = SimpleNamespace(
            send_msg=send, auth=SimpleNamespace(user_id="owner")
        )
        for chat, name, kind in (
            ("c1", "同事", 1),
            ("c2", "同事", 1),
            ("g1", "项目群", 2),
        ):
            for index, text in enumerate(("上线 100%", "排期确认", "上线明天")):
                msg = {
                    "msg_id": chat + str(index),
                    "chat_id": chat,
                    "from_id": "u" + chat,
                    "chat_type": kind,
                    "msg_type": 4,
                    "content": text,
                    "create_time": index + 1,
                }
                self.storage.save_message(msg, sender_name=name, chat_name=name)
                self.runtime.hub.observe(msg, name, name)
        self.server = create_mcp(HubAccess(lambda: self.runtime))

    async def test_tools_and_annotations(self):
        async with Client(self.server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            self.assertEqual(len(tools), 5)
            self.assertTrue(tools["search_messages"].annotations.read_only_hint)
            self.assertFalse(tools["send_message"].annotations.read_only_hint)
            self.assertTrue(tools["send_message"].annotations.destructive_hint)

    async def test_search_keeps_duplicate_names_and_pagination(self):
        async with Client(self.server) as client:
            result = await client.call_tool(
                "search_conversations", {"query": "同事", "kind": "private", "limit": 1}
            )
            data = result.structured_content
            self.assertEqual(data["total"], 2)
            self.assertEqual(data["next_offset"], 1)
            second = await client.call_tool(
                "search_conversations",
                {"query": "同事", "kind": "private", "limit": 1, "offset": 1},
            )
            self.assertNotEqual(
                data["conversations"][0]["chat_id"],
                second.structured_content["conversations"][0]["chat_id"],
            )

    async def test_history_pages_stay_in_chat(self):
        async with Client(self.server) as client:
            first = (
                await client.call_tool(
                    "get_chat_history", {"chat_id": "c1", "limit": 2}
                )
            ).structured_content
            self.assertEqual(
                [r["content"] for r in first["messages"]], ["上线明天", "排期确认"]
            )
            second = (
                await client.call_tool(
                    "get_chat_history",
                    {"chat_id": "c1", "before_id": first["next_before_id"], "limit": 2},
                )
            ).structured_content
            self.assertEqual(len(second["messages"]), 1)
            self.assertIsNone(second["next_before_id"])
            self.assertTrue(
                all(
                    r["chat_id"] == "c1" for r in first["messages"] + second["messages"]
                )
            )

    async def test_search_escapes_sql_wildcards(self):
        async with Client(self.server) as client:
            data = (
                await client.call_tool("search_messages", {"query": "100%"})
            ).structured_content
            self.assertEqual(len(data["messages"]), 3)
            self.assertTrue(all(r["content"] == "上线 100%" for r in data["messages"]))

    async def test_send_has_suffix_handoff_audit_and_idempotency(self):
        async with Client(self.server) as client:
            args = {"chat_id": "c1", "content": "测试", "request_id": "same-request"}
            first = await client.call_tool("send_message", args)
            second = await client.call_tool("send_message", args)
            self.assertEqual(first.structured_content["status"], "sent")
            self.assertEqual(
                first.structured_content["id"], second.structured_content["id"]
            )
            self.assertEqual(len(self.sent), 1)
            self.assertTrue(self.sent[0][0].endswith("reply by Bot"))
            self.assertTrue(self.runtime.hub.human_paused("c1"))
            self.assertEqual(len(self.runtime.hub.actions()), 1)

    async def test_unknown_recipient_and_invalid_limit_are_errors(self):
        async with Client(self.server) as client:
            unknown = await client.call_tool(
                "send_message",
                {"chat_id": "同事", "content": "测试", "request_id": "unknown"},
            )
            oversized = await client.call_tool(
                "get_chat_history", {"chat_id": "c1", "limit": 101}
            )
            self.assertTrue(unknown.is_error)
            self.assertTrue(oversized.is_error)
            self.assertFalse(self.sent)

    async def test_conflicting_send_id_cannot_send_to_another_person(self):
        async with Client(self.server) as client:
            await client.call_tool(
                "send_message",
                {
                    "chat_id": "c1",
                    "content": "测试",
                    "request_id": "same-request",
                },
            )
            conflict = await client.call_tool(
                "send_message",
                {
                    "chat_id": "c2",
                    "content": "测试",
                    "request_id": "same-request",
                },
            )
            self.assertTrue(conflict.is_error)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.sent[0][1], "c1")

    async def test_unconfirmed_send_is_never_reported_as_sent_or_repeated(self):
        def unconfirmed(text, chat, anchor):
            self.sent.append((text, chat, anchor))
            return None

        self.runtime.client.send_msg = unconfirmed
        async with Client(self.server) as client:
            args = {"chat_id": "c1", "content": "测试", "request_id": "unconfirmed"}
            first = await client.call_tool("send_message", args)
            second = await client.call_tool("send_message", args)
            self.assertEqual(first.structured_content["status"], "uncertain")
            self.assertEqual(second.structured_content["status"], "uncertain")
            self.assertEqual(len(self.sent), 1)

    def test_conversation_search_uses_one_database_query(self):
        queries = []

        def capture(connection, cursor, statement, parameters, context, executemany):
            queries.append(statement)

        event.listen(self.storage.engine, "before_cursor_execute", capture)
        try:
            result = self.runtime.hub.find_conversations("", "all", 1, 0)
        finally:
            event.remove(self.storage.engine, "before_cursor_execute", capture)
        self.assertEqual(result["total"], 3)
        self.assertEqual(len(queries), 1)

    async def test_stdio_bridge_send_uses_guards_and_preserves_request(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"status":"sent"}'
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.return_value = response
        with patch("larkx.workhub.mcp_server.requests.Session", return_value=session):
            result = await RemoteAccess("http://127.0.0.1:8765").send(
                "c1", "正文", "request-id", "thread"
            )
        self.assertEqual(result["status"], "sent")
        self.assertFalse(session.trust_env)
        kwargs = session.request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["X-Workhub"], "1")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["request_id"], "request-id")
        self.assertEqual(kwargs["json"]["anchor"], "thread")

    async def test_stdio_bridge_timeout_does_not_retry_send(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.request.side_effect = requests.Timeout()
        with patch("larkx.workhub.mcp_server.requests.Session", return_value=session):
            with self.assertRaisesRegex(ValueError, "发送结果未确认"):
                await RemoteAccess("http://localhost:8765").send("c1", "正文", "id", "")
        self.assertEqual(session.request.call_count, 1)

    def test_http_mount_and_external_origin_rejected(self):
        with TestClient(
            create_app(self.runtime, start_bridge=False),
            base_url="http://127.0.0.1:8765",
        ) as client:
            headers = {"Accept": "application/json, text/event-stream"}
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
            response = client.post("/mcp", json=init, headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertIn("result", response.json())
            call = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_chat_history",
                    "arguments": {"chat_id": "c1", "limit": 1},
                },
            }
            reply = client.post("/mcp", json=call, headers=headers)
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(
                len(reply.json()["result"]["structuredContent"]["messages"]), 1
            )
            forbidden = client.post(
                "/mcp", json=init, headers={**headers, "Origin": "https://evil.example"}
            )
            self.assertGreaterEqual(forbidden.status_code, 400)
            self.assertEqual(
                client.get("/api/integration/messages?limit=101").status_code, 422
            )

    def test_remote_hub_rejects_nonlocal_urls(self):
        for url in (
            "https://127.0.0.1:8765",
            "http://example.com",
            "http://localhost@evil.example",
            "http://localhost/other",
        ):
            with self.assertRaises(ValueError):
                RemoteAccess(url)
