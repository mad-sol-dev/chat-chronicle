"""Gemini chats from Google AI Studio: detect, read and convert to unified JSON.

AI Studio stores every chat (prompt) as a file in a folder in Google Drive,
next to uploaded files and voice recordings. An export is a download of this
folder from Drive: a zip (several for large folders) or an unpacked folder.
A chat is a file, usually without extension, with JSON that starts with
runSettings; its name is the chat's title (Drive replaces characters like
: and ? with _). Subfolders don't matter: a download may be sorted into
topic folders afterwards, chat and ID depend only on the file name.

The messages come as before (chat_archive, scripts/convert_to_unified.py,
GeminiConverter) from chunkedPrompt.chunks: one message per chunk with text,
role user or model, index = number of the chunk; thoughts (isThought) appear
like answer text. New:

  * The title is the file name, the time (created_at = updated_at) the last
    change of the file in Drive, as the download carries it. The old way took
    both from the name of its intermediate copy (<n>_GEM_<title>_<date>.json).
  * Chunks with a file become messages with metadata.attachments. What is
    embedded in the chat itself (inlineImage, inlineAudio, inlineFile, inlineData)
    ends up in attachments/<chat ID>/ in the corpus folder, as for claude.ai.
    The chat names files from Drive (driveAudio, driveImage, driveDocument,
    driveVideo) only by their Drive ID, which a download doesn't carry; they
    are only in the raw export, the corpus notes the ID.
  * IDs: those of the old converter (random) are in chronicle.db and still
    apply; new chats get a uuid5 of the file name. Renaming a chat in AI
    Studio gives a new one in the next download; the old one stays from the
    older export.
  * Chats without messages are left out, those with a gravestone too.
"""
import base64
import binascii
import re
import uuid
from collections import Counter
from datetime import datetime, timezone

PLATFORM = "gemini"
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL as for claude.ai

# Drive download: <folder>-20251228T101500Z-1-001.zip, time in UTC
NAME_DATE = re.compile(r"(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z")
DRIVE = {"driveAudio": "Audio from Drive", "driveImage": "Image from Drive",
         "driveDocument": "Document from Drive", "driveVideo": "Video from Drive"}
INLINE = ("inlineImage", "inlineAudio", "inlineFile")
EXTENSION = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif",
             "audio/ogg": ".ogg", "audio/webm": ".webm", "audio/mpeg": ".mp3", "audio/wav": ".wav",
             "video/mp4": ".mp4", "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
             "text/x-python": ".py", "application/json": ".json"}


# ---------------------------------------------------------------- Detect export

def _chats(ex):
    """Chat files of an export -> {file name: path in the export}; if a name is in
    two folders, the newer file wins."""
    out = {}
    for name, head in ex.heads(ex.names):
        title = name.rsplit("/", 1)[-1]
        if title.startswith(".") or not (head.lstrip()[:1] == b"{" and b'"runSettings"' in head):
            continue
        if title not in out or ex.mtime(name) > ex.mtime(out[title]):
            out[title] = name
    return out


def detect(ex):
    """At least one file with AI Studio JSON (starts with runSettings)."""
    return any(head.lstrip()[:1] == b"{" and b'"runSettings"' in head for _n, head in ex.heads(ex.names))


def parts(ex):
    """The whole Drive folder belongs to it, without hidden files (.directory etc.)."""
    return [n for n in ex.names if not any(t.startswith(".") for t in n.split("/"))]


def timestamp(name):
    """Time of the download from its name (UTC) or None."""
    m = NAME_DATE.search(name)
    return datetime(*map(int, m.groups()), tzinfo=timezone.utc) if m else None


def _latest(ex, chats):
    """Newest change of a chat; fallback when the name has no timestamp."""
    ts = max((ex.mtime(rel) for rel in chats.values()), default=0)
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def date(ex):
    """Time of the download (UTC): from the name, otherwise the newest change inside."""
    return timestamp(ex.original_name) or _latest(ex, _chats(ex))


def chat_id(title):
    return str(uuid.uuid5(NS, "gemini:" + title))


# ---------------------------------------------------------------- Convert

def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _attachments(chunk, idx, cid, att_files):
    """Files of a chunk -> entries for metadata.attachments; what is embedded in the
    chat goes into att_files (path in the corpus folder -> bytes)."""
    atts = []
    for k, label in DRIVE.items():
        if isinstance(chunk.get(k), dict):
            atts.append({"file_name": label, "file_type": k, "drive_id": chunk[k].get("id")})
    inline = [(k, chunk[k]) for k in INLINE if isinstance(chunk.get(k), dict)]
    if not inline:                            # generated files are only in parts
        inline = [("inlineData", p["inlineData"]) for p in chunk.get("parts") or []
                  if isinstance(p, dict) and isinstance(p.get("inlineData"), dict)]
    for k, (kind, d) in enumerate(inline):
        try:
            data = base64.b64decode(d.get("data") or "")
        except (binascii.Error, ValueError, TypeError):
            continue
        if not data:
            continue
        mime = d.get("mimeType") or ""
        fn = "%03d_%s%s%s" % (idx, kind, "_%d" % (k + 1) if len(inline) > 1 else "", EXTENSION.get(mime, ".bin"))
        rel = "attachments/%s/%s" % (cid, fn)
        att_files[rel] = data
        atts.append({"file_name": fn, "file_type": mime, "file_size": len(data), "extracted_path": rel})
    return atts


def convert_chat(title, data, mtime, cid, exported_at, rescued_from, att_files):
    when = _iso(mtime) if mtime else None
    messages = []
    for idx, chunk in enumerate((data.get("chunkedPrompt") or {}).get("chunks") or []):
        if not isinstance(chunk, dict):
            continue
        text = (chunk.get("text") or "").strip()
        atts = _attachments(chunk, idx, cid, att_files)
        if text or atts:
            messages.append({
                "index": idx,
                "role": chunk.get("role", "user"),
                "content": text,
                "timestamp": None,
                "metadata": {"attachments": atts} if atts else {},
            })
    rs = data.get("runSettings") or {}
    return {
        "version": "1.0",
        "id": cid,
        "source": {
            "platform": "gemini",
            "original_id": title,
            "exported_at": exported_at,
        },
        "metadata": {
            "title": title,
            "created_at": when,
            "updated_at": when,
            "message_count": len(messages),
            "model": rs.get("model", "gemini") if rs else "gemini",
            "tags": [],
            "rescued_from": rescued_from,
        },
        "messages": messages,
        "raw": {"format": "gemini", "preserved_fields": {}},
    }


# ---------------------------------------------------------------- Build

def build(exports, graves, ids=None):
    """Corpus from all exports -> (chats, files, n).

    exports  stored exports, the newest first
    graves   chronicle_db.gravestones(): chat IDs and file names
    ids      earlier IDs from chronicle.db: file name -> chat ID
    chats    unified documents, newest change first
    files    path in the corpus folder -> bytes (attachments/<chat ID>/...)
    n        Counter: gravestone (skipped), empty (no messages)

    A chat comes from the newest export that has it. Parts of one download
    (several zips) carry the same timestamp and all count as the newest."""
    ids = ids or {}
    n = Counter()
    chosen, buried, files = {}, set(), {}
    newest = None
    for ex in exports:
        chats = _chats(ex)
        when = timestamp(ex.original_name) or _latest(ex, chats) or datetime(1970, 1, 1, tzinfo=timezone.utc)
        newest = newest or when
        exported_at = when.isoformat().replace("+00:00", "Z")
        for title, rel in sorted(chats.items()):
            if title in chosen or title in buried:
                continue
            cid = ids.get(title) or chat_id(title)
            if title in graves or cid in graves:
                buried.add(title)
                continue
            try:
                data = ex.json(rel)
            except ValueError:
                n["broken"] += 1
                continue
            if not isinstance(data, dict):
                continue
            att_files = {}
            doc = convert_chat(title, data, ex.mtime(rel), cid, exported_at,
                               ex.name if when < newest else None, att_files)
            if not doc["messages"]:
                n["empty"] += 1
                continue
            chosen[title] = doc
            files.update(att_files)
    n["gravestone"] = len(buried)
    chats = sorted(chosen.values(), key=lambda d: d["metadata"]["updated_at"] or "", reverse=True)
    return chats, files, n
