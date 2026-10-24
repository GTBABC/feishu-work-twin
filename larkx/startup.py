"""Restart-only configuration, stored outside the selectable data directory."""

import json
import os
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from .secrets import SecretVault


def config_path():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    path = Path(
        os.environ.get("LARKX_STARTUP_PATH") or base / "LarkAgentX" / "startup.json"
    ).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    return path.resolve()


def runtime_dir():
    """Keep runtime identity in one installation, independent of user AppData views."""
    if os.environ.get("LARKX_STARTUP_PATH"):
        return config_path().expanduser().resolve().parent
    return Path(__file__).resolve().parents[1] / "data" / "run"


@contextmanager
def server_lock():
    """Changing the port must not start a second message receiver."""
    folder = runtime_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "server.lock").open("a+b") as lock:
        if not os.fstat(lock.fileno()).st_size:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("工作台已在运行，请先停止原服务再重启") from None
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_saved():
    path = config_path()
    if not path.exists():
        return {}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError()
        if "storage_secret" in saved:
            saved["storage_url"] = SecretVault(path.parent).decrypt(
                saved.pop("storage_secret")
            )
        return saved
    except (OSError, ValueError, TypeError):
        raise RuntimeError("无法读取启动配置，请检查 startup.json") from None


def effective():
    saved = read_saved()
    directory = (
        saved.get("data_dir")
        or os.environ.get("LARKX_HOME")
        or str(Path(__file__).resolve().parents[1] / "data")
    )
    directory = str(Path(directory).expanduser().resolve())
    return {
        "data_dir": directory,
        "storage_url": saved.get("storage_url")
        or os.environ.get("LARKX_STORAGE_URL")
        or f"sqlite:///{Path(directory) / 'messages.db'}",
        "port": saved.get("port", int(os.environ.get("LARKX_PORT") or 8765)),
    }


def describe(current):
    desired = effective()
    changed = desired != current
    url = make_url(desired["storage_url"])
    current = dict(current)
    current["storage_url"] = make_url(current["storage_url"]).render_as_string(
        hide_password=True
    )
    values = {**desired, "storage_url": url.render_as_string(hide_password=True)}
    return {
        "values": values,
        "current": current,
        "storage": {
            "storage_kind": "sqlite" if url.drivername == "sqlite" else "mysql",
            "sqlite_path": url.database if url.drivername == "sqlite" else "",
            "mysql_host": url.host or "localhost",
            "mysql_port": url.port or 3306,
            "mysql_database": url.database if url.drivername != "sqlite" else "",
            "mysql_user": url.username or "",
        },
        "password_set": bool(url.password),
        "restart_required": changed,
    }


def prepare(body):
    if not isinstance(body, dict) or body.keys() - {
        "data_dir",
        "storage_url",
        "port",
        "password",
        "storage_kind",
        "sqlite_path",
        "mysql_host",
        "mysql_port",
        "mysql_database",
        "mysql_user",
    }:
        raise ValueError("启动配置格式无效")
    structured = body.keys() & {
        "storage_kind",
        "sqlite_path",
        "mysql_host",
        "mysql_port",
        "mysql_database",
        "mysql_user",
    }
    if structured:
        if "storage_url" in body:
            raise ValueError("请勿同时提交数据库连接地址和分项配置")
        kind = body.get("storage_kind")
        if kind == "sqlite":
            if structured - {"storage_kind", "sqlite_path"}:
                raise ValueError("SQLite 配置不应包含 MySQL 字段")
            database = body.get("sqlite_path")
            if not isinstance(database, str) or not database.strip():
                raise ValueError("请填写数据库文件路径")
            address = URL.create("sqlite", database=database).render_as_string(
                hide_password=False
            )
        elif kind == "mysql":
            if "sqlite_path" in body:
                raise ValueError("MySQL 配置不应包含 SQLite 路径")
            fields = [
                body.get(key) for key in ("mysql_host", "mysql_database", "mysql_user")
            ]
            if any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 4096
                or any(c in value for c in "\r\n\x00")
                for value in fields
            ):
                raise ValueError("请填写 MySQL 主机、数据库名和账号")
            if any(c in fields[0].strip() for c in "/\\@?#[] \t"):
                raise ValueError("数据库主机请填写主机名或 IP，端口在下方单独填写")
            mysql_port = body.get("mysql_port", 3306)
            if type(mysql_port) is not int or not 1 <= mysql_port <= 65535:
                raise ValueError("MySQL 端口必须是 1 到 65535 的整数")
            previous = make_url(effective()["storage_url"])
            address = URL.create(
                "mysql+pymysql",
                host=fields[0].strip(),
                database=fields[1].strip(),
                username=fields[2].strip(),
                port=mysql_port,
                password=previous.password
                if previous.drivername == "mysql+pymysql"
                else None,
            ).render_as_string(hide_password=False)
        else:
            raise ValueError("请选择 SQLite 或 MySQL")
        body = {key: value for key, value in body.items() if key not in structured}
        body["storage_url"] = address
    values = {**effective(), **body}
    port = values["port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("服务端口必须是 1024 到 65535 的整数")
    directory = values["data_dir"]
    address = values["storage_url"]
    if any(
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4096
        or any(c in value for c in "\r\n\x00")
        for value in (directory, address)
    ):
        raise ValueError("请填写有效的数据目录和数据库地址")
    path = Path(directory).expanduser()
    if not path.is_absolute() or (path.exists() and not path.is_dir()):
        raise ValueError("数据目录必须是文件夹的绝对路径")
    try:
        url = make_url(address)
        if url.drivername not in ("sqlite", "mysql+pymysql") or url.query:
            raise ValueError()
        if url.drivername == "sqlite":
            if (
                url.host
                or url.username
                or url.password
                or not url.database
                or not Path(url.database).is_absolute()
            ):
                raise ValueError()
            database = Path(url.database)
            if database.exists() and not database.is_file():
                raise ValueError()
        elif not url.host or not url.username or not url.database:
            raise ValueError()
        elif url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError()
    except (ValueError, TypeError, ArgumentError):
        raise ValueError(
            "SQLite 请填写绝对文件路径；MySQL 请检查主机、端口、数据库名和账号"
        ) from None
    password = body.get("password", "")
    if not isinstance(password, str) or len(password) > 4096 or "\x00" in password:
        raise ValueError("数据库密码格式无效")
    if url.drivername == "mysql+pymysql":
        if password:
            url = url.set(password=password)
        elif url.password == "***":
            previous = make_url(effective()["storage_url"])
            url = url.set(password=previous.password or "")
    return {
        "data_dir": str(path.resolve()),
        "storage_url": url.render_as_string(hide_password=False),
        "port": port,
    }


def save(body):
    values = prepare(body)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {key: values[key] for key in ("data_dir", "port")}
    # Encrypt the complete connection string, including credentials.
    stored["storage_secret"] = SecretVault(path.parent).encrypt(values["storage_url"])
    descriptor, temporary = tempfile.mkstemp(
        prefix="startup-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(stored, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check(body):
    """Check a destination without creating a database or its schema."""
    values = prepare(body)
    parent = Path(values["data_dir"])
    while not parent.exists():
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise ValueError("数据目录不可写")
    url = make_url(values["storage_url"])
    if url.drivername == "sqlite":
        import sqlite3

        path = Path(url.database)
        if path.exists():
            try:
                with closing(
                    sqlite3.connect(
                        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3
                    )
                ) as connection:
                    connection.execute("PRAGMA schema_version").fetchone()
            except sqlite3.Error:
                raise ValueError("无法读取此 SQLite 数据库") from None
        else:
            parent = path.parent
            while not parent.exists():
                parent = parent.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                raise ValueError("数据库目录不可写")
        return {
            "ok": True,
            "message": "数据库可读取"
            if path.exists()
            else "目录可写，首次启动时创建数据库",
        }
    from sqlalchemy import create_engine, text

    engine = None
    try:
        engine = create_engine(
            url,
            connect_args={"connect_timeout": 5, "read_timeout": 5, "write_timeout": 5},
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except ImportError:
        raise ValueError("请先安装 MySQL 支持：uv sync --extra mysql") from None
    except Exception:
        raise ValueError("数据库连接失败，请检查地址、账号和权限") from None
    finally:
        if engine is not None:
            engine.dispose()
    return {"ok": True, "message": "数据库连接成功"}
