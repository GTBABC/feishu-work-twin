import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from larkx.storage import Storage
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.scheduling import ModelScheduler
from larkx.workhub.server import create_app
from larkx.workhub.store import HubStore
from tests.test_hub import FakeBackend


def analysis(overview, facts=(), tasks=()):
    return {
        "should_reply": False,
        "reply": "",
        "reply_reason": "无需回复",
        "overview": overview,
        "facts": list(facts),
        "tasks": list(tasks),
        "needs_attention": False,
        "attention_reason": "",
    }


class AnalysisSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.addAsyncCleanup(self.runtime.stop)
        self.runtime.client = SimpleNamespace(auth=SimpleNamespace(user_id="me"))

    def save(self, message_id, chat="c", anchor=""):
        msg = {
            "msg_id": message_id,
            "chat_id": chat,
            "from_id": "u",
            "chat_type": 2,
            "anchor": anchor,
            "content": message_id,
            "msg_type": 4,
        }
        self.storage.save_message(msg, chat_name="群", sender_name="同事")
        self.runtime.hub.observe(msg, "群", "同事")
        return msg

    def process(self, name, msg, *, background=False):
        return self.runtime.spawn(
            self.runtime.engine.process(
                name,
                msg,
                msg["content"],
                [],
                None,
                send_reply=False,
                background=background,
            )
        )

    async def queued(self, count):
        for _ in range(100):
            if self.runtime.model_slots.queued == count:
                return
            await asyncio.sleep(0)
        self.fail(
            f"Expected {count} queued analyses; got {self.runtime.model_slots.queued}"
        )

    async def test_same_chat_topics_are_serialized_and_next_prompt_is_fresh(self):
        first = self.save("first", anchor="topic-a")
        second = self.save("second", anchor="topic-b")
        first_started, second_started, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        prompts = {}

        async def run(key, prompt, **kwargs):
            prompts[key] = prompt
            if key == "work:first":
                first_started.set()
                await release.wait()
                return json.dumps(analysis("第一份概览", ["第一份事实"]))
            second_started.set()
            return json.dumps(analysis("第二份概览", ["第二份事实"]))

        self.runtime.backend.run = run
        old = self.process("first", first, background=True)
        await asyncio.wait_for(first_started.wait(), 1)
        new = self.process("second", second)
        await self.queued(1)
        self.assertFalse(second_started.is_set())
        release.set()
        await asyncio.gather(old, new)
        self.assertIn("第一份概览", prompts["work:second"])
        self.assertEqual(self.runtime.hub.context("c")["overview"], "第二份概览")

    async def test_other_chats_can_reply_while_one_chat_runs_in_background(self):
        started, other_started, release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def run(key, prompt, **kwargs):
            if key == "work:background":
                started.set()
                await release.wait()
            else:
                other_started.set()
            return json.dumps(analysis(key))

        self.runtime.backend.run = run
        background = self.process("background", self.save("old"), background=True)
        await asyncio.wait_for(started.wait(), 1)
        foreground = self.process("foreground", self.save("new", chat="other"))
        await asyncio.wait_for(other_started.wait(), 1)
        await foreground
        self.assertFalse(background.done())
        release.set()
        await background

    async def test_foreground_overtakes_queued_background_without_reverting_overview(
        self,
    ):
        events = {name: asyncio.Event() for name in ("hold-a", "hold-b", "old", "new")}
        release = {name: asyncio.Event() for name in ("hold-a", "hold-b")}
        order, prompts = [], {}

        async def run(key, prompt, **kwargs):
            name = key.removeprefix("work:")
            order.append(name)
            prompts[name] = prompt
            events[name].set()
            if name in release:
                await release[name].wait()
            return json.dumps(analysis(name, [name]))

        self.runtime.backend.run = run
        hold_a = self.process("hold-a", self.save("a", chat="a"))
        hold_b = self.process("hold-b", self.save("b", chat="b"))
        await asyncio.wait_for(
            asyncio.gather(events["hold-a"].wait(), events["hold-b"].wait()), 1
        )
        old_msg, new_msg = self.save("old"), self.save("new")
        old = self.process("old", old_msg, background=True)
        await self.queued(1)
        new = self.process("new", new_msg)
        await self.queued(2)
        release["hold-a"].set()
        await asyncio.wait_for(events["new"].wait(), 1)
        await new
        release["hold-b"].set()
        await asyncio.gather(hold_a, hold_b, old)
        self.assertLess(order.index("new"), order.index("old"))
        self.assertIn('"overview": "new"', prompts["old"])
        self.assertEqual(self.runtime.hub.context("c")["overview"], "new")
        self.assertEqual(
            {m["content"] for m in self.runtime.hub.memories("c")}, {"old", "new"}
        )

    async def test_background_never_occupies_both_model_slots(self):
        first_started, second_started, foreground_started, release = [
            asyncio.Event() for _ in range(4)
        ]

        async def run(key, prompt, **kwargs):
            if key == "work:bg-one":
                first_started.set()
                await release.wait()
            elif key == "work:bg-two":
                second_started.set()
            else:
                foreground_started.set()
            return json.dumps(analysis(key))

        self.runtime.backend.run = run
        first = self.process("bg-one", self.save("one", "one"), background=True)
        await asyncio.wait_for(first_started.wait(), 1)
        second = self.process("bg-two", self.save("two", "two"), background=True)
        await self.queued(1)
        foreground = self.process("foreground", self.save("three", "three"))
        await asyncio.wait_for(foreground_started.wait(), 1)
        await foreground
        self.assertFalse(second_started.is_set())
        self.assertEqual(self.runtime.model_slots.active_background, 1)
        release.set()
        await asyncio.gather(first, second)

    async def test_model_slot_is_freed_during_send_but_chat_remains_reserved(self):
        sending, release, other_started = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def run(key, prompt, **kwargs):
            if key != "work:send":
                other_started.set()
            result = analysis(key)
            if key == "work:send":
                result.update(should_reply=True, reply="回复")
            return json.dumps(result)

        async def send(*args):
            sending.set()
            await release.wait()

        self.runtime.backend.run = run
        msg = self.save("send")
        self.runtime.hub.update_persona("group:c", {"group_reply_policy": "all"})
        task = self.runtime.spawn(
            self.runtime.engine.process("send", msg, "请求", [], send)
        )
        await asyncio.wait_for(sending.wait(), 1)
        self.assertEqual(self.runtime.model_slots.active, 0)
        queued = self.process("same-chat", self.save("later"))
        await self.queued(1)
        other = self.process("other-chat", self.save("other", "other"))
        await asyncio.wait_for(other_started.wait(), 1)
        await other
        self.assertFalse(queued.done())
        release.set()
        await asyncio.gather(task, queued)

    async def test_cancelled_queued_and_running_requests_do_not_leak_capacity(self):
        release, started = asyncio.Event(), asyncio.Event()

        async def run(*args, **kwargs):
            started.set()
            await release.wait()
            return json.dumps(analysis("done"))

        self.runtime.backend.run = run
        running = self.process("running", self.save("one"), background=True)
        await asyncio.wait_for(started.wait(), 1)
        queued = self.process("queued", self.save("two"))
        await self.queued(1)
        queued.cancel()
        await asyncio.gather(queued, return_exceptions=True)
        self.assertEqual(self.runtime.model_slots.queued, 0)
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
        self.assertEqual(self.runtime.model_slots.active, 0)
        self.assertEqual(self.runtime.model_slots.active_background, 0)
        self.assertEqual(self.runtime.model_slots.busy_chats, set())
        release.set()
        await asyncio.wait_for(self.process("fresh", self.save("three")), 1)

    async def test_overview_version_survives_reopen_and_old_facts_are_not_lost(self):
        old_msg, new_msg = self.save("old"), self.save("new")
        self.runtime.hub.apply_analysis(new_msg, analysis("新概览", ["新事实"]))
        reopened = HubStore(self.storage)
        reopened.apply_analysis(
            old_msg, analysis("旧概览", ["旧事实"], [{"title": "遗漏待办"}])
        )
        self.assertEqual(reopened.context("c")["overview"], "新概览")
        self.assertEqual(
            {m["content"] for m in reopened.memories("c")}, {"新事实", "旧事实"}
        )
        self.assertEqual(reopened.tasks()[0]["title"], "遗漏待办")

    async def test_owner_intervention_while_queued_blocks_reply_even_after_pause_expires(
        self,
    ):
        started, release = asyncio.Event(), asyncio.Event()
        sent = []

        async def run(key, prompt, **kwargs):
            if key == "work:hold":
                started.set()
                await release.wait()
                return json.dumps(analysis("旧概览"))
            result = analysis("新概览", ["新事实"])
            result.update(should_reply=True, reply="不应自动发送")
            return json.dumps(result)

        async def send(*args, **kwargs):
            sent.append(args)

        self.runtime.backend.run = run
        old_msg, new_msg = self.save("old"), self.save("new")
        self.runtime.hub.update_persona("group:c", {"group_reply_policy": "all"})
        with patch("larkx.workhub.store.time.time", return_value=1000) as clock:
            hold = self.process("hold", old_msg, background=True)
            await asyncio.wait_for(started.wait(), 1)
            waiting = self.runtime.spawn(
                self.runtime.engine.process("new", new_msg, "请求", [], send)
            )
            await self.queued(1)
            self.runtime.hub.touch_handoff("c", manual=True)
            clock.return_value = 1121
            release.set()
            await asyncio.gather(hold, waiting)
        self.assertFalse(sent)
        self.assertEqual(self.runtime.hub.context("c")["overview"], "新概览")

    async def test_failed_model_releases_chat_for_queued_analysis(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def run(key, prompt, **kwargs):
            if key == "work:bad":
                started.set()
                await release.wait()
                raise RuntimeError("生成失败")
            return json.dumps(analysis("正确概览", ["有效事实"]))

        self.runtime.backend.run = run
        bad = self.process("bad", self.save("bad"), background=True)
        await asyncio.wait_for(started.wait(), 1)
        good = self.process("good", self.save("good"))
        await self.queued(1)
        release.set()
        result = await asyncio.gather(bad, good, return_exceptions=True)
        self.assertIsInstance(result[0], RuntimeError)
        self.assertEqual(self.runtime.hub.context("c")["overview"], "正确概览")
        self.assertEqual(self.runtime.model_slots.active, 0)
        self.assertFalse(self.runtime.model_slots.busy_chats)

    async def test_cancelling_just_granted_slot_does_not_leak_a_reservation(self):
        scheduler = ModelScheduler(limit=1, background_limit=1)

        async def wait():
            async with scheduler.slot(chat_id="c"):
                self.fail("cancelled admission must not enter the model")

        async with scheduler.slot() as held:
            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0)
            self.assertEqual(scheduler.queued, 1)
            held.release_model()
            self.assertEqual(scheduler.active, 1)
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        self.assertEqual(scheduler.active, 0)
        self.assertFalse(scheduler.busy_chats)
        self.assertEqual(scheduler.queued, 0)

    def observe(self, message_id, anchor=""):
        self.runtime.dispatcher = SimpleNamespace(
            should_handle=lambda *args: False, workers={}
        )
        msg = self.save(message_id, anchor=anchor)
        self.runtime.on_saved(msg, "群", "同事", False)

    async def test_background_coalesces_and_new_messages_share_one_worker(self):
        calls, started, release, finished = (
            [],
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def analyze(batch, *, background=False):
            self.assertTrue(background)
            calls.append([m["msg_id"] for m in batch])
            if len(calls) == 1:
                started.set()
                await release.wait()
            else:
                finished.set()

        self.runtime.analyze_messages = analyze
        with patch.object(self.runtime.hub, "background_interval", return_value=0.05):
            self.observe("one")
            self.observe("two")
            self.assertFalse(calls)
            await asyncio.wait_for(started.wait(), 1)
            self.observe("three")
            self.observe("four")
            self.assertEqual(len(self.runtime.observation_workers), 1)
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
        self.assertEqual(calls, [["one", "two"], ["three", "four"]])

    async def test_large_background_batches_and_topics_do_not_drop_or_mix_messages(
        self,
    ):
        batches, finished = [], asyncio.Event()

        async def analyze(batch, *, background=False):
            batches.append(batch)
            if sum(len(b) for b in batches) == 46:
                finished.set()

        self.runtime.analyze_messages = analyze
        with patch.object(self.runtime.hub, "background_interval", return_value=0.01):
            for i in range(45):
                self.observe(f"a{i}", "a")
            self.observe("b0", "b")
            await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(
            {m["msg_id"] for batch in batches for m in batch},
            {f"a{i}" for i in range(45)} | {"b0"},
        )
        self.assertEqual(sorted(len(b) for b in batches), [1, 5, 20, 20])
        self.assertTrue(
            all(len({m["anchor"] for m in batch}) == 1 for batch in batches)
        )

    async def test_background_and_manual_topic_histories_are_separate(self):
        first = self.save("TOPIC_A_CONTENT", anchor="a")
        second = self.save("TOPIC_B_CONTENT", anchor="b")
        calls = []

        async def run(key, prompt, **kwargs):
            calls.append((key, prompt))
            return json.dumps(analysis(""))

        self.runtime.backend.run = run
        await self.runtime.analyze_messages([first], background=True)
        await self.runtime.analyze_messages([second], background=True)
        await self.runtime.analyze_messages([first, second])
        self.assertEqual(
            [key for key, _ in calls],
            ["work:overview:c:a", "work:overview:c:b", "work:overview-all:c"],
        )
        self.assertNotIn("TOPIC_B_CONTENT", calls[0][1])
        self.assertNotIn("TOPIC_A_CONTENT", calls[1][1])
        self.assertIn("TOPIC_A_CONTENT", calls[2][1])
        self.assertIn("TOPIC_B_CONTENT", calls[2][1])

    async def test_failed_background_input_is_kept_without_automatic_retry_loop(self):
        attempts, failed = [], asyncio.Event()

        async def analyze(batch, *, background=False):
            attempts.append([m["msg_id"] for m in batch])
            failed.set()
            raise RuntimeError("模型不可用")

        self.runtime.analyze_messages = analyze
        with patch.object(self.runtime.hub, "background_interval", return_value=0.01):
            self.observe("one")
            await asyncio.wait_for(failed.wait(), 1)
            await asyncio.sleep(0.04)
            self.assertEqual(len(attempts), 1)
            self.assertEqual(
                [m["msg_id"] for m in self.runtime.observations[("c", "")]], ["one"]
            )
            self.assertFalse(self.runtime.observation_workers)
            self.observe("two")
            await asyncio.sleep(0.04)
        self.assertEqual(attempts, [["one"], ["one", "two"]])

    async def test_shutdown_cancels_background_workers_and_scheduler_waiters(self):
        started = asyncio.Event()

        async def run(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.runtime.backend.run = run
        with patch.object(self.runtime.hub, "background_interval", return_value=0.01):
            self.observe("one")
            await asyncio.wait_for(started.wait(), 1)
            self.process("waiting", self.save("two"))
            await self.queued(1)
            await self.runtime.stop()
        self.assertFalse(self.runtime.observation_workers)
        self.assertEqual(self.runtime.model_slots.active, 0)
        self.assertEqual(self.runtime.model_slots.queued, 0)
        self.assertEqual(self.runtime.model_slots.busy_chats, set())

    async def test_background_interval_validation_persists_without_partial_changes(
        self,
    ):
        self.assertEqual(self.runtime.hub.background_interval(), 30)
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            for value in (None, True, "5", 0, 601):
                with self.subTest(value=value):
                    response = client.patch(
                        "/api/settings",
                        headers={"X-Workhub": "1"},
                        json={
                            "own_rules": "should not change",
                            "background_interval": value,
                        },
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertNotEqual(
                        self.runtime.hub.setting("own_rules"), "should not change"
                    )
            response = client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={"background_interval": 45},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["background_interval"], 45)
        self.assertEqual(HubStore(self.storage).background_interval(), 45)
