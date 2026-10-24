import json
import hashlib
import time
import threading
from datetime import datetime
from sqlalchemy import and_, case, desc, func, insert, literal, or_, select
from sqlalchemy.orm import aliased
from ..storage import Storage
from ..storage.models import Message
from .models import Persona, Conversation, Task, Memory, Action, Setting, Job, record
from .work_items import WorkItems, ACTIVE_TASKS, VISIBLE_TASKS

from ..settings import DEFAULT_RULES as DEFAULT_RULES, DEFAULT_SUFFIX as DEFAULT_SUFFIX
from ..settings import SETTINGS, resolve_settings, validate_changes


class HubStore(WorkItems):
    def __init__(self, storage=None):
        self.storage = storage or Storage()
        self._work_items_lock = threading.RLock()
        # Models have been imported before Storage construction / metadata update.
        from ..storage.models import Base

        Base.metadata.create_all(self.storage.engine)
        if self.storage.url.startswith("sqlite:"):
            with self.storage.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        self._migrate_reply_defaults()
        self.work_item_migration = self.migrate_work_items()

    def _migrate_reply_defaults(self):
        # Old profiles did not distinguish inherited values. Keep them explicit
        # rather than silently changing who may receive automatic replies.
        with self.storage.Session() as session:
            key = "reply_defaults_v1"
            if session.get(Setting, key):
                return
            legacy_groups = (
                select(literal("group_reply_policy:") + Persona.chat_id, literal("off"))
                .where(
                    Persona.kind == "group",
                    ~select(Setting.key)
                    .where(
                        Setting.key == literal("group_reply_policy:") + Persona.chat_id
                    )
                    .exists(),
                )
                .distinct()
            )
            session.execute(
                insert(Setting).from_select(["key", "value"], legacy_groups)
            )
            session.add(Setting(key=key, value="done"))
            session.commit()

    @staticmethod
    def _profile(row, settings, policy=None, in_group=False):
        if row is None:
            return {}
        data = record(row)
        if row.kind == "group":
            policy = policy or "inherit"
            effective = (
                settings["default_group_reply"] if policy == "inherit" else policy
            )
            data.update(
                group_reply_policy=policy,
                effective_group_reply_policy=effective,
                effective_mode="off"
                if effective == "off" or row.mode == "off"
                else "draft"
                if row.mode == "draft"
                else "auto",
            )
        else:
            data["effective_mode"] = (
                ("auto" if in_group else settings["default_private_reply"])
                if row.mode == "inherit"
                else row.mode
            )
        return data

    def setting(self, key, default=""):
        with self.storage.Session() as s:
            row = s.get(Setting, key)
            return row.value if row else default

    def set_setting(self, key, value):
        with self.storage.Session() as s:
            row = s.get(Setting, key) or Setting(key=key)
            row.value = value
            s.add(row)
            s.commit()

    def settings(self):
        return self.settings_info()[0]

    def settings_info(self):
        with self.storage.Session() as session:
            overrides = dict(
                session.query(Setting.key, Setting.value)
                .filter(Setting.key.in_(SETTINGS))
                .all()
            )
        return resolve_settings(overrides)

    def update_settings(self, fields):
        # Validate and write the entire group in one transaction, including any
        # active handoff expirations affected by a duration change.
        with self.storage.Session() as session:
            rows = {
                row.key: row
                for row in session.query(Setting)
                .filter(Setting.key.in_(SETTINGS))
                .all()
            }
            current = resolve_settings({key: row.value for key, row in rows.items()})[0]
            values = validate_changes(fields, current)
            for key in fields:
                value = values[key]
                encoded = (
                    ("true" if value else "false")
                    if isinstance(value, bool)
                    else str(value)
                )
                row = rows.get(key) or Setting(key=key)
                row.value = encoded
                session.add(row)
            if "human_pause_seconds" in fields:
                seconds, now = values["human_pause_seconds"], time.time()
                for row in (
                    session.query(Setting)
                    .filter(Setting.key.startswith("human_pause:", autoescape=True))
                    .all()
                ):
                    state = json.loads(row.value)
                    if state.get("until", 0) <= now:
                        continue
                    last_activity = state.get(
                        "last_activity",
                        state["until"] - state.get("duration_seconds", 120),
                    )
                    state.update(
                        last_activity=last_activity,
                        duration_seconds=float(seconds),
                        until=last_activity + seconds,
                    )
                    row.value = json.dumps(state)
            session.commit()
        return values

    def batch_timing(self):
        values = self.settings()
        return {key: values[key] for key in ("batch_delay", "batch_max_wait")}

    def human_pause_seconds(self):
        return self.settings()["human_pause_seconds"]

    def configure_human_pause(self, seconds):
        self.update_settings({"human_pause_seconds": seconds})

    def background_interval(self):
        return self.settings()["background_interval"]

    def handoff(self, chat_id):
        return json.loads(self.setting("human_pause:" + str(chat_id), "{}"))

    def touch_handoff(self, chat_id, manual=False, activity_at=None):
        now = time.time()
        activity_at = min(now, activity_at) if activity_at is not None else now
        state = self.handoff(chat_id)
        # Other participants extend an existing human conversation, but cannot start one.
        if not manual and state.get("until", 0) <= activity_at:
            return state
        if manual:
            state["last_manual"] = max(state.get("last_manual", 0), activity_at)
            state["generation"] = state.get("generation", 0) + 1
        duration = self.human_pause_seconds()
        state["last_activity"] = max(
            state.get(
                "last_activity",
                state.get("until", 0) - state.get("duration_seconds", 120),
            ),
            activity_at,
        )
        state["duration_seconds"] = duration
        state["until"] = state["last_activity"] + duration
        self.set_setting("human_pause:" + str(chat_id), json.dumps(state))
        return state

    def human_paused(self, chat_id):
        return self.handoff(chat_id).get("until", 0) > time.time()

    def observe(self, msg, chat_name="", sender_name=""):
        chat_id = str(msg["chat_id"])
        is_group = msg.get("chat_type") in (2, 3)
        user_id = str(msg.get("from_id") or "")
        with self.storage.Session() as s:
            user_key = "user:" + user_id
            person = s.get(Persona, user_key)
            if not person:
                person = Persona(
                    key=user_key, external_id=user_id, name=sender_name or user_id
                )
                s.add(person)
            elif sender_name:
                person.name = sender_name
            if not is_group:
                person.chat_id = chat_id
            key = ("group:" + chat_id) if is_group else user_key
            if is_group and not s.get(Persona, key):
                s.add(
                    Persona(
                        key=key,
                        external_id=chat_id,
                        kind="group",
                        name=chat_name or chat_id,
                        chat_id=chat_id,
                    )
                )
            convo = s.get(Conversation, chat_id)
            if not convo:
                convo = Conversation(
                    chat_id=chat_id,
                    persona_key=key,
                    kind="group" if is_group else "private",
                )
                s.add(convo)
            convo.name = chat_name or sender_name or convo.name or chat_id
            convo.updated_at = datetime.now()
            s.commit()

    def seed_existing(self):
        """Hydrate metadata from new history, in one transaction, without replay."""
        with self.storage.Session() as s:
            progress = s.get(Setting, "seed_last_message_id")
            try:
                after = max(0, int(progress.value)) if progress else 0
            except (TypeError, ValueError):
                after = 0
            upper = (
                s.query(func.max(Message.id)).filter(Message.direction == "in").scalar()
            )
            if upper is None or upper == after:
                return
            if after > upper:
                after = 0  # Handle a restored database with an older history.
            interval = (
                Message.direction == "in",
                Message.id > after,
                Message.id <= upper,
            )
            # Keep the latest nonempty name and latest private chat separately:
            # a user's newest message may be an unnamed message in a group.
            users = (
                s.query(
                    Message.sender_id,
                    func.max(case((Message.sender_name != "", Message.id))).label(
                        "name_id"
                    ),
                    func.max(
                        case(
                            (
                                Message.chat_type.notin_((2, 3))
                                | Message.chat_type.is_(None),
                                Message.id,
                            )
                        )
                    ).label("private_id"),
                )
                .filter(*interval)
                .group_by(Message.sender_id)
                .subquery()
            )
            named_user, private_message = aliased(Message), aliased(Message)
            user_rows = (
                s.query(
                    users.c.sender_id, named_user.sender_name, private_message.chat_id
                )
                .select_from(users)
                .outerjoin(named_user, named_user.id == users.c.name_id)
                .outerjoin(private_message, private_message.id == users.c.private_id)
                .all()
            )
            chats = (
                s.query(
                    Message.chat_id,
                    func.max(Message.id).label("last_id"),
                    func.max(case((Message.chat_name != "", Message.id))).label(
                        "name_id"
                    ),
                )
                .filter(*interval)
                .group_by(Message.chat_id)
                .subquery()
            )
            last_message, named_chat = aliased(Message), aliased(Message)
            chat_rows = (
                s.query(
                    chats.c.chat_id,
                    last_message.chat_type,
                    last_message.sender_id,
                    last_message.sender_name,
                    last_message.stored_at,
                    named_chat.chat_name,
                )
                .select_from(chats)
                .join(last_message, last_message.id == chats.c.last_id)
                .outerjoin(named_chat, named_chat.id == chats.c.name_id)
                .all()
            )
            profiles = {p.key: p for p in s.query(Persona).all()}
            conversations = {c.chat_id: c for c in s.query(Conversation).all()}
            for sender_id, sender_name, private_chat_id in user_rows:
                user_id = str(sender_id or "")
                key = "user:" + user_id
                person = profiles.get(key)
                if person is None:
                    person = Persona(
                        key=key, external_id=user_id, name=sender_name or user_id
                    )
                    profiles[key] = person
                    s.add(person)
                elif sender_name:
                    person.name = sender_name
                if private_chat_id:
                    person.chat_id = private_chat_id
            for (
                chat_id,
                chat_type,
                sender_id,
                sender_name,
                stored_at,
                chat_name,
            ) in chat_rows:
                is_group = chat_type in (2, 3)
                key = "group:" + chat_id if is_group else "user:" + str(sender_id or "")
                if is_group:
                    group = profiles.get(key)
                    if group is None:
                        group = Persona(
                            key=key,
                            external_id=chat_id,
                            kind="group",
                            name=chat_name or chat_id,
                            chat_id=chat_id,
                        )
                        profiles[key] = group
                        s.add(group)
                    elif chat_name:
                        group.name = chat_name
                conversation = conversations.get(chat_id)
                if conversation is None:
                    conversation = Conversation(
                        chat_id=chat_id,
                        persona_key=key,
                        kind="group" if is_group else "private",
                    )
                    conversations[chat_id] = conversation
                    s.add(conversation)
                conversation.name = (
                    chat_name
                    or conversation.name
                    or (
                        profiles[key].name
                        if is_group
                        else sender_name or profiles[key].name
                    )
                    or chat_id
                )
                activity_at = stored_at or datetime.now()
                if (
                    conversation.updated_at is None
                    or activity_at > conversation.updated_at
                ):
                    conversation.updated_at = activity_at
            if progress is None:
                progress = Setting(key="seed_last_message_id")
                s.add(progress)
            progress.value = str(upper)
            s.commit()  # Metadata and progress succeed or roll back together.

    def conversations(self, chat_ids=None):
        settings = self.settings()
        with self.storage.Session() as s:
            latest = (
                select(Message.content)
                .where(Message.chat_id == Conversation.chat_id)
                .order_by(desc(Message.id))
                .limit(1)
                .correlate(Conversation)
                .scalar_subquery()
            )
            counts = (
                s.query(Task.chat_id, func.count(Task.id).label("open_tasks"))
                .filter(Task.status.in_(ACTIVE_TASKS))
                .group_by(Task.chat_id)
                .subquery()
            )
            pause, policy = aliased(Setting), aliased(Setting)
            statement = (
                s.query(
                    Conversation,
                    Persona,
                    latest,
                    func.coalesce(counts.c.open_tasks, 0),
                    pause.value,
                    policy.value,
                )
                .outerjoin(Persona, Persona.key == Conversation.persona_key)
                .outerjoin(counts, counts.c.chat_id == Conversation.chat_id)
                .outerjoin(
                    pause, pause.key == literal("human_pause:") + Conversation.chat_id
                )
                .outerjoin(
                    policy,
                    policy.key == literal("group_reply_policy:") + Conversation.chat_id,
                )
                .order_by(desc(Conversation.updated_at), Conversation.chat_id)
            )
            if chat_ids is not None:
                statement = statement.filter(Conversation.chat_id.in_(chat_ids))
            rows = statement.all()
            result = []
            for row, p, last_content, open_tasks, pause_value, policy_value in rows:
                data = record(row)
                data["persona"] = (
                    self._profile(p, settings, policy_value) if p else None
                )
                data["human_pause_until"] = json.loads(pause_value or "{}").get(
                    "until", 0
                )
                data["last_message"] = (last_content or "")[:120]
                data["open_tasks"] = open_tasks
                result.append(data)
            return result

    def personas(self, keys=None):
        settings = self.settings()
        with self.storage.Session() as s:
            policy = aliased(Setting)
            statement = (
                s.query(Persona, policy.value)
                .outerjoin(
                    policy,
                    policy.key == literal("group_reply_policy:") + Persona.chat_id,
                )
                .order_by(Persona.name, Persona.key)
            )
            if keys is not None:
                statement = statement.filter(Persona.key.in_(keys))
            rows = statement.all()
            result = []
            for row, policy_value in rows:
                data = self._profile(row, settings, policy_value)
                result.append(data)
            return result

    def group_reply_policy(self, chat_id):
        policy = self.setting("group_reply_policy:" + str(chat_id), "inherit")
        return self.settings()["default_group_reply"] if policy == "inherit" else policy

    def user_profiles(self, user_ids, in_group=False):
        keys = ["user:" + str(user_id) for user_id in user_ids if user_id]
        settings = self.settings()
        with self.storage.Session() as session:
            return {
                row.external_id: self._profile(row, settings, in_group=in_group)
                for row in session.query(Persona).filter(Persona.key.in_(keys)).all()
            }

    def update_persona(self, key, fields):
        with self.storage.Session() as s:
            row = s.get(Persona, key)
            if not row:
                raise ValueError("角色不存在")
            if "group_reply_policy" in fields:
                policy = fields["group_reply_policy"]
                if row.kind != "group" or policy not in (
                    "off",
                    "all",
                    "at_me",
                    "at_all",
                    "inherit",
                ):
                    raise ValueError("群聊回复选项无效")
                policy_key = "group_reply_policy:" + str(row.chat_id)
                setting = s.get(Setting, policy_key)
                if policy == "inherit":
                    if setting:
                        s.delete(setting)
                else:
                    setting = setting or Setting(key=policy_key)
                    setting.value = policy
                    s.add(setting)
            for field in ("role", "description", "reply_style", "mode"):
                if field in fields:
                    setattr(row, field, fields[field])
            if row.mode not in ("inherit", "auto", "draft", "off"):
                raise ValueError("回复模式无效")
            if "group_reply_policy" in fields:
                row.mode = (
                    "inherit"
                    if policy == "inherit"
                    else "off"
                    if policy == "off"
                    else "auto"
                )
            row.updated_at = datetime.now()
            s.commit()
            return self._profile(
                row,
                self.settings(),
                self.setting("group_reply_policy:" + str(row.chat_id), "inherit")
                if row.kind == "group"
                else None,
            )

    def reply_context(self, msg, limit=None):
        """Actual messages from the same chat/thread, including non-triggering ones."""
        limit = self.settings()["recent_message_limit"] if limit is None else limit
        with self.storage.Session() as session:
            current = (
                session.query(Message)
                .filter_by(
                    chat_id=str(msg["chat_id"]), msg_id=str(msg.get("msg_id") or "")
                )
                .first()
            )
            rows = session.query(Message).filter_by(
                chat_id=str(msg["chat_id"]), anchor=msg.get("anchor") or ""
            )
            if current:
                rows = rows.filter(Message.id <= current.id)
            result = []
            for row in reversed(rows.order_by(desc(Message.id)).limit(limit).all()):
                data = record(row)
                data["content"] = (row.content or "")[:1500]
                data["from_id"] = row.sender_id
                data["_sender_name"] = row.sender_name
                data["_chat_name"] = row.chat_name
                data["content_data"] = json.loads(row.content_data or "{}")
                result.append(data)
            return result

    def context(self, chat_id, sender_id="", anchor=None, settings=None):
        settings = settings if settings is not None else self.settings()
        with self.storage.Session() as s:
            c = s.get(Conversation, str(chat_id))
            p = s.get(Persona, c.persona_key) if c else None
            sender = s.get(Persona, "user:" + str(sender_id)) if sender_id else None
            policy = (
                s.get(Setting, "group_reply_policy:" + str(chat_id))
                if p and p.kind == "group"
                else None
            )
            memories = (
                s.query(Memory)
                .filter_by(chat_id=str(chat_id))
                .order_by(desc(Memory.id))
                .limit(settings["memory_limit"])
                .all()
            )
            tasks = (
                s.query(Task)
                .filter(Task.chat_id == str(chat_id), Task.status.in_(ACTIVE_TASKS))
                .filter(Task.anchor == anchor if anchor is not None else True)
                .order_by(Task.id)
                .limit(settings["task_context_limit"])
                .all()
            )
            recent_query = s.query(Message).filter_by(chat_id=str(chat_id))
            if anchor is not None:
                recent_query = recent_query.filter_by(anchor=anchor)
            recent = (
                recent_query.order_by(desc(Message.id))
                .limit(settings["recent_message_limit"])
                .all()
            )
            return {
                "overview": c.overview if c else "",
                "persona": self._profile(p, settings, policy.value if policy else None),
                "sender": self._profile(
                    sender, settings, in_group=bool(c and c.kind == "group")
                ),
                "memories": [record(m) for m in memories],
                "tasks": [record(t) for t in tasks],
                "recent_messages": [
                    {
                        "sender": r.sender_name,
                        "sender_id": r.sender_id,
                        "msg_id": r.msg_id,
                        "time": r.create_time,
                        "anchor": r.anchor,
                        "direction": r.direction,
                        "content": r.content[:1500],
                    }
                    for r in reversed(recent)
                ],
            }

    def is_tracked_send(self, message_id, chat_id="", content=""):
        with self.storage.Session() as s:
            if (
                s.query(Action)
                .filter_by(message_id=str(message_id), status="sent")
                .first()
            ):
                return True
            return bool(
                content
                and s.query(Action)
                .filter_by(status="pending", chat_id=str(chat_id), content=content)
                .first()
            )

    def messages(self, chat_id, limit=80):
        return [record(r) for r in self.storage.get_messages(chat_id, limit=limit)]

    def find_conversations(self, query="", kind="all", limit=30, offset=0):
        query = query.strip().casefold()
        # MCP needs names/IDs, not every persona, task count and handoff setting.
        # Fetch lightweight rows together rather than reusing the UI's N+1 queries.
        with self.storage.Session() as session:
            latest = (
                select(Message.content)
                .where(Message.chat_id == Conversation.chat_id)
                .order_by(desc(Message.id))
                .limit(1)
                .scalar_subquery()
            )
            statement = session.query(
                Conversation.chat_id,
                Conversation.name,
                Conversation.kind,
                Persona.name.label("person_name"),
                Persona.external_id,
                latest.label("last_message"),
            ).outerjoin(Persona, Conversation.persona_key == Persona.key)
            if kind != "all":
                statement = statement.filter(Conversation.kind == kind)
            rows = [
                dict(r._mapping)
                for r in statement.order_by(
                    desc(Conversation.updated_at), Conversation.chat_id
                ).all()
            ]
        if query:
            rows = [
                c
                for c in rows
                if any(
                    query in str(v or "").casefold()
                    for v in (
                        c["name"],
                        c["chat_id"],
                        c["person_name"],
                        c["external_id"],
                    )
                )
            ]
        selected = rows[offset : offset + limit]
        for row in selected:
            row["last_message"] = (row["last_message"] or "")[:120]
        return {
            "conversations": [
                {k: c[k] for k in ("chat_id", "name", "kind", "last_message")}
                for c in selected
            ],
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "total": len(rows),
        }

    def find_messages(
        self, chat_id="", query="", limit=50, before_id=None, chronological=False
    ):
        from ..media import display_parts

        with self.storage.Session() as session:
            if chat_id and not session.get(Conversation, chat_id):
                raise ValueError("会话不存在，请先搜索会话")
            rows = session.query(Message)
            if chat_id:
                rows = rows.filter(Message.chat_id == chat_id)
            if query:
                rows = rows.filter(Message.content.contains(query, autoescape=True))
            if before_id is not None:
                if chronological:
                    anchor = session.get(Message, before_id)
                    if not anchor or anchor.chat_id != chat_id:
                        raise ValueError("历史消息位置无效")
                    rows = rows.filter(
                        or_(
                            Message.create_time < anchor.create_time,
                            and_(
                                Message.create_time == anchor.create_time,
                                Message.id < anchor.id,
                            ),
                        )
                    )
                else:
                    rows = rows.filter(Message.id < before_id)
            order = (
                (desc(Message.create_time), desc(Message.id))
                if chronological
                else (desc(Message.id),)
            )
            found = rows.order_by(*order).limit(limit + 1).all()
            selected = found[:limit]
            messages = []
            for row in selected:
                data = {
                    k: getattr(row, k)
                    for k in (
                        "id",
                        "msg_id",
                        "chat_id",
                        "chat_name",
                        "sender_id",
                        "sender_name",
                        "direction",
                        "content",
                        "create_time",
                        "msg_type",
                        "anchor",
                    )
                }
                data["display_parts"] = display_parts(
                    row.msg_type, json.loads(row.content_data or "{}")
                )
                messages.append(data)
            return {
                "messages": messages,
                "next_before_id": selected[-1].id if len(found) > limit else None,
                "source": "local_synced_history",
            }

    def memories(self, chat_id):
        with self.storage.Session() as s:
            return [
                record(r)
                for r in s.query(Memory)
                .filter_by(chat_id=chat_id)
                .order_by(desc(Memory.id))
                .limit(100)
                .all()
            ]

    def forget_memory(self, memory_id):
        with self.storage.Session() as s:
            r = s.get(Memory, memory_id)
            if r:
                s.delete(r)
                s.commit()

    def analysis_version(self, msg):
        ids = {str(m.get("msg_id") or "") for m in msg.get("_batch") or [msg]} - {""}
        if not ids:
            return 0
        with self.storage.Session() as s:
            return (
                s.query(func.max(Message.id))
                .filter(Message.chat_id == str(msg["chat_id"]), Message.msg_id.in_(ids))
                .scalar()
                or 0
            )

    def apply_analysis(self, msg, analysis, allowed_task_ids=None, *, version=None):
        chat_id = str(msg["chat_id"])
        anchor = msg.get("anchor") or ""
        sources = [str(m.get("msg_id") or "") for m in msg.get("_batch") or [msg]]
        marker = (
            "analysis:"
            + hashlib.sha256(
                json.dumps([chat_id, anchor, sources]).encode()
            ).hexdigest()
        )
        if version is None:
            version = self.analysis_version(msg)
        with self.storage.Session() as s:
            key = "analysis_version:" + chat_id
            checkpoint = s.get(Setting, key)
            try:
                applied = int(checkpoint.value) if checkpoint else 0
            except (TypeError, ValueError):
                applied = 0
            # Replayed events must not recreate tasks, even after manual completion.
            if all(sources) and s.get(Setting, marker):
                return

            def existing_task(task_id):
                if (
                    not isinstance(task_id, int)
                    or isinstance(task_id, bool)
                    or task_id < 0
                ):
                    raise ValueError("AI 返回了无效的任务编号")
                if task_id == 0:
                    return None
                if allowed_task_ids is not None and task_id not in allowed_task_ids:
                    raise ValueError("AI 关联的任务不在本次上下文中")
                row = s.get(Task, task_id)
                if not row or row.chat_id != chat_id or (row.anchor or "") != anchor:
                    raise ValueError("AI 关联的任务不属于当前会话/话题")
                if row.status == "archived":
                    raise ValueError("AI 不能更新已归档记录")
                return row

            attention_id = analysis.get("attention_task_id", 0)
            attention_task = existing_task(attention_id)
            convo = s.get(Conversation, chat_id)
            # Priority can put a newer reply ahead of an older background batch.
            # Older input may add facts/tasks, but cannot replace a newer overview.
            if convo and analysis.get("overview") and version >= applied:
                convo.overview = analysis["overview"][:6000]
            if version > applied:
                checkpoint = checkpoint or Setting(key=key)
                checkpoint.value = str(version)
                s.add(checkpoint)
            for fact in analysis.get("facts", [])[:10]:
                fact = fact.strip()[:2000]
                if (
                    fact
                    and not s.query(Memory)
                    .filter_by(chat_id=chat_id, content=fact)
                    .first()
                ):
                    s.add(
                        Memory(
                            chat_id=chat_id,
                            content=fact,
                            source_msg_id=str(msg.get("msg_id") or ""),
                        )
                    )
            tasks = [dict(task) for task in analysis.get("tasks", [])[:10]]
            if analysis.get("needs_attention"):
                if attention_task is not None:
                    # A human may have completed it during generation; never reopen it.
                    if attention_task.status in ACTIVE_TASKS:
                        if attention_task.status == "open":
                            attention_task.status = "needs_attention"
                        tasks.append(
                            {
                                "task_id": attention_id,
                                "details": "需要你确认："
                                + (analysis.get("attention_reason") or ""),
                            }
                        )
                elif tasks:
                    tasks[0]["status"] = "needs_attention"
                    tasks[0]["draft_reply"] = analysis.get("reply", "")
                    tasks[0]["details"] = (
                        (tasks[0].get("details") or "")
                        + "\n需要你确认："
                        + (analysis.get("attention_reason") or "")
                    )
            for task in tasks:
                row = existing_task(task.get("task_id", 0))
                title = str(task.get("title") or "").strip()[:256]
                if row is None and title and "task_id" not in task:
                    row = (
                        s.query(Task)
                        .filter(
                            Task.chat_id == chat_id,
                            Task.anchor == anchor,
                            Task.title == title,
                            Task.status.in_(ACTIVE_TASKS),
                        )
                        .first()
                    )
                if row is not None:
                    if row.status not in ACTIVE_TASKS:
                        continue
                    # Preserve owner-edited title/status/dates/draft; append evidence only.
                    detail = str(task.get("details") or "").strip()
                    if detail and detail not in (row.details or ""):
                        addition = (
                            "\n\n补充（消息 " + ", ".join(sources) + "）：\n" + detail
                        )
                        row.details = ((row.details or "") + addition)[:6000]
                    row.updated_at = datetime.now()
                    continue
                if not title:
                    continue
                s.add(
                    Task(
                        title=title,
                        details=str(task.get("details") or "")[:6000],
                        chat_id=chat_id,
                        anchor=anchor,
                        origin_msg_id=str(msg.get("msg_id") or ""),
                        priority=task.get("priority")
                        if task.get("priority") in ("low", "normal", "high")
                        else "normal",
                        due_at=str(task.get("due_at") or "")[:80],
                        status=task.get("status") or "open",
                        draft_reply=task.get("draft_reply") or "",
                    )
                )
                # Make new rows visible to later entries in this same analysis.
                s.flush()
            if all(sources):
                s.add(Setting(key=marker, value="processed"))
            s.commit()

    def tasks(self):
        completed_limit = self.settings()["completed_task_limit"]
        with self.storage.Session() as s:
            order = (desc(Task.created_at), desc(Task.id))
            unfinished = (
                s.query(Task)
                .filter(Task.status.in_(ACTIVE_TASKS))
                .order_by(*order)
                .all()
            )
            completed = (
                s.query(Task)
                .filter(Task.status == "done")
                .order_by(*order)
                .limit(completed_limit)
                .all()
            )
            return [record(r) for r in unfinished + completed]

    def task(self, task_id):
        # Action endpoints must not depend on the UI's bounded completed history.
        with self.storage.Session() as s:
            row = s.get(Task, task_id)
            return record(row) if row and row.status in VISIBLE_TASKS else None

    def create_task(self, fields):
        with self.storage.Session() as s:
            row = Task(
                **{
                    k: v
                    for k, v in fields.items()
                    if k
                    in (
                        "title",
                        "details",
                        "chat_id",
                        "anchor",
                        "priority",
                        "due_at",
                        "draft_reply",
                        "status",
                    )
                }
            )
            if (
                not isinstance(row.title, str)
                or not row.title.strip()
                or len(row.title) > 256
            ):
                raise ValueError("任务标题不能为空，最多 256 字符")
            row.status = row.status or "open"
            row.priority = row.priority or "normal"
            if row.status not in (
                "open",
                "in_progress",
                "done",
                "needs_attention",
            ) or row.priority not in ("low", "normal", "high"):
                raise ValueError("任务状态或优先级无效")
            if row.chat_id and not s.get(Conversation, str(row.chat_id)):
                raise ValueError("关联会话不存在")
            s.add(row)
            s.commit()
            return record(row)

    def update_task(self, task_id, fields):
        with self.storage.Session() as s:
            row = s.get(Task, task_id)
            if not row:
                raise ValueError("任务不存在")
            if row.status == "archived":
                raise ValueError("记录已归档，请在执行记录查看")
            for field in (
                "title",
                "details",
                "status",
                "priority",
                "due_at",
                "draft_reply",
            ):
                if field in fields:
                    setattr(row, field, fields[field])
            if row.priority not in ("low", "normal", "high"):
                raise ValueError("任务优先级无效")
            if row.status not in ("open", "in_progress", "done", "needs_attention"):
                raise ValueError("任务状态无效")
            row.updated_at = datetime.now()
            s.commit()
            return record(row)

    def claim_send(self, request_id, chat_id, content, anchor="", target=None):
        key = "send_target:" + hashlib.sha256(request_id.encode()).hexdigest()
        target = target or {}
        with self.storage.Session() as s:
            existing = s.query(Action).filter_by(request_id=request_id).first()
            if existing:
                stored = s.get(Setting, key)
                if (json.loads(stored.value) if stored else {}) != target:
                    raise ValueError("该请求 ID 已用于不同的回复对象")
                if (existing.chat_id, existing.content, existing.anchor) != (
                    str(chat_id),
                    content,
                    anchor,
                ):
                    raise ValueError("该请求 ID 已用于其他消息")
                return record(existing), False
            row = Action(
                request_id=request_id, chat_id=chat_id, content=content, anchor=anchor
            )
            s.add(row)
            if target:
                s.add(Setting(key=key, value=json.dumps(target)))
            s.commit()
            return record(row), True

    def finish_action(self, action_id, status, message_id="", error=""):
        with self.storage.Session() as s:
            row = s.get(Action, action_id)
            row.status, row.message_id, row.error = status, message_id, error
            s.commit()
            return record(row)

    def actions(self):
        limit = self.settings()["actions_limit"]
        with self.storage.Session() as s:
            return [
                record(r)
                for r in s.query(Action).order_by(desc(Action.id)).limit(limit).all()
            ]

    def new_job(self, job_id, kind, text):
        with self.storage.Session() as s:
            s.add(Job(id=job_id, kind=kind, input=text))
            s.commit()

    def job(self, job_id):
        with self.storage.Session() as s:
            row = s.get(Job, job_id)
            return record(row) if row else None

    def control_history(self):
        with self.storage.Session() as s:
            rows = (
                s.query(Job)
                .filter_by(kind="control")
                .order_by(desc(Job.created_at))
                .limit(12)
                .all()
            )
            return [record(r) for r in reversed(rows)]

    def update_job(self, job_id, status, result=None, error=""):
        with self.storage.Session() as s:
            row = s.get(Job, job_id)
            row.status, row.error = status, error
            if result is not None:
                row.result = json.dumps(result, ensure_ascii=False)
            s.commit()

    def recover_interrupted(self):
        with self.storage.Session() as s:
            for row in s.query(Action).filter_by(status="pending").all():
                row.status, row.error = (
                    "uncertain",
                    "上次运行中断，发送结果待核对，不自动重发",
                )
            for row in s.query(Job).filter(Job.status.in_(["queued", "running"])).all():
                row.status, row.error = "failed", "上次运行中断，请重新提交指令"
            s.commit()
