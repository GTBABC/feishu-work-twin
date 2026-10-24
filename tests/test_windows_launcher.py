import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest
from urllib.request import urlopen


@unittest.skipUnless(os.name == "nt", "Windows manual-launch regression")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(
            self.enterContext(tempfile.TemporaryDirectory(prefix="workhub 空格 "))
        )
        self.repo = Path(__file__).resolve().parents[1]
        for filename in ("start-workhub.ps1", "start-workhub.cmd"):
            shutil.copyfile(self.repo / filename, self.temp / filename)
        self.powershell = str(
            Path(os.environ["SystemRoot"])
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.env = {
            **os.environ,
            "PYTHONPATH": str(self.repo),
            "PYTHONUTF8": "1",
            "UV_PROJECT": str(self.repo),
            "UV_OFFLINE": "1",
            "LARKX_HOME": str(self.temp / "data"),
            "LARKX_STARTUP_PATH": str(self.temp / "bootstrap" / "startup.json"),
            "LARKX_STORAGE_URL": "",
            "LARKX_PORT": str(self.port),
        }

    def batch(self, *args, env=None):
        return subprocess.run(
            [
                os.environ.get("COMSPEC", "cmd.exe"),
                "/d",
                "/c",
                str(self.temp / "start-workhub.cmd"),
                *args,
            ],
            cwd=self.repo,
            env=env or self.env,
            input=b"\n",
            capture_output=True,
            timeout=75,
        )

    def test_system_powershell_51_parses_the_real_entrypoint(self):
        script = (
            "$ProgressPreference='SilentlyContinue'; $tok=$null; $err=$null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{self.temp / 'start-workhub.ps1'}',"
            "[ref]$tok,[ref]$err) | Out-Null; "
            "[pscustomobject]@{version=$PSVersionTable.PSVersion.ToString();errors=@($err | ForEach-Object Message)} | ConvertTo-Json"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [
                self.powershell,
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        parsed = json.loads(result.stdout.decode("utf-8"))
        self.assertTrue(parsed["version"].startswith("5.1."))
        self.assertEqual(parsed["errors"], [])

    def test_double_click_entry_starts_real_service_status_and_stop(self):
        try:
            result = self.batch()
            self.assertEqual(
                result.returncode,
                0,
                (result.stdout + result.stderr).decode("utf-8", errors="replace"),
            )
            # Success must mean the web app is already reachable, rather than
            # merely that a background process was created.
            with urlopen(f"http://127.0.0.1:{self.port}/api/state", timeout=2) as response:
                self.assertTrue(json.load(response).get("status"))
            result = self.batch("-Status")
            self.assertEqual(result.returncode, 0)
            self.assertIn(b'"supervised": true', result.stdout)
            self.assertIn(b'"windowless": true', result.stdout)
            log = self.temp / "data" / "logs" / "workhub" / "startup.log"
            self.assertTrue(log.is_file())
            self.assertIn("supervised", log.read_text(encoding="utf-8-sig"))
        finally:
            stopped = self.batch("-Stop")
            self.assertEqual(
                stopped.returncode, 0, stopped.stderr.decode("utf-8", errors="replace")
            )
        with self.assertRaises(OSError):
            urlopen(f"http://127.0.0.1:{self.port}/", timeout=1)

    def test_failed_entry_keeps_error_and_writes_startup_log(self):
        # Fail before daemon launch; the wrapper must preserve a nonzero exit and
        # reach PAUSE rather than disappearing with the diagnostic output.
        commands = self.temp / "commands"
        commands.mkdir()
        (commands / "uv.cmd").write_text(
            "@echo off\necho synthetic launcher failure 1>&2\nexit /b 7\n"
        )
        result = self.batch(
            env={**self.env, "PATH": str(commands) + os.pathsep + self.env["PATH"]}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"Workhub startup failed", result.stdout)
        self.assertIn(b"synthetic launcher failure", result.stdout + result.stderr)
        log = self.temp / "data" / "logs" / "workhub" / "startup.log"
        self.assertTrue(log.is_file())
        self.assertIn("synthetic launcher failure", log.read_text(encoding="utf-8-sig"))
        self.assertFalse(
            (self.temp / "bootstrap" / "workhub-supervisor" / "state.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
