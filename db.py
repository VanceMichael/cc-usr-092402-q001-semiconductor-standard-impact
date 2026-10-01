"""数据库连接、迁移与时间戳工具。"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DEFAULT_DB_PATH = "data/app.db"


def db_path() -> str:
    return os.environ.get("APP_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """按文件名顺序应用 migrations/ 下尚未执行的脚本，可重复执行。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER PRIMARY KEY, "
        "applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(script.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(script.read_text(encoding="utf-8"))
        conn.execute("INSERT OR IGNORE INTO schema_version(version) VALUES (?)", (version,))
    conn.commit()


def new_id() -> str:
    return uuid.uuid4().hex


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(value: str) -> str:
    """把外部时间戳规范为 UTC ISO 格式，保证库内字符串可比较。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")
