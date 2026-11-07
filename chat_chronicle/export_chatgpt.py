"""ChatGPT exports: detect, read and convert to unified JSON.

The converter is the one from chat_archive (scripts/convert_to_unified.py,
ChatGPTConverter, as of December 2025), output as before; there it ran on the
single files of split_conversations.py. What's new is the handling around it (ingest.py):

  * An export is a zip, a 7z or an unpacked folder: conversations.json
    (a list, each chat with mapping), chat.html, user.json and further JSON
    files, images (file-..., file_...), audio and images per chat in
    <conversation-id>/, plus dalle-generations/ and user-<id>/.
  * The build uses all stored exports at once, the newest first; a chat
    comes from the newest export that has it. One deleted on the platform is
    kept that way, metadata.rescued_from names the export.
  * IDs: those of the old converter (random) are in chronicle.db and still
    apply; new chats get a uuid5 of the conversation_id. Chats with a
    gravestone are left out.
  * source.exported_at is the time of the export, not of the conversion.

As before, little is read: of each message only the text parts
(content.parts as strings), from all branches of the mapping sorted by
time. Code and output of the code interpreter, thoughts of the reasoning
models, search results, voice transcripts and attachments are dropped. A chat
without any text stays as an empty husk, also as before.

An export format with one file per chat instead of conversations.json
(according to chat_archive it exists by now) is not recognised by this
module; no example is at hand.
"""
import re
import uuid
from collections import Counter
from datetime import datetime, timezone

PLATFORM = "chatgpt"
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL as for claude.ai

# <account hash>-2025-12-29-03-04-12-<hex32>.zip, time in UTC
NAME_DATE = re.compile(r"-(\d{4})-(\d\d)-(\d\d)-(\d\d)-(\d\d)-(\d\d)-[0-9a-f]{32}")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------- Detect export

def detect(ex):
    """conversations.json with mapping (claude.ai has chat_messages there)."""
    if "conversations.json" not in ex.names:
        return False
    with ex.open("conversations.json") as f:
        head = f.read(1 << 16)
    return head.lstrip()[:1] == b"[" and b'"mapping"' in head


def parts(ex):
    """Files of the export: everything at the top except notes (*.md), plus the folders
    per chat (<conversation-id>/), dalle-generations/ and user-<id>/. Unpacked folders
    often have other things next to them (conversations/ from split_conversations.py,
    another export in a subfolder); those don't belong to it."""
    out = []
    for n in ex.names:
        top, _, rest = n.partition("/")
        if top.startswith("."):
            continue
        if (not rest and not n.endswith(".md")) or \
                (rest and (top == "dalle-generations" or top.startswith("user-") or UUID_RE.match(top))):
            out.append(n)
    return out


def timestamp(name):
    """Time of the export from its name (UTC) or None."""
    m = NAME_DATE.search(name)
    return datetime(*map(int, m.groups()), tzinfo=timezone.utc) if m else None


def _latest(convs):
    """Newest change in the export; fallback when the name has no timestamp."""
    ts = max((c.get("update_time") or c.get("create_time") or 0 for c in convs if isinstance(c, dict)),
             default=0)
    return datetime.fromtimestamp(ts, timezone.utc) if ts else None


def date(ex):
    """Time of the export (UTC): from the name, otherwise the newest change inside."""
    return timestamp(ex.original_name) or _latest(ex.json("conversations.json"))


def chat_id(conv_id):
    return str(uuid.uuid5(NS, "chatgpt:" + conv_id))


# ---------------------------------------------------------------- Convert (from chat_archive)

def _iso(ts):
    """Unix time -> ISO time in UTC with Z, as before utcfromtimestamp(ts).isoformat() + "Z"."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None).isoformat() + "Z" if ts else None


def convert_conversation(data, cid, exported_at, rescued_from):
    conv_id = data.get("conversation_id") or data.get("id", "unknown")
    created_ts = data.get("create_time", 0.0)
    updated_ts = data.get("update_time", created_ts)

    # all nodes of the mapping with content, discarded branches too, by time
    nodes = [n["message"] for n in (data.get("mapping") or {}).values()
             if isinstance(n, dict) and n.get("message") and n["message"].get("content")]
    nodes.sort(key=lambda msg: msg.get("create_time", 0) or 0)

    messages = []
    for idx, msg in enumerate(nodes):
        content = "".join(p for p in (msg.get("content") or {}).get("parts") or [] if isinstance(p, str))
        if content.strip():
            messages.append({
                "index": idx,
                "role": (msg.get("author") or {}).get("role", "user"),
                "content": content.strip(),
                "timestamp": _iso(msg.get("create_time", 0)),
                "metadata": {},
            })

    return {
        "version": "1.0",
        "id": cid,
        "source": {
            "platform": "chatgpt",
            "original_id": conv_id,
            "exported_at": exported_at,
        },
        "metadata": {
            "title": data.get("title", "Untitled"),
            "created_at": _iso(created_ts),
            "updated_at": _iso(updated_ts),
            "message_count": len(messages),
            "model": data.get("default_model_slug", "unknown"),
            "tags": [],
            "rescued_from": rescued_from,
        },
        "messages": messages,
        "raw": {"format": "chatgpt", "preserved_fields": {}},
    }


# ---------------------------------------------------------------- Build

def build(exports, graves, ids=None):
    """Corpus from all exports -> (chats, files, n).

    exports  stored exports, the newest first
    graves   chronicle_db.gravestones(): chat IDs and conversation_ids
    ids      earlier IDs from chronicle.db: conversation_id -> chat ID
    chats    unified documents, newest change first
    files    always empty, the converter takes over no attachments
    n        Counter: gravestone (skipped)"""
    ids = ids or {}
    n = Counter()
    chosen, buried = {}, set()
    for rank, ex in enumerate(exports):
        convs = ex.json("conversations.json")
        when = timestamp(ex.original_name) or _latest(convs) or datetime(1970, 1, 1, tzinfo=timezone.utc)
        exported_at = when.isoformat().replace("+00:00", "Z")
        for conv in convs:
            o = (conv.get("conversation_id") or conv.get("id")) if isinstance(conv, dict) else None
            if not o or o in chosen or o in buried:
                continue
            cid = ids.get(o) or chat_id(o)
            if o in graves or cid in graves:
                buried.add(o)
                continue
            chosen[o] = convert_conversation(conv, cid, exported_at, ex.name if rank else None)
        del convs
    n["gravestone"] = len(buried)
    chats = sorted(chosen.values(), key=lambda d: d["metadata"]["updated_at"] or "", reverse=True)
    return chats, {}, n
