"""What Cloud sync has sent, in a small SQLite base (~/.local/share/pycam/cloudsync.db).

A file counts as sent to an account when its size and modification time are those it had
when it was sent there and checked: a file changed since is sent again, and so is
everything for another account.
"""

import os
import sqlite3
import time


def default_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "pycam", "cloudsync.db")


class SentFiles:
    def __init__(self, path=None):
        path = path or default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sent (path TEXT, identity TEXT, size INTEGER, mtime REAL, "
            "checksum TEXT, sent_at REAL, PRIMARY KEY (path, identity))"
        )
        self.db.commit()

    def close(self):
        self.db.close()

    def is_sent(self, path, identity, size, mtime):
        row = self.db.execute("SELECT size, mtime FROM sent WHERE path = ? AND identity = ?", (path, identity)).fetchone()
        return row is not None and row[0] == size and abs(row[1] - mtime) < 1e-3

    def mark(self, path, identity, size, mtime, checksum):
        self.db.execute(
            "INSERT OR REPLACE INTO sent VALUES (?, ?, ?, ?, ?, ?)", (path, identity, size, mtime, checksum, time.time())
        )
        self.db.commit()

    def sent_files(self, identity):
        """{path: (size, mtime)} of what was sent to an account: one query for a whole card."""
        rows = self.db.execute("SELECT path, size, mtime FROM sent WHERE identity = ?", (identity,))
        return {path: (size, mtime) for path, size, mtime in rows}

    def count(self, identity):
        return self.db.execute("SELECT COUNT(*) FROM sent WHERE identity = ?", (identity,)).fetchone()[0]
