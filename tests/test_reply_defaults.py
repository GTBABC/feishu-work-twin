import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from larkx.agent.dispatcher import AgentDispatcher
from larkx.storage import Storage
from larkx.workhub.models import Persona, Setting
from larkx.workhub.runtime import HubRuntime
from larkx.workhub.server import create_app
from larkx.workhub.store import HubStore
from tests.test_hub import FakeBackend


class ReplyDefaultsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage("sqlite:///" + str(Path(self.temp) / "defaults.db"))
        self.addCleanup(self.storage.engine.dispose)
        self.runtime = HubRuntime(self.storage, FakeBackend())
        self.addAsyncCleanup(self.runtime.stop)
        self.hub = self.runtime.hub
        self.hub.update_settings(
            {
                "default_private_reply": "auto",
                "default_group_reply": "off",
            }
        )
        self.private = {
            "msg_id": "p",
            "chat_id": "p",
            "chat_type": 1,
            "from_id": "u",
            "content": "请跟进",
        }
        self.group = {
            "msg_id": "g",
            "chat_id": "g",
            "chat_type": 2,
            "from_id": "u",
            "content": "请跟进",
        }
        self.hub.observe(self.private)
        self.hub.observe(self.group)

    async def test_fresh_installation_does_not_reply_to_private_or_group_messages(self):
        fresh_storage = Storage("sqlite:///" + str(Path(self.temp) / "fresh.db"))
        self.addCleanup(fresh_storage.engine.dispose)
        with patch.dict("os.environ", {}, clear=True):
            runtime = HubRuntime(fresh_storage, FakeBackend())
            self.addAsyncCleanup(runtime.stop)
            self.assertEqual(runtime.hub.settings()["default_private_reply"], "off")
            self.assertEqual(runtime.hub.settings()["default_group_reply"], "off")

            async def send(*args):
                self.fail("fresh installations must not automatically reply")

            for msg in (self.private, self.group):
                runtime.hub.observe(msg)
                profile = runtime.hub.context(msg["chat_id"], "u")["persona"]
                self.assertEqual(profile["mode"], "inherit")
                self.assertEqual(profile["effective_mode"], "off")
                await runtime.engine.process(
                    msg["chat_id"] + ":", msg, msg["content"], [], send
                )

    def test_defaults_and_overrides_resolve_in_all_read_paths(self):
        self.hub.update_settings(
            {"default_private_reply": "draft", "default_group_reply": "at_me"}
        )
        self.assertEqual(
            self.hub.context("p", "u")["persona"]["effective_mode"], "draft"
        )
        group_ctx = self.hub.context("g", "u")
        self.assertEqual(group_ctx["persona"]["effective_group_reply_policy"], "at_me")
        # A private-chat default must not suppress a group's replies.
        self.assertEqual(group_ctx["sender"]["effective_mode"], "auto")
        self.hub.update_persona("user:u", {"mode": "auto"})
        self.hub.update_persona("group:g", {"group_reply_policy": "off"})
        self.hub.update_settings(
            {"default_private_reply": "off", "default_group_reply": "all"}
        )
        for profiles in (
            self.hub.personas(),
            [c["persona"] for c in self.hub.conversations()],
        ):
            person = next(p for p in profiles if p["kind"] == "person")
            group = next(p for p in profiles if p["kind"] == "group")
            self.assertEqual(person["effective_mode"], "auto")
            self.assertEqual(group["effective_group_reply_policy"], "off")
        self.hub.update_persona("user:u", {"mode": "inherit"})
        self.hub.update_persona("group:g", {"group_reply_policy": "inherit"})
        self.assertEqual(self.hub.context("p", "u")["sender"]["effective_mode"], "off")
        self.assertEqual(self.hub.context("g", "u")["sender"]["effective_mode"], "auto")
        self.assertEqual(self.hub.group_reply_policy("g"), "all")
        self.assertEqual(self.hub.setting("group_reply_policy:g", "absent"), "absent")
        self.assertEqual(HubStore(self.storage).group_reply_policy("g"), "all")

    def test_inherited_group_policy_controls_dispatch_and_explicit_override_wins(self):
        dispatcher = AgentDispatcher(
            self.runtime.backend, None, workflow=self.runtime.engine
        )
        dispatcher.cfg["triggers"] = {}
        for policy, flags, allowed in (
            ("off", {"at_me": True}, False),
            ("all", {}, True),
            ("at_me", {}, False),
            ("at_me", {"at_me": True}, True),
            ("at_all", {"at_me": True}, False),
            ("at_all", {"at_all": True}, True),
        ):
            self.hub.update_settings({"default_group_reply": policy})
            self.assertEqual(
                dispatcher.should_handle({**self.group, **flags}, "me"), allowed
            )
        self.hub.update_persona("group:g", {"group_reply_policy": "all"})
        self.hub.update_settings({"default_group_reply": "off"})
        self.assertTrue(dispatcher.should_handle(self.group, "me"))

    async def test_defaults_and_individual_overrides_determine_actual_send(self):
        cases = [
            ("auto", "inherit", 1, 0),
            ("off", "auto", 1, 0),
            ("auto", "off", 0, 0),
            ("draft", "inherit", 0, 1),
        ]
        self.hub.set_setting(
            "automation_enabled", "false"
        )  # Obsolete pause must not block explicit policies.
        sent = []

        async def send(*args):
            sent.append(args)

        for index, (default, override, sends, drafts) in enumerate(cases):
            self.hub.update_settings({"default_private_reply": default})
            self.hub.update_persona("user:u", {"mode": override})
            before_sent = len(sent)
            before_draft = self.hub.reply_draft("p")
            msg = {**self.private, "msg_id": f"private-{index}"}
            await self.runtime.engine.process("p:", msg, msg["content"], [], send)
            self.assertEqual(len(sent) - before_sent, sends)
            current_draft = self.hub.reply_draft("p")
            self.assertEqual(
                bool(current_draft and current_draft != before_draft), bool(drafts)
            )
        self.hub.update_persona("user:u", {"mode": "inherit"})
        self.hub.update_settings(
            {
                "default_private_reply": "off",
                "default_group_reply": "all",
            }
        )
        await self.runtime.engine.process(
            "g:", self.group, self.group["content"], [], send
        )
        self.assertEqual(len(sent), 3)

    async def test_default_changed_during_generation_is_rechecked_before_send(self):
        original = self.runtime.backend.run

        async def run(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.hub.update_settings(
                {"default_private_reply": "off", "default_group_reply": "off"}
            )
            return result

        self.runtime.backend.run = run

        async def send(*args):
            self.fail("changed defaults must suppress in-flight replies")

        await self.runtime.engine.process(
            "p:", self.private, self.private["content"], [], send
        )
        self.hub.update_settings({"default_group_reply": "all"})
        await self.runtime.engine.process(
            "g:", self.group, self.group["content"], [], send
        )

    def test_legacy_profiles_keep_behavior_and_migration_does_not_repeat(self):
        with self.storage.Session() as session:
            session.delete(session.get(Setting, "reply_defaults_v1"))
            session.get(Persona, "user:u").mode = "draft"
            session.get(Persona, "group:g").mode = "auto"
            session.add(
                Persona(key="group:old", chat_id="old", kind="group", mode="auto")
            )
            session.add(Setting(key="group_reply_policy:old", value="at_me"))
            session.commit()
        migrated = HubStore(self.storage)
        migrated.update_settings(
            {"default_private_reply": "auto", "default_group_reply": "all"}
        )
        self.assertEqual(
            migrated.context("p", "u")["persona"]["effective_mode"], "draft"
        )
        self.assertEqual(migrated.group_reply_policy("g"), "off")
        self.assertEqual(migrated.group_reply_policy("old"), "at_me")
        migrated.update_persona("group:g", {"group_reply_policy": "inherit"})
        migrated.observe({**self.group, "chat_id": "new"})
        restarted = HubStore(self.storage)
        self.assertEqual(restarted.group_reply_policy("g"), "all")
        self.assertEqual(restarted.group_reply_policy("new"), "all")

    def test_api_validates_defaults_and_emits_effective_policy_deltas(self):
        with TestClient(create_app(self.runtime, start_bridge=False)) as client:
            first = client.get("/api/updates").json()
            response = client.patch(
                "/api/settings",
                headers={"X-Workhub": "1"},
                json={
                    "default_private_reply": "draft",
                    "default_group_reply": "at_all",
                },
            )
            self.assertEqual(response.status_code, 200)
            delta = client.get(
                "/api/updates",
                params={"since": first["revision"], "epoch": first["epoch"]},
            ).json()
            profiles = {p["key"]: p for p in delta["changes"]["personas"]["upsert"]}
            self.assertEqual(profiles["user:u"]["effective_mode"], "draft")
            self.assertEqual(
                profiles["group:g"]["effective_group_reply_policy"], "at_all"
            )
            for payload in (
                {"default_private_reply": "inherit"},
                {"default_group_reply": "draft"},
            ):
                self.assertEqual(
                    client.patch(
                        "/api/settings", headers={"X-Workhub": "1"}, json=payload
                    ).status_code,
                    400,
                )
            self.assertEqual(
                client.patch(
                    "/api/personas/user:u",
                    headers={"X-Workhub": "1"},
                    json={"mode": "inherit"},
                ).status_code,
                200,
            )
            self.assertEqual(
                client.patch(
                    "/api/personas/group:g",
                    headers={"X-Workhub": "1"},
                    json={"group_reply_policy": "inherit"},
                ).status_code,
                200,
            )
        with patch.dict(
            "os.environ",
            {
                "LARKX_DEFAULT_PRIVATE_REPLY": "off",
                "LARKX_DEFAULT_GROUP_REPLY": "at_me",
            },
        ):
            # Saved settings continue to take priority over environment defaults.
            self.assertEqual(self.hub.settings()["default_group_reply"], "at_all")
