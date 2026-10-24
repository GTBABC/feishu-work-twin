"""Business tasks, conversation drafts and technical incidents have separate lifecycles."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from sqlalchemy import or_
from sqlalchemy.engine import make_url

from .models import Action, Incident, ReplyDraft, Task, TaskArchive, Setting, record

ACTIVE_TASKS = ("open", "in_progress", "needs_attention")
VISIBLE_TASKS = (*ACTIVE_TASKS, "done")


def error_category(error, stage):
    message = str(error).lower()
    if any(word in message for word in ("timeout", "timed out", "超时")):
        return "model_timeout", "模型处理超时", True
    if any(
        word in message
        for word in ("network", "connection", "网络", "连接失败", "502", "503")
    ):
        return "model_connection", "模型连接异常", True
    if any(
        word in message
        for word in ("登录", "密钥", "未找到", "unauthorized", "401", "403")
    ):
        return "model_access", "模型接入异常", True
    return stage, "自动处理异常" if stage == "reply" else "会话分析异常", False


def technical_task(task):
    """Only recognizable legacy machine-generated rows are migrated."""
    if task.origin_msg_id or task.due_at:
        return False
    known_title = task.title == "会话分析异常" or task.title.startswith(
        "自动处理异常："
    )
    signature = re.search(
        r"超时|timeout|timed out|网络|Codex|CLI|模型|JSON|Expecting value|"
        r"connection|401|403|429|502|503|工作分析返回|预算|上下文|未找到|"
        r"AI .{0,15}(?:回复|任务|引用)|schema|校验|解析",
        task.details or "",
        re.I,
    )
    return bool(known_title and signature)


class WorkItems:
    def report_incident(self, error, *, stage, provider, chat_id="", anchor=""):
        category, title, global_scope = error_category(error, stage)
        scope = "provider" if global_scope else f"{chat_id}:{anchor}"
        key = hashlib.sha256(f"{provider}|{category}|{scope}".encode()).hexdigest()
        now = datetime.now()
        with self._work_items_lock, self.storage.Session() as session:
            incident = session.get(Incident, key)
            action = session.get(Action, incident.action_id) if incident else None
            if not action or action.status != "failed":
                action = Action(
                    request_id="incident:" + str(uuid.uuid4()),
                    kind="system",
                    chat_id="" if global_scope else chat_id,
                    anchor=anchor,
                    status="failed",
                )
                session.add(action)
                session.flush()
                incident = incident or Incident(
                    key=key, provider=provider, category=category, scope=scope
                )
                incident.action_id, incident.occurrences, incident.first_seen = (
                    action.id,
                    0,
                    now,
                )
                session.add(incident)
            incident.occurrences += 1
            incident.last_seen = now
            action.content = (
                f"{title} · 累计 {incident.occurrences} 次\n模型：{provider}\n"
                f"首次：{incident.first_seen.isoformat(timespec='seconds')}\n"
                f"最近：{now.isoformat(timespec='seconds')}"
            )
            action.error = str(error)[:6000]
            session.commit()
            return record(action)

    def resolve_incidents(self, provider, chat_id, anchor=""):
        with self._work_items_lock, self.storage.Session() as session:
            incidents = (
                session.query(Incident)
                .filter(
                    Incident.provider == provider,
                    or_(
                        Incident.scope == "provider",
                        Incident.scope == f"{chat_id}:{anchor}",
                    ),
                )
                .all()
            )
            for incident in incidents:
                action = session.get(Action, incident.action_id)
                if action and action.status == "failed":
                    action.status = "resolved"
            session.commit()

    def incident_count(self):
        with self.storage.Session() as session:
            return (
                session.query(Action).filter_by(kind="system", status="failed").count()
            )

    def uncertain_send_count(self):
        with self.storage.Session() as session:
            return (
                session.query(Action).filter_by(kind="send", status="uncertain").count()
            )

    def confirm_send_checked(self, action_id):
        with self.storage.Session() as session:
            row = session.get(Action, action_id)
            if (
                not row
                or row.kind != "send"
                or row.status not in ("uncertain", "checked")
            ):
                raise ValueError("该记录没有待核对的发送结果")
            row.status = "checked"
            session.commit()
            return record(row)

    def save_reply_draft(self, chat_id, anchor, text, source_msg_id=""):
        key = hashlib.sha256(f"{chat_id}:{anchor}".encode()).hexdigest()
        with self.storage.Session() as session:
            row = session.get(ReplyDraft, key) or ReplyDraft(
                key=key, chat_id=chat_id, anchor=anchor
            )
            if (
                row.status == "pending"
                and row.content == text
                and row.source_msg_id == source_msg_id
            ):
                return record(row)
            if row.status == "pending" and row.content and row.content != text:
                session.add(
                    Action(
                        request_id="draft-history:" + str(uuid.uuid4()),
                        kind="draft",
                        chat_id=chat_id,
                        anchor=anchor,
                        status="archived",
                        content=f"回复草稿已更新\n来源消息：{row.source_msg_id}\n原草稿：{row.content}",
                        created_at=row.updated_at,
                    )
                )
            row.content, row.status, row.source_msg_id = text, "pending", source_msg_id
            row.updated_at = datetime.now()
            session.add(row)
            session.commit()
            return record(row)

    def reply_draft(self, chat_id):
        with self.storage.Session() as session:
            row = (
                session.query(ReplyDraft)
                .filter_by(chat_id=chat_id, status="pending")
                .order_by(ReplyDraft.updated_at.desc())
                .first()
            )
            return record(row) if row else None

    def clear_reply_draft(self, chat_id, key, version):
        with self.storage.Session() as session:
            row = session.get(ReplyDraft, key)
            if row and row.chat_id == chat_id and row.updated_at.isoformat() == version:
                row.status = "cleared"
                session.commit()
                return True
            return False

    def _task_archive_backup(self):
        url = make_url(self.storage.url)
        if url.drivername != "sqlite" or url.database in (None, "", ":memory:"):
            return None
        database = Path(url.database)
        folder = database.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            "before-task-archive-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".db"
        )
        source = sqlite3.connect(str(database), timeout=10)
        destination = sqlite3.connect(str(path))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        return str(path)

    def migrate_work_items(self, *, force=False):
        with self.storage.Session() as session:
            marker = "work_item_lifecycle_v1"
            if not force and session.get(Setting, marker):
                return {"archived": 0, "backup": None}
            candidates = (
                session.query(Task)
                .filter(Task.status.in_(VISIBLE_TASKS))
                .filter(
                    or_(
                        Task.title == "会话分析异常",
                        Task.title.like("自动处理异常：%"),
                        Task.title.like("审核回复：%"),
                        Task.title == "核对消息是否发送成功",
                    )
                )
                .all()
            )
            selected = [
                row
                for row in candidates
                if technical_task(row)
                or (
                    not row.origin_msg_id
                    and not row.due_at
                    and row.title.startswith("审核回复：")
                    and row.draft_reply
                )
                or (
                    not row.origin_msg_id
                    and not row.due_at
                    and row.title == "核对消息是否发送成功"
                )
            ]
            if not selected:
                if not session.get(Setting, marker):
                    session.add(Setting(key=marker, value="done"))
                    session.commit()
                return {"archived": 0, "backup": None}
            backup = self._task_archive_backup()
            for task in selected:
                snapshot = record(task)
                is_draft = task.title.startswith("审核回复：")
                reason = (
                    "draft"
                    if is_draft
                    else "uncertain_send"
                    if task.title == "核对消息是否发送成功"
                    else "technical_error"
                )
                session.add(
                    TaskArchive(
                        task_id=task.id,
                        reason=reason,
                        snapshot=json.dumps(snapshot, ensure_ascii=False),
                    )
                )
                session.add(
                    Action(
                        request_id=f"task-archive:{task.id}",
                        kind="archive",
                        chat_id=task.chat_id,
                        anchor=task.anchor,
                        status="archived",
                        created_at=task.created_at,
                        content=f"已归档待办 #{task.id}：{task.title}\n原状态：{task.status}\n{task.details or ''}"
                        + (f"\n原草稿：{task.draft_reply}" if task.draft_reply else ""),
                    )
                )
                if is_draft and task.status != "done":
                    key = hashlib.sha256(
                        f"{task.chat_id}:{task.anchor}".encode()
                    ).hexdigest()
                    draft = session.get(ReplyDraft, key)
                    if not draft or draft.updated_at < task.updated_at:
                        draft = draft or ReplyDraft(
                            key=key, chat_id=task.chat_id, anchor=task.anchor
                        )
                        draft.content, draft.source_msg_id, draft.status = (
                            task.draft_reply,
                            f"legacy-task:{task.id}",
                            "pending",
                        )
                        draft.updated_at = task.updated_at
                        session.add(draft)
                elif reason == "uncertain_send" and task.status != "done":
                    existing = (
                        session.query(Action)
                        .filter_by(
                            chat_id=task.chat_id,
                            anchor=task.anchor,
                            status="uncertain",
                            content=task.details,
                        )
                        .first()
                    )
                    if not existing:
                        session.add(
                            Action(
                                request_id=f"legacy-uncertain:{task.id}",
                                kind="send",
                                chat_id=task.chat_id,
                                anchor=task.anchor,
                                content=task.details,
                                status="uncertain",
                                error="历史发送结果待核对，不会自动重发",
                                created_at=task.created_at,
                            )
                        )
                task.status = "archived"
            if not session.get(Setting, marker):
                session.add(Setting(key=marker, value="done"))
            session.commit()
            return {"archived": len(selected), "backup": backup}
