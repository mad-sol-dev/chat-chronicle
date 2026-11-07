"""Ingest: raw exports into the archive, the corpus built from them.

    chronicle.py import <zip-7z-or-folder>...

  1. Detect the source (EXPORTERS: export_claude.py, export_chatgpt.py, export_gemini.py).
  2. Copy the raw export unchanged to archive/exports/<source>/<date>_<name>,
     by reflink: a zip or 7z as a whole, from an unpacked folder only the
     export's files. <date> is the day of the export (UTC), from the name or,
     if it has none, from the newest change inside. If the same export is
     already there, nothing happens; the comparison is a checksum over its
     files, so an archive and its unpacked folder count as the same. A 7z
     (the standard library can't read it) is unpacked by /usr/bin/7z into
     CACHE for the duration of the run.
  3. Rebuild corpus/<source>/ from all exports of that source, the newest
     first, older ones fill gaps. Chats with a gravestone (chronicle_db) are
     left out, even if an older export still has them.
  4. Report: new, grown, only in older exports now, gravestone.

exports/ only grows, nothing here deletes in it. corpus/<source>/ belongs to the build:
what it no longer produces disappears there. Only files whose content changes are
written, so the index re-reads only those; a chat that comes back unchanged in a
new export keeps its file and with it the exported_at of the export this state
first came from.

An exporter is a module with
  PLATFORM                    name, also the folder under exports/ and corpus/
  detect(ex)                  is ex an export of this source?
  parts(ex)                   files that belong to the export (checksum, copy of a folder)
  date(ex)                    time of the export (UTC)
  build(exports, graves, ids) -> (chats, files, Counter) from all exports, the newest
                              first; chats are unified documents, files {path in the
                              corpus folder: text or bytes}; ids see below
A chat that doesn't come from the newest export names its export in
metadata.rescued_from.

The old converter (chat_archive) gave ChatGPT and Gemini chats random IDs. So that
digests, clutter marks and gravestones stay attached to them, those IDs are kept
in chronicle.db (table ids, see chronicle_db.py). Chats missing there get, as for
claude.ai, a uuid5 derived from their platform ID. No build changes the ID of a
chat that is already in the corpus; if it would, it stops before writing anything.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path

from . import backup
from . import chronicle_db
from . import export_chatgpt
from . import export_claude
from . import export_gemini
from . import sources

EXPORTERS = [export_claude, export_chatgpt, export_gemini]
TMP = ".ingest-tmp"
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d_")
SEVENZ_MAGIC = b"7z\xbc\xaf\x27\x1c"
# Unpacked 7z archives, only during a run; not /tmp, which is often a small tmpfs
CACHE = sources.CACHE_HOME / "chat-chronicle" / "ingest"

_unpacked = {}                                # 7z path -> folder in CACHE


def _sevenz():
    return shutil.which("7z") or shutil.which("7za") or "/usr/bin/7z"


def _is_7z(path):
    try:
        with open(path, "rb") as f:
            return f.read(6) == SEVENZ_MAGIC
    except OSError:
        return False


def _list_7z(path):
    """Files of a 7z archive -> {name: mtime}; names that lead out of the folder are dropped."""
    out = subprocess.run([_sevenz(), "l", "-slt", "-ba", str(path)], capture_output=True, text=True,
                         check=True).stdout
    names, e = {}, {}
    for line in out.splitlines() + [""]:
        if line.strip():
            k, _, v = line.partition(" = ")
            e[k] = v
            continue
        name = e.get("Path", "")
        if name and e.get("Folder") != "+" and not e.get("Attributes", "").startswith("D") \
                and not name.startswith("/") and ".." not in name.split("/"):
            try:
                names[name] = time.mktime(time.strptime(e.get("Modified", "")[:19], "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                names[name] = 0.0
        e = {}
    return names


def _dir_7z(path):
    """Unpack a 7z into CACHE once per run -> folder; remove_unpacked() removes it."""
    key = os.path.realpath(path)
    if key not in _unpacked:
        CACHE.mkdir(parents=True, exist_ok=True)
        _unpacked[key] = Path(tempfile.mkdtemp(prefix="7z-", dir=CACHE))
        subprocess.run([_sevenz(), "x", "-y", "-bd", "-bso0", "-bsp0", "-o" + str(_unpacked[key]), key],
                       check=True)
    return _unpacked[key]


def remove_unpacked():
    """Remove the 7z archives unpacked during this run."""
    while _unpacked:
        shutil.rmtree(_unpacked.popitem()[1], ignore_errors=True)
    try:
        CACHE.rmdir()
    except OSError:                           # doesn't exist, or another run is using it
        pass


class Export:
    """An export as zip, 7z or folder; names are the files in it (POSIX, relative)."""

    def __init__(self, path, stored=False):
        self.path = Path(path)
        self.name = self.path.name
        # Name as downloaded; stored ones carry their date in front
        self.original_name = self.name[11:] if stored and DATE_RE.match(self.name) else self.name
        self.zip = self.path.is_file() and zipfile.is_zipfile(self.path)
        self.sevenz = self.path.is_file() and not self.zip and _is_7z(self.path)
        self.archive = self.zip or self.sevenz  # stored as a whole
        self._times = None                     # name -> mtime, for archives
        if self.zip:
            with zipfile.ZipFile(self.path) as z:  # times in a zip are local time
                self._times = {i.filename: time.mktime(i.date_time + (0, 0, -1))
                               for i in z.infolist() if not i.is_dir()}
        elif self.sevenz:
            self._times = _list_7z(self.path)
        elif self.path.is_dir():
            self.names = sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob("*")
                                if p.is_file() and not p.is_symlink())
        else:
            raise ValueError("Neither zip, 7z nor folder: %s" % path)
        if self._times is not None:
            self.names = sorted(self._times)

    @contextmanager
    def open(self, name):
        if self.zip:
            with zipfile.ZipFile(self.path) as z, z.open(name) as f:
                yield f
        else:
            with open((_dir_7z(self.path) if self.sevenz else self.path) / name, "rb") as f:
                yield f

    def heads(self, names, n=4096):
        """(name, first n bytes) for each of the files; a zip is opened only once for this."""
        if self.zip:
            with zipfile.ZipFile(self.path) as z:
                for name in names:
                    with z.open(name) as f:
                        yield name, f.read(n)
            return
        base = _dir_7z(self.path) if self.sevenz else self.path
        for name in names:
            with open(base / name, "rb") as f:
                yield name, f.read(n)

    def mtime(self, name):
        """Last change of a file (Unix time), as recorded in the export."""
        if self._times is not None:
            return self._times[name]
        return (self.path / name).stat().st_mtime

    def json(self, name):
        with self.open(name) as f:
            return json.load(f)


def exporter_of(ex):
    return next((m for m in EXPORTERS if m.detect(ex)), None)


def checksum(m, ex):
    """SHA-256 over names and contents of the export's files."""
    h = hashlib.sha256()
    for name in sorted(m.parts(ex)):
        d = hashlib.sha256()
        with ex.open(name) as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                d.update(b)
        h.update(name.encode() + b"\0" + d.digest())
    return h.hexdigest()


def stored_exports(m):
    """Stored exports of a source, the newest first (by the date in the name)."""
    d = sources.EXPORTS / m.PLATFORM
    if not d.is_dir():
        return []
    return [Export(p, stored=True) for p in sorted(d.iterdir(), key=lambda p: p.name, reverse=True)
            if not p.name.startswith(".")]


# ---------------------------------------------------------------- Store raw export

def _free(target):
    """Free name: x.zip, x-2.zip, x-3.zip ..."""
    stem, dot, rest = target.name.partition(".")
    k = 1
    while target.exists():
        k += 1
        target = target.with_name("%s-%d%s%s" % (stem, k, dot, rest))
    return target


def store(m, ex):
    """Copy an export to exports/<source>/ -> (target path, files not taken over)."""
    when = m.date(ex)
    day = when.strftime("%Y-%m-%d") if when else time.strftime("%Y-%m-%d", time.gmtime(ex.path.stat().st_mtime))
    folder = sources.EXPORTS / m.PLATFORM
    folder.mkdir(parents=True, exist_ok=True)
    target = _free(folder / ("%s_%s" % (day, ex.name)))
    if ex.archive:
        backup.copy_file(ex.path, target, ex.path.stat())
        return target, []
    parts = set(m.parts(ex))
    tmp = target.with_name("." + target.name + TMP)
    if tmp.exists():
        shutil.rmtree(tmp)
    for name in sorted(parts):
        src = ex.path / name
        backup.copy_file(src, tmp / name, src.stat())
    os.replace(tmp, target)
    return target, sorted({n.split("/")[0] + ("/" if "/" in n else "") for n in ex.names if n not in parts})


# ---------------------------------------------------------------- Build corpus

def _file_name(doc, taken):
    """<created>_<title>_<id8>.json; stays the same as long as title and ID do."""
    m = doc.get("metadata") or {}
    title = re.sub(r"[^\w\s-]", "", m.get("title") or "").strip()
    title = re.sub(r"\s+", "_", title)[:80].strip("_") or "untitled"
    name = "%s_%s_%s.json" % ((m.get("created_at") or "no-date")[:10], title, doc["id"][:8])
    if name in taken:
        name = name[:-5] + "_" + doc["id"] + ".json"
    taken.add(name)
    return name


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("." + p.name + TMP)
    tmp.write_bytes(data)
    os.replace(tmp, p)


def _same(p, data):
    try:
        return p.stat().st_size == len(data) and p.read_bytes() == data
    except FileNotFoundError:
        return False


def _core(doc, *fields):
    """Document without source.exported_at or metadata.rescued_from, for comparing."""
    d = dict(doc, source=dict(doc.get("source") or {}), metadata=dict(doc.get("metadata") or {}))
    if "exported_at" in fields:
        d["source"].pop("exported_at", None)
    if "rescued_from" in fields:
        d["metadata"].pop("rescued_from", None)
    return d


def build_corpus(m):
    """Rebuild corpus/<source>/ from all stored exports -> report (dict).

    Call under the lock of backup.py: deleting for good
    (cleanup.py) sets its gravestone under the same lock."""
    exports = stored_exports(m)
    target = sources.CORPUS_DIR / m.PLATFORM
    graves = chronicle_db.gravestones()
    existing = {}                             # id -> (path, document)
    if target.is_dir():
        for p in target.glob("*.json"):
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
                existing[doc["id"]] = (p, doc)
            except (OSError, ValueError, KeyError, TypeError):
                continue

    # Earlier IDs from chronicle.db
    ids = chronicle_db.ids(m.PLATFORM)
    chats, extra_files, n = m.build(exports, graves, ids)
    gone = {d["id"] for d in chats if d["id"] in graves or (d.get("source") or {}).get("original_id") in graves}
    if gone:                                  # build() should already leave them out
        n["gravestone"] += len(gone)
        chats = [d for d in chats if d["id"] not in gone]

    # A chat keeps its ID; digests, clutter marks and gravestones hang on it
    before = {(doc.get("source") or {}).get("original_id"): i for i, (_p, doc) in existing.items()}
    differ = [d for d in chats if before.get(d["source"].get("original_id"), d["id"]) != d["id"]]
    if differ:
        raise RuntimeError("The build would give %d chats in %s a different ID, e.g. %s “%s” (was %s); nothing "
                           "changed." % (len(differ), _show(target), differ[0]["id"][:8],
                                         sources.oneline(differ[0]["metadata"].get("title"), 60),
                                         before[differ[0]["source"]["original_id"]][:8]))
    target.mkdir(parents=True, exist_ok=True)
    report = {"exports": [e.name for e in exports], "new": [], "grown": [], "newly_older": []}
    keep, taken = set(), set()
    for doc in chats:
        p = target / _file_name(doc, taken)
        keep.add(p)
        prev = existing.pop(doc["id"], None)
        older = doc["metadata"].get("rescued_from")
        if older:
            n["older"] += 1
        if prev is None:
            kind, write = "new", True
        elif _core(prev[1], "exported_at") == _core(doc, "exported_at"):
            kind, write = "unchanged", False  # file stays, with the exported_at of the earlier export
        elif _core(prev[1], "exported_at", "rescued_from") == _core(doc, "exported_at", "rescued_from"):
            kind, write = "unchanged", True   # only the origin changed
        elif len(doc["messages"]) > len(prev[1].get("messages") or []):
            kind, write = "grown", True
        else:
            kind, write = "changed", True
        if write:
            _write(p, json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"))
        if prev and prev[0] != p:             # title changed, so the file name did too
            if write:
                prev[0].unlink()
            else:
                os.replace(prev[0], p)
        n[kind] += 1
        if kind in ("new", "grown"):
            report[kind].append(doc)
        if older and prev and not (prev[1].get("metadata") or {}).get("rescued_from"):
            report["newly_older"].append(doc)
    n["dropped"] = len(existing)
    report["dropped"] = [doc for _p, doc in existing.values()]

    for rel, content in extra_files.items():
        p = target / rel
        keep.add(p)
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        if not _same(p, data):
            _write(p, data)
    for dirpath, dirs, files in os.walk(target, topdown=False):
        for f in files:
            p = Path(dirpath, f)
            if p not in keep:
                p.unlink()
        if Path(dirpath) != target and not os.listdir(dirpath):
            os.rmdir(dirpath)
    n["chats"] = len(chats)
    report["n"] = n
    return report


# ---------------------------------------------------------------- Run

def run(paths, wait=600):
    """Store exports and rebuild the affected corpora; without paths rebuild all.

    -> {stored: [(source, target, not taken over)], present: [(source, target)],
        errors: [text], corpus: {platform: report}}"""
    out = {"stored": [], "present": [], "errors": [], "corpus": {}}
    with backup.lock(wait) as free:
        if not free:
            raise RuntimeError("A backup or another import is running, nothing imported")
        try:
            _run(paths, out)
        finally:
            remove_unpacked()
    return out


def _run(paths, out):
    sources.EXPORTS.mkdir(mode=0o700, exist_ok=True)
    sources.CORPUS_DIR.mkdir(mode=0o700, exist_ok=True)
    rebuild = [] if paths else [m for m in EXPORTERS if stored_exports(m)]
    sums = {}                                 # platform -> {checksum: stored export}
    for path in paths:
        path = Path(path).expanduser()
        try:
            ex = Export(path)
            m = exporter_of(ex)
        except (OSError, ValueError, zipfile.BadZipFile, subprocess.CalledProcessError) as e:
            out["errors"].append("%s: %s" % (path, e))
            continue
        if m is None:
            out["errors"].append("%s: not a known export format" % path)
            continue
        if m.PLATFORM not in sums:
            sums[m.PLATFORM] = {checksum(m, e): e.path for e in stored_exports(m)}
        sha = checksum(m, ex)
        if sha in sums[m.PLATFORM]:
            out["present"].append((path, sums[m.PLATFORM][sha]))
            continue
        target, left_out = store(m, ex)
        sums[m.PLATFORM][sha] = target
        out["stored"].append((path, target, left_out))
        if m not in rebuild:
            rebuild.append(m)
    for m in rebuild:
        out["corpus"][m.PLATFORM] = build_corpus(m)


def _show(p):
    p = Path(p)
    if p.is_relative_to(sources.ARCHIVE):
        return p.relative_to(sources.ARCHIVE).as_posix() + ("/" if p.is_dir() else "")
    return ("~/" + p.relative_to(sources.HOME).as_posix()) if p.is_relative_to(sources.HOME) else str(p)


def report_lines(r, limit=15):
    """Report of run() as lines of text."""
    out = []
    for src, target, left_out in r["stored"]:
        out.append("stored: %s  (from %s)" % (_show(target), _show(src)))
        if left_out:
            out.append("  not part of the export, not taken over: " + ", ".join(left_out))
    for src, target in r["present"]:
        out.append("already there: %s = %s" % (_show(src), _show(target)))
    for platform, b in r["corpus"].items():
        n = b["n"]
        out.append("%s: %d chats from %d exports in %s" % (platform, n["chats"], len(b["exports"]),
                                                          _show(sources.CORPUS_DIR / platform)))
        out.append("  %d new, %d grown, %d changed, %d unchanged, %d dropped"
                   % (n["new"], n["grown"], n["changed"], n["unchanged"], n["dropped"]))
        out.append("  %d only in older exports now, %d of them since the last build"
                   % (n["older"], len(b["newly_older"])))
        out.append("  skipped: %d with a gravestone, %d without content" % (n["gravestone"], n["empty"]))
        for key, label in (("new", "new"), ("grown", "grown"), ("newly_older", "only in older"),
                           ("dropped", "dropped")):
            docs = b[key]
            for d in docs[:limit]:
                m = d.get("metadata") or {}
                out.append("  %-20s %s  %s  %s" % (label + ":", d["id"][:8], (m.get("updated_at") or "")[:10],
                                                   sources.oneline(m.get("title"), 70)))
            if len(docs) > limit:
                out.append("  %-20s … and %d more" % (label + ":", len(docs) - limit))
    if not r["stored"] and not r["corpus"] and not r["errors"]:
        out.append("Nothing new, nothing imported.")
    out += ["Error: " + f for f in r["errors"]]
    return out
