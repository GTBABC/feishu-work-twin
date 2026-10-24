from datetime import datetime
from sqlalchemy import Column, Integer, String, Text, DateTime
from ..storage.models import Base


class Persona(Base):
    __tablename__ = "hub_personas"
    key = Column(String(80), primary_key=True)
    external_id = Column(String(40), default="")
    kind = Column(String(12), default="person")
    name = Column(String(256), default="")
    chat_id = Column(String(40), default="")
    role = Column(String(256), default="")
    description = Column(Text, default="")
    reply_style = Column(Text, default="")
    mode = Column(String(12), default="inherit")
    updated_at = Column(DateTime, default=datetime.now)


class Conversation(Base):
    __tablename__ = "hub_conversations"
    chat_id = Column(String(40), primary_key=True)
    name = Column(String(256), default="")
    kind = Column(String(12), default="private")
    persona_key = Column(String(80), default="")
    overview = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.now)


class Task(Base):
    __tablename__ = "hub_tasks"
    id = Column(Integer, primary_key=True)
    title = Column(String(256), nullable=False)
    details = Column(Text, default="")
    chat_id = Column(String(40), default="", index=True)
    anchor = Column(String(40), default="")
    origin_msg_id = Column(String(40), default="")
    status = Column(String(20), default="open")
    priority = Column(String(12), default="normal")
    due_at = Column(String(80), default="")
    draft_reply = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now)


class Memory(Base):
    __tablename__ = "hub_memories"
    id = Column(Integer, primary_key=True)
    chat_id = Column(String(40), index=True)
    content = Column(Text, nullable=False)
    source_msg_id = Column(String(40), default="")
    created_at = Column(DateTime, default=datetime.now)


class Action(Base):
    __tablename__ = "hub_actions"
    id = Column(Integer, primary_key=True)
    request_id = Column(String(100), unique=True, nullable=False)
    kind = Column(String(20), default="send")
    chat_id = Column(String(40), default="")
    anchor = Column(String(40), default="")
    content = Column(Text, default="")
    status = Column(String(20), default="pending")
    message_id = Column(String(40), default="")
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)


class Setting(Base):
    __tablename__ = "hub_settings"
    key = Column(String(80), primary_key=True)
    value = Column(Text, default="")


class Job(Base):
    __tablename__ = "hub_jobs"
    id = Column(String(40), primary_key=True)
    kind = Column(String(20), default="control")
    input = Column(Text, default="")
    status = Column(String(20), default="queued")
    result = Column(Text, default="")
    error = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)


class TaskArchive(Base):
    __tablename__ = "hub_task_archives"
    task_id = Column(Integer, primary_key=True)
    reason = Column(String(40), nullable=False)
    snapshot = Column(Text, nullable=False)
    archived_at = Column(DateTime, default=datetime.now)


class ReplyDraft(Base):
    __tablename__ = "hub_reply_drafts"
    key = Column(String(64), primary_key=True)
    chat_id = Column(String(40), index=True)
    anchor = Column(String(40), default="")
    source_msg_id = Column(String(80), default="")
    content = Column(Text, default="")
    status = Column(String(20), default="pending")
    updated_at = Column(DateTime, default=datetime.now)


class Incident(Base):
    __tablename__ = "hub_incidents"
    key = Column(String(64), primary_key=True)
    provider = Column(String(80), index=True)
    category = Column(String(40))
    scope = Column(String(100))
    action_id = Column(Integer)
    occurrences = Column(Integer, default=0)
    first_seen = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime, default=datetime.now)


def record(row):
    return {
        c.name: getattr(row, c.name).isoformat()
        if isinstance(getattr(row, c.name), datetime)
        else getattr(row, c.name)
        for c in row.__table__.columns
    }
