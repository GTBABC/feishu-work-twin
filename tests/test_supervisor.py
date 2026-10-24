import asyncio
from contextlib import chdir
import json
import os
from pathlib import Path
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

from larkx.workhub import supervisor as sup


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.folder = self.temp / "control"
        self.logs = self.temp / "logs"

    def events(self):
        return [
            json.loads(line)
            for line in (self.logs / "supervisor.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def guard(self, code, **kwargs):
        return sup.Supervisor(
            self.folder,
            self.logs,
            [sup.background_executable(), "-u", "-c", code],
            poll=0.02,
            grace=0.3,
            **kwargs,
        )

    def threaded(self, guard):
        errors = []

        def run():
            try:
                guard.run()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def cleanup():
            guard.stopping.set()
            thread.join(15)
            self.assertFalse(thread.is_alive(), "守护未结束")
            self.assertEqual(errors, [])

        self.addCleanup(cleanup)
        return thread

    def until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("等待守护状态超时")

    def test_crash_restarts_and_keeps_traceback_exit_code_and_duration(self):
        marker = self.temp / "attempt"
        code = (
            f"from pathlib import Path; p=Path({str(marker)!r}); "
            "first=not p.exists(); p.write_text('started'); "
            "print('worker output', flush=True); "
            "exec(\"raise RuntimeError('synthetic crash')\" if first else 'pass')"
        )
        guard = self.guard(code)
        with patch.object(sup, "restart_delay", return_value=0.05):
            guard.run()
        events = self.events()
        exits = [e for e in events if e["event"] == "service_exited"]
        self.assertEqual([e["exit_code"] for e in exits], [1, 0])
        self.assertEqual(exits[0]["reason"], "unexpected_exit")
        self.assertEqual(exits[0]["exit_code_hex"], "0x00000001")
        self.assertGreater(exits[0]["duration_seconds"], 0)
        self.assertIn(
            "RuntimeError: synthetic crash", (self.logs / "service.log.1").read_text()
        )
        self.assertEqual(sup.read_state(self.folder)["phase"], "stopped")
        self.assertFalse(sup.is_running(self.folder))

    def test_normal_exit_does_not_restart(self):
        self.guard("print('clean exit')").run()
        self.assertEqual(
            [e["event"] for e in self.events()].count("service_started"), 1
        )
        self.assertFalse(any(e["event"] == "restart_scheduled" for e in self.events()))

    def test_explicit_stop_is_graceful_and_does_not_restart(self):
        guard = self.guard(
            "import sys; sys.stdin.readline(); print('graceful shutdown')"
        )
        thread = self.threaded(guard)
        self.until(lambda: sup.read_state(self.folder).get("phase") == "running")
        sup.stop(self.folder, timeout=5)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        exits = [e for e in self.events() if e["event"] == "service_exited"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "requested_stop")
        self.assertEqual(exits[0]["exit_code"], 0)
        self.assertIn("graceful shutdown", (self.logs / "service.log").read_text())

    def test_stop_during_backoff_cancels_restart(self):
        guard = self.guard("raise SystemExit(9)")
        thread = self.threaded(guard)
        self.until(lambda: sup.read_state(self.folder).get("phase") == "backoff")
        sup.stop(self.folder, timeout=5)
        thread.join(5)
        self.assertEqual(
            [e["event"] for e in self.events()].count("service_started"), 1
        )

    def test_forced_stop_records_timeout_and_kills_process_tree(self):
        guard = self.guard("import time; time.sleep(60)")
        self.threaded(guard)
        self.until(lambda: sup.read_state(self.folder).get("phase") == "running")
        sup.stop(self.folder, timeout=10)
        self.assertTrue(any(e["event"] == "forced_stop" for e in self.events()))
        self.assertFalse(sup.is_running(self.folder))

    def test_duplicate_guard_cannot_launch_second_worker(self):
        guard = self.guard("import sys; sys.stdin.readline()")
        self.threaded(guard)
        self.until(lambda: sup.read_state(self.folder).get("phase") == "running")
        with self.assertRaises(sup.AlreadyRunning):
            self.guard("raise RuntimeError('must not run')").run()

    def test_prior_missing_exit_record_is_reported_and_stale_stop_ignored(self):
        self.folder.mkdir()
        sup.write_state(
            self.folder, {"phase": "running", "pid": 999999, "session": "old"}
        )
        (self.folder / "stop").write_text("old")
        self.guard("pass").run()
        event = next(
            e for e in self.events() if e["event"] == "previous_supervisor_lost"
        )
        self.assertEqual(event["previous_pid"], 999999)
        self.assertIn("未知", event["reason"])
        self.assertTrue(any(e["event"] == "service_started" for e in self.events()))

    def test_launch_failure_is_logged_and_can_be_stopped(self):
        guard = sup.Supervisor(
            self.folder, self.logs, [str(self.temp / "missing.exe")], poll=0.02
        )
        self.threaded(guard)
        self.until(lambda: sup.read_state(self.folder).get("phase") == "backoff")
        sup.stop(self.folder, timeout=5)
        event = next(e for e in self.events() if e["event"] == "launch_failed")
        self.assertEqual(event["error_type"], "FileNotFoundError")

    def test_restart_delay_is_bounded(self):
        self.assertEqual(
            [sup.restart_delay(i) for i in range(1, 8)], [2, 4, 8, 16, 32, 60, 60]
        )
        self.assertEqual(sup.restart_delay(10000), 60)

    def test_logs_rotate_without_overwriting_latest_failure(self):
        self.logs.mkdir()
        for i in range(7):
            sup.service_log(self.logs).write_text(str(i))
        self.assertEqual((self.logs / "service.log").read_text(), "6")
        self.assertEqual((self.logs / "service.log.4").read_text(), "2")
        self.assertEqual(len(list(self.logs.iterdir())), 5)

    def test_control_uses_lock_not_stale_pid(self):
        self.folder.mkdir()
        sup.write_state(
            self.folder, {"pid": os.getpid(), "session": "stale", "phase": "running"}
        )
        self.assertFalse(sup.is_running(self.folder))
        sup.stop(self.folder)
        self.assertFalse((self.folder / "stop").exists())

    def test_default_runtime_path_is_shared_across_user_directory_views(self):
        from larkx import startup

        program = self.temp / "program"
        with (
            patch.object(startup, "__file__", str(program / "larkx" / "startup.py")),
            patch.dict(os.environ, {"LARKX_STARTUP_PATH": ""}),
        ):
            for directory in (self.temp / "user-a", self.temp / "user-b"):
                with patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": str(directory), "LARKX_HOME": str(directory)},
                ):
                    self.assertEqual(
                        sup.control_dir(),
                        program / "data" / "run" / "workhub-supervisor",
                    )

    def test_relative_startup_path_does_not_change_with_working_directory(self):
        from larkx import startup

        program = self.temp / "program"
        elsewhere = self.temp / "elsewhere"
        elsewhere.mkdir()
        with (
            patch.object(startup, "__file__", str(program / "larkx" / "startup.py")),
            patch.dict(
                os.environ, {"LARKX_STARTUP_PATH": "profiles/test/startup.json"}
            ),
            chdir(elsewhere),
        ):
            self.assertEqual(
                startup.config_path(), program / "profiles" / "test" / "startup.json"
            )
            self.assertEqual(
                sup.control_dir(), program / "profiles" / "test" / "workhub-supervisor"
            )

    def test_inactive_status_does_not_echo_any_old_pid(self):
        self.folder.mkdir()
        previous = {
            "pid": os.getpid(),
            "child_pid": 12345,
            "phase": "running",
            "session": "stale",
        }
        sup.write_state(self.folder, previous)
        result = sup.status(self.folder)
        self.assertEqual(result["phase"], "stopped")
        self.assertIsNone(result["pid"])
        self.assertIsNone(result["child_pid"])
        self.assertFalse(result["supervised"])
        self.assertEqual(sup.read_state(self.folder), previous)

    def live_record(self, **changes):
        return {
            "pid": os.getpid(),
            "process_started": sup.process_started(os.getpid()),
            "phase": "running",
            "session": "test-session",
            "windowless": True,
            **changes,
        }

    def test_live_status_verifies_process_identity_and_reports_state_path(self):
        with sup.supervisor_lock(self.folder):
            sup.write_state(self.folder, self.live_record())
            current = sup.status(self.folder)
        self.assertEqual(current["pid"], os.getpid())
        self.assertTrue(current["supervised"])
        self.assertEqual(current["state_file"], str(self.folder / "state.json"))

    def test_recycled_pid_is_not_reported_or_targeted_for_stop(self):
        with sup.supervisor_lock(self.folder):
            sup.write_state(
                self.folder, self.live_record(process_started="old-process")
            )
            current = sup.status(self.folder)
            self.assertEqual(current["phase"], "unknown")
            self.assertIsNone(current["pid"])
            with self.assertRaisesRegex(RuntimeError, "未发送停止指令"):
                sup.stop(self.folder)
        self.assertFalse((self.folder / "stop").exists())

    def test_dead_pid_and_corrupt_record_never_become_current_pid(self):
        with sup.supervisor_lock(self.folder):
            sup.write_state(self.folder, self.live_record(pid=2**30))
            self.assertIsNone(sup.status(self.folder)["pid"])
            (self.folder / "state.json").write_text("{corrupt")
            self.assertIsNone(sup.status(self.folder)["pid"])

    def test_exited_or_recycled_worker_pid_is_removed_from_status(self):
        with sup.supervisor_lock(self.folder):
            sup.write_state(
                self.folder,
                self.live_record(
                    child_pid=os.getpid(), child_process_started="old-worker"
                ),
            )
            current = sup.status(self.folder)
        self.assertEqual(current["pid"], os.getpid())
        self.assertIsNone(current["child_pid"])

    def test_live_legacy_guard_can_be_stopped_without_trusting_its_pid(self):
        canonical = self.temp / "new-control"
        guard = self.guard("import sys; sys.stdin.readline()")
        guard.state.pop("process_started")
        with (
            patch.object(sup, "control_dir", return_value=canonical),
            patch.object(sup, "legacy_control_dir", return_value=self.folder),
        ):
            thread = self.threaded(guard)
            self.until(lambda: sup.read_state(self.folder).get("phase") == "running")
            self.assertEqual(sup.query_dir(), self.folder)
            self.assertIsNone(sup.status(self.folder)["pid"])
            sup.stop(self.folder, timeout=5)
            thread.join(5)
            self.assertEqual(sup.query_dir(), canonical)

    def test_new_control_takes_priority_over_legacy_running_record(self):
        legacy = self.temp / "legacy"
        with (
            patch.object(sup, "control_dir", return_value=self.folder),
            patch.object(sup, "legacy_control_dir", return_value=legacy),
            sup.supervisor_lock(self.folder),
            sup.supervisor_lock(legacy),
        ):
            self.assertEqual(sup.query_dir(), self.folder)

    def test_status_replace_retries_brief_windows_read_lock(self):
        self.folder.mkdir()
        replace = os.replace
        calls = 0

        def transient_lock(source, target):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError("synthetic reader lock")
            return replace(source, target)

        with patch.object(sup.os, "replace", side_effect=transient_lock):
            sup.write_state(self.folder, {"phase": "running"})
        self.assertEqual(sup.read_state(self.folder)["phase"], "running")
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_gui_interpreter_has_no_console_and_keeps_redirected_streams(self):
        program = sup.background_executable()
        self.assertEqual(Path(program).name.lower(), "pythonw.exe")
        code = (
            "import ctypes,json,sys; "
            "print(json.dumps({'console':ctypes.windll.kernel32.GetConsoleCP(),"
            "'input':sys.stdin.readline().strip()}),flush=True); "
            "print('stderr works',file=sys.stderr,flush=True)"
        )
        result = subprocess.run(
            [program, "-u", "-c", code],
            input=b"stop\n",
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            check=True,
        )
        self.assertEqual(json.loads(result.stdout), {"console": 0, "input": "stop"})
        self.assertIn(b"stderr works", result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_cli_and_cmd_adapter_have_no_visible_console(self):
        from larkx.agent.model_backend import communicate

        code = (
            "import ctypes; from ctypes import wintypes; "
            "k=ctypes.windll.kernel32; u=ctypes.windll.user32; "
            "k.GetConsoleWindow.restype=wintypes.HWND; "
            "u.IsWindowVisible.argtypes=[wintypes.HWND]; "
            "w=k.GetConsoleWindow(); print(int(bool(w and u.IsWindowVisible(w))))"
        )
        script = self.temp / "cli.cmd"
        script.write_text(f'@echo off\n"{sys.executable}" -c "{code}"\n')
        for args in ([sys.executable, "-c", code], [str(script)]):
            with self.subTest(args=args):
                output = asyncio.run(communicate(args, "", str(self.temp), 10))
                self.assertEqual(output.strip(), b"0")

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_missing_gui_interpreter_fails_instead_of_opening_console(self):
        with patch.object(sup.sys, "executable", str(self.temp / "python.exe")):
            with self.assertRaisesRegex(RuntimeError, "pythonw.exe"):
                sup.background_executable()

    @unittest.skipUnless(os.name == "nt", "Windows console regression")
    def test_old_guard_requires_manual_restart(self):
        with (
            patch.object(sup, "is_running", return_value=True),
            patch.object(sup, "read_state", return_value={"phase": "running"}),
            patch.object(sup.subprocess, "Popen") as spawn,
        ):
            with self.assertRaisesRegex(RuntimeError, "-Stop"):
                sup.start(self.folder)
        spawn.assert_not_called()

    def test_server_shutdown_event_reaches_uvicorn(self):
        from larkx.workhub.server import run

        event = threading.Event()
        server = Mock(should_exit=False)

        def serve():
            event.set()
            deadline = time.monotonic() + 3
            while not server.should_exit and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.should_exit)

        server.run.side_effect = serve
        with (
            patch("uvicorn.Server", return_value=server),
            patch("larkx.startup.server_lock"),
            patch("larkx.workhub.server.create_app", return_value=Mock()),
        ):
            run(stop_event=event)
        server.run.assert_called_once()


class SupervisorIntegrationTests(unittest.TestCase):
    def test_default_status_matches_guard_across_user_views_and_working_directories(
        self,
    ):
        # Reproduce separate AppData views with an isolated program installation.
        # No explicit startup override is used, so this exercises the real default.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            program = root / "program"
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            repo = Path(__file__).resolve().parents[1]
            shutil.copytree(
                repo / "larkx",
                program / "larkx",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            common = {
                **os.environ,
                "PYTHONPATH": str(program),
                "PYTHONUTF8": "1",
                "LARKX_STARTUP_PATH": "",
                "LARKX_HOME": "",
                "LARKX_STORAGE_URL": "",
                "LARKX_PORT": str(port),
            }
            a = {**common, "LOCALAPPDATA": str(root / "user-a")}
            b = {**common, "LOCALAPPDATA": str(root / "user-b")}
            for env in (a, b):
                stale = Path(env["LOCALAPPDATA"]) / "LarkAgentX" / "workhub-supervisor"
                stale.mkdir(parents=True)
                sup.write_state(
                    stale, {"phase": "running", "pid": 2**30, "session": "stale"}
                )

            def command(action, env, cwd):
                return json.loads(
                    subprocess.check_output(
                        [sys.executable, "-m", "larkx.workhub.supervisor", action],
                        cwd=cwd,
                        env=env,
                        text=True,
                        encoding="utf-8",
                        timeout=65,
                    )
                )

            try:
                initial = command("start", a, program)
                current = command("status", b, elsewhere)
                self.assertTrue(current["supervised"])
                self.assertEqual(current["pid"], initial["pid"])
                self.assertEqual(current["session"], initial["session"])
                self.assertEqual(
                    current["state_file"],
                    str(program / "data" / "run" / "workhub-supervisor" / "state.json"),
                )
                self.assertNotEqual(current["pid"], 2**30)
            finally:
                subprocess.run(
                    [sys.executable, "-m", "larkx.workhub.supervisor", "stop"],
                    cwd=elsewhere,
                    env=b,
                    check=True,
                    capture_output=True,
                    timeout=65,
                )
            stopped = command("status", a, elsewhere)
            self.assertFalse(stopped["supervised"])
            self.assertIsNone(stopped["pid"])

    def test_detached_start_real_worker_crash_recovery_and_stop(self):
        # Isolated credentials/database: no live Feishu account or message is touched.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = Path(__file__).resolve().parents[1]
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            env = {
                **os.environ,
                "PYTHONPATH": str(repo),
                "PYTHONUTF8": "1",
                "LARKX_STARTUP_PATH": str(root / "startup.json"),
                "LARKX_HOME": str(root / "data"),
                "LARKX_STORAGE_URL": "",
                "LARKX_PORT": str(port),
            }
            folder = root / "workhub-supervisor"

            def command(action):
                return subprocess.check_output(
                    [sys.executable, "-m", "larkx.workhub.supervisor", action],
                    cwd=root,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    timeout=55,
                )

            def ready(previous_pid=None):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    state = sup.read_state(folder)
                    if (
                        state.get("phase") == "running"
                        and state.get("child_pid") != previous_pid
                    ):
                        try:
                            with urlopen(
                                f"http://127.0.0.1:{port}/", timeout=1
                            ) as response:
                                if response.status == 200:
                                    return state
                        except OSError:
                            pass
                    time.sleep(0.1)
                self.fail("隔离服务未就绪")

            def assert_windowless():
                events = [
                    json.loads(line)
                    for line in (
                        root / "data" / "logs" / "workhub" / "supervisor.jsonl"
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertFalse(
                    next(
                        e
                        for e in reversed(events)
                        if e["event"] == "supervisor_started"
                    )["console_attached"]
                )
                entries = [
                    json.loads(line)
                    for line in (root / "data" / "logs" / "workhub" / "service.log")
                    .read_text(encoding="utf-8")
                    .splitlines()
                    if line.startswith('{"event": "worker_started"')
                ]
                self.assertEqual(len(entries), 1)
                self.assertFalse(entries[0]["console_attached"])

            try:
                first = json.loads(command("start"))
                self.assertTrue(first["supervised"])
                initial = ready()
                self.assertTrue(initial["windowless"])
                assert_windowless()
                # Original launcher has already exited; a second launch is idempotent.
                again = json.loads(command("start"))
                self.assertEqual(again["session"], initial["session"])
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(initial["child_pid"]), "/T", "/F"],
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
                else:
                    os.kill(initial["child_pid"], 9)
                recovered = ready(initial["child_pid"])
                assert_windowless()
                self.assertEqual(initial["session"], recovered["session"])
                self.assertNotEqual(initial["child_pid"], recovered["child_pid"])
                events = [
                    json.loads(line)
                    for line in (
                        root / "data" / "logs" / "workhub" / "supervisor.jsonl"
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                exit_event = next(e for e in events if e["event"] == "service_exited")
                self.assertNotEqual(exit_event["exit_code"], 0)
                self.assertEqual(exit_event["reason"], "unexpected_exit")
                # Losing the guardian closes its stdin pipe. The worker must not
                # remain as an orphan and collide with the next guarded launch.
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(recovered["pid"]), "/F"],
                        check=True,
                        capture_output=True,
                        timeout=10,
                    )
                else:
                    os.kill(recovered["pid"], 9)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/", timeout=1):
                            time.sleep(0.1)
                    except OSError:
                        break
                else:
                    self.fail("守护丢失后遗留了孤立服务")
                third = json.loads(command("start"))
                self.assertNotEqual(third["session"], recovered["session"])
                ready()
                assert_windowless()
                events = [
                    json.loads(line)
                    for line in (
                        root / "data" / "logs" / "workhub" / "supervisor.jsonl"
                    )
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertTrue(
                    any(e["event"] == "previous_supervisor_lost" for e in events)
                )
            finally:
                command("stop")
            self.assertFalse(sup.is_running(folder))
            self.assertEqual(sup.read_state(folder)["phase"], "stopped")
            self.assertFalse(json.loads(command("status"))["supervised"])
            with self.assertRaises(OSError):
                urlopen(f"http://127.0.0.1:{port}/", timeout=1)


if __name__ == "__main__":
    unittest.main()
