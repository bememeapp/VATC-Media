import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DATA = Path(os.getenv("DATA_DIR", "data")).resolve()
RETENTION = max(1, int(os.getenv("RETENTION_HOURS", "24"))) * 3600


@contextmanager
def db():
    conn = sqlite3.connect(DATA / "queue.sqlite", timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY, batch TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS posts_batch ON posts(batch);
          CREATE TABLE IF NOT EXISTS usage(day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(day,kind));
        """)
        # Do not blindly repeat a paid request interrupted by a process restart.
        for row in c.execute("SELECT id,data FROM posts").fetchall():
            post = json.loads(row["data"])
            if post["status"] in {"analysing", "editing"}:
                post.update(status="failed", error="Processing was interrupted by a server restart. Check usage before retrying.")
                c.execute("UPDATE posts SET data=? WHERE id=?", (json.dumps(post), row["id"]))


def save(post):
    post["updated"] = time.time()
    with db() as c:
        c.execute("UPDATE posts SET data=? WHERE id=?", (json.dumps(post), post["id"]))


def get(post_id):
    with db() as c:
        row = c.execute("SELECT data FROM posts WHERE id=?", (post_id,)).fetchone()
    return json.loads(row["data"]) if row else None


def claim():
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        for row in c.execute("SELECT posts.data,batches.created AS batch_created FROM posts JOIN batches ON batches.id=posts.batch ORDER BY posts.created").fetchall():
            post = json.loads(row["data"])
            if post["status"] == "queued":
                if row["batch_created"] + RETENTION < time.time():
                    post.update(status="failed",error="This batch expired before processing started.")
                    c.execute("UPDATE posts SET data=? WHERE id=?", (json.dumps(post),post["id"]))
                    continue
                post.update(status="analysing", updated=time.time())
                c.execute("UPDATE posts SET data=? WHERE id=?", (json.dumps(post), post["id"]))
                return post


def consume(kind):
    import datetime
    limit = int(os.getenv("MAX_DAILY_IMAGE_EDITS" if kind == "image" else "MAX_DAILY_TEXT_CALLS", "400" if kind == "image" else "800"))
    day = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT count FROM usage WHERE day=? AND kind=?", (day,kind)).fetchone()
        count = row["count"] if row else 0
        if count >= limit:
            raise ValueError("The dashboard's daily generation limit has been reached. It resets at midnight UTC.")
        c.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(day,kind) DO UPDATE SET count=count+1", (day,kind))


def directory(post):
    return DATA / post["batch"] / post["id"]
