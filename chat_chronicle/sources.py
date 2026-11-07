"""Source adapters of the chronicle: find chats on disk and read them into a
common format.

Sources:
  claude-code  ~/.claude/projects/*/*.jsonl (CLI and Code tab of all desktop instances)
  cowork       ~/.config/Claude*/local-agent-mode-sessions/**/.claude/projects/*/*.jsonl
  corpus       one unified JSON per chat (claude.ai, ChatGPT, Gemini), built by ingest.py
               from the raw exports in the archive

The index reads Claude Code, Cowork and the desktop metadata not at the
source but from the copy in ARCHIVE/live (see backup.py).

The common format is (meta, items) as in parse_session: items is chronological,
kind is user, assistant or event; an assistant item collects text, thinking
and tool blocks up to the next user message.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")
CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")
# Live sources; read only by backup.py
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CLAUDE_CONFIG = HOME / ".config"
# Own copy, which the index reads from
ARCHIVE = Path(os.path.abspath(Path(os.environ.get("CHRONICLE_ARCHIVE") or DATA_HOME / "chat-chronicle" / "archive")
                               .expanduser()))
LIVE_DIR = ARCHIVE / "live"
PROJECTS = LIVE_DIR / "claude-code"    # like ~/.claude/projects
DESKTOP = LIVE_DIR / "desktop"         # <instance>/ like ~/.config/<instance>/claude-code-sessions/
COWORK = LIVE_DIR / "cowork"           # <instance>/ like ~/.config/<instance>/local-agent-mode-sessions/
EXPORTS = ARCHIVE / "exports"          # <source>/ raw exports, unchanged (ingest.py)
CORPUS_DIR = ARCHIVE / "corpus"        # <source>/ built from them, rebuildable any time
CORPUS_DEFAULT = [CORPUS_DIR / d for d in ("claude.ai", "chatgpt", "gemini")]
CORPORA = [Path(p).expanduser() for p in os.environ["CHRONICLE_CORPUS"].split(":") if p] \
    if os.environ.get("CHRONICLE_CORPUS") is not None else CORPUS_DEFAULT

SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
WT_RE = re.compile(r"/\.claude/worktrees/([^/]+)")
DESKTOP_RE = re.compile(r"/\.config/(Claude[^/]*)/")

PLATFORM_LABEL = {"claude-code": "Claude Code", "cowork": "Cowork", "claude.ai": "claude.ai",
                  "chatgpt": "ChatGPT", "gemini": "Gemini"}
LIVE = ("claude-code", "cowork")     # sources that grow as JSONL while work goes on


def oneline(s, n=100):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def to_dt(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def fmt_ts(ts, fmt="%Y-%m-%d %H:%M"):
    dt = to_dt(ts)
    return dt.strftime(fmt) if dt else ""


def project_of(cwd):
    """cwd -> (project relative to ~, worktree name or '')."""
    if not cwd:
        return "?", ""
    wt = ""
    m = WT_RE.search(cwd)
    if m:
        wt = m.group(1)
        cwd = cwd[: m.start()]
    if "/local-agent-mode-sessions/" in cwd:
        return "(Cowork)", wt
    if DESKTOP_RE.search(cwd + "/"):
        return "(Desktop scratch)", wt
    try:
        rel = str(Path(cwd).relative_to(HOME))
    except ValueError:
        rel = cwd
    return (rel if rel != "." else "~"), wt


# ---------------------------------------------------------------- Read JSONL (Claude Code, Cowork)

def _images(blocks):
    out = []
    for c in blocks:
        if isinstance(c, dict) and c.get("type") == "image":
            src = c.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                out.append((src.get("media_type") or "image/png", src["data"]))
    return out


def _result_parts(content):
    """tool_result content -> (text, [(media type, base64)])."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False), []
    texts = [c.get("text") or "" for c in content
             if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(texts), _images(content)


def _tag(text, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), text, re.S)
    return m.group(1).strip() if m else ""


def _command_event(text):
    """Recognise the CLI's pseudo messages.

    None = normal message, "" = discard, otherwise event text."""
    if text.startswith("<local-command-caveat>"):
        return ""
    if text.startswith("<command-name>") or text.startswith("<command-message>"):
        name = _tag(text, "command-name") or _tag(text, "command-message")
        return ("Command: %s %s" % (name, _tag(text, "command-args"))).strip()
    if text.startswith("<local-command-stdout>") or text.startswith("<local-command-stderr>"):
        out = _tag(text, "local-command-stdout") or _tag(text, "local-command-stderr")
        return ("Output: " + out) if out else ""
    if text.startswith("<task-notification>"):
        summ = _tag(text, "summary") or _tag(text, "status") or re.sub(r"<[^>]+>", " ", text)
        return "Background task: " + oneline(summ, 300)
    if text.startswith("[Request interrupted by user"):
        return "Interrupted by user"
    return None


def _new_meta():
    return {"custom": None, "ai": None, "summary": None, "cost": None, "cwd": None,
            "first": None, "last": None, "models": [], "branch": None, "title": None,
            "project": None}


def parse_session(path):
    """One session file -> (meta, items).

    items is chronological; every assistant turn collects text, thinking and
    tool calls up to the next real user message."""
    meta = _new_meta()
    items, tools, seen = [], {}, set()
    cur = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "custom-title":
                meta["custom"] = d.get("customTitle") or meta["custom"]
                continue
            if t == "ai-title":
                meta["ai"] = d.get("aiTitle") or meta["ai"]
                continue
            if t == "summary":
                meta["summary"] = d.get("summary") or meta["summary"]
                continue
            if t == "cost-state":
                meta["cost"] = d.get("totalCostUSD")
                continue
            if t not in ("user", "assistant", "system") or d.get("isSidechain"):
                continue
            ts = d.get("timestamp")
            if ts:
                meta["first"] = meta["first"] or ts
                meta["last"] = ts
            if d.get("cwd") and not meta["cwd"]:
                meta["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                meta["branch"] = d["gitBranch"]

            if t == "system":
                st = d.get("subtype") or ""
                if st in ("stop_hook_summary", "turn_duration"):
                    continue
                items.append({"kind": "event", "sub": st, "ts": ts,
                              "text": str(d.get("content") or st)})
                cur = None
                continue

            msg = d.get("message") or {}
            content = msg.get("content")

            if t == "assistant":
                model = msg.get("model")
                if model and model != "<synthetic>" and model not in meta["models"]:
                    meta["models"].append(model)
                if cur is None:
                    cur = {"kind": "assistant", "blocks": [], "ts": ts}
                    items.append(cur)
                mid = msg.get("id")
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    bt = b.get("type") or ""
                    if bt in ("text", "thinking"):
                        tx = b.get("text") if bt == "text" else b.get("thinking")
                        key = (mid, bt, tx)
                        if tx and tx.strip() and key not in seen:
                            seen.add(key)
                            cur["blocks"].append({"type": bt, "text": tx})
                    elif bt in ("tool_use", "server_tool_use", "mcp_tool_use"):
                        tid = b.get("id") or "x%d" % len(tools)
                        if tid in tools:
                            continue
                        blk = {"type": "tool", "id": tid, "name": b.get("name") or "?",
                               "input": b.get("input") or {}, "result": None,
                               "error": False, "images": []}
                        tools[tid] = blk
                        cur["blocks"].append(blk)
                    elif bt.endswith("tool_result"):
                        blk = tools.get(b.get("tool_use_id"))
                        if blk is not None:
                            blk["result"], blk["images"] = _result_parts(b.get("content"))
                continue

            # --- user
            if d.get("isMeta"):
                continue
            if isinstance(content, str):
                parts = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                parts = content
            else:
                continue
            texts = []
            for b in parts:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    blk = tools.get(b.get("tool_use_id"))
                    if blk is not None:
                        blk["result"], blk["images"] = _result_parts(b.get("content"))
                        blk["error"] = bool(b.get("is_error"))
                elif b.get("type") == "text":
                    texts.append(b.get("text") or "")
            imgs = _images(parts)
            text = REMINDER_RE.sub("", "\n\n".join(texts)).strip()
            if not text and not imgs:
                continue
            if d.get("isCompactSummary"):
                items.append({"kind": "event", "sub": "compact", "text": text, "ts": ts})
                cur = None
                continue
            ev = _command_event(text)
            if ev is not None:
                if ev:
                    items.append({"kind": "event", "sub": "command", "text": ev, "ts": ts})
                continue
            items.append({"kind": "user", "text": text, "images": imgs, "ts": ts})
            cur = None
    return meta, items


def load_desktop_meta():
    """cliSessionId -> title, archive flag, cwd and instance from all desktop instances.

    Every instance has its own config directory (~/.config/Claude,
    ~/.config/Claude-two, ...), but writes its sessions to ~/.claude/projects.

    Plus how the transcripts are related: jumping back to an earlier message
    makes the app start a new transcript with the history up to there
    (rewindEdges: parent -> child); the old one is then only listed in
    priorCliSessionIds. A deliberate fork names its source in
    forkedFromSessionId (a local_ ID).

    Reads the copy in the archive; the instance is the folder name there."""
    out = {}
    for inst in sorted({p.name for root in (DESKTOP, COWORK) for p in root.glob("Claude*")}):
        for base in (DESKTOP / inst, COWORK / inst):
            if not base.is_dir():
                continue
            for p in base.glob("**/local_*.json"):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                cid = d.get("cliSessionId")
                if cid:
                    e = {"title": d.get("title"), "archived": bool(d.get("isArchived")),
                         "cwd": d.get("cwd"), "instance": inst, "cli": cid,
                         "prior": [x for x in d.get("priorCliSessionIds") or [] if isinstance(x, str)],
                         "rewinds": [(r.get("parent"), r.get("child")) for r in d.get("rewindEdges") or []
                                     if isinstance(r, dict)],
                         "forked_from": d.get("forkedFromSessionId")}
                    out[cid] = e
                    out["local:" + (d.get("sessionId") or p.stem)] = e   # for Cowork
    return out


def pick_title(meta, items, desk):
    for t in (meta["custom"], (desk or {}).get("title"), meta["ai"], meta["summary"], meta["title"]):
        if t and t.strip():
            return t.strip()
    for it in items:
        if it["kind"] == "user":
            return oneline(it["text"], 90)
    return "(empty)"


# ---------------------------------------------------------------- Read corpus (unified JSON)

MARK_RE = re.compile(r"^<<(tool|result|thinking)\b ?([^>\n]*)>> ?", re.M)
CUT_RE = re.compile(r" \.\.\. \[\+\d+ chars truncated\]")


def _segments(content, parts):
    """Split the text of a message into sections -> [(kind, text)].

    The converter in chat_archive appends tool calls and truncated results to the
    text as <<tool>>/<<result>>/<<thinking>> markers and, since claude_v3, writes
    the section lengths to metadata.parts - then the split is exact. Older
    corpora are guessed: a result ends at its truncation note, otherwise at the
    first empty paragraph. When in doubt, answer text thus stays visible instead
    of disappearing in a collapsed result."""
    if parts:
        out, pos = [], 0
        for kind, n in parts:
            out.append((kind, content[pos:pos + n]))
            pos += n + 2
        if pos - 2 == len(content):
            return out
    out = []
    ms = list(MARK_RE.finditer(content))
    out.append(("text", content[: ms[0].start()] if ms else content))
    for k, m in enumerate(ms):
        end = ms[k + 1].start() if k + 1 < len(ms) else len(content)
        seg = content[m.start():end]
        if m.group(1) == "tool":
            stop = seg.find("\n")
        else:
            cut = CUT_RE.search(seg)
            stop = cut.end() if cut else seg.find("\n\n")
        if stop < 0:
            stop = len(seg)
        out += [(m.group(1), seg[:stop]), ("text", seg[stop:])]
    return out


def _split_markers(content, prefix, parts=None):
    """Split assistant text with markers into text, thinking and tool blocks."""
    blocks, tools = [], []
    for k, (kind, seg) in enumerate(_segments(content, parts)):
        m = MARK_RE.match(seg) if kind in ("tool", "result", "thinking") else None
        if m is None:
            if seg.strip():
                blocks.append({"type": "text", "text": seg.strip()})
            continue
        arg, body = m.group(2).strip(), seg[m.end():].strip()
        if kind == "tool":
            try:
                inp = json.loads(body) if body else {}
            except ValueError:
                inp = {"input": body}
            if not isinstance(inp, dict):
                inp = {"input": body}
            blk = {"type": "tool", "id": "%s.%d" % (prefix, k), "name": arg or "tool",
                   "input": inp, "result": None, "error": False, "images": []}
            tools.append(blk)
            blocks.append(blk)
        elif kind == "thinking":
            blocks.append({"type": "thinking", "text": body})
        else:
            name = arg.replace(" ERROR", "").strip() or "tool"
            blk = next((b for b in reversed(tools) if b["result"] is None and b["name"] == name), None)
            if blk is None:
                blk = {"type": "tool", "id": "%s.%d" % (prefix, k), "name": name, "input": {},
                       "result": None, "error": False, "images": []}
                blocks.append(blk)
            blk["result"], blk["error"] = body, arg.endswith("ERROR")
    return blocks


def parse_unified(path):
    """unified JSON (chat_archive) -> (meta, items) in the same format as parse_session."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    m = doc.get("metadata") or {}
    meta = _new_meta()
    meta.update(title=m.get("title"), first=m.get("created_at"),
                last=m.get("updated_at"), project=m.get("project") or None,
                models=[m["model"]] if m.get("model") else [])
    meta["vendor_summary"] = m.get("summary")      # the provider's summary, not a title
    meta["id"] = doc.get("id")
    meta["platform"] = corpus_platform(doc)
    items, cur = [], None
    for x in doc.get("messages") or []:
        role, content, ts = x.get("role"), x.get("content") or "", x.get("timestamp")
        xm = x.get("metadata") or {}
        if role == "user":
            files = [a.get("file_name") for a in xm.get("attachments") or [] if a.get("file_name")]
            items.append({"kind": "user", "text": content, "images": [], "ts": ts, "files": files})
            cur = None
        elif role in ("assistant", "model"):
            if cur is None:
                cur = {"kind": "assistant", "blocks": [], "ts": ts}
                items.append(cur)
            cur["blocks"] += _split_markers(content, "m%s" % x.get("index"), xm.get("parts"))
            files = [a.get("file_name") for a in xm.get("attachments") or [] if a.get("file_name")]
            if files:                          # e.g. images Gemini generated
                cur["blocks"].append({"type": "text", "text": "*(Attachment: %s)*" % ", ".join(files)})
        elif role == "tool":
            if cur is None:
                cur = {"kind": "assistant", "blocks": [], "ts": ts}
                items.append(cur)
            cur["blocks"].append({"type": "tool", "id": "m%s" % x.get("index"), "name": "tool",
                                  "input": {}, "result": content, "error": False, "images": []})
        elif content:
            items.append({"kind": "event", "sub": role or "", "text": content, "ts": ts})
    return meta, items


def corpus_platform(doc):
    p = ((doc.get("source") or {}).get("platform") or "unknown").lower()
    return "claude.ai" if p == "claude" else p


# ---------------------------------------------------------------- Find files

def discover():
    """All chat files: path -> {platform, instance, id (already known for JSONL)}.

    Claude Code and Cowork come from the archive (LIVE_DIR), the corpus from
    CORPORA. The instance of a Code session only follows from the desktop
    metadata; here is only what the path reveals."""
    out, newest = {}, {}
    for p in PROJECTS.glob("*/*.jsonl"):
        if not SID_RE.match(p.stem):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        old = newest.get(p.stem)       # the same session in two project folders: newest wins
        if old and old[1].st_mtime >= st.st_mtime:
            continue
        if old:
            out.pop(str(old[0]), None)
        newest[p.stem] = (p, st)
        out[str(p)] = {"platform": "claude-code", "instance": None, "id": p.stem, "st": st}

    # Cowork: every turn starts a new CLI session with the whole history so far,
    # so the newest file per Cowork session contains everything.
    for inst in sorted(COWORK.glob("Claude*")):
        if not inst.is_dir():
            continue
        groups = {}
        for p in inst.glob("**/local_*/.claude/projects/*/*.jsonl"):
            if not SID_RE.match(p.stem):
                continue
            local = next(q for q in p.parents if q.name.startswith("local_"))
            try:
                st = p.stat()
            except OSError:
                continue
            g = groups.get(local)
            if g is None or g[1].st_mtime < st.st_mtime:
                groups[local] = (p, st)
        for local, (p, st) in groups.items():
            out[str(p)] = {"platform": "cowork", "instance": inst.name, "id": p.stem, "st": st,
                           "local": local.name}

    for d in CORPORA:
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            if p.name.startswith("_"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[str(p)] = {"platform": None, "instance": None, "id": None, "st": st}
    return out


def load(path, platform):
    """Read a file according to its source -> (meta, items)."""
    if platform in LIVE:
        return parse_session(path)
    return parse_unified(path)
