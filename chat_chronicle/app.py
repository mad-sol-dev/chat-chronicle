"""app -- web interface and command line of chat-chronicle, started by chronicle.py.

Reads Claude Code sessions (CLI and the Code tab of all desktop instances), Cowork
sessions and the unified corpus of the exports (claude.ai, ChatGPT, Gemini)
and serves everything as a small website on 127.0.0.1. Python standard
library only; MCP access lives in mcp_server.py (started by chronicle_mcp.py).

    chronicle.py                      start the server (default 127.0.0.1:5120)
    chronicle.py --reindex            drop the index and rebuild it
    chronicle.py export <id> [-o f]   print a chat as Markdown
    chronicle.py overview <id>        digest and outline of a chat
    chronicle.py search <query>       full-text search on the command line
    chronicle.py digests              create missing digests with an LLM
    chronicle.py backup [--gone]      copy live sources into the archive (also runs before every refresh)
    chronicle.py clutter [<id>...]    mark as clutter (--restore undoes it), list without IDs
    chronicle.py delete <id>...       delete clutter from the archive for good, asks first
    chronicle.py import <path>...     exports (zip or folder) into the archive, rebuild the corpus
"""
import argparse
import gzip
import html
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse

from . import backup
from . import chat_index
from . import chronicle_db
from . import cleanup
from . import ingest
from . import sources
from .chat_index import USER_LABEL, tool_input_text, tool_label, tool_summary
from .sources import fmt_ts, oneline, to_dt

SHOW_CHARS = 4000          # chars per tool input/output, the rest on click
# Belongs in every form that changes something: a foreign page can post to
# 127.0.0.1 but can't read the token (new on every start)
TOKEN = secrets.token_urlsafe(16)


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def fmt_num(n):
    return format(n or 0, ",")


# ---------------------------------------------------------------- Markdown

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([^`\s]*)")
LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
BLOCK_START = re.compile(r"^\s*(```|~~~|#{1,6}\s|>|[-*+]\s|\d+[.)]\s|\|)")


def inline(s):
    out = []
    for i, part in enumerate(re.split(r"(`[^`\n]+`)", s)):
        if i % 2:
            out.append("<code>" + esc(part[1:-1]) + "</code>")
            continue
        p = esc(part)
        p = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)",
                   r'<a href="\2" target="_blank" rel="noreferrer">\1</a>', p)
        p = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", p)
        out.append(p)
    return "".join(out)


def _row(s):
    return [c.strip() for c in re.split(r"(?<!\\)\|", s.strip().strip("|"))]


def md(text):
    """Small Markdown renderer for what Claude typically writes."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        s = ln.strip()
        m = FENCE_RE.match(ln)
        if m:
            fence, lang, buf = m.group(1), m.group(2), []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append('<pre class="code"><code data-lang="%s">%s</code></pre>'
                       % (esc(lang), esc("\n".join(buf))))
            continue
        if not s:
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = min(len(m.group(1)) + 2, 6)
            out.append("<h%d>%s</h%d>" % (lvl, inline(m.group(2)), lvl))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            out.append("<hr>")
            i += 1
            continue
        if s.startswith("|") and i + 1 < n and TABLE_SEP.match(lines[i + 1].strip()):
            head = _row(s)
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(_row(lines[i]))
                i += 1
            t = ["<div class=tbl><table><thead><tr>"]
            t += ["<th>%s</th>" % inline(c) for c in head]
            t.append("</tr></thead><tbody>")
            for r in rows:
                t.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>" + md("\n".join(buf)) + "</blockquote>")
            continue
        m = LIST_RE.match(ln)
        if m:
            ordered = m.group(2)[0].isdigit()
            start = int(re.match(r"\d+", m.group(2)).group()) if ordered else 1
            items = []
            while i < n:
                mm = LIST_RE.match(lines[i])
                if mm:
                    items.append([len(mm.group(1).expandtabs(4)), mm.group(3)])
                    i += 1
                elif items and lines[i].strip() and lines[i][:1] in (" ", "\t") \
                        and not FENCE_RE.match(lines[i]):
                    items[-1][1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            base = min(x[0] for x in items)
            lis = []
            for ind, t in items:
                style = ' style="margin-left:%.1fem"' % ((ind - base) / 2 * 1.2) if ind > base else ""
                lis.append("<li%s>%s</li>" % (style, inline(t)))
            if ordered:
                out.append('<ol start="%d">%s</ol>' % (start, "".join(lis)))
            else:
                out.append("<ul>%s</ul>" % "".join(lis))
            continue
        buf = []
        while i < n and lines[i].strip() and not BLOCK_START.match(lines[i]):
            buf.append(lines[i])
            i += 1
        if not buf:          # line looks like a block but isn't one
            buf = [lines[i]]
            i += 1
        out.append("<p>" + "<br>".join(inline(x) for x in buf) + "</p>")
    return "\n".join(out)


# ---------------------------------------------------------------- HTML

CSS = r"""
:root{--bg:#f6f5f1;--fg:#1d1d1b;--mut:#6b6a64;--line:#dedcd3;--card:#fff;--user:#e9eef7;
--acc:#b4542d;--code:#f0eee8;--mark:#ffe58a;--err:#b3261e;--tool:#faf9f6}
@media (prefers-color-scheme:dark){:root{--bg:#1b1b19;--fg:#e8e6df;--mut:#9a988f;--line:#36352f;
--card:#232320;--user:#25303f;--acc:#e08a5f;--code:#2b2a26;--mark:#6b5a12;--err:#f28b82;--tool:#20201d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;display:flex;gap:16px;align-items:center;padding:10px 20px;
background:var(--card);border-bottom:1px solid var(--line)}
.brand{font-weight:700;color:var(--fg);white-space:nowrap}
.hsearch{display:flex;gap:6px;flex:1;max-width:640px}
input,select,button{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:5px 9px}
.hsearch input{flex:1;min-width:0}
button{cursor:pointer}button:hover{border-color:var(--acc)}
main{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:1.45em;margin:.2em 0 .4em}
.mut{color:var(--mut)}.small{font-size:.85em}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0 14px}
table.list{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
table.list th,table.list td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
table.list th{font-size:.82em;color:var(--mut);font-weight:600}
table.list td.num{text-align:right;white-space:nowrap;color:var(--mut)}
td.date{white-space:nowrap;color:var(--mut);font-size:.9em}
.prev{display:block;color:var(--mut);font-size:.85em;max-height:2.9em;overflow:hidden}
.tag{display:inline-block;font-size:.75em;padding:0 6px;border:1px solid var(--line);border-radius:10px;color:var(--mut);margin-left:4px}
.metaline{display:flex;flex-wrap:wrap;gap:4px 18px;color:var(--mut);font-size:.88em}
code{font-family:ui-monospace,"JetBrains Mono",Menlo,monospace;font-size:.88em;background:var(--code);padding:1px 4px;border-radius:4px}
pre{font-family:ui-monospace,"JetBrains Mono",Menlo,monospace;font-size:.82em;background:var(--code);padding:10px 12px;
border-radius:6px;overflow:auto;max-height:560px;white-space:pre-wrap;word-break:break-word;margin:.5em 0}
pre code{background:none;padding:0;font-size:1em}
.conv{margin-top:18px}
.msg{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;margin:12px 0}
.msg.user{background:var(--user);margin-left:8%}
.msg.target{outline:2px solid var(--acc)}
.who{font-size:.8em;font-weight:600;color:var(--mut);margin-bottom:2px}
.who a.t{font-weight:400;color:var(--mut);margin-left:6px}
.body>:first-child{margin-top:.2em}.body>:last-child{margin-bottom:.2em}
.body h3,.body h4,.body h5,.body h6{margin:.9em 0 .3em}
.body blockquote{margin:.5em 0;padding-left:12px;border-left:3px solid var(--line);color:var(--mut)}
.tbl{overflow:auto}.body table{border-collapse:collapse;margin:.5em 0;font-size:.92em}
.body th,.body td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
details.tool,details.thinking,details.compact{margin:5px 0;border:1px solid var(--line);border-radius:6px;background:var(--tool)}
.msg,details{max-width:100%;overflow:hidden}
details>summary{display:block;cursor:pointer;padding:4px 10px;font-size:.86em;color:var(--mut);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"▸ ";color:var(--mut)}details[open]>summary::before{content:"▾ "}
details[open]>summary{border-bottom:1px solid var(--line)}
details>div,details>pre,details>img,details>button{margin:6px 10px}
.tname{font-family:ui-monospace,monospace;color:var(--fg)}
details.err .tname{color:var(--err)}
.tlabel{font-size:.75em;color:var(--mut);text-transform:uppercase;letter-spacing:.04em;margin-top:8px}
.event{text-align:center;color:var(--mut);font-size:.83em;margin:8px 0}
.day{text-align:center;color:var(--mut);font-size:.8em;margin:22px 0 4px;letter-spacing:.05em}
.msg img,details img{max-width:100%;max-height:480px;border-radius:6px;border:1px solid var(--line);display:block;margin:6px 0}
mark{background:var(--mark);color:inherit;border-radius:2px}
.hit{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:10px 0}
.hit ul{margin:6px 0 0;padding-left:18px}.hit li{margin:3px 0}
.kind{font-size:.72em;color:var(--mut);border:1px solid var(--line);border-radius:8px;padding:0 5px;margin-right:6px}
details.toc{background:var(--card)}details.toc ol{margin:6px 0;padding-left:28px;font-size:.9em;max-height:340px;overflow:auto}
.resume{user-select:all}
@media (max-width:700px){header{flex-wrap:wrap}.msg.user{margin-left:0}td.date,.hide-s{display:none}}
"""

JS = r"""
const qf=document.getElementById('qf');
if(qf){const rows=[...document.querySelectorAll('tr[data-s]')];
 qf.addEventListener('input',()=>{const v=qf.value.toLowerCase();rows.forEach(r=>{r.hidden=!!v&&!r.dataset.s.includes(v)})});}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button.more');if(!b)return;b.disabled=true;b.textContent='loading…';
 try{const r=await fetch(b.dataset.src);b.previousElementSibling.textContent=await r.text();b.remove();}
 catch(err){b.textContent='Error: '+err;}});
document.querySelectorAll('[data-toggle]').forEach(btn=>btn.addEventListener('click',()=>{
 const ds=[...document.querySelectorAll(btn.dataset.toggle)];const open=!ds.every(d=>d.open);ds.forEach(d=>d.open=open);}));
const conv=document.querySelector('.conv');
const q=new URLSearchParams(location.search).get('q');
if(conv&&q){
 const terms=(q.match(/"[^"]+"|\S+/g)||[]).map(t=>t.replace(/^"|"$/g,'').replace(/\*$/,''))
  .filter(t=>t.length>1&&!['OR','AND','NOT'].includes(t));
 if(terms.length){
  const re=new RegExp('('+terms.map(t=>t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|')+')','gi');
  const w=document.createTreeWalker(conv,NodeFilter.SHOW_TEXT);const nodes=[];
  while(w.nextNode())nodes.push(w.currentNode);
  nodes.forEach(n=>{const v=n.nodeValue;re.lastIndex=0;if(!re.test(v))return;
   const f=document.createDocumentFragment();let last=0;
   v.replace(re,(m,g,off)=>{f.append(v.slice(last,off));const mk=document.createElement('mark');mk.textContent=m;f.append(mk);last=off+m.length;return m;});
   f.append(v.slice(last));n.parentNode.replaceChild(f,n);});
  document.querySelectorAll('.conv details').forEach(d=>{if(d.querySelector('mark'))d.open=true;});
 }}
if(location.hash){const el=document.getElementById(location.hash.slice(1));
 if(el){el.classList.add('target');setTimeout(()=>el.scrollIntoView({block:'start'}),50);}}
"""

CSS += r"""
.src{display:inline-block;font-size:.72em;padding:0 6px;border-radius:8px;margin-right:6px;color:var(--mut);
border:1px solid var(--line);white-space:nowrap}
.digest{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:8px;
padding:8px 14px;margin:12px 0}.digest .who{margin-bottom:4px}
.files{font-size:.8em;color:var(--mut);margin-top:4px}
header nav{display:flex;gap:14px;font-size:.9em;white-space:nowrap}
.notice{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--acc);border-radius:8px;
padding:8px 14px;margin:10px 0}
form.inline{display:inline}
th.cb,td.cb{width:1.6em;padding-right:0}
.actions{position:sticky;bottom:0;display:flex;gap:10px;align-items:center;padding:10px 0;background:var(--bg)}
button.danger{border-color:var(--err);color:var(--err)}
h2{font-size:1.1em;margin:1.4em 0 .4em}
"""

JS += r"""
document.querySelectorAll('form.sel').forEach(f=>{const out=f.querySelector('.nsel');
 const upd=()=>{const n=f.querySelectorAll('input[name=id]:checked').length;if(out)out.textContent=n?n+' selected':'';
  f.querySelectorAll('button[data-need]').forEach(b=>b.disabled=!n);};
 f.addEventListener('change',upd);upd();});
document.querySelectorAll('input[data-all]').forEach(a=>a.addEventListener('change',()=>{
 a.closest('table').querySelectorAll('tbody tr:not([hidden]) input[name=id]').forEach(x=>x.checked=a.checked);
 a.form.dispatchEvent(new Event('change'));}));
"""


def page(title, body, q=""):
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s · chat-chronicle</title><style>%s</style></head><body>"
            "<header><a class=brand href='/'>chat-chronicle</a>"
            "<form class=hsearch action='/search'><input name=q value=\"%s\" "
            "placeholder='Full-text search (auto prefix, \"phrase\", OR, NOT)'>"
            "<button>Search</button></form><nav><a href='/suggestions'>Suggestions</a>"
            "<a href='/clutter'>Clutter</a></nav></header><main>%s</main><script>%s</script></body></html>"
            % (esc(title), CSS, esc(q), body, JS))


def pre_trunc(text, src):
    text = text or ""
    if len(text) <= SHOW_CHARS:
        return "<pre>%s</pre>" % esc(text)
    return ("<pre>%s</pre><button class=more data-src=\"%s\">Load all (%s chars)</button>"
            % (esc(text[:SHOW_CHARS] + "\n…"), esc(src), fmt_num(len(text))))


def render_tool(sid, b):
    tid = quote(b["id"], safe="")
    pending = "" if b["result"] is not None else " <span class=small>(no result)</span>"
    h = ['<details class="tool%s"><summary><span class=tname>%s</span> <span>%s</span>%s</summary>'
         % (" err" if b["error"] else "", esc(tool_label(b["name"])), esc(tool_summary(b["input"])), pending)]
    h.append("<div class=tlabel>Input</div>")
    h.append(pre_trunc(tool_input_text(b["name"], b["input"]), "/s/%s/t/%s/in" % (sid, tid)))
    if b["result"] is not None:
        h.append("<div class=tlabel>Result%s</div>" % (" · error" if b["error"] else ""))
        h.append(pre_trunc(b["result"], "/s/%s/t/%s/out" % (sid, tid)))
    for k in range(len(b["images"])):
        h.append("<img loading=lazy src='/s/%s/img/%s/%d'>" % (sid, tid, k))
    h.append("</details>")
    return "".join(h)


def src_tag(platform, instance=""):
    label = sources.PLATFORM_LABEL.get(platform, platform)
    if instance and instance != "Claude":
        label += " · " + instance
    return "<span class=src>%s</span>" % esc(label)


def render_digest(row, digest):
    if digest:
        stale = " · outdated, the session has grown since" if chat_index.is_stale(digest["basis_size"], row["size"]) else ""
        return ("<div class=digest><div class=who>Digest <span class='mut small'>%s · %s%s</span></div>"
                "<div class=body>%s</div></div>"
                % (esc(digest["model"]), esc(digest["created"][:10]), stale, md(digest["text"])))
    if row["summary"]:
        return ("<details class='digest compact'><summary>Summary from %s</summary><div class=body>%s</div></details>"
                % (esc(sources.PLATFORM_LABEL.get(row["platform"], row["platform"])), md(row["summary"])))
    return ""


def render_family(fam):
    """Earlier transcripts and forks as notice lines below the header."""
    me, up, out = fam["me"], fam["up"], []

    def link(r, n=None):
        return "<a href='/s/%s%s'>%s</a>" % (r["id"], "#i%d" % n if n is not None else "", esc(oneline(r["title"], 80)))
    if me and up and me["kind"] == "earlier":
        out.append("Earlier transcript of the session %s. It is no longer visible in the desktop app; "
                   "from <a href='#i%d'>message %d</a> on it differs from the later state."
                   % (link(up), me["branch_at"], me["branch_at"]))
    elif me and up:
        out.append("Forked from %s, own history from <a href='#i%d'>message %d</a>."
                   % (link(up, me["branch_at"]), me["branch_at"], me["branch_at"]))
    if fam["earlier"]:
        out.append("Earlier transcripts of this session (discarded states after jumping back): " + ", ".join(
            "<a href='/s/%s#i%d'>%s</a> from message %d, until %s"
            % (r["id"], r["branch_at"], r["id"][:8], r["branch_at"], fmt_ts(r["last_ts"])) for r in fam["earlier"]))
    if fam["forks"]:
        out.append("Forks: " + ", ".join("%s from message %d" % (link(r, r["branch_at"]), r["branch_at"])
                                         for r in fam["forks"]))
    return "".join("<div class='small mut'>%s</div>" % x for x in out)


def branch_tag(br):
    if not br:
        return ""
    return " <span class=tag>%s</span>" % ("earlier transcript" if br["kind"] == "earlier" else "fork")


# ---------------------------------------------------------------- Clutter (forms)

OK_TEXT = {"mark": "marked as clutter. <a href='/clutter'>View clutter</a>",
           "restore": "restored.", "deleted": "deleted for good."}
NEXT_RE = re.compile(r"^/[\w/.-]*$")


def hidden(**kw):
    """Hidden fields including the token."""
    return "".join("<input type=hidden name=%s value=\"%s\">" % (k, esc(v))
                   for k, v in [("token", TOKEN)] + list(kw.items()))


def next_url(target, **extra):
    """Target after a form: only a path of this site, old notices dropped."""
    u = urlparse(target or "/")
    qs = [(k, v) for k, v in parse_qsl(u.query) if k not in ("ok", "n")] + list(extra.items())
    return (u.path if NEXT_RE.match(u.path) else "/") + ("?" + urlencode(qs) if qs else "")


def notice(qs):
    ok, n = (qs.get("ok") or [""])[0], (qs.get("n") or [""])[0]
    if ok not in OK_TEXT or not n.isdigit():
        return ""
    return "<div class=notice>%s %s %s</div>" % (n, "chat" if n == "1" else "chats", OK_TEXT[ok])


def render_mark(sid, marks, fam, here):
    """Clutter state on the chat page -> (button for the bar, notice below it)."""
    form = "<form class=inline method=post action='/clutter'>" + hidden(id=sid, next=here) + "%s</form>"
    if sid in marks:
        return "", ("<div class=notice>Marked as clutter on %s. The list, search, MCP server and digests "
                    "don't see this chat. %s</div>"
                    % (fmt_ts(marks[sid]), form % "<button name=op value=restore>Restore</button> "
                       "<button class=danger formaction='/clutter/delete'>Delete for good …</button>"))
    up = fam["up"] if fam and fam["me"] and fam["me"]["kind"] == "earlier" else None
    if up and up["id"] in marks:
        return "", ("<div class=notice>Belongs to the session <a href='/s/%s'>%s</a>, which is marked as clutter; "
                    "it is deleted along with it.</div>" % (up["id"], esc(oneline(up["title"], 80))))
    return " · " + form % "<button name=op value=mark>Mark as clutter</button>", ""


def render_session(row, title, meta, items, digest, q="", fam=None, mark=("", "")):
    sid = row["id"]
    live = row["platform"] in sources.LIVE
    cwd = meta["cwd"] or ""
    toc, conv, last_day = [], [], None
    n_tools = 0
    for i, it in enumerate(items):
        day = fmt_day(it.get("ts"))
        if day and day != last_day:
            conv.append("<div class=day>%s</div>" % esc(day))
            last_day = day
        tm = fmt_ts(it.get("ts"), "%H:%M")
        k = it["kind"]
        if k == "user":
            toc.append("<li><a href='#i%d'>%s</a> <span class='mut small'>%s</span></li>"
                       % (i, esc(oneline(it["text"], 110)), tm))
            imgs = "".join("<img loading=lazy src='/s/%s/u/%d/%d'>" % (sid, i, j)
                           for j in range(len(it["images"])))
            files = ("<div class=files>Attachments: %s</div>" % esc(", ".join(it["files"]))) if it.get("files") else ""
            conv.append("<section class='msg user' id=i%d><div class=who>%s<a class=t href='#i%d'>%s</a></div>"
                        "<div class=body>%s</div>%s%s</section>"
                        % (i, esc(USER_LABEL), i, tm, md(it["text"]), files, imgs))
        elif k == "assistant":
            parts = []
            for b in it["blocks"]:
                if b["type"] == "text":
                    parts.append("<div class=body>%s</div>" % md(b["text"]))
                elif b["type"] == "thinking":
                    parts.append("<details class=thinking><summary>Thinking</summary><div class=body>%s</div></details>"
                                 % md(b["text"]))
                else:
                    n_tools += 1
                    parts.append(render_tool(sid, b))
            if parts:
                who = "Claude" if row["platform"] not in ("chatgpt", "gemini") else sources.PLATFORM_LABEL[row["platform"]]
                conv.append("<section class='msg asst' id=i%d><div class=who>%s<a class=t href='#i%d'>%s</a></div>%s</section>"
                            % (i, who, i, tm, "".join(parts)))
        elif it["sub"] == "compact":
            conv.append("<details class=compact id=i%d><summary>Context compacted · summary</summary>"
                        "<div class=body>%s</div></details>" % (i, md(it["text"])))
        else:
            conv.append("<div class=event id=i%d>%s · %s</div>" % (i, esc(oneline(it["text"], 300)), tm))

    cost = ("$%.2f" % meta["cost"]) if isinstance(meta["cost"], (int, float)) else ""
    head = ["<h1>%s</h1><div class=metaline>" % esc(title),
            "<span>%s</span>" % src_tag(row["platform"], row["instance"])]
    if row["project"]:
        head.append("<span>%s%s</span>" % (esc(row["project"]),
                                           (" <span class=tag>%s</span>" % esc(row["worktree"])) if row["worktree"] else ""))
    head += ["<span>%s – %s</span>" % (fmt_ts(meta["first"]), fmt_ts(meta["last"])),
             "<span>%d messages · %d tool calls</span>" % (len(toc), n_tools)]
    if meta["models"]:
        head.append("<span>%s</span>" % esc(", ".join(meta["models"])))
    if meta["branch"]:
        head.append("<span>Branch %s</span>" % esc(meta["branch"]))
    if cost:
        head.append("<span>%s</span>" % cost)
    head.append("</div>")
    head.append("<div class=bar><a href='/s/%s.md'>Markdown</a> · <a href='/s/%s.md?tools=0'>Markdown without tools</a>"
                " · <a href='/s/%s/raw'>%s</a> · <button data-toggle='details.tool'>Toggle tools</button>"
                "<button data-toggle='details.thinking'>Toggle thinking</button>%s</div>%s"
                % (sid, sid, sid, "JSONL" if live else "JSON", mark[0], mark[1]))
    if row["platform"] == "claude-code":
        resume = "cd %s && claude --resume %s" % (cwd or "~", sid)
        head.append("<div class='small mut'>Resume: <code class=resume>%s</code></div>" % esc(resume))
    if fam:
        head.append(render_family(fam))
    head.append(render_digest(row, digest))
    if toc:
        head.append("<details class='toc tool' style='margin-top:10px'><summary>Your messages (%d)</summary><ol>%s</ol></details>"
                    % (len(toc), "".join(toc)))
    return page(title, "".join(head) + "<div class=conv>%s</div>" % "".join(conv), q)


WEEKDAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fmt_day(ts):
    dt = to_dt(ts)
    return "%s, %s" % (WEEKDAY[dt.weekday()], dt.strftime("%Y-%m-%d")) if dt else ""


def filter_selects(c, proj, src):
    opts = ["<option value=''>all sources</option>"]
    for r in c.execute("SELECT platform, COUNT(*) n FROM chats WHERE n_user>0 AND %s "
                       "GROUP BY platform ORDER BY n DESC" % chat_index.VISIBLE):
        opts.append("<option value=\"%s\"%s>%s (%d)</option>"
                    % (esc(r["platform"]), " selected" if r["platform"] == src else "",
                       esc(sources.PLATFORM_LABEL.get(r["platform"], r["platform"])), r["n"]))
    h = ["<select name=src onchange='this.form.submit()'>%s</select>" % "".join(opts)]
    opts = ["<option value=''>all projects</option>"]
    for r in c.execute("SELECT project, COUNT(*) n FROM chats WHERE project!='' AND n_user>0 AND %s "
                       "GROUP BY project ORDER BY project" % chat_index.VISIBLE):
        opts.append("<option value=\"%s\"%s>%s (%d)</option>"
                    % (esc(r["project"]), " selected" if r["project"] == proj else "", esc(r["project"]), r["n"]))
    h.append("<select name=p onchange='this.form.submit()'>%s</select>" % "".join(opts))
    return "".join(h)


def render_index(idx, qs, here="/"):
    proj = (qs.get("p") or [""])[0]
    src = (qs.get("src") or [""])[0]
    c = idx.conn()
    try:
        sel = filter_selects(c, proj, src)
        rows = c.execute("SELECT * FROM chats WHERE (?='' OR project=?) AND (?='' OR platform=?) AND n_user>0 "
                         "AND %s ORDER BY last_ts DESC" % chat_index.VISIBLE, (proj, proj, src, src)).fetchall()
        total = c.execute("SELECT COUNT(*) FROM chats WHERE n_user>0 AND " + chat_index.VISIBLE).fetchone()[0]
        cost = c.execute("SELECT COALESCE(SUM(cost),0) FROM chats WHERE n_user>0").fetchone()[0]
        n_clutter = c.execute("SELECT COUNT(*) FROM chronicle.clutter").fetchone()[0]
    finally:
        c.close()
    branches = idx.branches()
    earlier = sum(1 for b in branches.values() if b["kind"] == "earlier")
    status = idx.status or ("Index refreshed %d s ago" % (time.time() - idx.last) if idx.last else "Building index …")
    h = ["<h1>Chats</h1>", notice(qs),
         "<form class=bar action='/'>%s<input id=qf placeholder='Filter titles …' autocomplete=off>"
         "<span class='mut small'>%d chats%s%s · $%.2f Claude Code total · %s</span></form>"
         % (sel, total, (" (%d earlier transcripts only with their session)" % earlier) if earlier else "",
            (" · <a href='/clutter'>%d in clutter</a>" % n_clutter) if n_clutter else "", cost, esc(status)),
         "<form class=sel method=post action='/clutter'>" + hidden(next=here),
         "<table class=list><thead><tr><th class=cb></th><th class=date>Last</th><th>Title</th><th class=hide-s>Project</th>"
         "<th class=num>Msgs</th><th class='num hide-s'>Tools</th><th class='num hide-s'>Cost</th></tr></thead><tbody>"]
    for r in rows:
        tags = (" <span class=tag>%s</span>" % esc(r["worktree"])) if r["worktree"] else ""
        if r["archived"]:
            tags += " <span class=tag>archived</span>"
        tags += branch_tag(branches.get(r["id"]))
        search = (r["title"] + " " + r["project"] + " " + (r["preview"] or "")).lower()
        h.append("<tr data-s=\"%s\"><td class=cb><input type=checkbox name=id value=%s></td><td class=date>%s</td>"
                 "<td>%s<a href='/s/%s'>%s</a>%s"
                 "<span class=prev>%s</span></td><td class='hide-s small'>%s</td><td class=num>%d</td>"
                 "<td class='num hide-s'>%d</td><td class='num hide-s'>%s</td></tr>"
                 % (esc(search), r["id"], fmt_ts(r["last_ts"]), src_tag(r["platform"], r["instance"]), r["id"],
                    esc(r["title"]), tags, esc(r["preview"]), esc(r["project"]), r["n_user"], r["n_tools"],
                    ("$%.2f" % r["cost"]) if r["cost"] is not None else ""))
    h.append("</tbody></table><div class=actions><button name=op value=mark data-need>"
             "Mark selected as clutter</button><span class='mut small nsel'></span></div></form>")
    return page("Chats", "".join(h))


def sel_table(rows, extra_head="", extra=None):
    """Chats with checkboxes (clutter, suggestions); extra(r) fills the last column."""
    h = ["<table class=list><thead><tr><th class=cb><input type=checkbox data-all title='select all'></th>"
         "<th class=date>Last</th><th>Title</th><th class=num>Msgs</th><th class='num hide-s'>Chars</th>"
         "<th class='hide-s'>%s</th></tr></thead><tbody>" % esc(extra_head)]
    for r in rows:
        h.append("<tr><td class=cb><input type=checkbox name=id value=%s></td><td class=date>%s</td>"
                 "<td>%s<a href='/s/%s'>%s</a><span class=prev>%s</span></td><td class=num>%d</td>"
                 "<td class='num hide-s'>%s</td><td class='hide-s small'>%s</td></tr>"
                 % (r["id"], fmt_ts(r["last_ts"]), src_tag(r["platform"], r["instance"]), r["id"], esc(r["title"]),
                    esc(r["preview"]), r["n_user"], fmt_num(r["chars"]), extra(r) if extra else ""))
    h.append("</tbody></table>")
    return "".join(h)


def render_clutter(idx, qs):
    c = idx.conn()
    try:
        rows = c.execute("SELECT x.id AS bid, x.since, c.* FROM chronicle.clutter x LEFT JOIN chats c ON c.id=x.id "
                         "ORDER BY x.since DESC").fetchall()
        earlier = dict(c.execute("SELECT b.head, COUNT(*) FROM branches b JOIN chronicle.clutter x ON x.id=b.head "
                                 "WHERE b.kind='earlier' GROUP BY b.head").fetchall())
    finally:
        c.close()
    known = [dict(r) for r in rows if r["id"]]
    lost = [r["bid"] for r in rows if not r["id"]]

    def extra(r):
        n = earlier.get(r["id"])
        return "marked %s%s" % (fmt_ts(r["since"], "%Y-%m-%d"), (" · plus %d earlier transcripts" % n) if n else "")
    h = ["<h1>Clutter</h1>", notice(qs),
         "<p class='mut small'>Chats marked as clutter are left out of the list, search, MCP server and digest runs, "
         "but stay unchanged in the archive. Deleting for good removes the copy in the archive and leaves a "
         "gravestone; you are asked first.</p>"]
    if known or lost:
        h.append("<form class=sel method=post action='/clutter'>" + hidden(next="/clutter"))
        h.append(sel_table(known, "marked", extra))
        if lost:
            h.append("<p class='mut small'>No longer in the index: %s</p>" % ", ".join(
                "<label><input type=checkbox name=id value=%s> %s</label>" % (i, i[:8]) for i in lost))
        h.append("<div class=actions><button name=op value=restore data-need>Restore</button>"
                 "<button class=danger formaction='/clutter/delete' data-need>Delete for good …</button>"
                 "<span class='mut small nsel'></span></div></form>")
    else:
        h.append("<p>Nothing marked as clutter. Candidates are listed under <a href='/suggestions'>Suggestions</a>.</p>")
    graves = chronicle_db.gravestone_list()
    if graves:
        h.append("<h2>Deleted for good (%d)</h2><table class=list><thead><tr><th class=date>Deleted</th>"
                 "<th>Title</th><th class=hide-s>Chat ID</th></tr></thead><tbody>" % len(graves))
        for g in graves:
            h.append("<tr><td class=date>%s</td><td>%s%s</td><td class='hide-s small'><code>%s</code></td></tr>"
                     % (fmt_ts(g["deleted"]), src_tag(g["platform"]), esc(g["title"]), g["id"]))
        h.append("</tbody></table>")
    return page("Clutter", "".join(h))


def render_delete(stones, errors):
    """Confirmation before deleting for good: what goes, what stays."""
    h = ["<h1>Delete for good</h1>",
         "<p>The copies in the archive go, for exports the corpus JSON. Each chat leaves a gravestone "
         "(ID, title, source, date) so that backup and index don't bring it back, even while the app "
         "still has it. The live sources (<code>~/.claude</code>, <code>~/.config/Claude*</code>) and the raw exports "
         "stay as they are. Afterwards the only way back is a snapshot.</p>"]
    if errors:
        h.append("<div class=notice>Not deleted:<ul>%s</ul></div>" % "".join("<li>%s</li>" % esc(f) for f in errors))
    for s in stones:
        files = "".join("<li><code>%s</code></li>" % esc(cleanup.show(p)) for p in s["files"]) \
            or "<li class=mut>no files left in the archive</li>"
        h.append("<div class=hit>%s<strong>%s</strong>%s <span class='mut small'>%s</span><ul>%s</ul></div>"
                 % (src_tag(s["platform"]), esc(s["title"]),
                    (" <span class=tag>earlier transcript of %s</span>" % s["belongs_to"][:8]) if s["belongs_to"] else "",
                    s["id"], files))
    ids = [s["id"] for s in stones if not s["belongs_to"]]
    if ids:
        h.append("<form method=post action='/clutter/delete'>%s%s<div class=actions>"
                 "<button class=danger name=yes value=1>Delete %d %s for good</button><a href='/clutter'>Cancel</a>"
                 "</div></form>" % (hidden(), "".join("<input type=hidden name=id value=%s>" % i for i in ids),
                                    len(stones), "chat" if len(stones) == 1 else "chats"))
    else:
        h.append("<p><a href='/clutter'>Back to clutter</a></p>")
    return page("Delete for good", "".join(h))


def render_suggestions(idx, qs):
    sugg = cleanup.suggestions(idx)

    def goal(r):
        # "Ziel" comes from digests written before the English version
        m = re.search(r"(?im)^\W*(?:Goal|Ziel)\W*:\s*(.+)$", r["digest"] or "")
        return esc(oneline(m.group(1), 120)) if m else ""
    h = ["<h1>Suggestions</h1>", notice(qs),
         "<p class='mut small'>Candidates for clutter. Only what you select gets marked; chats already marked and "
         "earlier transcripts are not listed here.</p>",
         "<form class=sel method=post action='/clutter'>" + hidden(next="/suggestions"),
         "<h2>Deleted in the desktop app (%d)</h2>" % len(sugg["app"]),
         sel_table(sugg["app"], "deleted", lambda r: fmt_ts(r["deleted"])) if sugg["app"] else "<p class=mut>None.</p>",
         "<h2>Digest: result open, no decisions (%d)</h2>" % len(sugg["open"]),
         sel_table(sugg["open"], "Goal", goal) if sugg["open"] else "<p class=mut>None.</p>",
         "<h2>Shorter than %s characters (%d)</h2>" % (fmt_num(cleanup.MIN_CHARS), len(sugg["short"])),
         sel_table(sugg["short"], "Project", lambda r: esc(r["project"])) if sugg["short"] else "<p class=mut>None.</p>",
         "<div class=actions><button name=op value=mark data-need>Mark selected as clutter</button>"
         "<span class='mut small nsel'></span></div></form>"]
    return page("Suggestions", "".join(h))


KIND_LABEL = {"user": USER_LABEL, "assistant": "Reply", "thinking": "Thinking", "tool": "Tool"}


def render_search(idx, qs):
    q = (qs.get("q") or [""])[0].strip()
    proj = (qs.get("p") or [""])[0]
    src = (qs.get("src") or [""])[0]
    order = (qs.get("o") or ["date"])[0]
    with_tools = (qs.get("t") or [""])[0] == "1"
    c = idx.conn()
    try:
        sel = filter_selects(c, proj, src)
        form = ("<form class=bar action='/search'><input name=q value=\"%s\" size=40>%s"
                "<select name=o onchange='this.form.submit()'><option value=date>newest first</option>"
                "<option value=relevance%s>best hits first</option></select>"
                "<label class=small><input type=checkbox name=t value=1%s onchange='this.form.submit()'> "
                "also tools and thinking</label><button>Search</button></form>"
                % (esc(q), sel, " selected" if order == "relevance" else "", " checked" if with_tools else ""))
        if not chat_index.fts_query(q):
            return page("Search", "<h1>Search</h1>" + form)
        kinds = ["user", "assistant"] + (["thinking", "tool"] if with_tools else [])
        try:
            res = chat_index.search(c, q, kinds, platform=src, project=proj, order=order, limit=300)
        except sqlite3.OperationalError as e:
            return page("Search", "<h1>Search</h1>%s<p>Invalid search query: %s</p>" % (form, esc(e)), q)
    finally:
        c.close()
    qp = urlencode({"q": q})
    branches = idx.branches()
    n_hits = sum(len(r["hits"]) for r in res["results"])
    h = ["<h1>Search</h1>", form,
         "<p class='mut small'>%d chats found%s%s%s</p>"
         % (res["total"], (", %d shown" % len(res["results"])) if res["total"] > len(res["results"]) else "",
            (" · %d hits" % n_hits) if n_hits else "",
            (" · only found approximately: %s" % esc(", ".join(res["weak"]))) if res["weak"] else "")]
    for r in res["results"]:
        sid = r["id"]
        h.append("<div class=hit>%s<a href='/s/%s?%s'><strong>%s</strong></a>%s <span class='mut small'>%s · %s</span><ul>"
                 % (src_tag(r["platform"], r["instance"]), sid, qp, esc(r["title"]), branch_tag(branches.get(sid)),
                    esc(r["project"]), fmt_ts(r["last_ts"])))
        for hit in r["hits"][:6]:
            snip = esc(hit["snip"]).replace("\x02", "<mark>").replace("\x03", "</mark>")
            h.append("<li><a href='/s/%s?%s#i%d'><span class=kind>%s</span></a>%s</li>"
                     % (sid, qp, hit["item"], KIND_LABEL.get(hit["kind"], hit["kind"]), snip))
        if len(r["hits"]) > 6:
            h.append("<li class='mut small'>… %d more hits in this chat</li>" % (len(r["hits"]) - 6))
        h.append("</ul></div>")
    return page("Search: " + q, "".join(h), q)


# ---------------------------------------------------------------- Server

class Handler(BaseHTTPRequestHandler):
    server_version = "chat-chronicle"
    idx = None
    allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}

    def log_message(self, fmt, *args):  # only errors go to the journal
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = dict(extra or {})
        if len(body) > 50000 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_safely(self.route)

    def do_POST(self):
        self.handle_safely(self.post)

    def handle_safely(self, fn):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in self.allowed_hosts:   # protection against DNS rebinding
            return self.send(403, "Host not allowed", "text/plain; charset=utf-8")
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                self.send(500, "Internal error: %r" % e, "text/plain; charset=utf-8")
            except Exception:
                pass

    def route(self):
        idx = self.idx
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        parts = [unquote(p) for p in u.path.split("/") if p]
        if not parts:
            idx.refresh(block=False)
            return self.send(200, render_index(idx, qs, self.path))
        if parts == ["search"]:
            idx.refresh(block=False)
            return self.send(200, render_search(idx, qs))
        if parts == ["clutter"]:
            return self.send(200, render_clutter(idx, qs))
        if parts == ["suggestions"]:
            idx.refresh(block=False)
            return self.send(200, render_suggestions(idx, qs))
        if parts == ["api", "status"]:
            return self.send(200, json.dumps({"status": idx.status, "last": idx.last}), "application/json")
        if parts[0] == "s" and len(parts) >= 2:
            sid, as_md = parts[1], False
            if sid.endswith(".md"):
                sid, as_md = sid[:-3], True
            if not sources.SID_RE.match(sid):
                return self.send(404, "Unknown chat", "text/plain; charset=utf-8")
            row = idx.row(sid)
            if not row:
                idx.refresh(force=True)
                row = idx.row(sid)
            if not row or not os.path.exists(row["path"]):
                for g in chronicle_db.gravestone_list(sid):
                    return self.send(410, page(g["title"], "<h1>%s</h1><p>%s, deleted for good on %s.</p>"
                                               % (esc(g["title"]), src_tag(g["platform"]), fmt_ts(g["deleted"]))))
                return self.send(404, "Unknown chat", "text/plain; charset=utf-8")
            return self.session_route(row, parts[2:], qs, as_md)
        return self.send(404, "Not found", "text/plain; charset=utf-8")

    def post(self):
        """Forms: mark as clutter and restore, delete for good."""
        origin = self.headers.get("Origin")
        n = max(0, min(int(self.headers.get("Content-Length") or 0), 1 << 20))
        form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
        if (origin and urlparse(origin).netloc.rsplit(":", 1)[0] not in self.allowed_hosts) \
                or not secrets.compare_digest((form.get("token") or [""])[0], TOKEN):
            return self.send(403, "Form rejected: foreign page or outdated (reload the page)",
                             "text/plain; charset=utf-8")
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        ids = [i for i in form.get("id", []) if sources.SID_RE.match(i)]
        op = (form.get("op") or [""])[0]
        if parts == ["clutter"] and op in ("mark", "restore"):
            n = chronicle_db.mark([i for i in ids if self.idx.row(i)]) if op == "mark" else chronicle_db.unmark(ids)
            return self.send(303, "", "text/plain; charset=utf-8",
                             {"Location": next_url((form.get("next") or ["/"])[0], ok=op, n=n)})
        if parts == ["clutter", "delete"]:
            try:
                stones, errors = cleanup.plan(self.idx, ids)
            except ValueError as e:
                return self.send(400, page("Delete", "<h1>Not deleted</h1><p>%s</p>" % esc(e)))
            if (form.get("yes") or [""])[0] != "1" or errors or not stones:
                return self.send(200, render_delete(stones, errors))
            n, errors = cleanup.delete(self.idx, stones)
            if errors:
                return self.send(500, page("Delete", "<h1>Partly deleted</h1><p>Gravestones are set, "
                                           "%d files and folders removed. Errors:</p><ul>%s</ul>"
                                           % (n, "".join("<li>%s</li>" % esc(f) for f in errors))))
            return self.send(303, "", "text/plain; charset=utf-8",
                             {"Location": next_url("/clutter", ok="deleted", n=len(stones))})
        return self.send(404, "Not found", "text/plain; charset=utf-8")

    def session_route(self, row, rest, qs, as_md):
        sid, path = row["id"], row["path"]
        if rest == ["raw"]:
            live = row["platform"] in sources.LIVE
            if live:
                backup.backup_file(path)
            with open(path, "rb") as fh:
                data = fh.read()
            return self.send(200, data, "application/x-ndjson" if live else "application/json",
                             {"Content-Disposition": "attachment; filename=%s.%s" % (sid, "jsonl" if live else "json")})
        meta, items, tools, title = chat_index.load_chat(row)
        if not rest:
            if as_md:
                with_tools = (qs.get("tools") or ["1"])[0] != "0"
                fname = re.sub(r"[^\w.-]+", "_", title)[:60] + ".md"
                return self.send(200, chat_index.to_markdown(sid, title, meta, items, with_tools, row["platform"]),
                                 "text/markdown; charset=utf-8",
                                 {"Content-Disposition": "inline; filename=\"%s\"" % fname})
            digest = chat_index.DigestStore().get(sid)
            fam = self.idx.family(sid)
            mark = render_mark(sid, chronicle_db.clutter(), fam, self.path)
            return self.send(200, render_session(row, title, meta, items, digest, (qs.get("q") or [""])[0],
                                                 fam, (mark[0], notice(qs) + mark[1])))
        if len(rest) == 3 and rest[0] == "t" and rest[1] in tools and rest[2] in ("in", "out"):
            b = tools[rest[1]]
            text = tool_input_text(b["name"], b["input"]) if rest[2] == "in" else (b["result"] or "")
            return self.send(200, text, "text/plain; charset=utf-8")
        if len(rest) == 3 and rest[0] == "img" and rest[1] in tools and rest[2].isdigit():
            imgs = tools[rest[1]]["images"]
            if int(rest[2]) < len(imgs):
                return self.send_image(*imgs[int(rest[2])])
        if len(rest) == 3 and rest[0] == "u" and rest[1].isdigit() and rest[2].isdigit():
            i, j = int(rest[1]), int(rest[2])
            if i < len(items) and items[i]["kind"] == "user" and j < len(items[i]["images"]):
                return self.send_image(*items[i]["images"][j])
        return self.send(404, "Not found", "text/plain; charset=utf-8")

    def send_image(self, mtype, data):
        import base64
        if not re.match(r"^image/[\w.+-]+$", mtype or ""):
            mtype = "application/octet-stream"
        try:
            raw = base64.b64decode(data)
        except ValueError:
            return self.send(404, "Image unreadable", "text/plain; charset=utf-8")
        return self.send(200, raw, mtype)


# ---------------------------------------------------------------- CLI

def _resolve(idx, ref):
    idx.refresh(force=True)
    try:
        return idx.resolve(ref)
    except ValueError as e:
        sys.exit(str(e))


def cmd_export(idx, sid, out, with_tools):
    row = _resolve(idx, sid)
    meta, items, _tools, title = chat_index.load_chat(row)
    text = chat_index.to_markdown(row["id"], title, meta, items, with_tools, row["platform"])
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print("written:", out)
    else:
        sys.stdout.write(text)


def cmd_overview(idx, sid, max_chars):
    row = _resolve(idx, sid)
    meta, items, _tools, title = chat_index.load_chat(row)
    print(chat_index.overview(row, meta, items, title, chat_index.DigestStore().get(row["id"]), max_chars,
                              idx.family(row["id"])))


def cmd_search(idx, q, src, limit, with_tools):
    idx.refresh(force=True)
    c = idx.conn()
    try:
        kinds = ["user", "assistant"] + (["thinking", "tool"] if with_tools else [])
        res = chat_index.search(c, q, kinds, platform=src, order="relevance", limit=limit)
    finally:
        c.close()
    print("%d chats found%s\n" % (res["total"], (" (approximate: %s)" % ", ".join(res["weak"])) if res["weak"] else ""))
    branches = idx.branches()
    for r in res["results"]:
        br = branches.get(r["id"])
        print("%s  %-11s %s  (%d hits)%s" % (r["id"][:8], sources.PLATFORM_LABEL.get(r["platform"], r["platform"]),
                                             oneline(r["title"], 70), len(r["hits"]),
                                             ("  [earlier transcript of %s]" % br["head"][:8]) if br and br["kind"] == "earlier"
                                             else ("  [fork of %s]" % br["branch_of"][:8]) if br else ""))
        for hit in sorted(r["hits"], key=lambda x: x["r"])[:2]:
            print("    [%d] %s" % (hit["item"], oneline(hit["snip"].replace("\x02", "»").replace("\x03", "«"), 140)))


def cmd_backup(gone):
    if gone:
        rows = backup.gone_list()
        for path, since, deleted in rows:
            print("%s  %s%s" % (fmt_ts(since), path,
                                ("  (deleted in the app on %s)" % fmt_ts(deleted)) if deleted else ""))
        print("%d copies whose source is gone" % len(rows))
        return
    n = backup.run()
    if n is None:
        sys.exit("Another run is backing up right now, nothing done.")
    print("Archive: %s" % sources.LIVE_DIR)
    print("%d files checked: %d new, %d grown, %d new versions, %d metadata, %d unchanged%s%s"
          % (sum(v for k, v in n.items() if not k.startswith("gone")), n["new"], n["grown"],
             n["versioned"], n["meta"], n["unchanged"],
             (", %d skipped (deleted for good)" % n["buried"]) if n["buried"] else "",
             (", %d errors" % n["errors"]) if n["errors"] else ""))
    print("gone at the source: %d (new since the last run: %d)" % (n["gone"], n["gone new"]))


def cmd_clutter(idx, refs, restore, deleted):
    if deleted:
        rows = chronicle_db.gravestone_list()
        for g in rows:
            print("%s  %s  %-11s %s" % (fmt_ts(g["deleted"]), g["id"][:8],
                                        sources.PLATFORM_LABEL.get(g["platform"], g["platform"]), oneline(g["title"], 70)))
        print("%d chats deleted for good" % len(rows))
        return
    idx.refresh(force=True)
    if not refs:
        marks = chronicle_db.clutter()
        for sid, since in sorted(marks.items(), key=lambda x: x[1], reverse=True):
            r = idx.row(sid)
            print("%s  %s  %-11s %s" % (fmt_ts(since), sid[:8], sources.PLATFORM_LABEL.get(r["platform"], r["platform"])
                                        if r else "", oneline(r["title"], 70) if r else "(not in the index)"))
        print("%d chats marked as clutter" % len(marks))
        return
    try:
        rows = [idx.resolve(r) for r in refs]
    except ValueError as e:
        sys.exit(str(e))
    n = (chronicle_db.unmark if restore else chronicle_db.mark)([r["id"] for r in rows])
    for r in rows:
        print("  %s  %s" % (r["id"][:8], oneline(r["title"], 70)))
    print("%d %s" % (n, "restored" if restore else "marked as clutter"))


def cmd_delete(idx, refs, yes):
    idx.refresh(force=True)
    try:
        stones, errors = cleanup.plan(idx, refs)
    except ValueError as e:
        sys.exit(str(e))
    if errors or not stones:
        sys.exit("\n".join(errors + ["Nothing deleted."]))
    for s in stones:
        print("%s  %-11s %s%s" % (s["id"][:8], sources.PLATFORM_LABEL.get(s["platform"], s["platform"]),
                                  oneline(s["title"], 70), ("  [earlier transcript of %s]" % s["belongs_to"][:8]) if s["belongs_to"] else ""))
        for p in s["files"] or ["(no files left in the archive)"]:
            print("    " + cleanup.show(p))
    if not yes:
        if not sys.stdin.isatty():
            sys.exit("The confirmation needs a terminal; without one, use --yes. Nothing deleted.")
        if input("Delete %d chats from the archive for good? Live sources and raw exports stay. [y/N] "
                 % len(stones)).strip().lower() not in ("y", "yes"):
            sys.exit("Nothing deleted.")
    n, errors = cleanup.delete(idx, stones)
    print("%d gravestones set, %d files and folders removed" % (len(stones), n))
    if errors:
        sys.exit("\n".join(["Errors:"] + errors))


def cmd_import(idx, paths):
    try:
        r = ingest.run(paths)
    except RuntimeError as e:
        sys.exit(str(e))
    for line in ingest.report_lines(r):
        print(line)
    if r["corpus"]:
        idx.refresh(force=True)
        print("Index refreshed")
    if r["errors"]:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description="Searchable archive of your own AI chats")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5120)
    ap.add_argument("--allow-host", action="append", default=[],
                    help="additionally allowed Host header (e.g. a WireGuard address such as 10.0.0.2)")
    ap.add_argument("--reindex", action="store_true", help="drop the index and rebuild it")
    sub = ap.add_subparsers(dest="cmd")
    ex = sub.add_parser("export", help="print a chat as Markdown")
    ex.add_argument("sid", help="chat ID or an unambiguous prefix of it")
    ex.add_argument("-o", "--out")
    ex.add_argument("--no-tools", action="store_true")
    ov = sub.add_parser("overview", help="digest and outline of a chat")
    ov.add_argument("sid")
    ov.add_argument("--chars", type=int, default=6000)
    se = sub.add_parser("search", help="full-text search")
    se.add_argument("query", nargs="+")
    se.add_argument("--source", default="", help="claude-code, cowork, claude.ai, chatgpt, gemini")
    se.add_argument("--count", type=int, default=10)
    se.add_argument("--tools", action="store_true", help="also search tool calls and thinking")
    dg = sub.add_parser("digests", help="create missing digests with an LLM")
    dg.add_argument("--provider", default="mistral", choices=["mistral", "ollama", "openai"])
    dg.add_argument("--model")
    dg.add_argument("--url")
    dg.add_argument("--count", type=int, default=20, help="at most this many chats (0 = all)")
    dg.add_argument("--source", default="")
    dg.add_argument("--id", action="append", default=[], help="only these chats (renews existing digests)")
    dg.add_argument("--force", action="store_true", help="also regenerate existing digests")
    dg.add_argument("--include-summarized", action="store_true",
                    help="also claude.ai chats that already come with a summary")
    dg.add_argument("--dry-run", action="store_true", help="only list, send nothing")
    bk = sub.add_parser("backup", help="copy live sources into the archive")
    bk.add_argument("--gone", action="store_true", help="list copies whose source has disappeared")
    cl = sub.add_parser("clutter", help="mark chats as clutter, list them without IDs")
    cl.add_argument("sid", nargs="*", help="chat IDs or unambiguous prefixes of them")
    cl.add_argument("--restore", action="store_true", help="remove the mark")
    cl.add_argument("--deleted", action="store_true", help="list chats deleted for good (gravestones)")
    de = sub.add_parser("delete", help="delete clutter from the archive for good")
    de.add_argument("sid", nargs="+", help="chat IDs or unambiguous prefixes of them")
    de.add_argument("--yes", action="store_true", help="without confirmation")
    im = sub.add_parser("import", help="take exports into the archive, rebuild the corpus")
    im.add_argument("path", nargs="*", help="zip or unpacked folder; without a path only rebuild the corpus")
    args = ap.parse_args()

    if args.cmd == "backup":          # needs no index
        return cmd_backup(args.gone)
    idx = chat_index.Index()
    if args.reindex:
        idx.reset()
    if args.cmd == "export":
        return cmd_export(idx, args.sid, args.out, not args.no_tools)
    if args.cmd == "overview":
        return cmd_overview(idx, args.sid, args.chars)
    if args.cmd == "clutter":
        return cmd_clutter(idx, args.sid, args.restore, args.deleted)
    if args.cmd == "delete":
        return cmd_delete(idx, args.sid, args.yes)
    if args.cmd == "import":
        return cmd_import(idx, args.path)
    if args.cmd == "search":
        return cmd_search(idx, " ".join(args.query), args.source, args.count, args.tools)
    if args.cmd == "digests":
        from . import digests
        return digests.run(idx, args.provider, args.url, args.model, args.count, args.source,
                           args.id, args.force, args.include_summarized, args.dry_run)

    Handler.idx = idx
    Handler.allowed_hosts = Handler.allowed_hosts | set(args.allow_host) | {args.host}
    threading.Thread(target=idx.refresh, kwargs={"force": True}, daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print("chat-chronicle running on http://%s:%d/" % (args.host, args.port), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
