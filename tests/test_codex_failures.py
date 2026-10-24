import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

from larkx.agent.codex_backend import CodexBackend, failure_reason
from tests import test_codex


class CodexFailureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_codex.BackendTests.setUp(self)

    def test_error_categories_do_not_expose_raw_output(self):
        cases = {
            "ERROR: stream disconnected: error sending request": "network",
            "ERROR: Unauthorized 401": "auth",
            "ERROR: usage_limit_reached": "limit",
            "ERROR: model_not_found": "model",
            "ERROR: invalid_json_schema": "schema",
            "ERROR: context_length_exceeded": "context",
            "ERROR: unexplained failure": "unknown",
        }
        for raw, expected in cases.items():
            category, message = failure_reason(
                (raw + "\nprivate-chat-and-secret").encode()
            )
            self.assertEqual(category, expected)
            self.assertNotIn("private-chat-and-secret", message)
        self.assertEqual(
            failure_reason(b"User prompt mentions network\nERROR: invalid_json_schema")[
                0
            ],
            "schema",
        )

    async def test_network_failure_retries_generation_once_not_history(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("larkx.agent.codex_backend.DATA_DIR", Path(folder)):
                backend = CodexBackend()
            calls = []

            async def spawn(*args, **kwargs):
                calls.append(args)
                output = Path(args[args.index("--output-last-message") + 1])
                first = len(calls) == 1

                class Process:
                    returncode = 1 if first else 0

                    async def communicate(self, data):
                        output.write_text(
                            "stale" if first else "fresh", encoding="utf-8"
                        )
                        return None, b"ERROR: stream disconnected" if first else b""

                return Process()

            with (
                patch("asyncio.create_subprocess_exec", spawn),
                patch("asyncio.sleep", new_callable=AsyncMock),
            ):
                self.assertEqual(await backend.run("chat", "question"), "fresh")
            self.assertEqual(len(calls), 2)
            self.assertIn("apps", calls[0])
            self.assertIn("plugins", calls[0])
            self.assertEqual(
                len(json.loads(backend._path("chat").read_text(encoding="utf-8"))), 2
            )

    async def test_auth_failure_does_not_retry_or_claim_network_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("larkx.agent.codex_backend.DATA_DIR", Path(folder)):
                backend = CodexBackend()

            async def spawn(*args, **kwargs):
                class Process:
                    returncode = 1

                    async def communicate(self, data):
                        return None, b"ERROR: Unauthorized 401; private-secret"

                return Process()

            with patch("asyncio.create_subprocess_exec", side_effect=spawn) as mocked:
                with self.assertRaisesRegex(RuntimeError, "登录凭据") as raised:
                    await backend.run("chat", "question")
                self.assertEqual(mocked.call_count, 1)
                self.assertNotIn("private-secret", str(raised.exception))
