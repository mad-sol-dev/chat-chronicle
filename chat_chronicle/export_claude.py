"""claude.ai exports: detect, read and convert to unified JSON.

The converter comes from chat_archive (scripts/claude_export_to_unified.py,
as of October 2026). Text, truncation, metadata.parts and the IDs (uuid5 of the
conversation UUID) are the same; what's new is the handling around it (ingest.py):

  * An export is a zip or an unpacked folder: conversations.json with
    chat_messages, plus projects.json or projects/*.json, users.json,
    memories.json.
  * The build uses all stored exports at once, the newest first. A chat comes
    from the newest export that has it with content. Exports contain husks of
    deleted chats (structure without text); older exports fill them in, and a
    chat that is only in older ones stays. This used to be --extra-export,
    metadata.rescued_from names the export.
  * Chats without messages are left out (formerly --skip-empty), chats with a
    gravestone too, along with their attachments.
  * source.exported_at is the time of the export the chat comes from, not of
    the conversion. If a chat comes back unchanged in a newer export, ingest.py
    keeps its file and with it this time.

Tool traffic is compacted, not dropped: thinking is dropped, tool_use keeps
name and truncated arguments, tool_result is truncated, search hits stay as
title, URL and a short excerpt. Attachments (extracted_content) and project
knowledge come back as separate files; ingest.py writes them.
"""
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone

PLATFORM = "claude.ai"
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # NAMESPACE_URL

TOOL_RESULT_CHARS = 1500   # chars per tool_result (0 = leave out entirely)
KNOWLEDGE_CHARS = 240      # chars of excerpt per search hit
TOOL_INPUT_CHARS = 300     # chars per tool_use argument
KEEP_THINKING = False      # keep thinking blocks

# Time in the name: data-2025-03-20-20-37-01... (until 2025) or
# data-<account UUID>-<unix time>-<hex>-batch-0000 (since 2026), both UTC
NAME_DATE = re.compile(r"data-(\d{4})-(\d\d)-(\d\d)-(\d\d)-(\d\d)-(\d\d)")
NAME_UNIX = re.compile(r"data-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}-(\d{10})-")


# ---------------------------------------------------------------- Detect export

def detect(ex):
    """conversations.json with chat_messages (ChatGPT has mapping there)."""
    if "conversations.json" not in ex.names:
        return False
    with ex.open("conversations.json") as f:
        head = f.read(1 << 16)
    return head.lstrip()[:1] == b"[" and b'"chat_messages"' in head


def parts(ex):
    """Files of the export: the JSON files at the top and projects/. Unpacked
    folders often have other things next to them (such as conversations/ from
    the former split per chat); those don't belong to it."""
    return [n for n in ex.names if ("/" not in n and n.endswith(".json")) or n.startswith("projects/")]


def timestamp(name):
    """Time of the export from its name (UTC) or None."""
    m = NAME_DATE.search(name)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=timezone.utc)
    m = NAME_UNIX.search(name)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    return None


def _latest(convs):
    """Newest change in the export; fallback when the name has no timestamp."""
    ts = max((c.get("updated_at") or "" for c in convs), default="")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def date(ex):
    """Time of the export (UTC): from the name, otherwise the newest change inside."""
    return timestamp(ex.original_name) or _latest(ex.json("conversations.json"))


def chat_id(conv_uuid):
    return str(uuid.uuid5(NS, "claude:" + conv_uuid))


# ---------------------------------------------------------------- Convert (from chat_archive)

def sanitize(raw_title):
    if not raw_title or not raw_title.strip():
        return "untitled"
    s = re.sub(r"[^\w\s-]", "", raw_title, flags=re.UNICODE)
    s = s.strip().replace(" ", "_")
    return (s[:100] or "untitled")


def iso(ts):
    if not ts:
        return None
    return ts.replace("+00:00", "Z")


def clip(text, limit):
    """Truncate to limit chars; returns (text, chars thrown away)."""
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text, 0
    return text[:limit].rstrip(), len(text) - limit


def result_text(block, snippet_chars=240):
    """Pull the text out of a tool_result block (content is a list).

    Search hits (type "knowledge") are rendered as title + URL + a short
    excerpt - that is their actual value as a signpost.
    """
    parts = []
    content = block.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    for c in content:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            parts.append(c.get("text", ""))
        elif t == "knowledge":
            snip, _ = clip((c.get("text") or "").replace("\n", " "),
                           snippet_chars)
            head = "- %s <%s>" % (c.get("title") or "untitled",
                                  c.get("url") or "")
            parts.append(head + ("\n  " + snip if snip else ""))
        elif t == "local_resource":
            parts.append("- [File] %s (%s) %s" % (
                c.get("name") or "", c.get("mime_type") or "",
                c.get("file_path") or ""))
        elif t == "image":
            parts.append("- [Image %s]" % (c.get("file_uuid") or ""))
        elif t == "image_gallery":
            parts.append("- [Image gallery, %d images]" %
                         len(c.get("images") or []))
        else:
            parts.append("- [%s]" % (t or "block"))
    return "\n".join(p for p in parts if p)


def render_message(msg, idx, att_files, conv_uuid, stats):
    """Turn one Claude message into a unified message entry.

    Split-out attachments go into att_files (path in the corpus folder -> text)."""
    role = "assistant" if msg.get("sender") == "assistant" else "user"
    blocks = msg.get("content") or []
    text_field = (msg.get("text") or "").strip()

    pieces = []                # (kind, text)
    meta = {}
    tools = []
    thinking_n = 0
    dropped = 0
    have_text_block = False

    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")

        if t == "text":
            have_text_block = True
            txt = (b.get("text") or "").strip()
            if txt:
                pieces.append(("text", txt))

        elif t == "thinking":
            thinking_n += 1
            if KEEP_THINKING:
                pieces.append(("thinking", "<<thinking>> " + (b.get("thinking") or "").strip()))
            else:
                dropped += len(b.get("thinking") or "")

        elif t == "tool_use":
            name = b.get("name") or "tool"
            tools.append(name)
            arg = json.dumps(b.get("input"), ensure_ascii=False,
                             sort_keys=True) if b.get("input") else ""
            arg, cut = clip(arg, TOOL_INPUT_CHARS)
            dropped += cut
            suffix = " ..." if cut else ""
            pieces.append(("tool", "<<tool %s>> %s%s" % (name, arg, suffix)))

        elif t == "tool_result":
            name = b.get("name") or "tool"
            body = result_text(b, KNOWLEDGE_CHARS)
            body, cut = clip(body, TOOL_RESULT_CHARS)
            dropped += cut
            tail = " ... [+%d chars truncated]" % cut if cut else ""
            flag = " ERROR" if b.get("is_error") else ""
            pieces.append(("result", "<<result %s%s>> %s%s" % (name, flag, body, tail)))

        else:
            pieces.append((t or "block", "<<%s>>" % (t or "block")))

    # Safety net: if there were no text blocks, take the text field
    if not have_text_block and text_field:
        pieces.insert(0, ("text", text_field))

    # Split out attachments
    atts = []
    for a in msg.get("attachments") or []:
        entry = {k: a.get(k) for k in ("file_name", "file_size", "file_type")}
        content = a.get("extracted_content") or ""
        if content and att_files is not None:
            fn = "%03d_%s.txt" % (idx, sanitize(a.get("file_name") or "file"))
            rel = "attachments/%s/%s" % (conv_uuid, fn)
            att_files[rel] = content
            entry["extracted_path"] = rel
            entry["extracted_chars"] = len(content)
            stats["attachments"] += 1
        atts.append(entry)

    for f in msg.get("files") or []:
        atts.append({"file_name": f.get("file_name"), "file_type": "upload"})

    pieces = [(k, p.strip()) for k, p in pieces if p and p.strip()]
    content = "\n\n".join(p for _, p in pieces)
    # Section lengths: tool results can contain blank lines; without this, the
    # text alone doesn't reliably show where the answer continues.
    if any(k != "text" for k, _ in pieces):
        meta["parts"] = [[k, len(p)] for k, p in pieces]
    if tools:
        meta["tools"] = sorted(set(tools))
    if thinking_n:
        meta["thinking_blocks"] = thinking_n
    if dropped:
        meta["dropped_chars"] = dropped
    if atts:
        meta["attachments"] = atts

    stats["dropped_chars"] += dropped
    stats["tool_calls"] += len(tools)

    if not content and not atts:
        return None

    return {
        "index": idx,
        "role": role,
        "content": content,
        "timestamp": iso(msg.get("created_at")),
        "metadata": meta,
    }


def convert_conversation(conv, projects, att_files, exported_at, rescued_from):
    conv_uuid = conv.get("uuid", "unknown")
    name = (conv.get("name") or "").strip()
    summary = (conv.get("summary") or "").strip()
    title = name or (summary[:50] if summary else "chat_%s" % conv_uuid[:8])

    stats = {"dropped_chars": 0, "tool_calls": 0, "attachments": 0}
    messages = []
    for idx, msg in enumerate(conv.get("chat_messages") or []):
        entry = render_message(msg, idx, att_files, conv_uuid, stats)
        if entry:
            entry["index"] = len(messages)
            messages.append(entry)

    all_tools = sorted({t for m in messages
                        for t in m["metadata"].get("tools", [])})

    return {
        "version": "1.0",
        "id": chat_id(conv_uuid),
        "source": {
            "platform": "claude",
            "original_id": conv_uuid,
            "exported_at": exported_at,
        },
        "metadata": {
            "title": title,
            "created_at": iso(conv.get("created_at")),
            "updated_at": iso(conv.get("updated_at")),
            "message_count": len(messages),
            "model": "claude",
            "tags": [],
            "summary": summary or None,
            "project": projects.get(conv_uuid),
            "tools_used": all_tools,
            "tool_calls": stats["tool_calls"],
            "attachments": stats["attachments"],
            "dropped_chars": stats["dropped_chars"],
            "compact": TOOL_RESULT_CHARS > 0 or not KEEP_THINKING,
            "rescued_from": rescued_from,
        },
        "messages": messages,
        "raw": {"format": "claude", "preserved_fields": {}},
    }


def substance(conv):
    """Rough measure of the actual content of a conversation.

    Deliberately strict: a mere file reference without text doesn't count,
    otherwise an export with empty husks would push out a better one.
    """
    total = 0
    for m in conv.get("chat_messages") or []:
        total += len((m.get("text") or "").strip())
        for b in m.get("content") or []:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                total += len((b.get("text") or "").strip())
            elif t == "thinking":
                total += len((b.get("thinking") or "").strip())
            elif t in ("tool_use", "tool_result"):
                total += 50
        for a in m.get("attachments") or []:
            total += len(a.get("extracted_content") or "")
    return total


# ---------------------------------------------------------------- Projects

def read_projects(ex):
    """Projects of an export: projects/<uuid>.json (since 2026-08) or a
    list in projects.json (before)."""
    if "projects.json" in ex.names:
        out = ex.json("projects.json")
        return [p for p in out if isinstance(p, dict)] if isinstance(out, list) else []
    out = []
    for n in ex.names:
        if n.startswith("projects/") and n.endswith(".json") and n.count("/") == 1:
            try:
                p = ex.json(n)
            except ValueError:
                continue
            if isinstance(p, dict):
                out.append(dict(p, uuid=p.get("uuid") or n[9:-5]))
    return out


def project_map(projects):
    """conv_uuid -> project name.

    Note: the Claude export does NOT link projects to conversations.
    Only explicit conversation lists are used; "docs" are
    project knowledge files, not chats."""
    out = {}
    for p in projects:
        pname = p.get("name") or p.get("uuid")
        for key in ("conversations", "chat_conversations"):
            for c in p.get(key) or []:
                if isinstance(c, dict) and c.get("uuid"):
                    out[c["uuid"]] = pname
    return out


def project_docs(projects):
    """Project knowledge (docs) as Markdown -> {project_docs/<file>: text}, with _catalog.json."""
    out, catalog = {}, []
    for p in sorted(projects, key=lambda p: p.get("uuid") or ""):
        pname = p.get("name") or p.get("uuid")
        docs = p.get("docs") or []
        catalog.append({"uuid": p.get("uuid"), "name": pname,
                        "description": p.get("description") or "",
                        "created_at": p.get("created_at"),
                        "prompt_template": p.get("prompt_template") or "",
                        "doc_count": len(docs)})
        for d in docs:
            fname = "%s__%s.md" % (sanitize(pname),
                                   sanitize(d.get("filename") or
                                            d.get("uuid", "doc")))
            body = d.get("content") or ""
            head = "# %s\n\nProject: %s\nFile: %s\n\n---\n\n" % (
                d.get("filename") or "Document", pname,
                d.get("filename") or "")
            out["project_docs/" + fname[:180]] = head + body
    if catalog:
        out["project_docs/_catalog.json"] = json.dumps(catalog, ensure_ascii=False, indent=2)
    return out


# ---------------------------------------------------------------- Build

def build(exports, graves, ids=None):
    """Corpus from all exports -> (chats, files, n).

    exports  stored exports, the newest first
    graves   chronicle_db.gravestones(): chat IDs and conversation UUIDs
    ids      earlier IDs from chronicle.db; empty for claude.ai, where the IDs
             have always been uuid5 of the conversation UUID
    chats    unified documents, newest change first
    files    path in the corpus folder -> text (attachments/<uuid>/..., project_docs/...)
    n        Counter: gravestone (skipped), empty (no messages)

    A chat comes from the newest export that has it with content; if none
    has it with content, the husk from the newest one stays. Projects too:
    the newest version of each project, also from older exports."""
    n = Counter()
    chosen, husks, projects = {}, {}, {}
    buried = set()
    for rank, ex in enumerate(exports):
        convs = ex.json("conversations.json")
        when = (timestamp(ex.original_name) or _latest(convs) or datetime(1970, 1, 1, tzinfo=timezone.utc))
        origin = (when.isoformat().replace("+00:00", "Z"), ex.name if rank else None)
        for p in read_projects(ex):
            projects.setdefault(p.get("uuid"), p)
        for conv in convs:
            u = conv.get("uuid") if isinstance(conv, dict) else None
            if not u or u in chosen or u in buried:
                continue
            if u in graves or chat_id(u) in graves:
                buried.add(u)
                continue
            if substance(conv) > 0:
                chosen[u] = (conv, origin)
                husks.pop(u, None)
            else:
                husks.setdefault(u, (conv, origin))
        del convs
    chosen.update(husks)
    n["gravestone"] = len(buried)

    projects = [p for k, p in projects.items() if k]
    proj_of = project_map(projects)
    chats, files = [], project_docs(projects)
    for u, (conv, (exported_at, rescued_from)) in chosen.items():
        att_files = {}
        doc = convert_conversation(conv, proj_of, att_files, exported_at, rescued_from)
        if not doc["messages"]:
            n["empty"] += 1
            continue
        chats.append(doc)
        files.update(att_files)
    chats.sort(key=lambda d: d["metadata"]["updated_at"] or "", reverse=True)
    return chats, files, n
