import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from loguru import logger
from ..agent.backend import get_backend
from ..auth import LarkAuth
from ..storage import Storage
from ..config import DATA_DIR
from .store import HubStore, DEFAULT_RULES, DEFAULT_SUFFIX
from .engine import WorkEngine


class HubRuntime:
    def __init__(self, storage=None, backend=None):
        self.storage = storage or Storage()
        self.hub = HubStore(self.storage)
        self.backend = backend or get_backend()
        if backend is None and getattr(self.backend, "name", "") != "codex":
            raise ValueError(
                "网页工作台需要 Codex 后端，请设置 LARKX_AGENT_BACKEND=codex 并完成 codex login"
            )
        self.model_slots = asyncio.Semaphore(2)
        self.engine = WorkEngine(self)
        self.client = None
        self.dispatcher = None
        self.connection = "starting"
        self.connection_error = ""
        self.last_analysis = ""
        self.tasks = set()
        self.observations = {}

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def start(self):
        self.hub.recover_interrupted()
        self.hub.seed_existing()
        self.spawn(self.bridge())

    async def stop(self):
        for task in list(self.tasks):
            task.cancel()
        workers = (
            [w.task for w in self.dispatcher.workers.values()]
            if self.dispatcher
            else []
        )
        for task in workers:
            task.cancel()
        await asyncio.gather(*list(self.tasks), *workers, return_exceptions=True)

    async def bridge(self):
        try:
            from ..cli import _listen

            auth = LarkAuth()
            await asyncio.to_thread(auth.require_valid)
            await _listen(SimpleNamespace(agent=True, runtime=self), auth)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.connection = "error"
            self.connection_error = f"{type(e).__name__}: {e}"
            logger.error(f"工作数字人消息连接异常: {type(e).__name__}")

    def on_saved(self, msg, chat_name, sender_name, is_own):
        if is_own and self.hub.is_tracked_send(
            msg.get("msg_id"), msg["chat_id"], msg.get("content") or ""
        ):
            return
        activity_at = float(msg.get("create_time") or time.time())
        if activity_at > 100000000000:
            activity_at /= 1000
        self.hub.touch_handoff(msg["chat_id"], manual=is_own, activity_at=activity_at)
        msg["_human_handled"] = self.hub.human_paused(msg["chat_id"])
        if not is_own:
            self.hub.observe(msg, chat_name, sender_name)
        if is_own or (
            self.dispatcher
            and not self.dispatcher.should_handle(msg, self.client.auth.user_id)
        ):
            key = (str(msg["chat_id"]), msg.get("anchor") or "")
            self.observations.setdefault(key, []).append(
                dict(msg, _chat_name=chat_name, _sender_name=sender_name)
            )
            if len(self.observations[key]) == 1:
                self.spawn(self.observe_batch(key))

    async def observe_batch(self, key):
        try:
            await asyncio.sleep(3)
            batch = self.observations.pop(key, [])
            if batch:
                await self.analyze_messages(batch)
        except Exception as e:
            self.hub.create_task(
                {
                    "title": "会话分析异常",
                    "details": str(e),
                    "chat_id": key[0],
                    "anchor": key[1],
                    "priority": "high",
                }
            )

    async def analyze_messages(self, batch):
        from ..agent.dispatcher import build_xml_message
        from ..media import prepare_image, image_messages

        images = []
        for msg in batch[-6:]:
            if msg.get("msg_type") in (2, 5) and self.client:
                try:
                    for image in image_messages(msg):
                        images.append(
                            await asyncio.to_thread(
                                prepare_image,
                                self.client.auth,
                                image,
                                DATA_DIR / "images",
                            )
                        )
                except Exception:
                    pass
        last = dict(batch[-1], _batch=batch)
        return await self.engine.process(
            "overview:" + str(last["chat_id"]),
            last,
            "\n".join(build_xml_message(m) for m in batch[-20:]),
            images,
            None,
            send_reply=False,
        )

    async def send(
        self,
        chat_id,
        content,
        request_id=None,
        anchor="",
        manual=False,
        reply_to_message_id="",
        mention_user_id="",
    ):
        chat_id = str(chat_id)
        suffix = self.hub.setting("reply_suffix", DEFAULT_SUFFIX).strip()
        content = content.strip()
        if not content:
            raise ValueError("消息不能为空")
        if suffix and not content.endswith(suffix):
            content += "\n\n" + suffix
        if not content.strip() or len(content) > 12000:
            raise ValueError("消息不能为空，且最多 12000 字符")
        conversation = next(
            (c for c in self.hub.conversations() if c["chat_id"] == chat_id), None
        )
        if not conversation:
            raise ValueError("收件会话不存在，请先在飞书建立对话")
        addressed = bool(reply_to_message_id or mention_user_id)
        mention_name = ""
        reply_root = ""
        if addressed:
            from ..storage.models import Message

            if conversation["kind"] != "group":
                raise ValueError("定向回复仅用于群聊")
            with self.storage.Session() as session:
                if reply_to_message_id:
                    source = (
                        session.query(Message)
                        .filter_by(
                            chat_id=chat_id,
                            msg_id=str(reply_to_message_id),
                            anchor=anchor,
                        )
                        .first()
                    )
                    if not source:
                        raise ValueError("引用消息不在当前群聊/话题中")
                    reply_root = source.root_id or source.msg_id
                if mention_user_id:
                    if not str(mention_user_id).isdigit() or str(
                        mention_user_id
                    ) == str(self.client.auth.user_id if self.client else ""):
                        raise ValueError("无效的 @用户")
                    source = (
                        session.query(Message)
                        .filter_by(
                            chat_id=chat_id,
                            sender_id=str(mention_user_id),
                            anchor=anchor,
                        )
                        .order_by(Message.id.desc())
                        .first()
                    )
                    if not source:
                        raise ValueError("@用户未在当前群聊/话题中发言")
                    mention_name = source.sender_name or str(mention_user_id)
            if mention_user_id:
                content = "@" + mention_name + " " + content
                if len(content) > 12000:
                    raise ValueError("消息含 @与尾缀最多 12000 字符")
        if not self.client or self.connection != "connected":
            raise ValueError("飞书尚未连接，未发送")
        action, claimed = self.hub.claim_send(
            request_id or str(uuid.uuid4()),
            chat_id,
            content,
            anchor,
            target={
                "reply_to_message_id": reply_to_message_id,
                "mention_user_id": mention_user_id,
            }
            if addressed
            else None,
        )
        if not claimed:
            return action
        if manual:
            self.hub.touch_handoff(chat_id, manual=True)
        try:
            parts = []
            if addressed:
                prefix = "@" + mention_name + " " if mention_user_id else ""
                if mention_user_id:
                    parts.append(
                        {
                            "type": "mention",
                            "user_id": str(mention_user_id),
                            "text": prefix.rstrip(),
                        }
                    )
                parts.append(
                    {
                        "type": "text",
                        "text": (" " if mention_user_id else "")
                        + content[len(prefix) :],
                    }
                )
                message_id = await asyncio.to_thread(
                    self.client.send_rich,
                    parts,
                    chat_id,
                    anchor or None,
                    reply_to_message_id or None,
                    reply_root or None,
                )
            else:
                message_id = await asyncio.to_thread(
                    self.client.send_msg, content, chat_id, anchor or None
                )
            if not message_id:
                result = self.hub.finish_action(
                    action["id"],
                    "uncertain",
                    error="发送结果未确认，请核对飞书，不会自动重发",
                )
                self.hub.create_task(
                    {
                        "title": "核对消息是否发送成功",
                        "details": content,
                        "chat_id": chat_id,
                        "priority": "high",
                    }
                )
                return result
            self.storage.save_message(
                {
                    "msg_id": message_id,
                    "chat_id": chat_id,
                    "from_id": self.client.auth.user_id,
                    "content": content,
                    "msg_type": 2 if addressed else 4,
                    "msg_type_name": "POST" if addressed else "TEXT",
                    "content_data": {
                        "_display_parts": [{"type": "text", "text": content}]
                    }
                    if addressed
                    else {},
                    "parent_id": reply_to_message_id,
                    "root_id": anchor or reply_root if addressed else "",
                    "anchor": anchor,
                    "create_time": int(time.time()),
                },
                sender_name="我",
                direction="out",
            )
            return self.hub.finish_action(action["id"], "sent", str(message_id))
        except Exception as e:
            return self.hub.finish_action(action["id"], "uncertain", error=str(e))

    async def send_parts(self, chat_id, parts, request_id):
        chat_id = str(chat_id)
        if not self.client or self.connection != "connected":
            raise ValueError("飞书尚未连接")
        if chat_id not in {c["chat_id"] for c in self.hub.conversations()}:
            raise ValueError("会话不存在")
        if not parts or len(parts) > 30:
            raise ValueError("消息内容无效")
        rendered = []
        for part in parts:
            if part["type"] == "text":
                if part.get("text"):
                    rendered.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image":
                asset = json.loads(
                    self.hub.setting("upload:" + part.get("upload_id", ""), "{}")
                )
                if not asset.get("key"):
                    raise ValueError("图片不存在，请重新上传")
                rendered.append(
                    {
                        "type": "image",
                        **{k: asset[k] for k in ("key", "width", "height")},
                    }
                )
            else:
                raise ValueError("消息内容类型无效")
        images = [p for p in rendered if p["type"] == "image"]
        text = "".join(p.get("text", "") for p in rendered)
        if not images:
            return await self.send(chat_id, text, request_id, manual=True)
        if len(images) > 6 or len(text) > 12000:
            raise ValueError("最多 6 张图片、12000 字符")
        suffix = self.hub.setting("reply_suffix", DEFAULT_SUFFIX).strip()
        if suffix and not text.rstrip().endswith(suffix):
            rendered.append({"type": "text", "text": "\n\n" + suffix})
        if sum(len(p.get("text", "")) for p in rendered) > 12000:
            raise ValueError("消息含尾缀最多 12000 字符")
        summary = "".join(p.get("text", "[图片]") for p in rendered)
        action, claimed = self.hub.claim_send(request_id, chat_id, summary)
        if not claimed:
            return action
        self.hub.touch_handoff(chat_id, manual=True)
        try:
            mid = await asyncio.to_thread(self.client.send_rich, rendered, chat_id)
            if not mid:
                return self.hub.finish_action(
                    action["id"], "uncertain", error="未确认发送结果，请核对飞书"
                )
            self.storage.save_message(
                {
                    "msg_id": mid,
                    "chat_id": chat_id,
                    "from_id": self.client.auth.user_id,
                    "content": summary,
                    "msg_type": 2,
                    "msg_type_name": "POST",
                    "create_time": int(time.time()),
                    "content_data": {"_display_parts": rendered},
                },
                sender_name="我",
                direction="out",
            )
            return self.hub.finish_action(action["id"], "sent", str(mid))
        except Exception as e:
            return self.hub.finish_action(
                action["id"], "uncertain", error=type(e).__name__
            )

    async def reply(
        self, chat_id, content, anchor=None, reply_to_message_id="", mention_user_id=""
    ):
        if self.hub.human_paused(chat_id):
            return None
        result = await self.send(
            chat_id,
            content,
            anchor=anchor or "",
            reply_to_message_id=reply_to_message_id,
            mention_user_id=mention_user_id,
        )
        if result["status"] != "sent":
            raise RuntimeError(result["error"] or "发送未确认")
        return result["message_id"]

    def submit_job(self, kind, text, chat_id=""):
        job_id = str(uuid.uuid4())
        self.hub.new_job(job_id, kind, text)
        self.spawn(self.run_job(job_id, kind, text, chat_id))
        return job_id

    async def run_job(self, job_id, kind, text, chat_id):
        self.hub.update_job(job_id, "running")
        try:
            if kind == "control":
                result = await self.engine.control(text, job_id)
            elif kind == "task_draft":
                payload = json.loads(text)
                task = next(
                    (t for t in self.hub.tasks() if t["id"] == payload["task_id"]), None
                )
                if not task:
                    raise ValueError("待办不存在")
                result = await self.engine.polish_task(
                    task, payload["text"], payload["instruction"]
                )
                self.hub.update_task(task["id"], {"draft_reply": result["reply"]})
            elif kind == "conversation_draft":
                payload = json.loads(text)
                if chat_id not in {c["chat_id"] for c in self.hub.conversations()}:
                    raise ValueError("会话不存在")
                result = await self.engine.polish_reply(
                    chat_id, payload["text"], payload["instruction"]
                )
            elif kind == "overview":
                rows = self.storage.get_messages(chat_id, limit=20)
                if not rows:
                    raise ValueError("暂无对话可分析")
                batch = [
                    {
                        "msg_id": r.msg_id,
                        "chat_id": r.chat_id,
                        "from_id": r.sender_id,
                        "chat_type": r.chat_type,
                        "chat_type_name": "GROUP" if r.chat_type in (2, 3) else "P2P",
                        "anchor": r.anchor,
                        "content": r.content,
                        "msg_type": r.msg_type,
                        "content_data": json.loads(r.content_data)
                        if r.content_data
                        else {},
                        "_sender_name": r.sender_name,
                        "_chat_name": r.chat_name,
                    }
                    for r in rows
                ]
                result = await self.analyze_messages(batch)
            else:
                raise ValueError("任务类型无效")
            self.hub.update_job(job_id, "done", result=result)
        except Exception as e:
            self.hub.update_job(job_id, "failed", error=str(e))

    def status(self):
        queue = (
            sum(w.queue.qsize() for w in self.dispatcher.workers.values())
            if self.dispatcher
            else 0
        )
        active = sum(
            lock.locked()
            for lock in getattr(self.backend, "session_locks", {}).values()
        )
        return {
            "connection": self.connection,
            "error": self.connection_error,
            "queued_messages": queue,
            "active_calls": active,
            "model": getattr(self.backend, "model", ""),
            "automation_enabled": self.hub.setting("automation_enabled", "true")
            == "true",
            "own_rules": self.hub.setting("own_rules", DEFAULT_RULES),
            "last_analysis": self.last_analysis,
            "reply_suffix": self.hub.setting("reply_suffix", DEFAULT_SUFFIX),
            **self.hub.batch_timing(),
            "human_pause_seconds": self.hub.human_pause_seconds(),
            "rules": "私聊自动处理；群聊默认不回复，可逐群选择闲聊模式、@我或@全体",
        }
