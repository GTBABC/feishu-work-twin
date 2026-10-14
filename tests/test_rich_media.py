import unittest, json, tempfile
from pathlib import Path
from types import SimpleNamespace
from larkx.proto import proto_pb2 as P
from larkx.proto.gateway import resolve_type
from larkx.media import display_parts
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from fastapi.testclient import TestClient


class RichTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_parts_and_gif_metadata(self):
        prop = resolve_type("entities.RichTextElement.ImageProperty")(
            token="img_key", customWidth=144, width=240, height=48
        )
        data = {
            "richText": {
                "elementIds": ["p", "i"],
                "elements": {
                    "dictionary": {
                        "p": {"tag": 3, "childIds": ["t"]},
                        "t": {
                            "tag": 1,
                            "property": P.TextProperty(
                                content="hello"
                            ).SerializeToString(),
                        },
                        "i": {"tag": 2, "property": prop.SerializeToString()},
                    }
                },
            }
        }
        parts = display_parts(2, data)
        self.assertEqual(parts[0]["text"], "hello")
        self.assertEqual(parts[-1]["key"], "img_key")
        self.assertEqual(parts[-1]["display_width"], 144)
        self.assertTrue(display_parts(10, {"key": "sticker"})[0]["sticker"])

    async def test_rich_send_order_idempotency_and_forged_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            st = Storage("sqlite:///" + str(Path(folder) / "test.db"))
            r = HubRuntime(st, SimpleNamespace())
            r.hub.observe(
                {"chat_id": "c", "from_id": "u", "chat_type": 1}, "同事", "同事"
            )
            r.hub.set_setting(
                "upload:asset",
                json.dumps({"key": "img_key", "width": 100, "height": 50}),
            )
            sent = []

            def send(parts, chat):
                sent.append(parts)
                return "msg"

            r.client = SimpleNamespace(
                auth=SimpleNamespace(user_id="me"), send_rich=send
            )
            r.connection = "connected"
            parts = [
                {"type": "text", "text": "before"},
                {"type": "image", "upload_id": "asset"},
                {"type": "text", "text": "after"},
            ]
            a = await r.send_parts("c", parts, "request")
            await r.send_parts("c", parts, "request")
            self.assertEqual(a["status"], "sent")
            self.assertEqual(len(sent), 1)
            self.assertEqual(
                [p["type"] for p in sent[0]], ["text", "image", "text", "text"]
            )
            self.assertTrue(r.hub.human_paused("c"))
            row = r.hub.messages("c")[0]
            self.assertEqual(row["msg_type"], 2)
            self.assertIn("_display_parts", json.loads(row["content_data"]))
            with self.assertRaises(ValueError):
                await r.send_parts(
                    "c", [{"type": "image", "upload_id": "fake"}], "fake"
                )
            st.engine.dispose()

    async def test_rich_endpoint_body_and_origin_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            st = Storage("sqlite:///" + str(Path(folder) / "test.db"))
            r = HubRuntime(st, SimpleNamespace())
            with TestClient(create_app(r, start_bridge=False)) as client:
                self.assertEqual(
                    client.post(
                        "/api/conversations/c/send-rich",
                        json={
                            "parts": [{"type": "text", "text": "x"}],
                            "request_id": "r",
                        },
                    ).status_code,
                    403,
                )
                response = client.post(
                    "/api/conversations/c/send-rich",
                    json={
                        "parts": [{"type": "image", "upload_id": "fake"}],
                        "request_id": "r",
                    },
                    headers={"X-Workhub": "1"},
                )
                self.assertEqual(response.status_code, 400)
            st.engine.dispose()


if __name__ == "__main__":
    unittest.main()
