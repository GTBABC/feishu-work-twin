import unittest

from larkx.agent.dispatcher import AgentDispatcher
from larkx.proto.decoders import mention_targets
from larkx.proto.gateway import resolve_type
from larkx.workhub.store import HubStore
from tests import test_message_context


class GroupPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        test_message_context.MessageContextTests.setUp(self)
        self.dispatcher = AgentDispatcher(
            self.runtime.backend, None, workflow=self.runtime.engine
        )
        self.dispatcher.cfg["triggers"] = {"group_at_only": True}
        self.msg = {
            "chat_id": "g",
            "chat_type": 2,
            "from_id": "li",
            "content": "进展？",
        }

    def test_new_groups_inherit_default_off(self):
        self.assertFalse(
            self.dispatcher.should_handle(dict(self.msg, at_me=True, at_all=True), "me")
        )
        group = next(p for p in self.runtime.hub.personas() if p["kind"] == "group")
        self.assertEqual(group["group_reply_policy"], "inherit")
        self.assertEqual(group["effective_group_reply_policy"], "off")

    def test_policy_matrix_and_private_unchanged(self):
        cases = [
            ({}, {"all"}),
            ({"at_me": True}, {"all", "at_me"}),
            ({"at_me": True, "at_all": True}, {"all", "at_all"}),
            (
                {"at_me": True, "at_all": True, "at_user_ids": ["me"]},
                {"all", "at_me", "at_all"},
            ),
        ]
        for flags, allowed in cases:
            for policy in ("off", "all", "at_me", "at_all"):
                self.runtime.hub.update_persona(
                    "group:g", {"group_reply_policy": policy}
                )
                with self.subTest(flags=flags, policy=policy):
                    self.assertEqual(
                        self.dispatcher.should_handle(dict(self.msg, **flags), "me"),
                        policy in allowed,
                    )
        self.assertTrue(
            self.dispatcher.should_handle(dict(self.msg, chat_type=1), "me")
        )
        self.assertFalse(
            self.dispatcher.should_handle(dict(self.msg, from_id="me"), "me")
        )

    def test_policy_persists_and_reaches_both_ui_views(self):
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "at_all"})
        store = HubStore(self.storage)
        self.assertEqual(store.group_reply_policy("g"), "at_all")
        group = next(p for p in store.personas() if p["kind"] == "group")
        self.assertEqual(group["group_reply_policy"], "at_all")
        self.assertEqual(
            store.conversations()[0]["persona"]["group_reply_policy"], "at_all"
        )
        with self.assertRaises(ValueError):
            store.update_persona("group:g", {"group_reply_policy": "invalid"})
        self.assertEqual(store.group_reply_policy("g"), "at_all")

    async def test_switching_off_during_generation_suppresses_reply(self):
        original = self.runtime.backend.run

        async def run(*args, **kwargs):
            result = await original(*args, **kwargs)
            self.runtime.hub.update_persona("group:g", {"group_reply_policy": "off"})
            return result

        self.runtime.backend.run = run
        self.runtime.hub.update_persona("group:g", {"group_reply_policy": "all"})
        sent = []

        async def reply(*args):
            sent.append(args)

        await self.runtime.engine.process("g:", self.msg, "进展？", [], reply)
        self.assertFalse(sent)

    def test_at_all_requires_real_mention_node(self):
        self.assertEqual(mention_targets({"text": "@全体成员"}), ([], False))
        prop = resolve_type("entities.RichTextElement.AtProperty")()
        prop.userId = "all"
        prop.content = "@全体成员"
        data = {
            "richText": {
                "elements": {
                    "dictionary": {
                        "0": {"tag": 5, "property": prop.SerializeToString()}
                    }
                }
            }
        }
        self.assertEqual(mention_targets(data), (["all"], True))
        prop.userId = "me"
        prop.content = "@陈进"
        data["richText"]["elements"]["dictionary"]["0"]["property"] = (
            prop.SerializeToString()
        )
        self.assertEqual(mention_targets(data), (["me"], False))
