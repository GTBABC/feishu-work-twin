import os
import hashlib
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlencode
import requests
from loguru import logger
from .auth import LarkAuth
from .config import load_config
from .region import get_region


def message_resource_url(msg_id: str, key: str, chat_id: str, host: str = None) -> str:
    """host 为所属区域的文件主机(LarkAuth.region.file_host),不传时取 LARKX_REGION。"""
    host = host or get_region(load_config()["region"]).file_host
    return f"{host}/download/messages/{quote(str(msg_id), safe='')}/keys/{quote(key, safe='')}?{urlencode({'chat_id': chat_id})}"


def extract_resource_key(msg_type: int, content_data: dict) -> str:
    if not content_data:
        return ""
    if msg_type == 5:
        v2 = content_data.get("imageV2") or {}
        if v2.get("imageKey"):
            return v2["imageKey"]
        img = (content_data.get("image") or {}).get("origin") or {}
        return img.get("key") or (content_data.get("image") or {}).get("imageKey", "")
    if msg_type in (3, 15, 7, 10):
        return content_data.get("key", "")
    return ""


def download(
    auth: LarkAuth, url: str, out_path: str = None, max_bytes: int = 200 * 1024 * 1024
):
    with requests.get(url, cookies=auth.cookies, timeout=60, stream=True) as r:
        r.raise_for_status()
        content_type = r.headers.get("content-type", "")
        if not out_path:
            buf = b""
            for chunk in r.iter_content(1 << 16):
                buf += chunk
                if len(buf) > max_bytes:
                    raise ValueError("文件过大")
            return (buf, content_type)
        n = 0
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                n += len(chunk)
                if n > max_bytes:
                    raise ValueError("文件过大")
        return (out_path, content_type, n)


def download_message_resource(auth: LarkAuth, msg: dict, out_path: str = None):
    key = extract_resource_key(msg.get("msg_type", 0), msg.get("content_data") or {})
    if not key:
        raise ValueError("该消息没有可下载的资源")
    url = message_resource_url(
        msg["msg_id"], key, msg["chat_id"], host=auth.region.file_host
    )
    logger.info(f"下载资源: {url}")
    return download(auth, url, out_path)


def prepare_image(auth: LarkAuth, msg: dict, cache_dir: Path) -> str:
    """Download authenticated message pixels, validate and cache as a Codex input."""
    from PIL import Image, ImageOps

    key = extract_resource_key(msg.get("msg_type", 0), msg.get("content_data") or {})
    if not key:
        raise ValueError("图片没有资源 key")
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(
        f"{msg['chat_id']}:{msg['msg_id']}:{key}".encode()
    ).hexdigest()
    target = cache_dir / f"{name}.png"
    if target.exists():
        return str(target.resolve())
    url = message_resource_url(
        msg["msg_id"], key, msg["chat_id"], host=auth.region.file_host
    )
    data, _ = download(auth, url, max_bytes=20 * 1024 * 1024)
    with Image.open(BytesIO(data)) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((2048, 2048))
        image.save(target, format="PNG")
    logger.info(f"图片已准备 [{msg['chat_id']}] bytes={len(data)}")
    return str(target.resolve())


def display_parts(msg_type, data):
    """Keep renderable media metadata before protobuf property bytes are discarded."""
    if "_display_parts" in data:
        return data["_display_parts"]
    if msg_type in (5, 10):
        key = extract_resource_key(msg_type, data)
        return [{"type": "image", "key": key, "sticker": msg_type == 10}] if key else []
    if msg_type not in (2, 4):
        return []
    from .proto import proto_pb2 as P
    from .proto.gateway import resolve_type

    rich = data.get("richText") or {}
    elements = (rich.get("elements") or {}).get("dictionary") or {}
    parts, seen = [], set()

    def visit(eid):
        if eid in seen or eid not in elements:
            return
        seen.add(eid)
        e = elements[eid]
        tag = e.get("tag")
        raw = e.get("property")
        if isinstance(raw, (bytes, bytearray)):
            try:
                if tag == 2:
                    prop = resolve_type("entities.RichTextElement.ImageProperty")()
                    prop.ParseFromString(raw)
                    key = prop.token or prop.originKey
                    if key:
                        parts.append(
                            {
                                "type": "image",
                                "key": key,
                                "width": prop.width or prop.originWidth,
                                "height": prop.height or prop.originHeight,
                                "display_width": prop.customWidth,
                            }
                        )
                elif tag == 5:
                    prop = P.AtProperty()
                    prop.ParseFromString(raw)
                    parts.append({"type": "text", "text": prop.content or "@"})
                elif tag in (1, 6, 7, 8, 9, 17):
                    prop = P.TextProperty()
                    prop.ParseFromString(raw)
                    if prop.content:
                        parts.append({"type": "text", "text": prop.content})
                elif tag == 10:
                    # Unicode emoji text is rendered naturally; unsupported custom reactions retain a placeholder.
                    parts.append({"type": "text", "text": "[表情]"})
            except Exception:
                pass
        for child in e.get("childIds") or []:
            visit(child)
        if tag in (3, 4, 14) and parts and parts[-1].get("text") != "\n":
            parts.append({"type": "text", "text": "\n"})

    roots = rich.get("elementIds") or list(elements)
    for eid in roots:
        visit(eid)
    return parts


def image_messages(msg):
    """Return each image in a post in the same shape as a standalone image."""
    if msg.get("msg_type") == 5:
        return [msg]
    if msg.get("msg_type") != 2:
        return []
    images = []
    for part in display_parts(2, msg.get("content_data") or {}):
        if part["type"] == "image":
            images.append(
                dict(
                    msg,
                    msg_type=5,
                    content_data={"image": {"origin": {"key": part["key"]}}},
                )
            )
    return images[:3]
