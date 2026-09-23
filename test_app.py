import sqlite3

def test_database_is_sqlite():
    with sqlite3.connect(":memory:") as db:
        assert db.execute("select 1").fetchone()[0] == 1
