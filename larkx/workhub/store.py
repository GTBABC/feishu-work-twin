import json
import math
import os
import hashlib
import time
from datetime import datetime
from sqlalchemy import desc, select
from ..storage import Storage
from ..storage.models import Message
from .models import Persona, Conversation, Task, Memory, Action, Setting, Job

DEFAULT_RULES = (
    "你是我的工作数字人，以我的身份和同事沟通。回复自然、简短、准确。"
    "尽量自动处理沟通，明确请求提取为待办。不能假装已经执行实际工作；"
    "不能编造排期、承诺、文件内容或他人决定。需要我决策、授权或资料时生成异常待办。"
    "只记录工作事实、需求、决定和偏好，不记录普通寒暄、表情或辱骂为长期记忆。"
)
DEFAULT_SUFFIX = "reply by Bot"


def record(row):
    return {
        c.name: (
            getattr(row, c.name).isoformat()
            if isinstance(getattr(row, c.name), datetime)
            else getattr(row, c.name)
        )
        for c in row.__table__.columns
    }


class HubStore:
    def __init__(self, storage=None):
        self.storage = storage or Storage()
        # Models have been imported before Storage construction / metadata update.
        from ..storage.models import Base

        Base.metadata.create_all(self.storage.engine)
        if self.storage.url.startswith("sqlite:"):
            with self.storage.engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")

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

    def batch_timing(self):
        def number(key, env, default, ceiling):
            try:
                value = float(self.setting(key, os.environ.get(env, str(default))))
                if not math.isfinite(value):
                    return default
                return max(0.01, min(value, ceiling))
            except (TypeError, ValueError):
                return default

        delay = number("batch_delay", "LARKX_BATCH_DELAY", 1.5, 30)
        maximum = number("batch_max_wait", "LARKX_BATCH_MAX_WAIT", 5, 60)
        return {"batch_delay": delay, "batch_max_wait": max(delay, maximum)}

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
        state["until"] = max(state.get("until", 0), activity_at + 120)
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
        with self.storage.Session() as s:
            rows = (
                s.query(Message)
                .filter(Message.direction == "in")
                .order_by(Message.id)
                .all()
            )
            records = [
                (r.chat_id, r.chat_type, r.sender_id, r.chat_name, r.sender_name)
                for r in rows
            ]
        for chat_id, chat_type, sender_id, chat_name, sender_name in records:
            self.observe(
                {"chat_id": chat_id, "chat_type": chat_type, "from_id": sender_id},
                chat_name,
                sender_name,
            )

    def conversations(self):
        with self.storage.Session() as s:
            rows = s.query(Conversation).order_by(desc(Conversation.updated_at)).all()
            result = []
            for row in rows:
                data = record(row)
                p = s.get(Persona, row.persona_key)
                data["persona"] = record(p) if p else None
                if p and p.kind == "group":
                    data["persona"]["group_reply_policy"] = self.group_reply_policy(
                        row.chat_id
                    )
                data["human_pause_until"] = self.handoff(row.chat_id).get("until", 0)
                last = (
                    s.query(Message)
                    .filter_by(chat_id=row.chat_id)
                    .order_by(desc(Message.id))
                    .first()
                )
                data["last_message"] = last.content[:120] if last else ""
                data["open_tasks"] = (
                    s.query(Task)
                    .filter(Task.chat_id == row.chat_id, Task.status != "done")
                    .count()
                )
                result.append(data)
            return result

    def personas(self):
        with self.storage.Session() as s:
            result = []
            for row in s.query(Persona).order_by(Persona.name).all():
                data = record(row)
                if row.kind == "group":
                    data["group_reply_policy"] = self.group_reply_policy(row.chat_id)
                result.append(data)
            return result

    def group_reply_policy(self, chat_id):
        return self.setting("group_reply_policy:" + str(chat_id), "off")

    def user_profiles(self, user_ids):
        keys = ["user:" + str(user_id) for user_id in user_ids if user_id]
        with self.storage.Session() as session:
            return {
                row.external_id: record(row)
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
                ):
                    raise ValueError("群聊回复选项无效")
                key = "group_reply_policy:" + str(row.chat_id)
                setting = s.get(Setting, key) or Setting(key=key)
                setting.value = policy
                s.add(setting)
            for field in ("role", "description", "reply_style", "mode"):
                if field in fields:
                    setattr(row, field, fields[field])
            if row.mode not in ("auto", "draft", "off"):
                raise ValueError("回复模式无效")
            if "group_reply_policy" in fields:
                row.mode = "off" if policy == "off" else "auto"
            row.updated_at = datetime.now()
            s.commit()
            data = record(row)
            if row.kind == "group":
                data["group_reply_policy"] = self.group_reply_policy(row.chat_id)
            return data

    def reply_context(self, msg, limit=24):
        """Actual messages from the same chat/thread, including non-triggering ones."""
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

    def context(self, chat_id, sender_id="", anchor=None):
        with self.storage.Session() as s:
            c = s.get(Conversation, str(chat_id))
            p = s.get(Persona, c.persona_key) if c else None
            sender = s.get(Persona, "user:" + str(sender_id)) if sender_id else None
            memories = (
                s.query(Memory)
                .filter_by(chat_id=str(chat_id))
                .order_by(desc(Memory.id))
                .limit(15)
                .all()
            )
            tasks = (
                s.query(Task)
                .filter(Task.chat_id == str(chat_id), Task.status != "done")
                .filter(Task.anchor == anchor if anchor is not None else True)
                .order_by(Task.id)
                .limit(100)
                .all()
            )
            recent_query = s.query(Message).filter_by(chat_id=str(chat_id))
            if anchor is not None:
                recent_query = recent_query.filter_by(anchor=anchor)
            recent = recent_query.order_by(desc(Message.id)).limit(24).all()
            return {
                "overview": c.overview if c else "",
                "persona": record(p) if p else {},
                "sender": record(sender) if sender else {},
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

    def find_messages(self, chat_id="", query="", limit=50, before_id=None):
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
                rows = rows.filter(Message.id < before_id)
            found = rows.order_by(desc(Message.id)).limit(limit + 1).all()
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

    def apply_analysis(self, msg, analysis, allowed_task_ids=None):
        chat_id = str(msg["chat_id"])
        anchor = msg.get("anchor") or ""
        sources = [str(m.get("msg_id") or "") for m in msg.get("_batch") or [msg]]
        marker = (
            "analysis:"
            + hashlib.sha256(
                json.dumps([chat_id, anchor, sources]).encode()
            ).hexdigest()
        )
        with self.storage.Session() as s:
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
                return row

            attention_id = analysis.get("attention_task_id", 0)
            attention_task = existing_task(attention_id)
            convo = s.get(Conversation, chat_id)
            if convo and analysis.get("overview"):
                convo.overview = analysis["overview"][:6000]
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
                    if attention_task.status != "done":
                        tasks.append(
                            {
                                "task_id": attention_id,
                                "details": analysis.get("attention_reason") or "",
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
                else:
                    tasks.append(
                        {
                            "title": "需要你处理："
                            + (analysis.get("attention_reason") or "待确认")[:160],
                            "details": analysis.get("attention_reason") or "",
                            "status": "needs_attention",
                            "priority": "high",
                            "draft_reply": analysis.get("reply", ""),
                        }
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
                            Task.status != "done",
                        )
                        .first()
                    )
                if row is not None:
                    if row.status == "done":
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
        with self.storage.Session() as s:
            return [
                record(r)
                for r in s.query(Task).order_by(desc(Task.created_at)).limit(300).all()
            ]

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
        with self.storage.Session() as s:
            return [
                record(r)
                for r in s.query(Action).order_by(desc(Action.id)).limit(100).all()
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
