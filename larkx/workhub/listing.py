"""Database-backed pages for the workbench, independent of tool history caps."""

from datetime import date, datetime, time, timedelta

from sqlalchemy import desc, func, or_

from .models import Action, Conversation, Persona, Task
from .store import record
from .work_items import VISIBLE_TASKS

MODELS = {
    "tasks": Task,
    "conversations": Conversation,
    "personas": Persona,
    "actions": Action,
}


def page(
    hub,
    collection,
    *,
    query="",
    kind="all",
    status="all",
    start="",
    end="",
    number=1,
    size=10,
):
    model = MODELS[collection]
    with hub.storage.Session() as session:
        rows = session.query(model)
        if query.strip():
            columns = {
                "tasks": (Task.title, Task.details),
                "conversations": (
                    Conversation.name,
                    Conversation.overview,
                    Conversation.chat_id,
                ),
                "personas": (Persona.name, Persona.role, Persona.description),
                "actions": (Action.content, Action.error, Conversation.name),
            }[collection]
            if collection == "actions":
                rows = rows.outerjoin(
                    Conversation, Action.chat_id == Conversation.chat_id
                )
            needle = (
                query.strip()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            rows = rows.filter(
                or_(*(column.ilike(f"%{needle}%", escape="\\") for column in columns))
            )
        if collection in ("conversations", "personas"):
            if kind not in ("all", "private", "group"):
                raise ValueError("会话类型无效")
            if kind != "all":
                rows = rows.filter(
                    model.kind
                    == (
                        "person"
                        if collection == "personas" and kind == "private"
                        else kind
                    )
                )
        elif collection == "tasks":
            rows = rows.filter(Task.status.in_(VISIBLE_TASKS))
            if status not in ("all", "open", "in_progress", "done", "needs_attention"):
                raise ValueError("任务状态无效")
            if status == "open":
                rows = rows.filter(Task.status.in_(("open", "needs_attention")))
            elif status != "all":
                rows = rows.filter(Task.status == status)
        else:
            if status not in (
                "all",
                "pending",
                "sent",
                "failed",
                "uncertain",
                "resolved",
                "archived",
                "checked",
            ):
                raise ValueError("执行状态无效")
            if status != "all":
                rows = rows.filter(Action.status == status)
            try:
                lower = date.fromisoformat(start) if start else None
                upper = date.fromisoformat(end) if end else None
                if lower and upper and lower > upper:
                    raise ValueError()
                if lower:
                    rows = rows.filter(
                        Action.created_at >= datetime.combine(lower, time())
                    )
                if upper:
                    rows = rows.filter(
                        Action.created_at
                        < datetime.combine(upper + timedelta(days=1), time())
                    )
            except (ValueError, OverflowError):
                raise ValueError("请选择有效的起止日期") from None
        total = rows.count()
        pages = max(1, (total + size - 1) // size)
        number = min(number, pages)
        order = (
            (Persona.name, Persona.key)
            if collection == "personas"
            else (
                (desc(Conversation.updated_at), Conversation.chat_id)
                if collection == "conversations"
                else (desc(model.created_at), desc(model.id))
            )
        )
        selected = rows.order_by(*order).offset((number - 1) * size).limit(size).all()
        if collection == "conversations":
            items = hub.conversations(chat_ids=[row.chat_id for row in selected])
        elif collection == "personas":
            items = hub.personas(keys=[row.key for row in selected])
        else:
            names = (
                dict(
                    session.query(Conversation.chat_id, Conversation.name)
                    .filter(Conversation.chat_id.in_([row.chat_id for row in selected]))
                    .all()
                )
                if selected
                else {}
            )
            items = [
                {**record(row), "chat_name": names.get(row.chat_id, "未关联会话")}
                for row in selected
            ]
        return {
            "items": items,
            "total": total,
            "page": number,
            "pages": pages,
            "size": size,
        }


def totals(hub):
    with hub.storage.Session() as session:
        tasks = dict(
            session.query(Task.status, func.count()).group_by(Task.status).all()
        )
        return {
            "waiting": tasks.get("open", 0) + tasks.get("needs_attention", 0),
            "attention": tasks.get("needs_attention", 0),
            "contacts": session.query(func.count())
            .select_from(Persona)
            .filter(Persona.kind == "person")
            .scalar(),
            "completed": tasks.get("done", 0),
        }
