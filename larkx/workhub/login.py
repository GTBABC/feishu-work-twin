"""On-demand browser login; no background polling after the dialog closes."""

import asyncio
import base64
import time
import uuid
from io import BytesIO

import qrcode

from ..auth import LarkAuth, QrLogin
from ..config import DATA_DIR
from ..region import get_region


class BrowserLogin:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.session = None

    def clear(self):
        if self.session:
            self.session.session.close()
        self.session = None

    async def start(self, region, runtime, identity=None):
        if region not in ("feishu", "lark"):
            raise ValueError("请选择飞书或 Lark")
        if identity is not None:
            try:
                uuid.UUID(identity)
            except (ValueError, TypeError, AttributeError):
                raise ValueError("登录请求格式无效") from None
        async with self.lock:
            runtime.require_idle_connection()
            self.clear()
            try:
                self.session = await asyncio.to_thread(QrLogin, get_region(region))
            except Exception:
                raise ValueError("获取二维码失败，请检查网络后重试") from None
            self.id = identity or uuid.uuid4().hex
            self.deadline = time.monotonic() + 180
            buffer = BytesIO()
            qrcode.make(self.session.qr_content).save(buffer, format="PNG")
            return {
                "id": self.id,
                "image": "data:image/png;base64,"
                + base64.b64encode(buffer.getvalue()).decode(),
            }

    async def poll(self, identity, runtime):
        async with self.lock:
            if (
                not self.session
                or identity != self.id
                or time.monotonic() > self.deadline
            ):
                raise ValueError("二维码已失效，请重新获取")
            try:
                data = await asyncio.to_thread(self.session.poll)
                step = data.get("next_step")
                status = (data.get("step_info") or {}).get("status")
                if status in (3, 4, 5):
                    self.clear()
                    raise ValueError("二维码已失效，请重新获取")
                if (
                    step == "qr_login_polling"
                    and not self.session.session.cookies.get("session")
                    and status != 0
                ):
                    return {
                        "done": False,
                        "message": "请在手机上确认" if status == 2 else "等待扫码",
                    }
                runtime.require_idle_connection()
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                temporary = DATA_DIR / ("credentials.login-" + self.id + ".json")
                try:
                    auth = LarkAuth(path=temporary)
                    await asyncio.to_thread(self.session.finish, auth)
                    await runtime.reconnect(credentials=temporary)
                finally:
                    temporary.unlink(missing_ok=True)
                self.clear()
                return {"done": True, "message": "登录成功，正在连接"}
            except ValueError:
                raise
            except Exception:
                self.clear()
                raise ValueError("登录失败，请重新获取二维码") from None
