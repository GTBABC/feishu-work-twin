import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from .startup import effective

# Freeze startup settings for this process; a web save takes effect on restart.
STARTUP = effective()
DATA_DIR = Path(STARTUP["data_dir"])
CREDENTIALS_PATH = DATA_DIR / "credentials.json"
DB_DEFAULT_PATH = DATA_DIR / "messages.db"

DEFAULTS = {
    "region": "feishu",
    "storage_url": STARTUP["storage_url"],
    "context_scope": "anchor",
    "agent_backend": "claude",
    "system_prompt": "",
    "mark_read": True,
    "triggers": {
        "prefix": "",
        "chat_types": [],
        "include_chats": [],
        "exclude_chats": [],
        "include_senders": [],
        "keywords": [],
        "group_at_only": False,
    },
}

TRIGGER_ENV = {
    "prefix": "LARKX_TRIGGER_PREFIX",
    "chat_types": "LARKX_TRIGGER_CHAT_TYPES",
    "include_chats": "LARKX_TRIGGER_INCLUDE_CHATS",
    "exclude_chats": "LARKX_TRIGGER_EXCLUDE_CHATS",
    "include_senders": "LARKX_TRIGGER_INCLUDE_SENDERS",
    "keywords": "LARKX_TRIGGER_KEYWORDS",
    "group_at_only": "LARKX_TRIGGER_GROUP_AT_ONLY",
}


def _as_list(v: str):
    v = v.strip()
    if v.startswith("["):
        try:
            return json.loads(v)
        except Exception:
            return []
    return [x.strip() for x in v.split(",") if x.strip()]


def _as_bool(v: str):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for key in cfg:
        if key in ("triggers", "storage_url"):
            continue
        v = os.environ.get("LARKX_" + key.upper())
        if v:
            cfg[key] = v
    if isinstance(cfg.get("mark_read"), str):
        cfg["mark_read"] = cfg["mark_read"].strip().lower() in ("1", "true", "yes", "on")
    triggers = dict(DEFAULTS["triggers"])
    for key, env_name in TRIGGER_ENV.items():
        v = os.environ.get(env_name)
        if v is None:
            continue
        if key == "group_at_only":
            triggers[key] = _as_bool(v)
        elif isinstance(DEFAULTS["triggers"][key], list):
            triggers[key] = _as_list(v)
        else:
            triggers[key] = v
    cfg["triggers"] = triggers
    return cfg
