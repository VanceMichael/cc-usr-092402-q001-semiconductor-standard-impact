"""数据库连接与迁移执行。迁移脚本按文件名版本号递增执行，可重复运行。"""
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def connect(db_path):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def applied_version(db):
    try:
        row = db.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return row["v"] or 0


def migrate(db_path):
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(db_path)
    try:
        current = applied_version(db)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version <= current:
                continue
            db.executescript(path.read_text(encoding="utf-8"))
            current = version
        return current
    finally:
        db.close()
