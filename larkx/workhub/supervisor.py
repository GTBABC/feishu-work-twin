"""Detached local supervisor. Process control stays local and uses no HTTP endpoint."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import URLError
from urllib.request import urlopen


class AlreadyRunning(RuntimeError):
    pass


def background_executable():
    if os.name != "nt":
        return sys.executable
    # The venv python.exe redirector can create a console for its child even
    # when its own process was detached. Use the GUI interpreter at both levels.
    executable = Path(sys.executable).with_name("pythonw.exe")
    if not executable.is_file():
        raise RuntimeError("缺少 pythonw.exe，请修复 Python 安装后重新创建虚拟环境")
    return str(executable)


def console_attached():
    if os.name != "nt":
        return False
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleCP())


def control_dir():
    from ..startup import runtime_dir

    return runtime_dir() / "workhub-supervisor"


def legacy_control_dir():
    from ..startup import config_path

    return config_path().expanduser().resolve().parent / "workhub-supervisor"


def query_dir():
    folder = control_dir()
    legacy = legacy_control_dir()
    # A manual -Stop must still reach an old guardian during the first upgrade.
    if folder != legacy and not is_running(folder) and is_running(legacy):
        return legacy
    return folder


def process_started(pid):
    """An OS creation marker distinguishes a live process from a recycled PID."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [
            ctypes.POINTER(wintypes.FILETIME)
        ] * 4
        kernel.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if (
                not kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                or code.value != 259
            ):
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if stat[0] == "Z":
            return None
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + stat[19]
    except FileNotFoundError:
        # macOS and other Unix systems without procfs expose start time via ps.
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return result.stdout.strip() or None if result.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None
    except (OSError, ValueError, IndexError):
        return None


@contextmanager
def supervisor_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "supervisor.lock").open("a+b") as lock:
        if not os.fstat(lock.fileno()).st_size:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AlreadyRunning("守护进程已在运行") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def is_running(folder):
    try:
        with supervisor_lock(folder):
            return False
    except AlreadyRunning:
        return True


def read_state(folder):
    try:
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def status(folder):
    source = {"state_file": str((folder / "state.json").resolve())}
    if not is_running(folder):
        return {
            **source,
            "phase": "stopped",
            "pid": None,
            "child_pid": None,
            "supervised": False,
        }
    state = read_state(folder)
    actual = process_started(state.get("pid"))
    recorded = state.get("process_started")
    if not actual or recorded != actual:
        return {
            **source,
            "phase": "unknown",
            "pid": None,
            "child_pid": None,
            "supervised": True,
            "status_error": "运行锁被占用，但状态文件与进程身份不一致",
        }
    current = {**state, **source, "supervised": True}
    child = state.get("child_pid")
    if child is not None:
        child_started = process_started(child)
        if (
            not child_started
            or state.get("child_process_started", child_started) != child_started
        ):
            current["child_pid"] = None
    return current


def write_state(folder, state):
    # Readers must never see a partially written status file.
    temp = folder / (uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(temp, folder / "state.json")
                break
            except PermissionError:
                # Windows status readers can briefly deny file replacement.
                if attempt == 5:
                    raise
                time.sleep(0.02)
    finally:
        temp.unlink(missing_ok=True)


def event_logger(folder):
    folder.mkdir(parents=True, exist_ok=True)
    logger = logging.Logger("workhub-supervisor")
    handler = RotatingFileHandler(
        folder / "supervisor.jsonl",
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger


def service_log(folder):
    path = folder / "service.log"
    for index in range(4, 0, -1):
        source = path if index == 1 else folder / f"service.log.{index - 1}"
        target = folder / f"service.log.{index}"
        if source.exists():
            os.replace(source, target)
    return path


def restart_delay(failures):
    return min(60, 2 ** min(failures, 6))


class Supervisor:
    def __init__(self, folder, logs, command=None, *, poll=0.2, grace=30):
        self.folder = Path(folder).resolve()
        self.logs = Path(logs)
        self.command = command or [
            background_executable(),
            "-X",
            "faulthandler",
            "-m",
            "larkx.workhub.supervisor",
            "worker",
        ]
        self.poll = poll
        self.grace = grace
        self.stopping = threading.Event()
        self.state = {
            "session": uuid.uuid4().hex,
            "pid": os.getpid(),
            "process_started": process_started(os.getpid()),
        }
        self.logger = None

    def record(self, event, **details):
        entry = {
            "time": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "session": self.state["session"],
            **details,
        }
        self.logger.info(json.dumps(entry, ensure_ascii=False))

    def update(self, phase, **details):
        self.state.update(phase=phase, updated_at=time.time(), **details)
        write_state(self.folder, self.state)

    def stop_requested(self):
        if self.stopping.is_set():
            return True
        try:
            return (self.folder / "stop").read_text() == self.state["session"]
        except OSError:
            return False

    def stop_child(self, child):
        if child.poll() is not None:
            return
        self.record("stop_requested", child_pid=child.pid)
        try:
            child.stdin.write(b"\n")
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        try:
            child.wait(timeout=self.grace)
        except subprocess.TimeoutExpired:
            self.record("forced_stop", child_pid=child.pid, timeout_seconds=self.grace)
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    check=False,
                    timeout=10,
                )
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)

    def run(self):
        with supervisor_lock(self.folder):
            self.logger = event_logger(self.logs)
            previous = read_state(self.folder)
            if previous.get("phase") in ("starting", "running", "backoff"):
                self.record(
                    "previous_supervisor_lost",
                    previous_pid=previous.get("pid"),
                    reason="上次守护未记录正常结束；可能被外部终止或系统关闭，具体原因未知",
                )
            attached = console_attached()
            self.update("starting", log_dir=str(self.logs), windowless=not attached)
            self.record(
                "supervisor_started",
                pid=os.getpid(),
                console_attached=attached,
                state_file=str(self.folder / "state.json"),
            )
            failures = 0
            child = None
            try:
                while not self.stop_requested():
                    path = service_log(self.logs)
                    began = time.monotonic()
                    try:
                        with path.open("ab", buffering=0) as output:
                            options = (
                                {"creationflags": subprocess.CREATE_NO_WINDOW}
                                if os.name == "nt"
                                else {"start_new_session": True}
                            )
                            child = subprocess.Popen(
                                self.command,
                                stdin=subprocess.PIPE,
                                stdout=output,
                                stderr=subprocess.STDOUT,
                                env={
                                    **os.environ,
                                    "PYTHONUTF8": "1",
                                    "PYTHONUNBUFFERED": "1",
                                },
                                **options,
                            )
                            self.update(
                                "running",
                                child_pid=child.pid,
                                child_process_started=process_started(child.pid),
                                restart_in_seconds=None,
                            )
                            self.record(
                                "service_started",
                                child_pid=child.pid,
                                log_file=str(path),
                            )
                            while child.poll() is None and not self.stop_requested():
                                self.stopping.wait(self.poll)
                            requested = self.stop_requested()
                            if requested:
                                self.stop_child(child)
                            code = child.wait()
                            duration = round(time.monotonic() - began, 3)
                            self.record(
                                "service_exited",
                                child_pid=child.pid,
                                exit_code=code,
                                exit_code_hex=f"0x{code & 0xFFFFFFFF:08X}",
                                duration_seconds=duration,
                                reason="requested_stop"
                                if requested
                                else (
                                    "normal_exit" if code == 0 else "unexpected_exit"
                                ),
                                log_file=str(path),
                            )
                            child.stdin.close()
                            child = None
                            if requested or code == 0:
                                break
                    except OSError as exc:
                        if child is not None:
                            raise
                        duration = time.monotonic() - began
                        self.record(
                            "launch_failed",
                            error_type=type(exc).__name__,
                            error=str(exc),
                        )
                    failures = 1 if duration >= 60 else failures + 1
                    delay = restart_delay(failures)
                    self.update("backoff", child_pid=None, restart_in_seconds=delay)
                    self.record("restart_scheduled", delay_seconds=delay)
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline and not self.stop_requested():
                        self.stopping.wait(self.poll)
                self.update("stopped", child_pid=None)
                self.record("supervisor_stopped")
            except BaseException as exc:
                self.record(
                    "supervisor_failed", error_type=type(exc).__name__, error=str(exc)
                )
                self.update("failed")
                raise
            finally:
                if child is not None:
                    self.stop_child(child)
                    child.stdin.close()
                for handler in self.logger.handlers:
                    handler.close()


def worker():
    from .server import run

    print(
        json.dumps({"event": "worker_started", "console_attached": console_attached()}),
        flush=True,
    )
    stopped = threading.Event()

    def watch_parent():
        # EOF also shuts down the worker if the guardian itself is killed.
        sys.stdin.buffer.readline()
        stopped.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    run(stop_event=stopped)


def start(folder):
    if is_running(folder):
        state = read_state(folder)
        if os.name == "nt" and not state.get("windowless"):
            raise RuntimeError(
                "已有旧版或有窗口的守护在运行，请先执行 .\\start-workhub.ps1 -Stop，再重新启动"
            )
        current = status(folder)
        if current.get("status_error"):
            raise RuntimeError(current["status_error"])
        return current
    from ..config import DATA_DIR, STARTUP

    try:
        with urlopen(f"http://127.0.0.1:{STARTUP['port']}/api/state", timeout=2):
            raise RuntimeError("已有未受守护的服务在运行，请先停止原服务再启动守护")
    except URLError:
        pass
    logs = DATA_DIR / "logs" / "workhub"
    logs.mkdir(parents=True, exist_ok=True)
    options = (
        {
            "creationflags": subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )
    began = time.time()
    with (logs / "launcher.log").open("ab") as output:
        child = subprocess.Popen(
            [background_executable(), "-m", "larkx.workhub.supervisor", "run"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUTF8": "1"},
            **options,
        )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = read_state(folder)
        if (
            is_running(folder)
            and state.get("phase") in ("running", "backoff")
            and state.get("updated_at", 0) >= began
        ):
            current = status(folder)
            if current.get("pid"):
                return current
        if child.poll() is not None and not is_running(folder):
            break
        time.sleep(0.1)
    raise RuntimeError(f"守护启动失败或超时，请查看 {logs / 'launcher.log'}")


def stop(folder, timeout=60):
    if not is_running(folder):
        return
    state = status(folder)
    if state.get("status_error"):
        previous = read_state(folder)
        if (
            folder.resolve() == legacy_control_dir().resolve()
            and previous.get("process_started") is None
            and previous.get("session")
        ):
            # Legacy migration only sends its session token; it never kills a PID
            # from an unverified file. Its old guardian must acknowledge the stop.
            state = previous
        else:
            raise RuntimeError(state["status_error"] + "，未发送停止指令")
    if not state.get("session"):
        raise RuntimeError("守护正在启动，请稍后停止")
    (folder / "stop").write_text(state["session"])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(folder):
            return
        time.sleep(0.1)
    raise RuntimeError("停止超时，请查看守护日志；未强制终止其他进程")


def main():
    # Load .env before resolving the independent control directory.
    from .. import config

    parser = argparse.ArgumentParser(description="工作台后台进程守护")
    parser.add_argument("action", choices=("start", "stop", "status", "run", "worker"))
    args = parser.parse_args()
    folder = control_dir() if args.action in ("run", "worker") else query_dir()
    if args.action == "worker":
        worker()
    elif args.action == "run":
        guard = Supervisor(folder, config.DATA_DIR / "logs" / "workhub")
        for name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(name, lambda *_: guard.stopping.set())
        try:
            guard.run()
        except AlreadyRunning:
            return
    elif args.action == "stop":
        stop(folder)
        print("工作台和守护已停止")
    else:
        state = start(folder) if args.action == "start" else status(folder)
        print(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
