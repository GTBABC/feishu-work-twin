"""Local native dialogs return paths without uploading or opening file contents."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

KINDS = {"directory", "database", "program"}


def dialog_program():
    if (
        os.name != "nt"
        or not importlib.util.find_spec("tkinter")
        or not importlib.util.find_spec("_tkinter")
    ):
        return None
    gui = Path(sys.executable).with_name("pythonw.exe")
    return str(gui) if gui.is_file() else sys.executable


def parent_alive(identity):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, identity)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return (
            bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
            and code.value == 259
        )
    finally:
        kernel.CloseHandle(handle)


def initial_directory(initial):
    candidate = Path(initial).expanduser() if initial else Path.home()
    if not candidate.is_absolute():
        return str(Path.home())
    while not candidate.is_dir():
        if candidate.parent == candidate:
            return str(Path.home())
        candidate = candidate.parent
    return str(candidate)


def native_dialog(request_file, result_file):
    """Run only in a disposable GUI process, on its main thread."""
    root = None
    finished = threading.Event()
    result = {"error": "无法打开系统选择框，请手动填写路径"}
    try:
        request = json.loads(Path(request_file).read_text(encoding="utf-8"))
        if request["kind"] not in KINDS:
            raise ValueError()

        def watch_parent():
            deadline = time.monotonic() + 300
            while not finished.wait(1):
                if time.monotonic() >= deadline or not parent_alive(
                    request["parent_id"]
                ):
                    os._exit(1)

        threading.Thread(target=watch_parent, daemon=True).start()
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.title("工作数字人 · 选择路径")
        root.geometry("360x90")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        tk.Label(root, text="完成选择后将返回设置页面。", padx=24, pady=24).pack()
        root.update_idletasks()
        root.eval("tk::PlaceWindow . center")
        options = {"parent": root, "initialdir": initial_directory(request["initial"])}
        kind = request["kind"]
        if kind == "directory":
            chosen = filedialog.askdirectory(
                title="选择数据目录", mustexist=True, **options
            )
        else:
            options["filetypes"] = [
                ("SQLite 数据库", "*.db *.sqlite *.sqlite3"),
                ("所有文件", "*.*"),
            ]
            if kind == "program":
                options["filetypes"] = [
                    ("程序文件", "*.exe *.cmd *.bat *.com *.ps1"),
                    ("所有文件", "*.*"),
                ]
                chosen = filedialog.askopenfilename(title="选择 CLI 程序", **options)
            else:
                chosen = filedialog.askopenfilename(title="选择已有数据库", **options)
        result = {"path": chosen or None}
    except Exception:
        pass
    finally:
        finished.set()
        if root is not None:
            root.destroy()
        Path(result_file).write_text(
            json.dumps(result, ensure_ascii=False), encoding="utf-8"
        )


class PathPicker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.process = None

    def status(self):
        return {"available": bool(dialog_program()), "busy": self.lock.locked()}

    async def close(self):
        process = self.process
        if process and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

    async def pick(self, body):
        if not isinstance(body, dict) or body.keys() - {"kind", "initial"}:
            raise ValueError("路径选择参数格式无效")
        kind, initial = body.get("kind"), body.get("initial", "")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("不支持的路径选择类型")
        if (
            not isinstance(initial, str)
            or len(initial) > 4096
            or any(c in initial for c in "\x00\r\n")
        ):
            raise ValueError("初始路径格式无效")
        program = dialog_program()
        if not program:
            raise ValueError("当前环境无法打开系统选择框，请手动填写路径")
        if self.lock.locked():
            raise ValueError("已有选择窗口打开，请先完成或取消选择")
        async with self.lock:
            with tempfile.TemporaryDirectory(prefix="larkx-picker-") as temp:
                request_file, result_file = (
                    Path(temp) / "request.json",
                    Path(temp) / "result.json",
                )
                request_file.write_text(
                    json.dumps(
                        {"kind": kind, "initial": initial, "parent_id": os.getpid()},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                try:
                    self.process = await asyncio.create_subprocess_exec(
                        program,
                        "-m",
                        "larkx.workhub.path_picker",
                        str(request_file),
                        str(result_file),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW
                        if os.name == "nt"
                        else 0,
                    )
                    await asyncio.wait_for(self.process.wait(), 300)
                    if self.process.returncode:
                        raise ValueError("无法打开系统选择框，请手动填写路径")
                    result = json.loads(result_file.read_text(encoding="utf-8"))
                    if not isinstance(result, dict) or "path" not in result:
                        raise ValueError("无法打开系统选择框，请手动填写路径")
                    chosen = result["path"]
                    if chosen is None:
                        return {"path": None}
                    if not isinstance(chosen, str) or not chosen or len(chosen) > 4096:
                        raise ValueError("未取得有效路径，请重试")
                    path = Path(chosen)
                    if not path.is_absolute():
                        raise ValueError("未取得有效路径，请重试")
                    if kind == "directory" and not path.is_dir():
                        raise ValueError("所选目录不存在")
                    if kind in ("database", "program") and not path.is_file():
                        raise ValueError("所选文件不存在")
                    return {"path": str(path)}
                except asyncio.TimeoutError:
                    raise ValueError("选择窗口已超时，请重试或手动填写路径") from None
                except (OSError, UnicodeError, json.JSONDecodeError):
                    raise ValueError("无法读取所选路径，请手动填写") from None
                finally:
                    await self.close()
                    self.process = None


if __name__ == "__main__":
    native_dialog(sys.argv[1], sys.argv[2])
