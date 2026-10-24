import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from larkx.agent.codex_backend import CodexBackend
from larkx.agent.context import compact_context, compact_message_body, context_budget
from larkx.agent.dispatcher import build_xml_message
from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from tests.test_hub import FakeBackend


class CompressionTests(unittest.TestCase):
    def test_context_is_bounded_without_mutating_source_or_losing_identity(self):
        context = {
            "overview": "工作概览" * 4000,
            "persona": {
                "key": "group:g",
                "role": "负责人",
                "description": "背景" * 8000,
                "mode": "draft",
            },
            "participants": {
                "u": {"external_id": "u", "role": "同事", "description": "身份" * 5000}
            },
            "self_user_id": "me",
            "recent_messages": [
                {"msg_id": f"m{n}", "sender_id": "u", "content": "历史" * 5000}
                for n in range(24)
            ],
            "tasks": [
                {"id": n, "title": "待办", "status": "open", "details": "详情" * 5000}
                for n in range(20)
            ],
            "memories": [{"id": n, "content": "记忆" * 5000} for n in range(15)],
        }
        original = copy.deepcopy(context)
        result = compact_context(context, 12000, ["m23"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 12000)
        self.assertEqual(result["recent_messages"][-1]["content"], "[见本次消息]")
        self.assertEqual(result["recent_messages"][-1]["sender_id"], "u")
        self.assertEqual(result["persona"]["mode"], "draft")
        self.assertEqual(result["participants"]["u"]["external_id"], "u")
        self.assertEqual([row["id"] for row in result["tasks"]], list(range(20)))
        self.assertEqual(context, original)

    def test_message_clipping_keeps_valid_xml_all_senders_and_latest_tail(self):
        messages = [
            build_xml_message(
                {
                    "msg_id": f"m{n}",
                    "chat_id": "c",
                    "from_id": f"u{n}",
                    "content": "&<>长消息" * 10000 + "最后一个明确请求",
                }
            )
            for n in range(20)
        ]
        result = compact_message_body("\n".join(messages), 12000)
        self.assertLessEqual(len(result), 12000)
        xml = ElementTree.fromstring("<batch>" + result + "</batch>")
        self.assertEqual(len(xml), 20)
        self.assertEqual(xml[-1].get("id"), "m19")
        self.assertEqual(xml[-1].find("sender").get("id"), "u19")
        self.assertIn("最后一个明确请求", xml[-1].find("content").text)

    def test_tiny_metadata_budget_fails_instead_of_dropping_ids(self):
        with self.assertRaises(ValueError):
            compact_context(
                {
                    "tasks": [
                        {"id": n, "title": str(n), "status": "open"} for n in range(20)
                    ]
                },
                30,
            )
        with self.assertRaises(ValueError):
            compact_message_body(
                build_xml_message(
                    {"chat_id": "c", "from_id": "u", "content": "很长" * 1000}
                ),
                20,
            )

    def test_budget_is_configurable_and_clamped(self):
        for value, expected in (
            ("bad", 48000),
            ("1", 24000),
            ("80000", 80000),
            ("99999999", 200000),
        ):
            with patch.dict("os.environ", {"LARKX_CONTEXT_BUDGET": value}):
                self.assertEqual(context_budget(), expected)


class BackendCompressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(
            patch.dict(
                "os.environ",
                {"LARKX_CODEX_PATH": sys.executable, "LARKX_CONTEXT_BUDGET": "48000"},
            )
        )
        self.enterContext(patch("larkx.agent.codex_backend.DATA_DIR", Path(self.temp)))
        self.backend = CodexBackend()
        self.inputs, self.calls = [], []
        self.reply = {
            "answer": "小张在跟进",
            "actions": [{"content": "不要重复携带的动作"}],
        }

        async def spawn(*args, **kwargs):
            output = Path(args[args.index("--output-last-message") + 1])
            owner = self

            class Process:
                returncode = 0

                async def communicate(self, data):
                    owner.inputs.append(data.decode("utf-8"))
                    output.write_text(
                        json.dumps(owner.reply, ensure_ascii=False), encoding="utf-8"
                    )
                    return None, b""

            self.calls.append(args)
            return Process()

        self.enterContext(patch("asyncio.create_subprocess_exec", spawn))

    async def test_structured_calls_drop_snapshot_history_and_expire_images(self):
        image = Path(self.temp) / "image.png"
        image.write_bytes(b"fixture")
        self.backend._path("a").write_text(
            json.dumps(
                [
                    {
                        "role": "user",
                        "content": "LEGACY_FULL_PROMPT" * 20000,
                        "images": [str(image)],
                    }
                ]
            ),
            encoding="utf-8",
        )
        await self.backend.run("a", "新上下文", response_schema={"type": "object"})
        await self.backend.run("a", "第二轮", response_schema={"type": "object"})
        self.assertNotIn("LEGACY_FULL_PROMPT", self.inputs[0])
        self.assertNotIn("新上下文", self.inputs[1])
        self.assertIn(str(image), self.calls[1])
        history = self.backend._path("a").read_text(encoding="utf-8")
        self.assertNotIn("新上下文", history)
        self.assertNotIn("不要重复携带的动作", history)
        self.assertLess(len(history), 1000)
        for _ in range(5):
            await self.backend.run("a", "后续分析", response_schema={"type": "object"})
        self.assertNotIn(str(image), self.calls[-1])
        self.assertFalse(self.backend._path("a").exists())

    async def test_owner_history_keeps_short_input_and_answer_but_not_task_snapshot(
        self,
    ):
        await self.backend.run(
            "owner",
            "任务快照" * 4000,
            response_schema={"type": "object"},
            history_text="谁在跟进这个项目",
        )
        await self.backend.run(
            "owner", "然后呢", response_schema={"type": "object"}, history_text="然后呢"
        )
        self.assertIn("谁在跟进这个项目", self.inputs[1])
        self.assertIn("小张在跟进", self.inputs[1])
        self.assertNotIn("任务快照", self.inputs[1])
        self.assertNotIn("不要重复携带的动作", self.inputs[1])

    async def test_budget_bounds_history_and_never_silently_cuts_current_rules(self):
        self.backend._path("a").write_text(
            json.dumps(
                [
                    {"role": "user", "content": "OLD_SNAPSHOT" + "x" * 40001},
                    *[
                        {"role": "user", "content": "历史" * 5000, "compact": True}
                        for _ in range(18)
                    ],
                    {"role": "assistant", "content": "RECENT"},
                ]
            ),
            encoding="utf-8",
        )
        await self.backend.run("a", "当前" * 21000)
        self.assertLessEqual(len(self.inputs[0]), 48000)
        self.assertNotIn("OLD_SNAPSHOT", self.inputs[0])
        self.assertIn("RECENT", self.inputs[0])
        with self.assertRaises(ValueError):
            await self.backend.run("a", "当前规则" * 20000)
        self.assertEqual(len(self.inputs), 1)

    async def test_engine_and_polish_use_stateless_schema_and_preserve_owner_rules(
        self,
    ):
        storage = Storage("sqlite:///" + str(Path(self.temp) / "test.db"))
        self.addCleanup(storage.engine.dispose)
        runtime = HubRuntime(storage, FakeBackend())
        captured = []
        original_run = runtime.backend.run

        async def run(*args, **kwargs):
            captured.append((args[1], kwargs))
            return await original_run(*args, **kwargs)

        runtime.backend.run = run
        runtime.hub.set_setting("own_rules", "必须保留的工作原则")
        msg = {
            "chat_id": "c",
            "msg_id": "m",
            "chat_type": 1,
            "from_id": "u",
            "content": "很长" * 40000,
        }
        storage.save_message(msg)
        runtime.hub.observe(msg, "同事", "同事")
        for index in range(100):
            runtime.hub.create_task({"title": f"已有事项 {index}", "chat_id": "c"})
        await runtime.engine.process(
            "c", msg, build_xml_message(msg), [], None, send_reply=False
        )
        await runtime.engine.polish_reply("c", "保留的草稿", "更简短")
        for prompt, options in captured:
            self.assertTrue(options["response_schema"])
            self.assertNotIn("history_text", options)
            self.assertIn("必须保留的工作原则", prompt)
            self.assertLess(len(prompt), 48000)
        self.assertIn("已有事项 99", captured[0][0])
        self.assertEqual(storage.get_messages("c")[0].content, msg["content"])
        self.assertIn("保留的草稿", captured[1][0])
