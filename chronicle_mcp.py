#!/usr/bin/env python3
"""chronicle_mcp -- MCP access to the chronicle (all of your own AI chats).

Built around zoom levels, so that a model doesn't dump whole chats into its
context: search_chats -> chat_overview -> find_in_chat / read_chat.

Chats marked as clutter are invisible to the server, even by their ID.
Marking and deleting are left out here on purpose: a human decides that,
not a model.

Needs the mcp package (>= 2.0), the rest is standard library:
    <venv>/bin/python chronicle_mcp.py                      stdio (Claude Desktop, Claude Code)
    <venv>/bin/python chronicle_mcp.py --http --port 5121   Streamable HTTP (e.g. Open WebUI)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chat_index                                # noqa: E402
import sources                                   # noqa: E402
from sources import oneline                      # noqa: E402
from mcp.server.mcpserver import MCPServer       # noqa: E402

MAX_CHARS = 40000          # upper limit for every single answer

mcp = MCPServer(
    "chat-chronicle",
    description="Archive of all of the user's own AI chats: Claude Code and Cowork (live), claude.ai, "
                "ChatGPT, Gemini. First search_chats or recent_chats, then chat_overview, then "
                "find_in_chat and read_chat for specific parts. Never read a whole chat unless necessary.")

idx = chat_index.Index()
digest_store = chat_index.DigestStore()


def _snip(s):
    return (s or "").replace("\x02", "»").replace("\x03", "«")


def _brief(row):
    """Short version for hit lists: first lines of the digest or of the provider's summary."""
    d = digest_store.get(row["id"])
    if d:
        return oneline(d["text"], 240)
    if row["summary"]:
        return oneline(row["summary"], 240)
    return ""


def _chat(row, branches=None):
    d = {"id": row["id"], "source": row["platform"], "instance": row["instance"] or None,
         "title": row["title"], "project": row["project"] or None,
         "first": sources.fmt_ts(row["first_ts"], "%Y-%m-%d %H:%M"),
         "last": sources.fmt_ts(row["last_ts"], "%Y-%m-%d %H:%M"),
         "messages_user": row["n_user"], "tool_calls": row["n_tools"], "text_chars": row["chars"],
         "brief": _brief(row) or None}
    br = (branches or {}).get(row["id"])
    if br:      # shared beginning is with the counterpart, hits here only come from the own part
        d["branch"] = {"kind": "earlier_transcript" if br["kind"] == "earlier" else "fork",
                       "of": br["head"] or br["branch_of"], "own_from_message": br["branch_at"]}
    return d


def _row(chat_id):
    idx.refresh()
    return idx.resolve(chat_id, clutter=False)


@mcp.tool()
def search_chats(query: str, limit: int = 8, source: str = "", project: str = "",
                 since: str = "", until: str = "", include_tools: bool = False) -> dict:
    """Full-text search across all chats.

    A chat is a hit if ALL terms occur somewhere in it (not necessarily in the
    same message). Terms are prefixes, "in quotes" is a phrase; with OR/NOT the
    expression applies per message.
    Returns per chat: header data, short version (if there is one) and the best
    snippets with their message number. Then call chat_overview(id).

    query:         search terms, e.g. 'docker compose' or '"exact phrase"'
    source:        optional claude-code, cowork, claude.ai, chatgpt or gemini
    project:       optional project path relative to ~ (Claude Code/Cowork only)
    since, until:  ISO date, e.g. 2026-09-01
    include_tools: also search tool calls/results and thinking
    """
    idx.refresh()
    kinds = ["user", "assistant"] + (["thinking", "tool"] if include_tools else [])
    c = idx.conn()
    try:
        res = chat_index.search(c, query, kinds, platform=source, project=project, since=since, until=until,
                                order="relevance", limit=max(1, min(limit, 50)))
    finally:
        c.close()
    out, branches = [], idx.branches()
    for r in res["results"]:
        d = _chat(r, branches)
        best = sorted(r["hits"], key=lambda h: h["r"])[:3]
        d["hits"] = len(r["hits"])
        d["snippets"] = [{"message": h["item"], "kind": h["kind"], "text": _snip(h["snip"])} for h in best]
        out.append(d)
    return {"total": res["total"], "results": out, "weak_terms": res["weak"]}


@mcp.tool()
def chat_overview(chat_id: str, max_chars: int = 6000) -> str:
    """Overview of a chat without reading all of it.

    Header data, digest (goal, result, decisions, open points) or the provider's
    summary, edited files and an outline with one line per message. The numbers
    in [square brackets] are message numbers for read_chat. chat_id may be an
    unambiguous prefix.
    """
    try:
        row = _row(chat_id)
    except ValueError as e:
        return str(e)
    meta, items, _tools, title = chat_index.load_chat(row)
    return chat_index.overview(row, meta, items, title, digest_store.get(row["id"]),
                               max(1000, min(max_chars, MAX_CHARS)), idx.family(row["id"]))


@mcp.tool()
def find_in_chat(chat_id: str, query: str, include_tools: bool = True, limit: int = 20) -> dict:
    """Find places inside a chat, with message number and snippet.

    Useful for finding, in a long chat, exactly the message you then read with
    read_chat(chat_id, start=message, end=message+1).
    """
    try:
        row = _row(chat_id)
    except ValueError as e:
        return {"error": str(e)}
    kinds = ["user", "assistant"] + (["thinking", "tool"] if include_tools else [])
    c = idx.conn()
    try:
        hits = chat_index.find_in_chat(c, row["id"], query, kinds, max(1, min(limit, 100)))
    finally:
        c.close()
    return {"chat_id": row["id"], "title": row["title"],
            "hits": [{"message": h["item"], "kind": h["kind"], "text": h["text"]} for h in hits]}


@mcp.tool()
def read_chat(chat_id: str, start: int = 0, end: int = -1, include_tools: bool = False,
              max_chars: int = 12000) -> dict:
    """Messages of a chat verbatim, as Markdown.

    start/end are message numbers from chat_overview or find_in_chat
    (end exclusive, -1 = to the end). If the answer is cut off, next_start
    says where it continues. include_tools shows tool calls and results
    (truncated) and thinking - considerably longer.
    """
    try:
        row = _row(chat_id)
    except ValueError as e:
        return {"error": str(e)}
    meta, items, _tools, title = chat_index.load_chat(row)
    text, nxt = chat_index.read_range(items, start, None if end < 0 else end, include_tools,
                                      max(1000, min(max_chars, MAX_CHARS)),
                                      who=chat_index.assistant_label(row["platform"]))
    return {"chat_id": row["id"], "title": title, "start": start, "next_start": nxt,
            "total_messages": len(items), "text": text}


@mcp.tool()
def recent_chats(days: int = 7, source: str = "", project: str = "", limit: int = 30) -> list:
    """Chats of the last few days, newest first, with a short version if there is one.

    For questions like "what did I work on last week?".
    """
    idx.refresh()
    since = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).strftime("%Y-%m-%dT%H:%M")
    c = idx.conn()
    try:
        rows = c.execute("SELECT * FROM chats WHERE n_user>0 AND last_ts>=? AND (?='' OR platform=?) "
                         "AND (?='' OR project=?) AND %s ORDER BY last_ts DESC LIMIT ?" % chat_index.VISIBLE,
                         (since, source, source, project, project, max(1, min(limit, 200)))).fetchall()
    finally:
        c.close()
    branches = idx.branches()
    return [_chat(r, branches) for r in rows]


@mcp.tool()
def archive_stats() -> dict:
    """Size of the archive: chats per source and instance, time span, projects, digests.

    Earlier transcripts (discarded states after a rewind in the desktop app)
    are not counted; they belong to their session."""
    idx.refresh()
    c = idx.conn()
    try:
        src = [dict(r) for r in c.execute(
            "SELECT platform AS source, instance, COUNT(*) AS chats, MIN(first_ts) AS first, "
            "MAX(last_ts) AS last FROM chats WHERE n_user>0 AND %s GROUP BY platform, instance "
            "ORDER BY chats DESC" % chat_index.VISIBLE)]
        projects = [dict(r) for r in c.execute(
            "SELECT project, COUNT(*) AS chats, MAX(last_ts) AS last FROM chats WHERE project!='' AND %s "
            "GROUP BY project ORDER BY last DESC LIMIT 40" % chat_index.VISIBLE)]
        hidden = c.execute("SELECT COUNT(*) FROM branches WHERE kind='earlier'").fetchone()[0]
    finally:
        c.close()
    return {"sources": src, "projects": projects, "earlier_transcripts": hidden, "digests": len(digest_store.sizes()),
            "source_labels": sources.PLATFORM_LABEL}


if __name__ == "__main__":
    if "--http" in sys.argv:
        port = 5121
        if "--port" in sys.argv:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        mcp.run(transport="streamable-http", host="127.0.0.1", port=port)
    else:
        mcp.run()
