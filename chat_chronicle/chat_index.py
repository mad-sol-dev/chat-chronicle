"""Index, search and zoom levels of the chronicle.

Shared base for the web interface, CLI and MCP server. A chat can be viewed in
levels, each much cheaper than the next:

  0 Hits       search()        title, date, source, snippets
  1 Digest     DigestStore     a few lines, generated once by an LLM (digests.py)
  2 Outline    outline()       one line per exchange, no LLM
  3 Excerpt    find_in_chat()  hits inside one chat with message number
  4 Full text  read_range()    Markdown, page by page
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter, OrderedDict
from pathlib import Path

from . import backup
from . import chronicle_db
from . import sources
from .sources import fmt_ts, oneline, project_of

DB_PATH = Path(os.environ.get("CHRONICLE_INDEX") or sources.CACHE_HOME / "chat-chronicle" / "index.db")
SCHEMA = "5"
USER_LABEL = os.environ.get("CHRONICLE_USER", "You")
REFRESH_INTERVAL = 15      # seconds between two file system scans
EXPORT_CHARS = 20000       # chars per tool output in the Markdown export
INDEX_TOOL_CHARS = 3000    # chars per tool input/output in the search index
ROW_LIMIT = 20000          # hit rows per search before it is cut off


# ---------------------------------------------------------------- Tools

def tool_label(name):
    """mcp__remote-devices__Desktop_Commander__read_file -> Desktop_Commander · read_file"""
    if name.startswith("mcp__"):
        segs = [s for s in name[5:].split("__") if s]
        return " · ".join(segs[-2:]) if segs else name
    return name


def tool_summary(inp):
    if not isinstance(inp, dict):
        return oneline(inp, 110)
    for k in ("description", "command", "file_path", "path", "pattern", "query",
              "url", "subject", "prompt", "skill", "input"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return oneline(v, 110)
    return oneline(json.dumps(inp, ensure_ascii=False), 110)


def tool_input_text(name, inp):
    if not isinstance(inp, dict):
        return str(inp)
    if isinstance(inp.get("command"), str):
        t = inp["command"]
        if inp.get("description"):
            t = "# %s\n%s" % (inp["description"], t)
        return t
    if "old_string" in inp and "new_string" in inp:
        return "%s\n--- old\n%s\n+++ new\n%s" % (inp.get("file_path") or inp.get("path") or "",
                                                inp["old_string"], inp["new_string"])
    if isinstance(inp.get("content"), str) and (inp.get("file_path") or inp.get("path")):
        return "%s\n\n%s" % (inp.get("file_path") or inp.get("path"), inp["content"])
    return json.dumps(inp, ensure_ascii=False, indent=2)


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def edited_file(b):
    """Path a tool call writes to, else None."""
    name = b["name"].rsplit("__", 1)[-1]
    if b["name"] in EDIT_TOOLS or name in ("write_file", "edit_block", "edit_file"):
        inp = b["input"] if isinstance(b["input"], dict) else {}
        return inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
    return None


# ---------------------------------------------------------------- Markdown

def _fence(text, lang=""):
    runs = re.findall(r"`{3,}", text)
    f = "`" * max(3, max((len(r) for r in runs), default=0) + 1)
    return "%s%s\n%s\n%s" % (f, lang, text, f)


def item_markdown(it, with_tools=True, tool_chars=EXPORT_CHARS, num=None, who="Claude"):
    """One item as Markdown lines; num puts the message number into the heading."""
    k = it["kind"]
    nr = "[%d] " % num if num is not None else ""
    out = []
    if k == "user":
        out += ["## %s%s · %s" % (nr, USER_LABEL, fmt_ts(it["ts"])), "", it["text"], ""]
        if it.get("images"):
            out += ["*(%d image(s) attached)*" % len(it["images"]), ""]
        if it.get("files"):
            out += ["*(Attachments: %s)*" % ", ".join(it["files"]), ""]
    elif k == "assistant":
        body = []
        for b in it["blocks"]:
            if b["type"] == "text":
                body += [b["text"], ""]
            elif not with_tools:
                continue
            elif b["type"] == "thinking":
                body += ["<details><summary>Thinking</summary>", "", b["text"], "", "</details>", ""]
            else:
                res = b["result"] or ""
                if len(res) > tool_chars:
                    res = res[:tool_chars] + "\n… (%d chars truncated)" % (len(b["result"]) - tool_chars)
                inp = tool_input_text(b["name"], b["input"])
                if len(inp) > tool_chars:
                    inp = inp[:tool_chars] + "\n… (%d chars truncated)" % (len(inp) - tool_chars)
                body += ["<details><summary>%s: %s</summary>" % (
                    tool_label(b["name"]), tool_summary(b["input"]).replace("<", "&lt;")), "",
                    _fence(inp), ""]
                if b["result"] is not None:
                    body += ["Result%s:" % (" (error)" if b["error"] else ""), "", _fence(res), ""]
                body += ["</details>", ""]
        if body:
            out += ["## %s%s · %s" % (nr, who, fmt_ts(it["ts"])), ""] + body
        elif not with_tools and num is not None:
            n = sum(1 for b in it["blocks"] if b["type"] == "tool")
            out += ["## %s%s · %s" % (nr, who, fmt_ts(it["ts"])), "", "*(only %d tool calls)*" % n, ""]
    elif with_tools or it["sub"] == "compact":
        out += ["> %s*%s*" % (nr, oneline(it["text"], 300)), ""]
    return out


def to_markdown(sid, title, meta, items, with_tools=True, platform="claude-code"):
    out = ["# " + title, ""]
    if platform in sources.LIVE:
        out.append("- Project: `%s`" % (meta["cwd"] or "?"))
    else:
        out.append("- Source: %s" % sources.PLATFORM_LABEL.get(platform, platform))
    out += ["- Period: %s – %s" % (fmt_ts(meta["first"]), fmt_ts(meta["last"])),
            "- Chat: `%s`" % sid]
    if meta["models"]:
        out.append("- Model: " + ", ".join(meta["models"]))
    out.append("")
    for it in items:
        out += item_markdown(it, with_tools, who=assistant_label(platform))
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------- Branches

def item_hashes(items):
    """Short fingerprint per message, to find the shared beginning of two
    transcripts. Events ('-') don't count, they are not at the same position in
    every copy."""
    out = []
    for it in items:
        if it["kind"] == "user":
            s = "u\0" + it["text"]
        elif it["kind"] == "assistant":
            s = "a\0" + "\0".join(b.get("id") or b.get("text") or "" for b in it["blocks"])
        else:
            out.append("-")
            continue
        out.append(hashlib.blake2s(s.encode("utf-8", "replace"), digest_size=5).hexdigest())
    return " ".join(out)


def shared_prefix(a, b):
    """Shared beginning of two fingerprint lists -> [(nr in a, nr in b)]."""
    xa = [(i, x) for i, x in enumerate(a.split()) if x != "-"]
    xb = [(i, x) for i, x in enumerate(b.split()) if x != "-"]
    out = []
    for (i, x), (j, y) in zip(xa, xb):
        if x != y:
            break
        out.append((i, j))
    return out


def branch_point(other, mine):
    """First message of mine that is not part of the shared beginning with other."""
    p = shared_prefix(other, mine)
    return p[-1][1] + 1 if p else 0


# ---------------------------------------------------------------- Index

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS chats(
    id TEXT PRIMARY KEY, platform TEXT, instance TEXT, path TEXT, mtime REAL, size INTEGER,
    title TEXT, project TEXT, worktree TEXT, cwd TEXT, first_ts TEXT, last_ts TEXT,
    n_user INTEGER, n_tools INTEGER, chars INTEGER, cost REAL, archived INTEGER,
    models TEXT, preview TEXT, summary TEXT, rid_lo INTEGER, rid_hi INTEGER);
CREATE INDEX IF NOT EXISTS chats_path ON chats(path);
CREATE INDEX IF NOT EXISTS chats_last ON chats(last_ts);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    sid UNINDEXED, item UNINDEXED, kind UNINDEXED, body,
    tokenize="unicode61 remove_diacritics 2");
CREATE TABLE IF NOT EXISTS branches(
    id TEXT PRIMARY KEY, hashes TEXT, branch_of TEXT, branch_at INTEGER, kind TEXT, head TEXT);
"""

# Marked as clutter (chronicle.db), including earlier transcripts of a marked session
CLUTTER = ("(id IN (SELECT id FROM chronicle.clutter) OR id IN (SELECT b.id FROM branches b "
           "JOIN chronicle.clutter x ON x.id=b.head WHERE b.kind='earlier'))")
# Chats that appear in lists: earlier transcripts only show with their session, clutter
# only in its own view
VISIBLE = "id NOT IN (SELECT id FROM branches WHERE kind='earlier') AND id NOT IN (SELECT id FROM chronicle.clutter)"


class Index:
    def __init__(self, path=DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.last = 0.0
        self.status = ""
        self.skip = {}             # path -> (mtime, size) of corpus files with a gravestone
        chronicle_db.ready()
        c = self.conn()
        try:
            ver = None
            try:
                row = c.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
                ver = row[0] if row else None
            except sqlite3.OperationalError:
                pass
            if ver != SCHEMA:
                self._drop(c)
            c.executescript(SCHEMA_SQL)
            c.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA,))
            c.commit()
        finally:
            c.close()

    def conn(self):
        """Connection to the index, chronicle.db attached as 'chronicle' (clutter, digests).

        chronicle.db is only read here in single queries; inside a write transaction
        of the index every query would keep it locked until the commit."""
        c = sqlite3.connect(str(self.path), timeout=60)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("ATTACH DATABASE ? AS chronicle", (str(chronicle_db.PATH),))
        return c

    @staticmethod
    def _drop(c):
        c.executescript("DROP TABLE IF EXISTS main.fts; DROP TABLE IF EXISTS main.sessions; "
                        "DROP TABLE IF EXISTS main.chats; DROP TABLE IF EXISTS main.meta; "
                        "DROP TABLE IF EXISTS main.branches;")

    def reset(self):
        with self.lock:
            c = self.conn()
            try:
                self._drop(c)
                c.executescript(SCHEMA_SQL)
                c.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (SCHEMA,))
                c.commit()
            finally:
                c.close()
        self.last = 0.0

    def refresh(self, force=False, block=True):
        if not force and time.time() - self.last < REFRESH_INTERVAL:
            return
        if not self.lock.acquire(blocking=block):
            return
        try:
            self._refresh()
            self.last = time.time()
        finally:
            self.status = ""
            self.lock.release()

    def _refresh(self):
        self.status = "Backing up"
        try:                       # first the live sources into the archive, the index only reads there
            backup.run(wait=60)
        except Exception as e:
            print("Backup failed: %r" % e, file=sys.stderr, flush=True)
        desk = sources.load_desktop_meta()
        graves = chronicle_db.gravestones()    # deleted for good; corpus files only know their ID after reading
        files = {p: f for p, f in sources.discover().items()
                 if not {f["id"], f.get("local")} & graves
                 and self.skip.get(p) != (f["st"].st_mtime, f["st"].st_size)}
        c = self.conn()
        try:
            known = {r["path"]: (r["id"], r["mtime"], r["size"])
                     for r in c.execute("SELECT id, path, mtime, size FROM chats")}
            for p in [p for p, k in known.items() if p not in files or k[0] in graves]:
                self._drop_rows(c, known[p][0])
                c.execute("DELETE FROM chats WHERE path=?", (p,))
                c.execute("DELETE FROM branches WHERE id=?", (known[p][0],))
                del known[p]
            todo = [p for p, f in files.items()
                    if known.get(p, (None,))[1:] != (f["st"].st_mtime, f["st"].st_size)]
            for n, p in enumerate(todo, 1):
                self.status = "Indexing %d/%d" % (n, len(todo))
                try:
                    if not self._index_one(c, p, files[p], desk, graves):
                        self.skip[p] = (files[p]["st"].st_mtime, files[p]["st"].st_size)
                except Exception as e:  # one broken file must not hold up the rest
                    print("Error indexing %s: %r" % (p, e), file=sys.stderr, flush=True)
                if n % 200 == 0:
                    c.commit()
            c.commit()
            self._link(c, desk)
            for sid, d in desk.items():
                c.execute("UPDATE chats SET archived=?, instance=? WHERE id=?",
                          (int(d["archived"]), d["instance"], sid))
                for p in d.get("prior", ()):
                    c.execute("UPDATE chats SET instance=? WHERE id=?", (d["instance"], p))
            c.commit()
        finally:
            c.close()

    def _index_one(self, c, path, f, desk, graves=()):
        """Put one file into the index -> False if the chat has a gravestone."""
        platform = f["platform"]
        meta, items = sources.load(path, platform)
        platform = platform or meta.get("platform") or "unknown"
        sid = f["id"] or meta.get("id") or Path(path).stem
        if sid in graves:
            return False
        d = desk.get(sid) or (desk.get("local:" + f["local"]) if f.get("local") else None)
        if platform in sources.LIVE:
            proj, wt = project_of(meta["cwd"] or (d or {}).get("cwd"))
        else:
            proj, wt = meta["project"] or "", ""
        instance = f["instance"] or (d or {}).get("instance") or ""
        title = sources.pick_title(meta, items, d)
        rows, n_user, n_tools, chars, preview = [], 0, 0, 0, ""
        for i, it in enumerate(items):
            if it["kind"] == "user":
                n_user += 1
                chars += len(it["text"])
                preview = preview or oneline(it["text"], 220)
                rows.append((sid, i, "user", it["text"]))
            elif it["kind"] == "assistant":
                txt = "\n".join(b["text"] for b in it["blocks"] if b["type"] == "text")
                if txt:
                    chars += len(txt)
                    rows.append((sid, i, "assistant", txt))
                th = "\n".join(b["text"] for b in it["blocks"] if b["type"] == "thinking")
                if th:
                    rows.append((sid, i, "thinking", th))
                for b in it["blocks"]:
                    if b["type"] == "tool":
                        n_tools += 1
                        body = "%s\n%s\n%s" % (b["name"],
                                               tool_input_text(b["name"], b["input"])[:INDEX_TOOL_CHARS],
                                               (b["result"] or "")[:INDEX_TOOL_CHARS])
                        rows.append((sid, i, "tool", body))
        if platform == "claude-code":
            c.execute("INSERT INTO branches(id, hashes) VALUES(?,?) "
                      "ON CONFLICT(id) DO UPDATE SET hashes=excluded.hashes", (sid, item_hashes(items)))
            br = c.execute("SELECT branch_at FROM branches WHERE id=?", (sid,)).fetchone()
            if br and br[0]:           # shared beginning is already in the index with the counterpart
                rows = [r for r in rows if r[1] >= br[0]]
        self._drop_rows(c, sid)
        c.executemany("INSERT INTO fts(sid, item, kind, body) VALUES(?,?,?,?)", rows)
        hi = c.execute("SELECT rowid FROM fts ORDER BY rowid DESC LIMIT 1").fetchone() if rows else None
        lo, hi = (hi[0] - len(rows) + 1, hi[0]) if hi else (None, None)
        c.execute("INSERT OR REPLACE INTO chats VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, platform, instance, path, f["st"].st_mtime, f["st"].st_size, title, proj, wt,
                   meta["cwd"] or "", meta["first"] or "", meta["last"] or "",
                   n_user, n_tools, chars, meta["cost"], int(bool((d or {}).get("archived"))),
                   ", ".join(meta["models"]), preview, meta.get("vendor_summary"), lo, hi))
        return True

    def _link(self, c, desk):
        """Take related transcripts from the desktop metadata.

        Earlier transcripts of a session (after a rewind) and deliberate forks
        contain the shared beginning a second time. It stays in the search index
        only with the counterpart, here only from branch_at on; otherwise every
        search would return the same history several times. kind is 'earlier' (no
        longer visible in the app, head = the session where it continued) or 'fork'.

        If the counterpart of a fork is clutter or deleted, it doesn't count:
        the fork is then fully in the index, including the shared beginning."""
        clutter = set(chronicle_db.clutter())
        want = {}                                  # id -> (counterpart, kind, head)
        for key, d in desk.items():
            if key.startswith("local:"):
                continue
            for p in d["prior"]:
                want[p] = (key, "earlier", key)
            for parent, child in d["rewinds"]:     # direct successor, if known
                if parent and child and parent != key:
                    want[parent] = (child, "earlier", key)
            src = desk.get("local:" + d["forked_from"]) if d["forked_from"] else None
            if src and src["cli"] != key:
                want[key] = (src["cli"], "fork", None)
        rows = {r["id"]: r for r in c.execute(
            "SELECT c.id, c.path, b.hashes, b.branch_of, b.branch_at, b.kind, b.head "
            "FROM chats c LEFT JOIN branches b ON b.id=c.id WHERE c.platform='claude-code'")}
        hashes = {}

        def fp(sid):
            if sid not in hashes:
                h = rows[sid]["hashes"]
                if h is None:                       # indexed by an older version
                    h = item_hashes(sources.parse_session(rows[sid]["path"])[1])
                    c.execute("INSERT INTO branches(id, hashes) VALUES(?,?) "
                              "ON CONFLICT(id) DO UPDATE SET hashes=excluded.hashes", (sid, h))
                hashes[sid] = h
            return hashes[sid]

        for sid, r in rows.items():
            other, kind, head = want.get(sid, (None, None, None))
            if kind == "fork" and other in clutter:
                other = None
            if other not in rows or other == sid:
                other = head if head in rows and head != sid else None
            if other is None:
                kind = head = None
            at = branch_point(fp(other), fp(sid)) if other else None
            if (other, at, kind, head) == (r["branch_of"], r["branch_at"], r["kind"], r["head"]):
                continue
            c.execute("INSERT INTO branches(id, branch_of, branch_at, kind, head) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET branch_of=excluded.branch_of, "
                      "branch_at=excluded.branch_at, kind=excluded.kind, head=excluded.head",
                      (sid, other, at, kind, head))
            try:
                self._index_one(c, r["path"], {"platform": "claude-code", "instance": None, "id": sid,
                                               "st": os.stat(r["path"])}, desk)
            except Exception as e:
                print("Error indexing %s: %r" % (r["path"], e), file=sys.stderr, flush=True)
        c.commit()

    @staticmethod
    def _drop_rows(c, sid):
        """Delete a chat's index rows. For WHERE sid=? FTS5 would have to read the
        whole table; but a chat's rows are contiguous (rid_lo..rid_hi)."""
        r = c.execute("SELECT rid_lo, rid_hi FROM chats WHERE id=?", (sid,)).fetchone()
        if r and r[0] is not None:
            c.execute("DELETE FROM fts WHERE rowid BETWEEN ? AND ?", (r[0], r[1]))
        elif r is not None:
            c.execute("DELETE FROM fts WHERE sid=?", (sid,))

    def row(self, sid):
        c = self.conn()
        try:
            return c.execute("SELECT * FROM chats WHERE id=?", (sid,)).fetchone()
        finally:
            c.close()

    def branches(self):
        """id -> relationship for all chats that branch off another one."""
        c = self.conn()
        try:
            return {r["id"]: dict(r) for r in c.execute(
                "SELECT id, branch_of, branch_at, kind, head FROM branches WHERE branch_of IS NOT NULL")}
        finally:
            c.close()

    def family(self, sid):
        """Related transcripts of a chat: me (own branch or None), up (chat it
        belongs to), earlier (earlier transcripts), forks. Those marked as clutter
        are missing from earlier and forks."""
        q = ("SELECT c.id, c.title, c.first_ts, c.last_ts, c.models, b.branch_of, b.branch_at, b.kind, b.head "
             "FROM branches b JOIN chats c ON c.id=b.id ")
        excl = " AND c.id NOT IN (SELECT id FROM chronicle.clutter)"
        c = self.conn()
        try:
            me = c.execute(q + "WHERE b.id=? AND b.branch_of IS NOT NULL", (sid,)).fetchone()
            up = c.execute("SELECT id, title FROM chats WHERE id=?",
                           (me["head"] or me["branch_of"],)).fetchone() if me else None
            earlier = c.execute(q + "WHERE b.head=? AND b.kind='earlier'" + excl + " ORDER BY c.last_ts",
                                (sid,)).fetchall()
            forks = c.execute(q + "WHERE b.branch_of=? AND b.kind='fork'" + excl + " ORDER BY c.first_ts",
                              (sid,)).fetchall()
        finally:
            c.close()
        return {"me": me, "up": up, "earlier": earlier, "forks": forks}

    def resolve(self, ref, clutter=True):
        """Chat ID or an unambiguous prefix of it -> row; else ValueError with the reason.
        clutter=False leaves out chats marked as clutter (MCP server)."""
        ref = (ref or "").strip()
        extra = "" if clutter else " AND NOT " + CLUTTER
        c = self.conn()
        try:
            r = c.execute("SELECT * FROM chats WHERE id=?" + extra, (ref,)).fetchone()
            if r:
                return r
            if len(ref) < 4:
                raise ValueError("Chat ID too short: %r" % ref)
            rows = c.execute("SELECT * FROM chats WHERE id LIKE ?" + extra + " LIMIT 3",
                             (ref.replace("%", "") + "%",)).fetchall()
        finally:
            c.close()
        if len(rows) != 1:
            raise ValueError("%s chats match %r" % ("No" if not rows else "Several", ref))
        return rows[0]


# ---------------------------------------------------------------- Loading chats

_cache = OrderedDict()
_cache_lock = threading.Lock()


def load_chat(row):
    """Read the chat for an index row afresh -> (meta, items, tools, title).

    Small LRU cache (path, mtime, size); running sessions are therefore read
    again as soon as the file grows. That one file is backed up first."""
    path, platform = row["path"], row["platform"]
    if platform in sources.LIVE:
        backup.backup_file(path)
    st = os.stat(path)
    key = (path, st.st_mtime, st.st_size)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    meta, items = sources.load(path, platform)
    tools = {}
    for it in items:
        for b in it.get("blocks", []):
            if b["type"] == "tool":
                tools[b["id"]] = b
    if platform in sources.LIVE:
        desk = sources.load_desktop_meta()
        d = desk.get(row["id"])
        title = sources.pick_title(meta, items, d) if (d or meta["custom"] or meta["ai"]) else row["title"]
    else:
        title = row["title"]
    val = (meta, items, tools, title)
    with _cache_lock:
        _cache[key] = val
        while len(_cache) > 8:
            _cache.popitem(last=False)
    return val


# ---------------------------------------------------------------- Search

OPS = ("OR", "NOT", "AND")


def fts_query(q):
    """Free text -> FTS5 expression. Words become prefixes, "..." stays a phrase,
    OR/NOT are passed through."""
    out = []
    for tok in re.findall(r'"[^"]+"|\S+', q):
        if tok in OPS:
            out.append(tok)
            continue
        phrase = tok.startswith('"')
        core = tok.strip('"').rstrip("*").replace('"', '""')
        if not core:
            continue
        out.append('"%s"' % core + ("" if phrase else "*"))
    while out and out[-1] in OPS:
        out.pop()
    while out and out[0] in ("OR", "AND"):
        out.pop(0)
    return " ".join(out)


def _ids_param(ids):
    return json.dumps(sorted(ids))


def search(c, q, kinds=("user", "assistant"), platform="", project="", instance="",
           since="", until="", order="date", limit=200):
    """Find chats in which ALL terms occur somewhere.

    FTS5 only combines terms within one row; but often one is in the question
    and the other three answers later. So the hit sets per term are intersected
    at chat level (taken from chat_archive). With OR/NOT the FTS5 expression
    applies as written, per message. Terms the tokenizer swallows are searched
    by a LIKE fallback ('weak'). Clutter (including earlier transcripts of a
    marked session) is left out."""
    kinds = list(kinds)
    kq = ",".join("?" * len(kinds))
    empty = {"results": [], "total": 0, "weak": [], "capped": False}
    expr = fts_query(q)
    if not expr:
        return empty

    allowed = None
    cond, args = [], []
    for col, val in (("platform", platform), ("project", project), ("instance", instance)):
        if val:
            cond.append("%s=?" % col)
            args.append(val)
    if since:
        cond.append("last_ts>=?")
        args.append(since)
    if until:
        cond.append("first_ts<=?")
        args.append(until)
    if c.execute("SELECT 1 FROM chronicle.clutter LIMIT 1").fetchone():
        cond.append("NOT " + CLUTTER)
    if cond:
        allowed = {r[0] for r in c.execute("SELECT id FROM chats WHERE " + " AND ".join(cond), args)}
        if not allowed:
            return empty

    weak = []
    candidates = allowed
    if not any(t in OPS for t in expr.split()):
        units = re.findall(r'"(?:[^"]|"")*"\*?', expr)
        for u in units:
            try:
                hits = {r[0] for r in c.execute(
                    "SELECT DISTINCT sid FROM fts WHERE fts MATCH ? AND kind IN (%s)" % kq, [u] + kinds)}
            except sqlite3.OperationalError:
                hits = set()
            if not hits:
                bare = u.rstrip("*")[1:-1].replace('""', '"')
                hits = {r[0] for r in c.execute(
                    "SELECT DISTINCT sid FROM fts WHERE kind IN (%s) AND body LIKE ?" % kq,
                    kinds + ["%" + bare + "%"])}
                if hits:
                    weak.append(bare)
            candidates = hits if candidates is None else candidates & hits
            if not candidates:
                return dict(empty, weak=weak)
        expr = " OR ".join(units)

    sql = ("SELECT sid, item, kind, bm25(fts) AS r, snippet(fts, 3, char(2), char(3), '…', 18) AS s "
           "FROM fts WHERE fts MATCH ? AND kind IN (%s)" % kq)
    params = [expr] + kinds
    if candidates is not None:
        sql += " AND sid IN (SELECT value FROM json_each(?))"
        params.append(_ids_param(candidates))
    sql += " LIMIT %d" % ROW_LIMIT
    rows = c.execute(sql, params).fetchall()

    agg = {}
    for r in rows:
        e = agg.setdefault(r["sid"], {"score": 0.0, "hits": []})
        e["score"] += r["r"]
        e["hits"].append({"item": r["item"], "kind": r["kind"], "snip": r["s"], "r": r["r"]})
    for sid in candidates or ():          # only found via LIKE: no bm25
        agg.setdefault(sid, {"score": 0.0, "hits": []})
    if not agg:
        return dict(empty, weak=weak)

    meta = {r["id"]: r for r in c.execute(
        "SELECT * FROM chats WHERE id IN (SELECT value FROM json_each(?))", (_ids_param(agg),))}
    out = []
    for sid, e in agg.items():
        r = meta.get(sid)
        if r is None:
            continue
        d = dict(r)
        e["hits"].sort(key=lambda h: h["item"])
        d.update(score=round(e["score"], 2), hits=e["hits"])
        out.append(d)
    if order == "relevance":
        out.sort(key=lambda d: (d["score"], -len(d["hits"])))
    else:
        out.sort(key=lambda d: d["last_ts"] or "", reverse=True)
    return {"results": out[:limit], "total": len(out), "weak": weak, "capped": len(rows) >= ROW_LIMIT}


def _chat_hits(c, sid, expr, kinds, depth=0):
    """FTS hits of a chat -> [(nr, kind, snippet)], including the shared beginning,
    which for a branch is only in the index with the counterpart."""
    hits = [(r["item"], r["kind"], r["s"]) for r in c.execute(
        "SELECT item, kind, snippet(fts, 3, '»', '«', '…', 24) AS s FROM fts "
        "WHERE fts MATCH ? AND sid=? AND kind IN (%s)" % ",".join("?" * len(kinds)), [expr, sid] + kinds)]
    b = c.execute("SELECT b.branch_of, b.branch_at, b.hashes, o.hashes AS oh FROM branches b "
                  "JOIN branches o ON o.id=b.branch_of WHERE b.id=?", (sid,)).fetchone()
    if b and b["branch_at"] and b["oh"] and depth < 8:
        nr = dict(shared_prefix(b["oh"], b["hashes"]))      # number in the counterpart -> own number
        hits += [(nr[i], k, s) for i, k, s in _chat_hits(c, b["branch_of"], expr, kinds, depth + 1)
                 if i in nr and nr[i] < b["branch_at"]]
    return hits


def find_in_chat(c, sid, q, kinds=("user", "assistant", "thinking", "tool"), limit=30):
    """Level 3: hits inside one chat, with the message number to read on from."""
    expr = fts_query(q)
    if not expr:
        return []
    hits = sorted(_chat_hits(c, sid, expr, list(kinds)))[:limit]
    return [{"item": i, "kind": k, "text": s} for i, k, s in hits]


# ---------------------------------------------------------------- Outline (level 2)

def tool_list(blocks, n=8):
    """Tool blocks of a turn -> 'Bash ×5, Read ×2, edited: chronicle.py, README.md'."""
    counts, files = Counter(), []
    for b in blocks:
        if b["type"] != "tool":
            continue
        f = edited_file(b)
        if f:
            base = os.path.basename(str(f))
            if base not in files:
                files.append(base)
        else:
            counts[tool_label(b["name"])] += 1
    parts = ["%s ×%d" % (k, v) if v > 1 else k for k, v in counts.most_common(n)]
    if files:
        parts.append("edited: " + ", ".join(files[:8]) + (" …" if len(files) > 8 else ""))
    return ", ".join(parts)


def touched_files(items):
    out = []
    for it in items:
        for b in it.get("blocks", []):
            if b["type"] == "tool":
                f = edited_file(b)
                if f and f not in out:
                    out.append(f)
    return out


def assistant_label(platform):
    return sources.PLATFORM_LABEL[platform] if platform in ("chatgpt", "gemini") else "Claude"


def outline(items, max_chars=6000, who="Claude"):
    """Level 2: one line per message, no LLM.

    User: start of the message. Claude: start of the turn's last text answer
    (that is usually where the result is) plus a compact tool list. If that
    doesn't fit the budget, the lines get shorter; if that isn't enough, the
    start and end stay."""
    entries = []
    for i, it in enumerate(items):
        if it["kind"] == "user":
            text = it["text"] or ("(%d image(s))" % len(it["images"]) if it["images"] else "")
            entries.append((i, "%s: " % USER_LABEL, text, ""))
        elif it["kind"] == "assistant":
            texts = [b["text"] for b in it["blocks"] if b["type"] == "text"]
            tools = tool_list(it["blocks"])
            if texts or tools:
                entries.append((i, "%s: " % who, texts[-1] if texts else "", tools))
        elif it.get("sub") == "compact":
            entries.append((i, "", "(context compacted)", ""))
    if not entries:
        return ""

    def fmt(e, width):
        i, who, text, tools = e
        line = "[%d] %s%s" % (i, who, oneline(text, width) if text else "")
        if tools:
            line += (" · " if text else "") + "Tools: " + oneline(tools, max(60, width))
        return line

    for width in (320, 220, 150, 100, 70):
        lines = [fmt(e, width) for e in entries]
        if sum(len(x) + 1 for x in lines) <= max_chars:
            return "\n".join(lines)
    head, tail, used = [], [], 80
    lo, hi = 0, len(lines) - 1
    while lo <= hi:
        for side in (0, 1):
            if lo > hi:
                break
            ln = lines[lo] if side == 0 else lines[hi]
            if used + len(ln) + 1 > max_chars:
                lo = hi + 1
                break
            used += len(ln) + 1
            if side == 0:
                head.append(ln)
                lo += 1
            else:
                tail.insert(0, ln)
                hi -= 1
    skipped = len(lines) - len(head) - len(tail)
    mid = ["… %d lines skipped (messages %d–%d) …"
           % (skipped, entries[len(head)][0], entries[len(lines) - len(tail) - 1][0])] if skipped else []
    return "\n".join(head + mid + tail)


# ---------------------------------------------------------------- Full text (level 4)

def read_range(items, start=0, end=None, with_tools=False, max_chars=12000, tool_chars=1500, who="Claude"):
    """Messages start..end as Markdown, at most max_chars.

    Returns (text, next number or None). A single message that is too long is
    truncated instead of skipped."""
    end = len(items) if end is None else min(end, len(items))
    out, used = [], 0
    for i in range(max(0, start), end):
        md = "\n".join(item_markdown(items[i], with_tools, tool_chars, num=i, who=who))
        if not md.strip():
            continue
        if used + len(md) > max_chars:
            if not out:
                out.append(md[:max_chars] + "\n… (message truncated)")
                return "\n".join(out), (i + 1 if i + 1 < end else None)
            return "\n".join(out), i
        out.append(md)
        used += len(md) + 1
    return "\n".join(out), None


# ---------------------------------------------------------------- Digests (level 1)

class DigestStore:
    """Stored LLM digests, in chronicle.db next to the archive: they cost money
    or GPU time and can't be rebuilt like the index."""

    def conn(self):
        return chronicle_db.conn()

    def get(self, sid):
        c = self.conn()
        try:
            r = c.execute("SELECT * FROM digests WHERE id=?", (sid,)).fetchone()
            return dict(r) if r else None
        finally:
            c.close()

    def put(self, sid, text, model, size, n_items):
        c = self.conn()
        try:
            c.execute("INSERT OR REPLACE INTO digests VALUES(?,?,?,?,?,?)",
                      (sid, text, model, size, n_items, time.strftime("%Y-%m-%dT%H:%M:%S")))
            c.commit()
        finally:
            c.close()

    def sizes(self):
        c = self.conn()
        try:
            return {r["id"]: r["basis_size"] for r in c.execute("SELECT id, basis_size FROM digests")}
        finally:
            c.close()


def is_stale(basis_size, size):
    """A digest counts as stale once the session has grown considerably since."""
    return size > basis_size * 1.25 and size - basis_size > 20000


def family_lines(fam):
    """Related transcripts (Index.family) as short text lines."""
    if not fam:
        return []
    out, me, up = [], fam["me"], fam["up"]
    if me and up and me["kind"] == "earlier":
        out.append("Earlier transcript of session %s (“%s”); differs from the later state "
                   "from message %d on" % (up["id"], oneline(up["title"], 60), me["branch_at"]))
    elif me and up:
        out.append("Fork of %s (“%s”), own history from message %d"
                   % (up["id"], oneline(up["title"], 60), me["branch_at"]))
    if fam["earlier"]:
        out.append("Earlier transcripts of this session (discarded states): " + ", ".join(
            "%s from message %d, until %s" % (r["id"][:8], r["branch_at"], fmt_ts(r["last_ts"]))
            for r in fam["earlier"]))
    if fam["forks"]:
        out.append("Forks: " + ", ".join("%s “%s” from message %d"
                                         % (r["id"][:8], oneline(r["title"], 50), r["branch_at"])
                                         for r in fam["forks"]))
    return out


def overview(row, meta, items, title, digest=None, max_chars=6000, family=None):
    """Level 1 + 2 in one text: header data, digest or summary, outline."""
    plat = sources.PLATFORM_LABEL.get(row["platform"], row["platform"])
    head = ["# " + title,
            "Chat: %s · %s%s" % (row["id"], plat, (" (%s)" % row["instance"]) if row["instance"] else ""),
            "Period: %s – %s · %d messages by %s, %d tool calls, about %d chars of text"
            % (fmt_ts(meta["first"]), fmt_ts(meta["last"]), row["n_user"], USER_LABEL,
               row["n_tools"], row["chars"] or 0)]
    if row["project"]:
        head.append("Project: %s%s" % (row["project"], (" · working directory " + row["cwd"]) if row["cwd"] else ""))
    files = [short_path(f, row["cwd"]) for f in touched_files(items)]
    if files:
        head.append("Edited files: " + ", ".join(files[:15]) + (" …" if len(files) > 15 else ""))
    head += family_lines(family)
    body = []
    if digest:
        stale = " (stale, the session has grown since)" if is_stale(digest["basis_size"], row["size"]) else ""
        body += ["", "## Digest%s" % stale, digest["text"].strip()]
    elif row["summary"]:
        body += ["", "## Summary (%s)" % plat, oneline(row["summary"], 1500)]
    text = "\n".join(head + body)
    rest = max(800, max_chars - len(text) - 40)
    body = ["", "## Outline ([n] = message number to read on from)",
            outline(items, rest, assistant_label(row["platform"]))]
    return text + "\n" + "\n".join(body)


def short_path(path, cwd):
    """Path relative to the working directory, else relative to ~."""
    p = str(path)
    for base, prefix in ((cwd, ""), (str(sources.HOME), "~/")):
        if base and p.startswith(base.rstrip("/") + "/"):
            return prefix + p[len(base.rstrip("/")) + 1:]
    return p
