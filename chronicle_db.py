"""chronicle.db: what cost money or work and can't be rebuilt.

Lives next to live/ in the archive (ARCHIVE/chronicle.db), so in the same snapshots
and backups as the copies:

  digests      digests (level 1) per chat ID
  clutter      chats marked as clutter: gone from list, search, MCP server and
               digest runs, but unchanged in the archive
  gravestones  chats deleted for good; backup, index and import bring back
               nothing listed here (gravestones())
  ids          pinned chat IDs per platform ID; ingest.py uses them instead of the
               derived ones (used for chats that got random IDs from an older
               converter), so digests and gravestones stay on the same chats

The index (chat_index.py) attaches the file as schema 'chronicle', so clutter
drops out in the same queries as earlier transcripts.
"""
import json
import sqlite3
import threading
import time

import sources

PATH = sources.ARCHIVE / "chronicle.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS digests(id TEXT PRIMARY KEY, text TEXT, model TEXT,
    basis_size INTEGER, basis_items INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS clutter(id TEXT PRIMARY KEY, since TEXT);
CREATE TABLE IF NOT EXISTS gravestones(id TEXT PRIMARY KEY, platform TEXT, title TEXT, deleted TEXT,
    local TEXT, original TEXT, files TEXT);
CREATE TABLE IF NOT EXISTS ids(platform TEXT, original TEXT, id TEXT, PRIMARY KEY(platform, original));
"""

_ready = False
_lock = threading.Lock()


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def ready():
    """Once per process: create file and tables."""
    global _ready
    with _lock:
        if _ready:
            return
        PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        c = sqlite3.connect(str(PATH), timeout=60)
        try:
            c.executescript(SCHEMA_SQL)
        finally:
            c.close()
        _ready = True


def conn():
    ready()
    c = sqlite3.connect(str(PATH), timeout=60)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- Clutter

def clutter():
    """id -> since when marked as clutter."""
    c = conn()
    try:
        return {r["id"]: r["since"] for r in c.execute("SELECT id, since FROM clutter")}
    finally:
        c.close()


def mark(ids):
    """Mark chats as clutter -> number newly marked."""
    c = conn()
    try:
        n = c.executemany("INSERT OR IGNORE INTO clutter VALUES(?,?)", [(i, now()) for i in ids]).rowcount
        c.commit()
        return n
    finally:
        c.close()


def unmark(ids):
    """Remove the clutter mark -> number."""
    c = conn()
    try:
        n = c.executemany("DELETE FROM clutter WHERE id=?", [(i,) for i in ids]).rowcount
        c.commit()
        return n
    finally:
        c.close()


# ---------------------------------------------------------------- Gravestones

def gravestones():
    """Keys of all chats deleted for good: chat IDs, the local_ ID of their
    desktop or Cowork metadata and, for exports, the platform's ID. Whatever
    occurs in here, backup, index and import don't bring back."""
    c = conn()
    try:
        return {x for r in c.execute("SELECT id, local, original FROM gravestones") for x in r if x}
    finally:
        c.close()


def gravestone_list(sid=None):
    """Gravestones, newest first; with sid only its own (empty list if none)."""
    c = conn()
    try:
        if sid:
            return c.execute("SELECT * FROM gravestones WHERE id=?", (sid,)).fetchall()
        return c.execute("SELECT * FROM gravestones ORDER BY deleted DESC").fetchall()
    finally:
        c.close()


def bury(stones):
    """Set gravestones; the chats' clutter mark and digest go away.

    stones: [{id, platform, title, local, original, files}], files as text."""
    c = conn()
    try:
        for s in stones:
            c.execute("INSERT OR REPLACE INTO gravestones VALUES(?,?,?,?,?,?,?)",
                      (s["id"], s["platform"], s["title"], now(), s.get("local"), s.get("original"),
                       json.dumps(s["files"], ensure_ascii=False)))
            c.execute("DELETE FROM clutter WHERE id=?", (s["id"],))
            c.execute("DELETE FROM digests WHERE id=?", (s["id"],))
        c.commit()
    finally:
        c.close()


# ---------------------------------------------------------------- Earlier IDs

def ids(platform):
    """original_id -> chat ID, as an earlier converter assigned it."""
    c = conn()
    try:
        return {r["original"]: r["id"] for r in c.execute("SELECT original, id FROM ids WHERE platform=?",
                                                          (platform,))}
    finally:
        c.close()
