import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .runtime import HubRuntime
from .updates import StateUpdates
from ..agent.model_backend import ModelRouter
from ..settings import (
    GROUPS,
    MESSAGE_MAX_CHARS,
    message_limits,
    settings_metadata,
)

STATIC = Path(__file__).parent / "static"


class SendBody(BaseModel):
    content: str = Field(min_length=1, max_length=MESSAGE_MAX_CHARS)
    request_id: str = Field(min_length=1, max_length=100)
    anchor: str = Field(default="", max_length=40)
    reply_to_message_id: str = Field(default="", max_length=40)
    mention_user_id: str = Field(default="", max_length=40)


class ControlBody(BaseModel):
    text: str = Field(min_length=1, max_length=10000)


class DraftBody(BaseModel):
    text: str = Field(default="", max_length=MESSAGE_MAX_CHARS)
    instruction: str = Field(default="", max_length=2000)


class ClearDraftBody(BaseModel):
    key: str = Field(max_length=64)
    version: str = Field(max_length=80)


class RichPart(BaseModel):
    type: Literal["text", "image"]
    text: str = Field(default="", max_length=MESSAGE_MAX_CHARS)
    upload_id: str = Field(default="", max_length=40)


class RichSendBody(BaseModel):
    parts: list[RichPart] = Field(min_length=1, max_length=30)
    request_id: str = Field(min_length=1, max_length=100)
    anchor: str = Field(default="", max_length=40)


class ImageUploadBody(BaseModel):
    data: str = Field(max_length=14000000)


def create_app(runtime=None, start_bridge=True):
    from .mcp_server import HubAccess, create_mcp
    from .login import BrowserLogin
    from .path_picker import PathPicker

    login = BrowserLogin()
    picker = PathPicker()
    access = HubAccess(lambda: app.state.hub)
    mcp = create_mcp(access)
    mcp_app = mcp.streamable_http_app(json_response=True, stateless_http=True)

    @asynccontextmanager
    async def lifespan(app):
        app.state.hub = runtime or HubRuntime()
        if start_bridge:
            await app.state.hub.start()
        app.state.updates = StateUpdates(app.state.hub)
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await app.state.hub.stop()
            app.state.updates.close()
            login.clear()
            await picker.close()

    app = FastAPI(title="我的工作数字人", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "testserver"]
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method not in (
            "GET",
            "HEAD",
        ):
            origin = request.headers.get("origin")
            if request.headers.get("x-workhub") != "1" or (
                origin and urlparse(origin).netloc != request.headers.get("host")
            ):
                return JSONResponse({"detail": "请从本地控制台操作"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def hub(request):
        return request.app.state.hub

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/integration/status")
    async def integration_status():
        return await access.status()

    @app.get("/api/integration/conversations")
    async def integration_conversations(
        query: str = Query(default="", max_length=200),
        kind: Literal["all", "private", "group"] = "all",
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        return await access.conversations(query, kind, limit, offset)

    @app.get("/api/integration/messages")
    async def integration_messages(
        chat_id: str = Query(default="", max_length=40),
        query: str = Query(default="", max_length=200),
        limit: int = Query(default=50, ge=1, le=100),
        before_id: int | None = Query(default=None, ge=1),
    ):
        return await access.messages(chat_id, query, limit, before_id)

    @app.get("/api/state")
    async def state(request: Request):
        snapshot = request.app.state.updates.state()
        return {**snapshot.data, "epoch": snapshot.epoch, "revision": snapshot.revision}

    @app.get("/api/updates")
    async def updates(
        request: Request,
        since: int = Query(default=0, ge=0),
        epoch: str = Query(default="", max_length=64),
        scope: Literal["all", "web"] = "all",
    ):
        source = request.app.state.updates
        return (source.web_state() if scope == "web" else source.state()).delta(
            since, epoch
        )

    @app.get("/api/lists/{collection}")
    async def list_page(
        request: Request,
        collection: Literal["tasks", "conversations", "personas", "actions"],
        query: str = Query(default="", max_length=200),
        kind: str = Query(default="all", max_length=20),
        status: str = Query(default="all", max_length=20),
        start: str = Query(default="", max_length=10),
        end: str = Query(default="", max_length=10),
        page: int = Query(default=1, ge=1, le=10000000),
        size: int = Query(default=10, ge=1, le=100),
    ):
        from .listing import page as select_page

        snapshot = request.app.state.updates.web_state()
        result = select_page(
            hub(request).hub,
            collection,
            query=query,
            kind=kind,
            status=status,
            start=start,
            end=end,
            number=page,
            size=size,
        )
        return {**result, "revision": snapshot.data["status"]["list_revision"]}

    @app.get("/api/conversations/{chat_id}")
    async def conversation(
        chat_id: str,
        request: Request,
        since: int | None = Query(default=None, ge=0),
        epoch: str = Query(default="", max_length=64),
    ):
        snapshot = await request.app.state.updates.conversation(
            chat_id, lambda: build_conversation(chat_id, request)
        )
        if since is not None:
            return snapshot.delta(since, epoch)
        return {**snapshot.data, "epoch": snapshot.epoch, "revision": snapshot.revision}

    async def build_conversation(chat_id, request):
        r = hub(request)
        rows = r.hub.messages(chat_id)
        stale = [
            m
            for m in rows
            if m["msg_type"] == 2
            and m["position"]
            and "_display_parts" not in json.loads(m.get("content_data") or "{}")
        ]
        if stale and r.client:
            attempts = getattr(r, "media_backfill", set())
            r.media_backfill = attempts
            pending = [m for m in stale if m["msg_id"] not in attempts]
            if pending:
                attempts.update(m["msg_id"] for m in pending)
                try:
                    fresh = await asyncio.to_thread(
                        r.client.pull_history, chat_id, [m["position"] for m in pending]
                    )
                    from ..storage.models import Message

                    with r.storage.Session() as session:
                        for message in fresh:
                            row = (
                                session.query(Message)
                                .filter_by(chat_id=chat_id, msg_id=message["msg_id"])
                                .first()
                            )
                            if row:
                                row.content_data = json.dumps(
                                    message["content_data"],
                                    ensure_ascii=False,
                                    default=lambda b: "<bytes>",
                                )
                        session.commit()
                    rows = r.hub.messages(chat_id)
                except Exception:
                    pass
        from ..media import display_parts

        for m in rows:
            m["display_parts"] = display_parts(
                m["msg_type"], json.loads(m.get("content_data") or "{}")
            )
        from ..storage.models import Message

        from sqlalchemy import and_, or_

        earliest = rows[0] if rows else None
        with r.storage.Session() as session:
            more = (
                earliest is not None
                and session.query(Message.id)
                .filter(
                    Message.chat_id == chat_id,
                    or_(
                        Message.create_time < earliest["create_time"],
                        and_(
                            Message.create_time == earliest["create_time"],
                            Message.id < earliest["id"],
                        ),
                    ),
                )
                .first()
                is not None
            )
        return {
            "context": r.hub.context(chat_id),
            "draft": r.hub.reply_draft(chat_id) or {},
            "messages": rows,
            "memories": r.hub.memories(chat_id),
            "history": {"next_before_id": earliest["id"] if more else None},
        }

    @app.get("/api/conversations/{chat_id}/history")
    async def conversation_history(
        chat_id: str,
        request: Request,
        before_id: int = Query(ge=1),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        return hub(request).hub.find_messages(
            chat_id=chat_id, before_id=before_id, limit=limit, chronological=True
        )

    @app.post("/api/conversations/{chat_id}/draft/clear")
    async def clear_conversation_draft(
        chat_id: str, body: ClearDraftBody, request: Request
    ):
        cleared = hub(request).hub.clear_reply_draft(chat_id, body.key, body.version)
        if not cleared:
            raise HTTPException(409, "草稿已更新，请刷新后重试")
        return {"cleared": True}

    @app.post("/api/actions/{action_id}/checked")
    async def confirm_send_checked(action_id: int, request: Request):
        return hub(request).hub.confirm_send_checked(action_id)

    @app.post("/api/conversations/{chat_id}/send")
    async def send(chat_id: str, body: SendBody, request: Request):
        return await hub(request).send(
            chat_id,
            body.content,
            body.request_id,
            body.anchor,
            manual=True,
            reply_to_message_id=body.reply_to_message_id,
            mention_user_id=body.mention_user_id,
        )

    @app.post("/api/conversations/{chat_id}/send-rich")
    async def send_rich(chat_id: str, body: RichSendBody, request: Request):
        return await hub(request).send_parts(
            chat_id, [p.model_dump() for p in body.parts], body.request_id, body.anchor
        )

    @app.post("/api/media/upload")
    async def upload_image(body: ImageUploadBody, request: Request):
        import base64, uuid
        from io import BytesIO
        from PIL import Image
        from ..config import DATA_DIR

        r = hub(request)
        if not r.client or r.connection != "connected":
            raise ValueError("飞书尚未连接")
        try:
            data = base64.b64decode(body.data, validate=True)
            maximum = r.hub.settings()["upload_max_mb"]
            if len(data) > maximum * 1024 * 1024:
                raise ValueError(f"图片最多 {maximum} MB")
            with Image.open(BytesIO(data)) as im:
                width, height = im.size
                fmt = im.format
                if (
                    fmt not in ("PNG", "JPEG", "GIF", "WEBP")
                    or width * height > 20000000
                ):
                    raise ValueError("图片格式或尺寸不支持")
                im.verify()
        except ValueError:
            raise
        except Exception:
            raise ValueError("无效的图片文件")
        uid = str(uuid.uuid4())
        folder = DATA_DIR / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            uid + "." + {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}[fmt]
        )
        path.write_bytes(data)
        try:
            key = await asyncio.to_thread(r.client.upload_image, path)
        except Exception:
            raise ValueError("图片上传失败，请重试")
        r.hub.set_setting(
            "upload:" + uid,
            json.dumps(
                {
                    "key": key,
                    "width": width,
                    "height": height,
                    "path": str(path),
                    "mime": Image.MIME[fmt],
                    "size": len(data),
                }
            ),
        )
        return {
            "upload_id": uid,
            "url": "/api/media/uploads/" + uid,
            "width": width,
            "height": height,
        }

    @app.get("/api/media/uploads/{upload_id}")
    async def upload_preview(upload_id: str, request: Request):
        asset = json.loads(hub(request).hub.setting("upload:" + upload_id, "{}"))
        if not asset.get("path"):
            raise HTTPException(404, "图片不存在")
        return FileResponse(asset["path"], media_type=asset["mime"])

    @app.get("/api/conversations/{chat_id}/media/{message_id}/{index}")
    async def message_image(
        chat_id: str, message_id: str, index: int, request: Request
    ):
        from ..media import display_parts, download, message_resource_url
        from ..storage.models import Message
        from ..config import DATA_DIR
        from PIL import Image
        from io import BytesIO
        import hashlib

        r = hub(request)
        if not r.client:
            raise HTTPException(503, "飞书尚未连接")
        with r.storage.Session() as session:
            row = (
                session.query(Message)
                .filter_by(chat_id=chat_id, msg_id=message_id)
                .first()
            )
            if not row:
                raise HTTPException(404, "消息不存在")
            parts = display_parts(row.msg_type, json.loads(row.content_data or "{}"))
        if index < 0 or index >= len(parts) or parts[index]["type"] != "image":
            raise HTTPException(404, "图片不存在")
        key = parts[index]["key"]
        folder = DATA_DIR / "images"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (
            hashlib.sha256((chat_id + message_id + key).encode()).hexdigest() + ".media"
        )
        if not path.exists():
            try:
                data, _ = await asyncio.to_thread(
                    download,
                    r.client.auth,
                    message_resource_url(
                        message_id, key, chat_id, r.client.auth.region.file_host
                    ),
                    max_bytes=20 * 1024 * 1024,
                )
                with Image.open(BytesIO(data)) as im:
                    im.verify()
                path.write_bytes(data)
            except Exception:
                raise HTTPException(502, "图片暂时无法加载")
        with Image.open(path) as im:
            mime = Image.MIME.get(im.format, "image/png")
        return FileResponse(path, media_type=mime)

    @app.post("/api/conversations/{chat_id}/polish")
    async def conversation_polish(chat_id: str, body: DraftBody, request: Request):
        r = hub(request)
        if chat_id not in {c["chat_id"] for c in r.hub.conversations()}:
            raise HTTPException(404, "会话不存在")
        return {
            "job_id": r.submit_job(
                "conversation_draft", body.model_dump_json(), chat_id
            )
        }

    @app.post("/api/conversations/{chat_id}/overview")
    async def overview(chat_id: str, request: Request):
        return {"job_id": hub(request).submit_job("overview", "", chat_id)}

    @app.get("/api/conversations/{chat_id}/images/{message_id}")
    async def image(chat_id: str, message_id: str, request: Request):
        from ..media import prepare_image
        from ..config import DATA_DIR
        from ..storage.models import Message

        r = hub(request)
        if not r.client:
            raise HTTPException(503, "飞书尚未连接")
        with r.storage.Session() as s:
            row = (
                s.query(Message)
                .filter_by(chat_id=chat_id, msg_id=message_id, msg_type=5)
                .first()
            )
            if not row:
                raise HTTPException(404, "图片不存在")
            msg = {
                "msg_id": row.msg_id,
                "chat_id": row.chat_id,
                "msg_type": row.msg_type,
                "content_data": json.loads(row.content_data or "{}"),
            }
        path = await asyncio.to_thread(
            prepare_image, r.client.auth, msg, DATA_DIR / "images"
        )
        return FileResponse(path, media_type="image/png")

    @app.patch("/api/personas/{key}")
    async def persona(key: str, request: Request):
        fields = await request.json()
        if not isinstance(fields, dict) or any(
            len(str(v)) > 10000 for v in fields.values()
        ):
            raise ValueError("角色描述过长或格式无效")
        return hub(request).hub.update_persona(key, fields)

    @app.post("/api/tasks")
    async def task_create(request: Request):
        fields = await request.json()
        if not fields.get("title") or len(fields["title"]) > 256:
            raise ValueError("请填写任务标题，最多 256 字符")
        return hub(request).hub.create_task(fields)

    @app.patch("/api/tasks/{task_id}")
    async def task_update(task_id: int, request: Request):
        return hub(request).hub.update_task(task_id, await request.json())

    @app.post("/api/tasks/{task_id}/reply")
    async def task_reply(task_id: int, body: SendBody, request: Request):
        r = hub(request)
        task = r.hub.task(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return await r.send(
            task["chat_id"], body.content, body.request_id, task["anchor"], manual=True
        )

    @app.post("/api/tasks/{task_id}/polish")
    async def task_polish(task_id: int, body: DraftBody, request: Request):
        r = hub(request)
        if not r.hub.task(task_id):
            raise HTTPException(404, "任务不存在")
        payload = json.dumps(
            {"task_id": task_id, "text": body.text, "instruction": body.instruction},
            ensure_ascii=False,
        )
        return {"job_id": r.submit_job("task_draft", payload)}

    @app.delete("/api/memories/{memory_id}")
    async def forget(memory_id: int, request: Request):
        hub(request).hub.forget_memory(memory_id)
        return {"ok": True}

    @app.get("/api/settings")
    async def read_settings(request: Request):
        r = hub(request)
        values, defaults, sources = r.hub.settings_info()
        from ..config import DATA_DIR, load_config
        from sqlalchemy.engine import make_url

        database = make_url(r.storage.url)
        return {
            "groups": GROUPS,
            "fields": settings_metadata(),
            "values": values,
            "defaults": defaults,
            "sources": sources,
            "limits": message_limits(values),
            "models": r.models.describe(),
            "startup": startup_info(request),
            "path_picker": picker.status(),
            "connection": {
                "飞书连接": r.connection,
                "数据目录": str(DATA_DIR),
                "存储类型": database.drivername,
                "数据库": database.database or "内存数据库",
                "服务地址": str(request.base_url).rstrip("/"),
                "登录区域": getattr(
                    getattr(getattr(r.client, "auth", None), "region", None),
                    "name",
                    load_config()["region"],
                ),
                "模型接入": ": ".join(r.models.active().values()),
            },
        }

    def startup_info(request):
        from ..config import DATA_DIR
        from ..startup import describe

        return describe(
            {
                "data_dir": str(DATA_DIR),
                "storage_url": hub(request).storage.url,
                "port": request.url.port or 80,
            }
        )

    @app.put("/api/settings/startup")
    async def save_startup(request: Request):
        from ..startup import check, save

        body = await request.json()
        await asyncio.to_thread(check, body)
        await asyncio.to_thread(save, body)
        return startup_info(request)

    @app.post("/api/settings/startup/reset")
    async def reset_startup(request: Request):
        from ..config import DATA_DIR
        from ..startup import save

        await asyncio.to_thread(
            save,
            {
                "data_dir": str(DATA_DIR),
                "storage_url": hub(request).storage.url,
                "port": request.url.port or 80,
            },
        )
        return startup_info(request)

    @app.post("/api/settings/startup/check")
    async def check_startup(request: Request):
        from ..startup import check

        body = await request.json()
        return await asyncio.to_thread(check, body)

    @app.post("/api/settings/path-picker")
    async def pick_path(request: Request):
        return await picker.pick(await request.json())

    @app.post("/api/connection/reconnect")
    async def reconnect(request: Request):
        await hub(request).reconnect()
        return {"ok": True}

    @app.post("/api/connection/login")
    async def start_login(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("登录参数格式无效")
        return await login.start(body.get("region"), hub(request), body.get("id"))

    @app.post("/api/connection/login/poll")
    async def poll_login(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("登录参数格式无效")
        return await login.poll(body.get("id"), hub(request))

    @app.post("/api/connection/login/cancel")
    async def cancel_login(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("登录参数格式无效")
        async with login.lock:
            if login.session and body.get("id") == login.id:
                login.clear()
        return {"ok": True}

    @app.patch("/api/settings")
    async def settings(request: Request):
        r = hub(request)
        values = r.hub.update_settings(await request.json())
        r.model_slots.configure(
            values["max_concurrency"], values["background_concurrency"]
        )
        return r.status()

    @app.put("/api/models/connection")
    async def save_model(request: Request):
        r = hub(request)
        r.models.save(await request.json())
        return r.models.describe()

    @app.post("/api/models/check")
    async def check_model(request: Request):
        r = hub(request)
        return await ModelRouter(r.models).check(await request.json())

    @app.post("/api/models/test")
    async def test_model(request: Request):
        r = hub(request)
        body = await request.json()
        try:
            async with r.model_slots.slot():
                return await ModelRouter(r.models).test(body, r.hub.settings())
        except RuntimeError as error:
            raise ValueError(str(error)) from None

    @app.post("/api/models/list")
    async def list_models(request: Request):
        r = hub(request)
        try:
            return await ModelRouter(r.models).list_models(await request.json())
        except RuntimeError as error:
            raise ValueError(str(error)) from None

    @app.post("/api/control")
    async def control(body: ControlBody, request: Request):
        return {"job_id": hub(request).submit_job("control", body.text)}

    @app.get("/api/control/history")
    async def control_history(request: Request):
        return hub(request).hub.control_history()

    @app.get("/api/jobs/{job_id}")
    async def job(job_id: str, request: Request):
        result = hub(request).hub.job(job_id)
        if not result:
            raise HTTPException(404, "处理记录不存在")
        if result["result"]:
            result["result"] = json.loads(result["result"])
        return result

    # Keep the catch-all mount last so it cannot hide existing API/UI routes.
    app.mount("/", mcp_app)
    return app


def run(port=None, *, stop_event=None):
    import uvicorn
    from ..config import STARTUP
    from ..startup import server_lock

    port = STARTUP["port"] if port is None else port
    with server_lock():
        if stop_event is None:
            uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="info")
            return

        import threading

        server = uvicorn.Server(
            uvicorn.Config(
                create_app(),
                host="127.0.0.1",
                port=port,
                log_level="info",
                timeout_graceful_shutdown=20,
            )
        )
        finished = threading.Event()

        def watch_stop():
            while not finished.wait(0.1):
                if stop_event.is_set():
                    server.should_exit = True
                    return

        monitor = threading.Thread(target=watch_stop, daemon=True)
        monitor.start()
        try:
            server.run()
        finally:
            finished.set()
            monitor.join(timeout=1)
