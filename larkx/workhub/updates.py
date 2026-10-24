"""Cached snapshots and bounded record deltas for workbench polling."""

import asyncio
import copy
import time
import uuid
from collections import OrderedDict

from sqlalchemy import event


class Snapshot:
    def __init__(self, keys):
        self.keys = keys
        self.epoch = uuid.uuid4().hex
        self.revision = 0
        self.minimum = 0
        self.data = {}
        self.versions = {}
        self.removed = {}
        self.order_versions = {}
        self.scalar_versions = {}

    def update(self, data):
        if data == self.data:
            return
        self.revision += 1
        version = self.revision
        for name, key in self.keys.items():
            if data[name] is self.data.get(name):
                continue
            old = {row[key]: row for row in self.data.get(name, [])}
            new = {row[key]: row for row in data[name]}
            versions = self.versions.setdefault(name, {})
            removed = self.removed.setdefault(name, {})
            for identity, row in new.items():
                if old.get(identity) != row:
                    versions[identity] = version
                removed.pop(identity, None)
            for identity in old.keys() - new.keys():
                removed[identity] = version
                versions.pop(identity, None)
            if list(old) != list(new):
                self.order_versions[name] = version
            if len(removed) > 2000:
                cutoff = sorted(removed.values())[-2000]
                self.minimum = max(self.minimum, cutoff)
                self.removed[name] = {k: v for k, v in removed.items() if v > cutoff}
        for name, value in data.items():
            if name in self.keys:
                continue
            old = self.data.get(name, {})
            self.scalar_versions.setdefault(name, {}).update(
                {
                    key: version
                    for key in old.keys() | value.keys()
                    if old.get(key) != value.get(key)
                }
            )
        self.data = {
            name: self.data[name]
            if value == self.data.get(name)
            else copy.deepcopy(value)
            for name, value in data.items()
        }

    def delta(self, since=0, epoch=""):
        reset = epoch != self.epoch or since < self.minimum or since > self.revision
        changes = {}
        if not reset and since == self.revision:
            return {
                "epoch": self.epoch,
                "revision": self.revision,
                "reset": False,
                "changes": changes,
            }
        for name, key in self.keys.items():
            rows = self.data.get(name, [])
            upsert = [
                row
                for row in rows
                if reset or self.versions.get(name, {}).get(row[key], 0) > since
            ]
            removed = (
                [
                    identity
                    for identity, version in self.removed.get(name, {}).items()
                    if version > since
                ]
                if not reset
                else []
            )
            change = {"upsert": upsert, "removed": removed}
            if reset or self.order_versions.get(name, 0) > since:
                change["order"] = [row[key] for row in rows]
            if upsert or removed or "order" in change:
                changes[name] = change
        for name, value in self.data.items():
            if name not in self.keys:
                patch = {
                    key: item
                    for key, item in value.items()
                    if reset or self.scalar_versions[name].get(key, 0) > since
                }
                if patch or reset:
                    changes[name] = patch
        return {
            "epoch": self.epoch,
            "revision": self.revision,
            "reset": reset,
            "changes": changes,
        }


class StateUpdates:
    def __init__(self, runtime):
        self.runtime = runtime
        self.engine = runtime.storage.engine
        self.generation = 0
        # Same-engine commits include in-memory SQLite, where data_version alone
        # cannot observe writes made on the same DBAPI connection.
        self.on_commit = lambda connection: self._committed()
        event.listen(self.engine, "commit", self.on_commit)
        self.sqlite = self.engine.dialect.name == "sqlite"
        self.connection = (
            self.engine.connect()
            if self.sqlite and self.engine.url.database not in (None, "", ":memory:")
            else None
        )
        self.root = Snapshot(
            {
                "tasks": "id",
                "conversations": "chat_id",
                "personas": "key",
                "actions": "id",
            }
        )
        self.marker = None
        self.web = Snapshot(
            {
                "tasks": "id",
                "conversations": "chat_id",
                "personas": "key",
                "actions": "id",
            }
        )
        self.web_marker = None
        self.list_revision = 0
        self.chats = OrderedDict()
        self.chat_lock = asyncio.Lock()

    def _committed(self):
        self.generation += 1

    def database_version(self):
        if self.connection is not None:
            value = self.connection.exec_driver_sql("PRAGMA data_version").scalar()
            self.connection.rollback()  # Do not pin a read transaction.
            return self.generation, value
        # Other databases still use record deltas, with a short cache lifetime.
        return self.generation, 0 if self.sqlite else int(time.monotonic())

    def state(self):
        marker = self.database_version()
        if marker != self.marker:
            r = self.runtime
            data = {
                "status": r.status(),
                "tasks": r.hub.tasks(),
                "conversations": r.hub.conversations(),
                "personas": r.hub.personas(),
                "actions": r.hub.actions(),
            }
            self.marker = marker  # A commit during reads is rechecked next time.
        else:
            data = {
                **self.root.data,
                "status": {**self.root.data["status"], **self.runtime.live_status()},
            }
        self.root.update(data)
        return self.root

    def web_state(self):
        from .listing import page, totals

        marker = self.database_version()
        if marker != self.web_marker:
            self.list_revision += 1
            data = {
                "status": {
                    **self.runtime.status(),
                    "list_revision": self.list_revision,
                    "totals": totals(self.runtime.hub),
                },
                "tasks": [],
                "personas": [],
                "actions": [],
                "conversations": page(self.runtime.hub, "conversations", size=4)[
                    "items"
                ],
            }
            self.web_marker = marker
        else:
            data = {
                **self.web.data,
                "status": {**self.web.data["status"], **self.runtime.live_status()},
            }
        self.web.update(data)
        return self.web

    async def conversation(self, chat_id, build):
        # Backfill awaits network IO; serialize snapshots so an older response
        # cannot overwrite a newer snapshot. Cache at most eight open chats.
        async with self.chat_lock:
            marker = self.database_version()
            cached = self.chats.pop(chat_id, None)
            if cached is None:
                snapshot = Snapshot({"messages": "id", "memories": "id"})
                old_marker = None
            else:
                snapshot, old_marker = cached
            if marker != old_marker:
                snapshot.update(await build())
            self.chats[chat_id] = snapshot, marker
            limit = self.runtime.hub.settings()["chat_cache_limit"]
            while len(self.chats) > limit:
                self.chats.popitem(last=False)
            return snapshot

    def close(self):
        event.remove(self.engine, "commit", self.on_commit)
        if self.connection is not None:
            self.connection.close()
