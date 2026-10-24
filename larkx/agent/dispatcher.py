import asyncio
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

from loguru import logger

from ..config import load_config
from ..settings import resolve_settings

INSTRUCTIONS = "以上是飞书会话里新到的一条消息(XML 里有会话/发送者/内容)。你是这个数字人,完整对话历史你都有。如果对方也是机器人且只是互相客套寒暄,直接结束不要再接话。"
OUTPUT_RULES = "不需要回复就直接结束,不要解释、不要复述消息。"


def _ts(msg: dict) -> str:
    t = msg.get("create_time")
    if not t:
        return ""
    try:
        return datetime.fromtimestamp(int(t)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(t)


def build_xml_message(m: dict) -> str:
    lines = [f"<message id={quoteattr(str(m.get('msg_id') or ''))}>"]
    lines.append(
        f"  <chat id={quoteattr(str(m.get('chat_id')))} name={quoteattr(str(m.get('_chat_name') or ''))} type={quoteattr(str(m.get('chat_type_name') or ''))} anchor={quoteattr(str(m.get('anchor') or ''))}/>"
    )
    lines.append(
        f"  <sender id={quoteattr(str(m.get('from_id')))} name={quoteattr(str(m.get('_sender_name') or ''))}/>"
    )
    if m.get("at_me"):
        lines.append("  <at_me>true</at_me>")
    lines.append(f"  <time>{escape(_ts(m))}</time>")
    lines.append(f"  <content>{escape(str(m.get('content') or ''))}</content>")
    lines.append("</message>")
    return "\n".join(lines)


class SessionWorker:
    def __init__(self, dispatcher, session_key: str):
        self.dispatcher = dispatcher
        self.session_key = session_key
        self.queue = asyncio.Queue()
        self.collecting = False
        self.task = asyncio.create_task(self._run())

    async def collect_batch(self, first):
        # Wait for a quiet interval, not a fixed interval after the first sentence.
        workflow = getattr(self.dispatcher, "workflow", None)
        settings = (
            workflow.runtime.hub.settings() if workflow else resolve_settings()[0]
        )
        delay, maximum = settings["batch_delay"], settings["batch_max_wait"]
        deadline = asyncio.get_running_loop().time() + maximum
        batch = [first]
        self.collecting = True
        try:
            while len(batch) < settings["batch_size"]:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(
                        await asyncio.wait_for(self.queue.get(), min(delay, remaining))
                    )
                except asyncio.TimeoutError:
                    break
        finally:
            self.collecting = False
        return batch

    async def _run(self):
        while True:
            msg = await self.queue.get()
            try:
                if msg.get("_reply_context_only"):
                    continue
                if self.dispatcher.backend.returns_reply:
                    batch = await self.collect_batch(msg)
                    if len(batch) > 1:
                        msg = dict(batch[-1], _batch=batch)
                await self.dispatcher.handle_message(self.session_key, msg)
            except Exception as e:
                logger.error(f"处理消息异常 [{self.session_key}]: {e}")
                workflow = getattr(self.dispatcher, "workflow", None)
                if workflow:
                    workflow.runtime.report_processing_error(e, msg, "reply")


class AgentDispatcher:
    def __init__(self, backend, reply_fn, image_prepare=None, workflow=None):
        self.cfg = load_config()
        self.backend = backend
        self.reply_fn = reply_fn
        self.image_prepare = image_prepare
        self.workflow = workflow
        self.workers = {}

    def session_key_of(self, msg: dict) -> str:
        # A work twin must not mix one contact's model history into another chat.
        if self.workflow:
            return f"{msg.get('chat_id')}:{msg.get('anchor') or ''}"
        scope = self.cfg["context_scope"]
        if scope == "global":
            return "global"
        if scope == "chat":
            return str(msg.get("chat_id"))
        return f"{msg.get('chat_id')}:{msg.get('anchor') or ''}"

    def should_handle(self, msg: dict, my_user_id: str) -> bool:
        if str(msg.get("from_id")) == str(my_user_id):
            return False
        t = self.cfg.get("triggers") or {}
        content = (msg.get("content") or "").strip()
        chat_id = str(msg.get("chat_id"))
        sender_id = str(msg.get("from_id"))
        type_name = {1: "p2p", 2: "group", 3: "topic_group"}.get(
            msg.get("chat_type"), ""
        )
        if t.get("prefix") and (not content.startswith(t["prefix"])):
            return False
        chat_types = t.get("chat_types") or []
        if chat_types and type_name not in chat_types:
            return False
        include = [str(x) for x in t.get("include_chats") or []]
        if include and chat_id not in include:
            return False
        exclude = [str(x) for x in t.get("exclude_chats") or []]
        if exclude and chat_id in exclude:
            return False
        senders = [str(x) for x in t.get("include_senders") or []]
        if senders and sender_id not in senders:
            return False
        keywords = t.get("keywords") or []
        if keywords and (not any((kw in content for kw in keywords))):
            return False
        if self.workflow and type_name in ("group", "topic_group"):
            policy = self.workflow.runtime.hub.group_reply_policy(chat_id)
            if policy == "off":
                return False
            if policy == "at_all" and not msg.get("at_all"):
                return False
            if policy == "at_me" and not (
                str(my_user_id) in msg.get("at_user_ids", [])
                or (msg.get("at_me") and not msg.get("at_all"))
            ):
                return False
        elif (
            t.get("group_at_only")
            and type_name in ("group", "topic_group")
            and (not msg.get("at_me"))
        ):
            return False
        return True

    async def submit(
        self,
        msg: dict,
        chat_name: str = "",
        sender_name: str = "",
        my_user_id: str = "",
    ):
        if not self.should_handle(msg, my_user_id):
            # A non-mention can complete an already-triggered group request during
            # its quiet interval, but must never start a new automatic reply.
            worker = self.workers.get(self.session_key_of(msg))
            followup = (
                self.workflow
                and msg.get("chat_type") in (2, 3)
                and self.workflow.runtime.hub.group_reply_policy(msg["chat_id"])
                in ("at_me", "at_all")
                and worker
                and (worker.collecting or not worker.queue.empty())
                and self.should_handle(
                    dict(msg, at_me=True, at_all=True, at_user_ids=[str(my_user_id)]),
                    my_user_id,
                )
            )
            if not followup:
                return
            msg = dict(msg, _reply_context_only=True)
        cmd, remainder = (
            (None, None)
            if self.workflow
            else self._parse_command(msg.get("content") or "")
        )
        key = self.session_key_of(msg)
        chat_id = str(msg["chat_id"])
        root_id = (msg.get("anchor") or "") or None
        if cmd == "clear":
            self._drain_queue(key)
            self.backend.reset_session(key)
            if not remainder:
                await self.reply_fn(chat_id, "✅ 已开启新会话", root_id)
                return
        elif cmd == "stop":
            n = self._drain_queue(key)
            if not remainder:
                await self.reply_fn(
                    chat_id,
                    f"⏹ 已清空 {n} 条排队消息" if n else "⏹ 队列已清空",
                    root_id,
                )
                return
        msg = dict(msg)
        if cmd:
            msg["content"] = remainder
        msg["_chat_name"] = chat_name
        msg["_sender_name"] = sender_name
        if key not in self.workers:
            self.workers[key] = SessionWorker(self, key)
        await self.workers[key].queue.put(msg)

    def _parse_command(self, content: str):
        t = self.cfg.get("triggers") or {}
        prefix = t.get("prefix") or ""
        c = content.strip()
        if prefix and c.startswith(prefix):
            c = c[len(prefix) :].lstrip()
        for cmd in ("/clear", "/stop"):
            if c == cmd:
                return cmd[1:], ""
            if (
                c.startswith(cmd)
                and len(c) > len(cmd)
                and (c[len(cmd)] in (" ", "\t", "\n"))
            ):
                return cmd[1:], c[len(cmd) :].strip()
        return None, None

    def _drain_queue(self, key) -> int:
        w = self.workers.get(key)
        if not w:
            return 0
        n = 0
        while True:
            try:
                w.queue.get_nowait()
                n += 1
            except asyncio.QueueEmpty:
                return n

    async def handle_message(self, session_key: str, msg: dict):
        batch = msg.get("_batch") or [msg]
        body = "\n".join(build_xml_message(item) for item in batch)
        context = []
        if self.workflow:
            ids = {str(item.get("msg_id")) for item in batch}
            context = await asyncio.to_thread(
                self.workflow.runtime.hub.reply_context, msg
            )
            context = [item for item in context if str(item.get("msg_id")) not in ids]
            if context:
                body = (
                    "同一会话/话题的最近背景消息（仅作上下文，不独立触发回复）：\n"
                    + "\n".join(build_xml_message(item) for item in context)
                    + "\n本次连续收到的消息（请合并理解）：\n"
                    + body
                )
        image_paths = []
        settings = (
            self.workflow.runtime.hub.settings()
            if self.workflow
            else resolve_settings()[0]
        )
        image_limit = settings["model_image_limit"]
        if self.backend.returns_reply and self.image_prepare and image_limit:
            # Include images immediately preceding a standalone @mention too.
            latest_time = float(msg.get("create_time") or 0)
            if latest_time > 100000000000:
                latest_time /= 1000
            preceding_images = []
            image_window = (
                settings["image_reference_seconds"]
                if any(word in body for word in ("截图", "图片", "上面", "照片"))
                else settings["image_lookback_seconds"]
            )
            for item in context:
                timestamp = float(item.get("create_time") or 0)
                if timestamp > 100000000000:
                    timestamp /= 1000
                if latest_time and 0 <= latest_time - timestamp <= image_window:
                    preceding_images.append(item)
            for item in batch + list(reversed(preceding_images)):
                if len(image_paths) >= image_limit:
                    break
                if item.get("msg_type") in (2, 5):
                    try:
                        prepared = await self.image_prepare(item)
                        image_paths.extend(
                            prepared if isinstance(prepared, list) else [prepared]
                        )
                    except Exception as e:
                        logger.warning(
                            f"图片下载失败 [{session_key}]: {type(e).__name__}"
                        )
                        body += "\n此条图片下载失败，无法查看；请明确说明并请对方重发，不要猜测。"
            image_paths = image_paths[:image_limit]
        chat_id = str(msg["chat_id"])
        root_id = (msg.get("anchor") or "") or None
        reply_cmd = f'lark send {chat_id} "<回复内容>"'
        if self.workflow:
            msg = dict(
                msg, _context_message_ids=[str(m.get("msg_id") or "") for m in context]
            )
            await self.workflow.process(
                session_key, msg, body, image_paths, self.reply_fn
            )
            return
        if root_id:
            reply_cmd += f" --root {root_id}"
        if self.backend.returns_reply:
            rules = (
                "此消息已经通过自动回复触发规则，必须给出简短自然的回复正文。"
                "表情也要回应；无法看到表情内容时不要猜测其含义。"
                "不要输出 [[NO_REPLY]]，不要执行发送命令。"
            )
        else:
            rules = f"要回复就执行: {reply_cmd}\n" + OUTPUT_RULES
        if self.cfg.get("system_prompt"):
            instructions = self.cfg["system_prompt"] + "\n" + rules
        else:
            instructions = INSTRUCTIONS + "\n" + rules
        prompt = body + "\n\n" + instructions
        kwargs = {"chat_id": chat_id, "anchor": msg.get("anchor") or ""}
        if self.backend.returns_reply:
            kwargs["require_reply"] = True
            kwargs["image_paths"] = image_paths
        out = await self.backend.run(session_key, prompt, **kwargs)
        if out:
            if self.backend.returns_reply:
                await self.reply_fn(chat_id, out, root_id)
                logger.info(f"自动回复已提交 [{session_key}]")
            else:
                logger.debug(f"[{session_key}] agent 输出: {out[:200]}")
