"""Cleanup: delete for good and suggestions for clutter.

Three steps, each triggered deliberately (docs/how-it-works.md):

  clutter      mark in chronicle.db (chronicle_db.py); the chat is missing from list,
               search, MCP server and digest runs, but stays in the archive
  delete       clutter only: the copy in the archive or the corpus JSON disappears,
               a gravestone keeps backup, index and import from bringing it
               back
  suggestions  candidates for clutter; nothing is marked here

A session takes its earlier transcripts with it (discarded states after a
jump back, worthless without the session). Forks are sessions of their own and
stay; the index then takes them in along with the shared beginning.

The live sources (~/.claude, ~/.config/Claude*) and raw exports (archive/exports)
are not touched by anything here.
"""
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import backup
from . import chat_index
from . import chronicle_db
from . import sources
from .digests import MIN_CHARS

# The German labels come from digests written before the English version
RESULT_OPEN = re.compile(r"(?im)^\W*(?:Result|Ergebnis)\W*:\W*(?:open|offen)\b")
NO_DECISIONS = re.compile(r"(?im)^\W*(?:Decisions|Entscheidungen)\W*:[\s*-]*(?:none|keine)[.!]?\s*$")


# ---------------------------------------------------------------- Delete for good

def _desktop_meta(sid):
    """Desktop metadata (archive copy) whose cliSessionId is the session."""
    for p in sources.DESKTOP.glob("*/**/local_*.json"):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("cliSessionId") == sid:
                yield p
        except (OSError, ValueError, AttributeError):
            continue


def _stone(row):
    """Gravestone of a chat along with the files that disappear with it."""
    sid, plat = row["id"], row["platform"]
    s = {"id": sid, "platform": plat, "title": row["title"], "local": None, "original": None, "files": []}
    if plat in sources.LIVE and not sources.SID_RE.match(sid):
        raise ValueError("Unexpected chat ID: %r" % sid)
    if plat == "claude-code":            # transcript, old versions, session folder, release marker
        s["files"] = sorted(p for p in sources.PROJECTS.glob("*/%s*" % sid)
                            if p.name == sid or p.name.startswith(sid + "."))
        for p in _desktop_meta(sid):
            s["files"].append(p)
            s["local"] = p.stem
    elif plat == "cowork":               # the whole local_ folder: all turns, outputs/, uploads/
        local = next((q for q in Path(row["path"]).parents if q.name.startswith("local_")), None)
        if local is None:
            raise ValueError("No local_ folder above %s" % row["path"])
        s["local"] = local.name
        s["files"] = [local, local.with_name(local.name + ".json")]
    else:                                # export: the corpus JSON and its split-out attachments
        s["files"] = [Path(row["path"])]
        try:
            doc = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
            s["original"] = (doc.get("source") or {}).get("original_id")
        except (OSError, ValueError, AttributeError):
            pass
        folder = Path(row["path"]).parent  # claude.ai: attachments/<uuid>, Gemini: attachments/<chat ID>
        if folder.is_relative_to(sources.CORPUS_DIR):
            s["files"] += [folder / "attachments" / k for k in (s["original"], sid)
                           if isinstance(k, str) and sources.SID_RE.match(k)]
    s["files"] = [p for p in s["files"] if os.path.lexists(p)]
    return s


def _may_go(p):
    """Only copies in archive/live and files in archive/corpus, never the live sources and
    never a corpus outside the archive (CHRONICLE_CORPUS can point to one)."""
    p = os.path.realpath(p)
    allowed = [sources.LIVE_DIR, sources.CORPUS_DIR]
    if any(Path(p).is_relative_to(os.path.realpath(v)) for v in (sources.CLAUDE_PROJECTS, sources.CLAUDE_CONFIG)):
        return False
    return any(Path(p).is_relative_to(os.path.realpath(a)) and p != os.path.realpath(a) for a in allowed)


def plan(idx, refs):
    """What deleting for good would remove -> (stones, errors).

    Clutter only; a marked session takes its earlier transcripts along (field
    'belongs_to' = the session). Every stone gets its files in 'files'."""
    stones, errors, seen = [], [], set()
    c = idx.conn()
    try:
        marked = {r[0] for r in c.execute("SELECT id FROM chats WHERE " + chat_index.CLUTTER)}
        for ref in refs:
            try:
                row = idx.resolve(ref)
            except ValueError as e:
                errors.append(str(e))
                continue
            if row["id"] not in marked:
                errors.append("%s “%s” is not marked as clutter" % (row["id"][:8], sources.oneline(row["title"], 60)))
                continue
            rows = [(row, None)] + [(r, row["id"]) for r in c.execute(
                "SELECT c.* FROM branches b JOIN chats c ON c.id=b.id WHERE b.kind='earlier' AND b.head=? "
                "ORDER BY c.last_ts", (row["id"],))]
            for r, head in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    stones.append(dict(_stone(r), belongs_to=head))
    finally:
        c.close()
    for s in stones:
        for p in s["files"]:
            if not _may_go(p):
                raise ValueError("Not in archive/live or archive/corpus, not deleting: %s" % p)
    return stones, errors


def show(p):
    """Path for display and gravestone: relative to the archive, otherwise with ~."""
    p = Path(p)
    if p.is_relative_to(sources.ARCHIVE):
        return p.relative_to(sources.ARCHIVE).as_posix() + ("/" if p.is_dir() else "")
    return ("~/" + p.relative_to(sources.HOME).as_posix()) if p.is_relative_to(sources.HOME) else str(p)


def delete(idx, stones):
    """Set gravestones, then remove the files -> (number of files/folders, errors).

    Under the lock of backup.py, so no run that started before the gravestone
    brings a copy back. Gravestone first: if deleting breaks off, leftovers
    remain, but nothing comes back into list or search."""
    with backup.lock(60) as free:
        if not free:
            raise RuntimeError("Backup is running, nothing deleted")
        chronicle_db.bury([dict(s, files=[show(p) for p in s["files"]]) for s in stones])
        n, errors = 0, []
        for s in stones:
            for p in s["files"]:
                try:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    n += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    errors.append("%s: %s" % (p, e))
    idx.refresh(force=True)
    return n, errors


# ---------------------------------------------------------------- Suggestions

def _sid_of(rel):
    """Note in backup.db (path relative to live/) -> session ID or None."""
    part = rel.split("/")
    if part[0] == "claude-code" and len(part) == 3 and part[2].endswith(".jsonl"):
        return part[2][:-6]
    if part[0] == "desktop" and part[-1].startswith("local_"):
        try:
            return json.loads((sources.LIVE_DIR / rel).read_text(encoding="utf-8")).get("cliSessionId")
        except (OSError, ValueError, AttributeError):
            return None
    return None


def suggestions(idx):
    """Candidates for clutter -> {short, app, open}, each a list of index rows.

    short  under MIN_CHARS characters of text (they get no digest either),
           without chats that were still running in the last 24 h
    app    deleted in the desktop app (release marker, see backup.py); field 'deleted'
    open   digest says "Result: open" and "Decisions: none"; field 'digest'
    Chats already marked and earlier transcripts are left out."""
    app = {}
    for path, _since, deleted in backup.gone_list():
        sid = _sid_of(path) if deleted else None
        if sid:
            app.setdefault(sid, deleted)
    quiet = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    c = idx.conn()
    try:
        short = [dict(r) for r in c.execute(
            "SELECT * FROM chats WHERE chars<? AND last_ts<? AND %s ORDER BY platform, last_ts DESC"
            % chat_index.VISIBLE, (MIN_CHARS, quiet))]
        unresolved = {r["id"]: r["text"] for r in c.execute("SELECT id, text FROM chronicle.digests")
                      if RESULT_OPEN.search(r["text"] or "") and NO_DECISIONS.search(r["text"] or "")}

        def rows_for(ids, field, values):
            rows = c.execute("SELECT * FROM chats WHERE id IN (SELECT value FROM json_each(?)) AND %s "
                             "ORDER BY last_ts DESC" % chat_index.VISIBLE, (json.dumps(sorted(ids)),))
            return [dict(r, **{field: values[r["id"]]}) for r in rows]
        return {"short": short, "app": rows_for(app, "deleted", app), "open": rows_for(unresolved, "digest", unresolved)}
    finally:
        c.close()
