import asyncio
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from loguru import logger
from ..agent.model_backend import ModelRouter
from ..agent.model_connections import ModelConnections
from ..auth import LarkAuth
from ..storage import Storage
from ..config import DATA_DIR
from .store import HubStore, DEFAULT_SUFFIX
from ..settings import message_limits
from .engine import WorkEngine
from .scheduling import ModelScheduler


class SendUncertainError(RuntimeError):
    pass


class HubRuntime:
    def __init__(self, storage=None, backend=None):
        self.storage = storage or Storage()
        self.hub = HubStore(self.storage)
        migration = self.hub.work_item_migration
        if migration["archived"]:
            logger.info(
                "历史待办已整理：{} 条；迁移前备份：{}",
                migration["archived"],
                migration["backup"] or "原始快照已保存",
            )
        self.models = ModelConnections(self.hub)
        self.backend = backend or ModelRouter(self.models)
        settings = self.hub.settings()
        self.model_slots = ModelScheduler(
            settings["max_concurrency"], settings["background_concurrency"]
        )
        self.engine = WorkEngine(self)
        self.client = None
        self.dispatcher = None
        self.connection = "starting"
        self.connection_error = ""
        self.last_analysis = ""
        self.tasks = set()
        self.observations = {}
        self.observation_workers = {}
        self.bridge_task = None
        self.reconnect_lock = asyncio.Lock()
        self.connection_changing = False

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def processing_provider(self):
        active = self.models.active()
        return active["kind"] + ":" + active["provider"]

    def report_processing_error(self, error, msg, stage):
        if isinstance(error, SendUncertainError):
            return None
        return self.hub.report_incident(
            error,
            stage=stage,
            provider=self.processing_provider(),
            chat_id=str(msg.get("chat_id") or ""),
            anchor=str(msg.get("anchor") or ""),
        )

    async def start(self):
        self.hub.recover_interrupted()
        self.hub.seed_existing()
        self.bridge_task = self.spawn(self.bridge())

    def require_idle_connection(self):
        live = self.live_status()
        if (
            any(
                live[key]
                for key in (
                    "active_calls",
                    "queued_messages",
                    "queued_analyses",
                    "background_pending_messages",
                )
            )
            or (
                self.dispatcher
                and any(w.collecting for w in self.dispatcher.workers.values())
            )
            or any(
                task is not self.bridge_task and not task.done() for task in self.tasks
            )
        ):
            raise ValueError("正在处理消息，请稍后重新连接")

    def require_stable_connection(self):
        if self.connection_changing:
            raise ValueError("正在切换连接，请稍后重试")

    async def reconnect(self, credentials=None):
        async with self.reconnect_lock:
            self.require_idle_connection()
            self.connection_changing = True
            self.connection = "connecting"
            try:
                if self.bridge_task:
                    self.bridge_task.cancel()
                    await asyncio.gather(self.bridge_task, return_exceptions=True)
                self.require_idle_connection()
                if self.dispatcher:
                    workers = [w.task for w in self.dispatcher.workers.values()]
                    for worker in workers:
                        worker.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                if credentials is not None:
                    from ..config import CREDENTIALS_PATH
                    import os

                    os.replace(credentials, CREDENTIALS_PATH)
                self.client = None
                self.dispatcher = None
                self.connection_error = ""
                self.bridge_task = self.spawn(self.bridge())
            except Exception:
                self.connection = "error"
                self.connection_error = "重新连接失败，请重试"
                raise ValueError(self.connection_error) from None
            finally:
                self.connection_changing = False

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
            # Topics have separate input batches, but share the chat's update lock.
            key = (str(msg["chat_id"]), str(msg.get("anchor") or ""))
            self.observations.setdefault(key, []).append(
                dict(msg, _chat_name=chat_name, _sender_name=sender_name)
            )
            if key not in self.observation_workers:
                self.observation_workers[key] = self.spawn(self.observe_batch(key))

    async def observe_batch(self, key):
        try:
            while self.observations.get(key):
                await asyncio.sleep(self.hub.background_interval())
                pending = self.observations[key]
                size = self.hub.settings()["batch_size"]
                batch = pending[:size]
                del pending[:size]
                try:
                    await self.analyze_messages(batch, background=True)
                except asyncio.CancelledError:
                    pending[:0] = batch
                    raise
                except Exception as e:
                    # Keep failed input for a later message/manual retry, without
                    # repeatedly calling a broken backend or creating error spam.
                    pending[:0] = batch
                    self.report_processing_error(
                        e, {"chat_id": key[0], "anchor": key[1]}, "analysis"
                    )
                    return
            self.observations.pop(key, None)
        finally:
            self.observation_workers.pop(key, None)

    async def analyze_messages(self, batch, *, background=False):
        self.require_stable_connection()
        from ..agent.dispatcher import build_xml_message
        from ..media import prepare_image, image_messages

        settings = self.hub.settings()
        images = []
        for msg in batch[-6:] if settings["model_image_limit"] else []:
            if len(images) >= settings["model_image_limit"]:
                break
            if msg.get("msg_type") in (2, 5) and self.client:
                try:
                    for image in image_messages(
                        msg, settings["model_image_limit"] - len(images)
                    ):
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
        last = batch[-1]
        anchors = {str(msg.get("anchor") or "") for msg in batch}
        # Whole-chat manual summaries must not mix the model history of topics.
        session_key = (
            "overview:" + str(last["chat_id"]) + ":" + str(last.get("anchor") or "")
            if len(anchors) == 1
            else "overview-all:" + str(last["chat_id"])
        )
        return await self.engine.process(
            session_key,
            dict(last, _batch=batch),
            "\n".join(build_xml_message(m) for m in batch),
            images,
            None,
            send_reply=False,
            background=background,
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
        self.require_stable_connection()
        chat_id = str(chat_id)
        suffix = self.hub.setting("reply_suffix", DEFAULT_SUFFIX).strip()
        content = content.strip()
        if not content:
            raise ValueError("消息不能为空")
        if suffix and not content.endswith(suffix):
            content += "\n\n" + suffix
        limit = self.hub.settings()["message_max_chars"]
        if not content.strip() or len(content) > limit:
            raise ValueError(f"消息不能为空，且最多 {limit} 字符")
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
                if len(content) > limit:
                    raise ValueError(f"消息含 @与尾缀最多 {limit} 字符")
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

    async def send_parts(self, chat_id, parts, request_id, anchor=""):
        self.require_stable_connection()
        limits = message_limits(self.hub.settings())
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
                size = asset.get("size", 0)
                if not size and asset.get("path"):
                    cached = Path(asset["path"])
                    size = cached.stat().st_size if cached.is_file() else 0
                if size > limits["upload_bytes"]:
                    raise ValueError("图片超过当前大小上限，请重新上传")
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
            return await self.send(
                chat_id, text, request_id, anchor=anchor, manual=True
            )
        if len(images) > limits["send_images"] or len(text) > limits["message_chars"]:
            raise ValueError(
                f"最多 {limits['send_images']} 张图片、{limits['message_chars']} 字符"
            )
        suffix = self.hub.setting("reply_suffix", DEFAULT_SUFFIX).strip()
        if suffix and not text.rstrip().endswith(suffix):
            rendered.append({"type": "text", "text": "\n\n" + suffix})
        if sum(len(p.get("text", "")) for p in rendered) > limits["message_chars"]:
            raise ValueError(f"消息含尾缀最多 {limits['message_chars']} 字符")
        summary = "".join(p.get("text", "[图片]") for p in rendered)
        action, claimed = self.hub.claim_send(request_id, chat_id, summary, anchor)
        if not claimed:
            return action
        self.hub.touch_handoff(chat_id, manual=True)
        try:
            mid = (
                await asyncio.to_thread(
                    self.client.send_rich, rendered, chat_id, anchor
                )
                if anchor
                else await asyncio.to_thread(self.client.send_rich, rendered, chat_id)
            )
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
                    "anchor": anchor,
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
            raise SendUncertainError(result["error"] or "发送未确认")
        return result["message_id"]

    def submit_job(self, kind, text, chat_id=""):
        self.require_stable_connection()
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
                task = self.hub.task(payload["task_id"])
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
                rows = self.storage.get_messages(
                    chat_id, limit=self.hub.settings()["batch_size"]
                )
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

    def live_status(self):
        queue = (
            sum(w.queue.qsize() for w in self.dispatcher.workers.values())
            if self.dispatcher
            else 0
        )
        active = self.model_slots.active
        return {
            "connection": self.connection,
            "error": self.connection_error,
            "queued_messages": queue,
            "active_calls": active,
            "queued_analyses": self.model_slots.queued,
            "background_pending_messages": sum(
                len(batch) for batch in self.observations.values()
            ),
            "last_analysis": self.last_analysis,
        }

    def status(self):
        settings = self.hub.settings()
        return {
            **settings,
            **self.live_status(),
            "system_incidents": self.hub.incident_count(),
            "uncertain_sends": self.hub.uncertain_send_count(),
            "limits": message_limits(settings),
            "model": self.models.profile(**self.models.active())["model"]
            if isinstance(self.backend, ModelRouter)
            else settings["codex_model"]
            if getattr(self.backend, "name", "") == "codex"
            else getattr(self.backend, "model", ""),
            "rules": "私聊自动处理；群聊默认不回复，可逐群选择闲聊模式、@我或@全体",
        }
