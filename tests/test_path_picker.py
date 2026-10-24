import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from larkx.storage import Storage
from larkx.workhub.path_picker import PathPicker, native_dialog
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from tests.test_hub import FakeBackend


class PathPickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.picker = PathPicker()
        self.addAsyncCleanup(self.picker.close)
        self.enterContext(
            patch(
                "larkx.workhub.path_picker.dialog_program",
                return_value="pythonw.exe",
            )
        )

    async def choose(self, kind, chosen, initial=""):
        process = Mock(returncode=0)

        async def spawn(*args, **kwargs):
            process.request = json.loads(Path(args[-2]).read_text(encoding="utf-8"))
            process.request_file = args[-2]

            async def wait():
                Path(args[-1]).write_text(
                    json.dumps({"path": chosen}, ensure_ascii=False), encoding="utf-8"
                )
                return 0

            process.wait = AsyncMock(side_effect=wait)
            return process

        with patch(
            "asyncio.create_subprocess_exec", AsyncMock(side_effect=spawn)
        ) as call:
            result = await self.picker.pick({"kind": kind, "initial": initial})
        return result, process, call

    async def test_paths_with_spaces_unicode_and_shell_characters_are_only_data(self):
        path = self.folder / "中文 路径'$(example).db"
        path.write_bytes(b"fixture")
        hint = "'; Write-Output should-not-run; '"
        result, process, spawn = await self.choose("database", str(path), hint)
        self.assertEqual(result, {"path": str(path)})
        self.assertEqual(path.read_bytes(), b"fixture")
        self.assertNotIn(hint, " ".join(spawn.call_args.args))
        self.assertIn("-m", spawn.call_args.args)
        self.assertIn("larkx.workhub.path_picker", spawn.call_args.args)
        self.assertEqual(process.request["initial"], hint)
        self.assertFalse(Path(process.request_file).exists())

    async def test_directory_and_program_choices_do_not_copy_files(self):
        result, _, _ = await self.choose("directory", str(self.folder))
        self.assertEqual(result["path"], str(self.folder))
        program = self.folder / "fixture.cmd"
        program.write_text("fixture", encoding="utf-8")
        self.assertEqual(
            (await self.choose("program", str(program)))[0]["path"], str(program)
        )
        self.assertEqual(program.read_text(encoding="utf-8"), "fixture")

    async def test_cancel_is_distinct_from_errors_and_does_not_write_anything(self):
        self.assertEqual((await self.choose("directory", None))[0], {"path": None})
        self.assertFalse(list(self.folder.iterdir()))
        for kind, chosen in (
            ("directory", str(self.folder / "missing")),
            ("database", str(self.folder)),
            ("program", "relative.exe"),
        ):
            with self.assertRaises(ValueError):
                await self.choose(kind, chosen)

    async def test_invalid_or_unsupported_requests_never_start_a_process(self):
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            for body in (
                [],
                {"kind": []},
                {"kind": "command"},
                {"kind": "database_new"},
                {"kind": "directory", "initial": "x\x00y"},
                {"kind": "directory", "initial": 1},
                {"kind": "directory", "command": "no"},
            ):
                with self.assertRaises(ValueError):
                    await self.picker.pick(body)
            with patch("larkx.workhub.path_picker.dialog_program", return_value=None):
                self.assertFalse(self.picker.status()["available"])
                with self.assertRaisesRegex(ValueError, "手动填写"):
                    await self.picker.pick({"kind": "directory"})
            spawn.assert_not_called()

    async def test_only_one_dialog_can_be_open_and_cancellation_stops_the_helper(self):
        entered = asyncio.Event()
        process = Mock(returncode=None)

        async def wait():
            if process.returncode is None:
                entered.set()
                await asyncio.Future()
            return process.returncode

        process.wait = AsyncMock(side_effect=wait)
        process.kill.side_effect = lambda: setattr(process, "returncode", -1)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            task = asyncio.create_task(self.picker.pick({"kind": "directory"}))
            await entered.wait()
            self.assertTrue(self.picker.status()["busy"])
            with self.assertRaisesRegex(ValueError, "已有选择窗口"):
                await self.picker.pick({"kind": "program"})
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        self.assertEqual(process.wait.await_count, 2)
        self.assertFalse(self.picker.status()["busy"])
        self.assertIsNone(self.picker.process)

    async def test_timeout_and_native_dialog_failure_have_actionable_errors(self):
        process = Mock(returncode=None)
        process.wait = AsyncMock(side_effect=[asyncio.TimeoutError, -1])
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, "超时"):
                await self.picker.pick({"kind": "directory"})
        process.kill.assert_called_once()
        process.returncode = 1
        process.wait = AsyncMock(return_value=1)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, "手动填写"):
                await self.picker.pick({"kind": "directory"})

    async def test_api_requires_local_console_headers_and_never_saves_the_choice(self):
        storage = Storage("sqlite:///" + str(self.folder / "test.db"))
        self.addCleanup(storage.engine.dispose)
        runtime = HubRuntime(storage, FakeBackend())
        original = runtime.hub.settings()
        with patch.object(
            PathPicker, "pick", AsyncMock(return_value={"path": str(self.folder)})
        ) as pick:
            with TestClient(create_app(runtime, start_bridge=False)) as client:
                self.assertTrue(
                    client.get("/api/settings").json()["path_picker"]["available"]
                )
                self.assertEqual(
                    client.post(
                        "/api/settings/path-picker", json={"kind": "directory"}
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    client.post(
                        "/api/settings/path-picker",
                        headers={"X-Workhub": "1", "Origin": "https://example.com"},
                        json={"kind": "directory"},
                    ).status_code,
                    403,
                )
                pick.assert_not_awaited()
                response = client.post(
                    "/api/settings/path-picker",
                    headers={"X-Workhub": "1"},
                    json={"kind": "directory", "initial": str(self.folder)},
                )
                self.assertEqual(response.json(), {"path": str(self.folder)})
                pick.assert_awaited_once()
                self.assertEqual(runtime.hub.settings(), original)

    def test_gui_worker_uses_native_dialogs_and_always_disposes_its_root(self):
        from types import SimpleNamespace

        for kind, method in (
            ("directory", "askdirectory"),
            ("database", "askopenfilename"),
            ("program", "askopenfilename"),
        ):
            request = self.folder / "request.json"
            output = self.folder / "result.json"
            request.write_text(
                json.dumps(
                    {"kind": kind, "initial": str(self.folder), "parent_id": 123}
                ),
                encoding="utf-8",
            )
            root, dialogs = Mock(), Mock()
            getattr(dialogs, method).return_value = "C:/chosen/path"
            tk = SimpleNamespace(
                Tk=Mock(return_value=root), Label=Mock(), filedialog=dialogs
            )
            with patch.dict("sys.modules", {"tkinter": tk}), patch("threading.Thread"):
                native_dialog(request, output)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"path": "C:/chosen/path"},
            )
            root.destroy.assert_called_once()
            self.assertEqual(getattr(dialogs, method).call_args.kwargs["parent"], root)
