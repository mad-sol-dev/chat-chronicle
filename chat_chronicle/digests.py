"""Digests (level 1): a few lines per chat, generated once by an LLM and stored.

Providers:
  mistral  api.mistral.ai, key in MISTRAL_API_KEY (or CHRONICLE_LLM_KEY)
  ollama   local or remote Ollama, URL via --url / CHRONICLE_LLM_URL
  openai   any OpenAI-compatible endpoint (llama.cpp, vLLM, LM Studio ...)

Before sending, obvious secrets (API keys, tokens, private keys, password
assignments) are redacted; chats contain everything that was ever pasted into
a shell. This is a net, not a guarantee.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import chat_index
from . import sources
from .sources import oneline

PROVIDERS = {
    "mistral": {"url": "https://api.mistral.ai/v1", "model": "mistral-small-latest", "key_env": "MISTRAL_API_KEY"},
    "ollama": {"url": "http://127.0.0.1:11434", "model": None, "key_env": None},
    "openai": {"url": "http://127.0.0.1:8080/v1", "model": None, "key_env": "OPENAI_API_KEY"},
}
INPUT_CHARS = 24000        # most history that goes to the model (~6-8k tokens)
MIN_CHARS = 1500           # shorter chats need no digest, the outline is enough
OLLAMA_CTX = 16384

SYSTEM = ("You write brief digests of archived AI chats. They are used later to decide quickly "
          "whether a chat is worth a look. Write in English, factual, without an introduction and "
          "without closing remarks. Don't invent anything: leave out whatever is not in the history.")

PROMPT = """Chat: {title}
Source: {platform}, {first} to {last}

Write the digest exactly in this format (at most 160 words):
Goal: <one sentence>
Result: <one or two sentences; "open" if there was none>
Decisions: <bullets "- ... (because ...)", or "none">
Open points: <bullets, or "none">
Keywords: <5 to 10 terms: devices, programs, files, hosts, technical terms>

History ([n] = message number, truncated):
{history}"""

SECRET_RES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[PRIVATE KEY]"),
    (re.compile(r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,})"), "[KEY]"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "[KEY]"),
    (re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})"), "[KEY]"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), "[KEY]"),
    (re.compile(r"\b(AIza[0-9A-Za-z_-]{30,})"), "[KEY]"),
    (re.compile(r"\b(mstrl_[A-Za-z0-9_-]{20,})"), "[KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[TOKEN]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [TOKEN]"),
    # also MISTRAL_API_KEY=, DB_PASSWORD:, GITHUB_TOKEN=
    (re.compile(r"(?i)(?<![A-Za-z])([A-Z0-9_]*(?:pass(?:wor[dt])?|pwd|secret|token|api[_-]?key)[A-Z0-9_]*)"
                r"(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|\S+)"), r"\1\2[REDACTED]"),
]


def redact(text):
    for rx, rep in SECRET_RES:
        text = rx.sub(rep, text)
    return text


def tidy(text):
    """'**Goal:**' -> 'Goal:'; mistral-small puts the headings in bold in about every second digest."""
    return re.sub(r"(?m)^\*\*(Goal|Result|Decisions|Open points|Keywords)(?::\*\*|\*\*:)",
                  r"\1:", text)


def build_input(items, budget=INPUT_CHARS, who="Claude"):
    """History for the model: user messages and the last answer to each, plus a tool list.

    If the texts get too long, every message shrinks first, then the middle
    drops out - start (the task) and end (the result) stay."""
    turns = []
    for i, it in enumerate(items):
        if it["kind"] == "user":
            turns.append((i, chat_index.USER_LABEL, it["text"], ""))
        elif it["kind"] == "assistant":
            texts = [b["text"] for b in it["blocks"] if b["type"] == "text"]
            tools = chat_index.tool_list(it["blocks"])
            if texts or tools:
                turns.append((i, who, texts[-1] if texts else "", tools))
    for width in (2000, 1200, 700, 400, 250):
        parts = []
        for i, who, text, tools in turns:
            t = text if len(text) <= width else text[: width * 2 // 3] + " […] " + text[-width // 3:]
            parts.append("[%d] %s: %s%s" % (i, who, t, ("\n(Tools: %s)" % tools) if tools else ""))
        out = "\n\n".join(parts)
        if len(out) <= budget:
            return out
    head, tail, used = [], [], 60
    lo, hi, front = 0, len(parts) - 1, True
    while lo <= hi:
        p = parts[lo] if front else parts[hi]
        if used + len(p) + 2 > budget:
            break
        used += len(p) + 2
        if front:
            head.append(p)
            lo += 1
        else:
            tail.insert(0, p)
            hi -= 1
        front = not front
    return "\n\n".join(head + ["[… middle skipped …]"] + tail)


def _post(url, payload, key=None, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ask(provider, url, model, key, system, user):
    """One request to the model, returns the answer text."""
    if provider == "ollama":
        d = _post(url.rstrip("/") + "/api/chat",
                  {"model": model, "stream": False,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                   "options": {"num_ctx": OLLAMA_CTX, "temperature": 0.2}})
        text = (d.get("message") or {}).get("content") or ""
    else:
        d = _post(url.rstrip("/") + "/chat/completions",
                  {"model": model, "temperature": 0.2, "max_tokens": 700,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
                  key)
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.S).strip()


def config(provider, url=None, model=None, need_key=True):
    p = PROVIDERS[provider]
    url = url or os.environ.get("CHRONICLE_LLM_URL") or p["url"]
    model = model or os.environ.get("CHRONICLE_LLM_MODEL") or p["model"]
    key = os.environ.get("CHRONICLE_LLM_KEY") or (os.environ.get(p["key_env"]) if p["key_env"] else None)
    if not model:
        sys.exit("No model given: set --model or CHRONICLE_LLM_MODEL (e.g. for Ollama)")
    if provider == "mistral" and not key and need_key:
        sys.exit("No Mistral key: set MISTRAL_API_KEY")
    return url, model, key


def digest_one(idx, digest_store, row, provider, url, model, key):
    meta, items, _tools, title = chat_index.load_chat(row)
    prompt = PROMPT.format(title=title, platform=sources.PLATFORM_LABEL.get(row["platform"], row["platform"]),
                           first=sources.fmt_ts(meta["first"], "%Y-%m-%d"),
                           last=sources.fmt_ts(meta["last"], "%Y-%m-%d"),
                           history=redact(build_input(items, who=chat_index.assistant_label(row["platform"]))))
    text = tidy(ask(provider, url, model, key, SYSTEM, prompt))
    if not text:
        raise RuntimeError("empty answer")
    digest_store.put(row["id"], text, "%s:%s" % (provider, model), row["size"], len(items))
    return text


def candidates(idx, digest_store, platform="", ids=(), force=False, with_summary=False, min_chars=MIN_CHARS):
    """Chats without a (current) digest, newest first; clutter and earlier transcripts
    are left out (chat_index.VISIBLE), explicitly named ids are not."""
    have = digest_store.sizes()
    c = idx.conn()
    try:
        if ids:
            return [idx.resolve(i) for i in ids]
        rows = c.execute("SELECT * FROM chats WHERE n_user>0 AND chars>=? AND (?='' OR platform=?) AND %s "
                         "ORDER BY last_ts DESC" % chat_index.VISIBLE, (min_chars, platform, platform)).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        if r["summary"] and not with_summary:
            continue              # claude.ai already ships a summary
        if not force and r["id"] in have and not chat_index.is_stale(have[r["id"]], r["size"]):
            continue
        out.append(r)
    return out


def run(idx, provider, url=None, model=None, limit=20, platform="", ids=(), force=False,
        with_summary=False, dry=False):
    url, model, key = config(provider, url, model, need_key=not dry)
    digest_store = chat_index.DigestStore()
    idx.refresh(force=True)
    todo = candidates(idx, digest_store, platform, ids, force or bool(ids), with_summary)
    print("%d chats without a current digest, processing %s with %s:%s"
          % (len(todo), "all" if not limit else "at most %d" % limit, provider, model), flush=True)
    if limit:
        todo = todo[:limit]
    if dry:
        chars = 0
        for r in todo:
            _meta, items, _t, _title = chat_index.load_chat(r)
            n = len(build_input(items))
            chars += n + len(PROMPT) + len(SYSTEM)
            print("  %s  %-11s %6d chars  %s" % (r["id"][:8], r["platform"], n, oneline(r["title"], 70)))
        print("In total about %d chars of input, roughly %d tokens (at ~3.5 chars per token)"
              % (chars, chars / 3.5))
        return
    for n, r in enumerate(todo, 1):
        t0 = time.time()
        try:
            text = digest_one(idx, digest_store, r, provider, url, model, key)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
            body = ""
            if isinstance(e, urllib.error.HTTPError):
                body = e.read().decode("utf-8", "replace")[:300]
            print("  [%d/%d] %s error: %s %s" % (n, len(todo), r["id"][:8], e, body), file=sys.stderr, flush=True)
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                sys.exit("Access denied, stopping")
            continue
        print("  [%d/%d] %s %.1fs  %s\n      %s" % (n, len(todo), r["id"][:8], time.time() - t0,
                                                   oneline(r["title"], 70), oneline(text, 160)), flush=True)
