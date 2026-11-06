"""Backup: own copy of the live sources in the archive.

Claude Code deletes transcripts on its own (30 days after the last change,
unless configured otherwise) and with every session deleted in the app.
The chronicle therefore keeps a copy under archive/live/, and the index reads
only from that:

  live/claude-code/<project>/...   ~/.claude/projects/<project>/...
  live/desktop/<instance>/...      ~/.config/<instance>/claude-code-sessions/...
  live/cowork/<instance>/...       ~/.config/<instance>/local-agent-mode-sessions/...

Rules per file:
  new                                       copy
  grown, old content is the beginning       overwrite (JSONL is appended)
  changed otherwise or shrunk               keep old version as <name>.<date>, copy
  desktop metadata (local_*.json)           overwrite, no versions
  gone at the source                        copy stays, noted in live/backup.db
  deleted for good (gravestone)             no longer copied (cleanup.py)

Copies are made by reflink: original and copy share their blocks until one
side changes. The live sources are only read.
"""
import fcntl
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from collections import Counter
from contextlib import contextmanager
from fnmatch import fnmatch
from pathlib import Path

import chronicle_db
import sources

FICLONE = 0x40049409                       # ioctl from linux/fs.h
VERSION_RE = re.compile(r"\.\d{4}-\d\d-\d\dT\d{6}(-\d+)?$")
TMP = ".backup-tmp"
DB_NAME = "backup.db"
# Never copied, even if it lies under a backed-up path
NEVER = (".audit-key", ".credentials.json", ".claude.json", ".claude.json.*", "*.pem",
         "id_rsa*", "id_ed25519*", "id_ecdsa*", "cowork-*-cache.json")
NEVER_DIRS = ("skills-plugin", "rpm")      # only in local-agent-mode-sessions
COWORK_PARTS = (".claude/projects", "outputs", "uploads")
SUB = {"desktop": "claude-code-sessions", "cowork": "local-agent-mode-sessions"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gone(path TEXT PRIMARY KEY, since TEXT, deleted TEXT);
"""


# ---------------------------------------------------------------- Paths

def _never(name):
    return name.endswith(TMP) or any(fnmatch(name, p) for p in NEVER)


def _instances():
    return [p for p in sorted(sources.CLAUDE_CONFIG.glob("Claude*")) if p.is_dir()]


def source_of(target):
    """Path in the archive -> path of the live source; ValueError outside live/."""
    part = Path(target).relative_to(sources.LIVE_DIR).parts
    if len(part) > 1 and part[0] == "claude-code":
        return sources.CLAUDE_PROJECTS.joinpath(*part[1:])
    if len(part) > 2 and part[0] in SUB:
        return sources.CLAUDE_CONFIG.joinpath(part[1], SUB[part[0]], *part[2:])
    raise ValueError("not in %s: %s" % (sources.LIVE_DIR, target))


def _buried(target, graves):
    """Does the archive file belong to a chat deleted for good? Claude Code
    by session ID (transcript, versions, folder, release marker), desktop and
    Cowork by the local_ ID (metadata, Cowork folder)."""
    part = Path(target).relative_to(sources.LIVE_DIR).parts
    if part[0] == "claude-code":
        return len(part) > 2 and part[2].split(".", 1)[0] in graves
    return any(t.split(".", 1)[0] in graves for t in part[2:] if t.startswith("local_"))


def _tree(top):
    """All files under top (without following symlinks)."""
    for dirpath, _dirs, files in os.walk(top):
        for f in files:
            if not _never(f):
                yield Path(dirpath, f)


def _metadata():
    """Desktop metadata of all instances -> (source, target, overwrite_only)."""
    for inst in _instances():
        for kind, sub in SUB.items():
            base = inst / sub
            for dirpath, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in NEVER_DIRS and not d.startswith("local_")]
                for f in files:
                    if fnmatch(f, "local_*.json"):
                        src = Path(dirpath, f)
                        yield src, sources.LIVE_DIR / kind / inst.name / src.relative_to(base), True


def _all_files():
    """All files to back up -> (source, target, overwrite_only)."""
    root = sources.CLAUDE_PROJECTS
    for proj in sorted(p for p in root.glob("*") if p.is_dir() and not p.is_symlink()):
        with os.scandir(proj) as it:
            entries = sorted(it, key=lambda e: e.name)
        for e in entries:
            if e.is_file(follow_symlinks=False):
                if e.name.endswith((".jsonl", ".desktop-released.json")) and not _never(e.name):
                    yield Path(e.path), sources.PROJECTS / proj.name / e.name, False
            elif e.is_dir(follow_symlinks=False) and sources.SID_RE.match(e.name):
                for src in _tree(e.path):     # subagents/, tool-results/, custom-title.json
                    yield src, sources.PROJECTS / src.relative_to(root), False
    yield from _metadata()
    for inst in _instances():
        base = inst / "local-agent-mode-sessions"
        for dirpath, dirs, _files in os.walk(base):
            for d in dirs:
                if d.startswith("local_"):    # key, audit log and .claude.json stay out
                    for part in COWORK_PARTS:
                        for src in _tree(Path(dirpath, d, part)):
                            yield src, sources.COWORK / inst.name / src.relative_to(base), False
            dirs[:] = [d for d in dirs if d not in NEVER_DIRS and not d.startswith("local_")]


# ---------------------------------------------------------------- Copying

def copy_file(src, dst, st):
    """src to dst, by reflink if possible, mode and mtime as in st.

    First into a temporary file, then rename: whoever is reading the old copy
    reads it to the end."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + TMP)
    with open(src, "rb") as fi, open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fo:
        try:
            fcntl.ioctl(fo.fileno(), FICLONE, fi.fileno())
        except OSError:                   # other filesystem: copy normally
            shutil.copyfileobj(fi, fo, 1 << 20)
    os.chmod(tmp, stat.S_IMODE(st.st_mode))
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, dst)


def _is_prefix(short, long, n):
    """Are the first n bytes of long equal to the content of short?"""
    with open(short, "rb") as a, open(long, "rb") as b:
        while n > 0:
            x = a.read(min(n, 1 << 20))
            if not x or x != b.read(len(x)):
                return False
            n -= len(x)
    return True


def _version(dst, old):
    """Free name for the old version, dated to its last change."""
    base = "%s.%s" % (dst.name, time.strftime("%Y-%m-%dT%H%M%S", time.localtime(old.st_mtime)))
    name, k = base, 1
    while dst.with_name(name).exists():
        k += 1
        name = "%s-%d" % (base, k)
    return dst.with_name(name)


def _back_up(src, dst, overwrite_only):
    """Sync one file by the rules -> 'unchanged', 'new', 'grown',
    'versioned', 'meta' or None (source gone meanwhile or not a regular file)."""
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        old = os.stat(dst)
    except FileNotFoundError:
        old = None
    if old is None:
        kind = "new"
    elif (old.st_size, old.st_mtime_ns) == (st.st_size, st.st_mtime_ns):
        return "unchanged"
    elif overwrite_only:
        kind = "meta"
    elif old.st_size <= st.st_size and _is_prefix(dst, src, old.st_size):
        kind = "grown"
    else:
        os.rename(dst, _version(dst, old))
        kind = "versioned"
    copy_file(src, dst, st)
    return kind


# ---------------------------------------------------------------- Lock and notes

@contextmanager
def lock(wait):
    """Only one run at a time (timer, web service, MCP server). -> True, or False
    if the lock did not come free within wait seconds."""
    for d in (sources.ARCHIVE, sources.LIVE_DIR):
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fh = open(sources.LIVE_DIR / ".backup.lock", "a")
    try:
        end = time.time() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= end:
                    yield False
                    return
                time.sleep(0.1)
        yield True
    finally:
        fh.close()


def _db():
    c = sqlite3.connect(str(sources.LIVE_DIR / DB_NAME), timeout=60)
    c.executescript(SCHEMA_SQL)
    return c


def _released(rel):
    """releasedAt of the release marker, if the session was deleted in the app."""
    part = rel.split("/")
    sid = None
    if part[0] == "claude-code" and len(part) > 2:
        sid = part[2][:-6] if part[2].endswith(".jsonl") else part[2]
    elif part[0] == "desktop":
        try:
            sid = json.loads((sources.LIVE_DIR / rel).read_text(encoding="utf-8")).get("cliSessionId")
        except (OSError, ValueError, AttributeError):
            pass
    if not sid or not sources.SID_RE.match(sid):
        return None
    for root in (sources.PROJECTS, sources.CLAUDE_PROJECTS):
        for p in root.glob("*/%s.desktop-released.json" % sid):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and d.get("reason") == "delete":
                return d.get("releasedAt") or "?"
    return None


def _note_gone(seen, n):
    """Find copies whose source has disappeared and note them in backup.db.
    If the source shows up again (say from a snapshot), the note is dropped."""
    gone = set()
    for top in [sources.PROJECTS] + sorted(sources.DESKTOP.glob("*")) + sorted(sources.COWORK.glob("*")):
        for dirpath, _dirs, files in os.walk(top):
            for f in files:
                p = os.path.join(dirpath, f)
                if f.endswith(TMP):          # leftover of an aborted run
                    os.unlink(p)
                    continue
                if p in seen or VERSION_RE.search(f):
                    continue
                if not os.path.lexists(source_of(p)):
                    gone.add(Path(p).relative_to(sources.LIVE_DIR).as_posix())
    c = _db()
    try:
        old = {r[0]: r[2] for r in c.execute("SELECT path, since, deleted FROM gone")}
        c.executemany("DELETE FROM gone WHERE path=?", [(p,) for p in set(old) - gone])
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        for p in sorted(gone):
            if p not in old:
                c.execute("INSERT INTO gone VALUES(?,?,?)", (p, now, _released(p)))
                n["gone new"] += 1
            elif old[p] is None:             # marker may come after the file
                c.execute("UPDATE gone SET deleted=? WHERE path=?", (_released(p), p))
        c.commit()
    finally:
        c.close()
    n["gone"] = len(gone)


def gone_list():
    """Noted copies whose source has disappeared -> [(path, since, deleted)]."""
    if not (sources.LIVE_DIR / DB_NAME).exists():
        return []
    c = _db()
    try:
        return c.execute("SELECT path, since, deleted FROM gone ORDER BY since, path").fetchall()
    finally:
        c.close()


# ---------------------------------------------------------------- Runs

def run(wait=600):
    """Sync all live sources with archive/live -> Counter per case, or None
    if another run did not release the lock."""
    with lock(wait) as free:
        if not free:
            return None
        n, seen, graves = Counter(), set(), chronicle_db.gravestones()
        for src, dst, meta in _all_files():
            if graves and _buried(dst, graves):
                n["buried"] += 1
                continue
            seen.add(str(dst))
            try:
                kind = _back_up(src, dst, meta)
            except OSError as e:
                kind = "errors"
                print("Error backing up %s: %r" % (src, e), file=sys.stderr, flush=True)
            if kind:
                n[kind] += 1
        _note_gone(seen, n)
        return n


def backup_file(target, wait=10):
    """Before a chat is read, back up only its file and the desktop metadata,
    so a running session can still be followed as before."""
    try:
        src = source_of(target)
    except ValueError:
        return
    try:                    # if it fails, the existing copy is read instead
        with lock(wait) as free:
            if free:
                graves = chronicle_db.gravestones()
                if not _buried(target, graves):
                    _back_up(src, Path(target), False)
                for s, d, meta in _metadata():
                    if not _buried(d, graves):
                        _back_up(s, d, meta)
    except (OSError, sqlite3.Error) as e:
        print("Error backing up %s: %r" % (src, e), file=sys.stderr, flush=True)
