import tempfile
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from larkx.agent.codex_backend import CodexBackend
from larkx.agent.dispatcher import AgentDispatcher
from larkx.agent.dispatcher import SessionWorker


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.path_patch = patch.dict("os.environ", {"LARKX_CODEX_PATH": sys.executable})
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    async def test_context_isolation_reset_and_skip(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("larkx.agent.codex_backend.DATA_DIR", Path(folder)):
                backend = CodexBackend()
            replies = iter(["你好", "[[NO_REPLY]]", "独立会话", "[[NO_REPLY]]"])
            inputs = []
            calls = []
            image_path = Path(folder) / "image.png"
            image_path.write_bytes(b"fixture")

            async def spawn(*args, **kwargs):
                output = Path(args[args.index("--output-last-message") + 1])
                self.assertIn("read-only", args)
                self.assertIn("shell_tool", args)

                class Process:
                    returncode = 0

                    async def communicate(self, data):
                        inputs.append(data.decode("utf-8"))
                        output.write_text(next(replies), encoding="utf-8")
                        return None, b""

                calls.append(args)
                return Process()

            with patch("asyncio.create_subprocess_exec", spawn):
                self.assertEqual(
                    await backend.run("a", "第一条", image_paths=[str(image_path)]),
                    "你好",
                )
                self.assertEqual(await backend.run("a", "第二条"), "")
                self.assertEqual(await backend.run("b", "另外一个人"), "独立会话")
                self.assertEqual(
                    await backend.run("a", "[表情]", require_reply=True),
                    "收到啦，有什么需要我帮忙的吗？",
                )
            self.assertIn("第一条", inputs[1])
            self.assertNotIn("第一条", inputs[2])
            self.assertIn(str(image_path), calls[0])
            self.assertIn(str(image_path), calls[1])
            self.assertNotIn(str(image_path), calls[2])
            backend.reset_session("a")
            self.assertFalse(backend._path("a").exists())
            self.assertTrue(backend._path("b").exists())

    async def test_burst_messages_are_merged(self):
        class Backend:
            returns_reply = True

        class Dispatcher:
            backend = Backend()

            async def handle_message(self, key, msg):
                self.message = msg
                finished.set()

        dispatcher = Dispatcher()
        finished = asyncio.Event()
        with patch(
            "larkx.agent.dispatcher.resolve_settings",
            return_value=(
                {"batch_delay": 0.01, "batch_max_wait": 1, "batch_size": 20},
                {},
                {},
            ),
        ):
            worker = SessionWorker(dispatcher, "a")
            await worker.queue.put({"content": "第一条"})
            await worker.queue.put({"content": "第二条"})
            await asyncio.wait_for(finished.wait(), 1)
            self.assertEqual(
                [m["content"] for m in dispatcher.message["_batch"]],
                ["第一条", "第二条"],
            )
            worker.task.cancel()
            try:
                await worker.task
            except asyncio.CancelledError:
                pass

    async def test_image_download_validation_and_cache(self):
        from io import BytesIO
        from PIL import Image
        from types import SimpleNamespace
        from larkx.media import prepare_image

        data = BytesIO()
        Image.new("RGB", (32, 16), "white").save(data, format="JPEG")
        msg = {
            "msg_id": "1",
            "chat_id": "2",
            "msg_type": 5,
            "content_data": {"imageV2": {"imageKey": "key"}},
        }
        auth = SimpleNamespace(
            region=SimpleNamespace(file_host="https://example.invalid")
        )
        with tempfile.TemporaryDirectory() as folder:
            with patch(
                "larkx.media.download", return_value=(data.getvalue(), "image/jpeg")
            ) as download:
                first = prepare_image(auth, msg, Path(folder))
                self.assertEqual(prepare_image(auth, msg, Path(folder)), first)
                self.assertEqual(download.call_count, 1)
                with Image.open(first) as image:
                    self.assertEqual(image.size, (32, 16))
            with patch(
                "larkx.media.download", return_value=(b"not an image", "text/html")
            ):
                with self.assertRaises(Exception):
                    prepare_image(auth, dict(msg, msg_id="bad"), Path(folder))

    async def test_dispatch_sends_once_with_thread(self):
        class Backend:
            returns_reply = True

            async def run(self, *args, **kwargs):
                self.prompt = args[1]
                return "测试回复"

        backend = Backend()
        sent = []

        async def send(*args):
            sent.append(args)

        dispatcher = AgentDispatcher(backend, send)
        await dispatcher.handle_message(
            "chat:thread",
            {
                "chat_id": "chat",
                "anchor": "thread",
                "content": "你好",
                "from_id": "other",
            },
        )
        self.assertEqual(sent, [("chat", "测试回复", "thread")])
        self.assertNotIn("要回复就执行:", backend.prompt)

    async def test_cli_failure_does_not_return_reply(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("larkx.agent.codex_backend.DATA_DIR", Path(folder)):
                backend = CodexBackend()

            async def spawn(*args, **kwargs):
                class Process:
                    returncode = 1

                    async def communicate(self, data):
                        return None, b"failure"

                return Process()

            with patch("asyncio.create_subprocess_exec", spawn):
                with self.assertRaises(RuntimeError):
                    await backend.run("a", "message")
            self.assertFalse(backend._path("a").exists())


if __name__ == "__main__":
    unittest.main()
