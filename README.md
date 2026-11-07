<img width="2100" height="806" alt="chat-chronicle logo" src="https://github.com/user-attachments/assets/707ee271-43b6-4595-b2fc-8eb747836fe2" />

# chat-chronicle

> **Text:** written by Claude, read and fact-checked by the maintainer.
> **Code:** AI-generated, tested by trying it against real sessions and exports, not reviewed line by line.

Claude Code deletes its local session transcripts after 30 days unless told otherwise, and the chats on claude.ai, ChatGPT and Gemini live on the providers' servers, reachable only through an occasional export. chat-chronicle keeps one's own copy of all of them on one's own disk and makes it browsable and searchable: a web interface, a command line, and an MCP server, so that Claude (or any other MCP client) can search one's past conversations as a kind of long-term memory.

It started as the maintainer's (@mad-sol-dev) private tool, written in German. This repository is the English version of it, published so others can set it up too.

It is work in progress: it covers what the maintainer uses day to day, on Linux, and there are rough edges and ideas for more. Questions and wishes are welcome as issues.

## Why local

The idea behind it is to work locally: Claude Code runs on one's own machine, the code goes to a git remote anyway, and the chat history is the one part that exists only on the local disk. chat-chronicle copies that part into an archive before Claude Code cleans it up, and takes the exports of the web chats as well. Once an export is imported, the chats can be deleted on the platform; the archive keeps them.

"Local" is about where the history is kept, not about what the model sees: everything typed into a chat was sent to the provider at the time, and how long the provider keeps it depends on the account and its settings. chat-chronicle doesn't change that. It makes sure there is a complete copy that is one's own.

Claude Code's cloud sessions are the other way round: their history is kept by Anthropic, and they need access to a GitHub repository. chat-chronicle doesn't read them.

## What it reads

| Source | Where it comes from | Updated |
|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl`, from the CLI and the Code tab of every desktop instance | live, at least hourly |
| Claude desktop metadata | `~/.config/Claude*/claude-code-sessions/`: titles, archive flag, earlier transcripts and forks | live |
| Cowork | `~/.config/Claude*/local-agent-mode-sessions/` | live |
| claude.ai, ChatGPT, Gemini (AI Studio) | exports downloaded by hand, imported with `chronicle.py import` | on import |

Details on every source, including what is skipped, are in [docs/how-it-works.md](docs/how-it-works.md).

## Requirements

- **Linux.** On macOS the Claude Code sessions would be found (`~/.claude/projects`), but desktop and Cowork data is only looked for in `~/.config/Claude*`, and the services are systemd units; this has not been tried. On Windows it doesn't start (it uses `fcntl`).
- **Python 3.10 or newer** with SQLite FTS5, which most distributions ship. The tool itself needs only the standard library.
- Optional: `7z` (p7zip) to import exports packed as 7z; the `mcp` package for the MCP server (see [MCP server](#mcp-server)); an LLM endpoint for [digests](#digests).

## Installation

```bash
git clone https://github.com/mad-sol-dev/chat-chronicle.git
cd chat-chronicle
python3 chronicle.py backup        # first copy of the live sources into the archive
python3 chronicle.py               # web interface on http://127.0.0.1:5120/
```

The archive goes to `~/.local/share/chat-chronicle/archive` (see [Configuration](#configuration) to put it elsewhere). The first start builds the search index, which takes a few seconds per thousand chats.

### Running it as a service

The web interface and an hourly backup run as systemd user units:

```bash
./install-systemd.sh              # writes the units with this checkout's path, enables and starts them
./install-systemd.sh --remove     # stops and removes them again; the archive stays
```

| Unit | What it does |
|---|---|
| `chat-chronicle.service` | web interface on `127.0.0.1:5120` |
| `chat-chronicle-backup.timer` | runs `chronicle.py backup` hourly, so copies are made even when nobody opens the tool; catches up after the machine was off |

```bash
systemctl --user list-timers chat-chronicle-backup   # next run
journalctl --user -u chat-chronicle-backup           # what each run copied
journalctl --user -u chat-chronicle                  # errors while indexing end up here
```

The units point to the checkout, so after moving it, run `./install-systemd.sh` again.

### MCP server

The MCP server needs the `mcp` package in a virtual environment inside the checkout:

```bash
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
# or without uv:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

For Claude Code:

```bash
claude mcp add --scope user chat-chronicle -- "$PWD/.venv/bin/python" "$PWD/chronicle_mcp.py"
```

For the Claude desktop app, in `claude_desktop_config.json`:

```json
"mcpServers": {
  "chat-chronicle": {
    "command": "/path/to/chat-chronicle/.venv/bin/python",
    "args": ["/path/to/chat-chronicle/chronicle_mcp.py"]
  }
}
```

Other clients can use Streamable HTTP: `.venv/bin/python chronicle_mcp.py --http --port 5121` (for example for Open WebUI).

| Tool | Purpose |
|---|---|
| `search_chats` | full-text search; returns title, date, source, the digest line and the best snippets per chat |
| `recent_chats` | chats of the last days |
| `chat_overview` | digest or summary, edited files, and an outline with message numbers |
| `find_in_chat` | hits inside one chat, with message numbers |
| `read_chat` | messages by number, as Markdown, page by page |
| `archive_stats` | chats per source and instance, projects |

The tools are built so that a model doesn't pull whole chats into its context: it searches, looks at the outline, and then reads only the messages it needs (see [Zoom levels](docs/how-it-works.md#zoom-levels)). Every answer is capped at 40,000 characters. The server can only read; marking and deleting chats isn't possible through it.

## Importing exports

claude.ai, ChatGPT and Gemini have no local files, their chats come from exports:

| Source | How to get an export | What arrives |
|---|---|---|
| claude.ai | Settings → Privacy → Export data; the link comes by email | a zip with `conversations.json`, projects, memories |
| ChatGPT | Settings → Data controls → Export data; the link comes by email | a zip with `conversations.json`, `chat.html`, images, audio |
| Gemini (AI Studio) | AI Studio keeps every prompt as a file in the Drive folder `Google AI Studio`; download that folder from Google Drive | one or more zips of the folder |

The menu paths are the ones known when this was written; the platforms move them now and then. Chats from the Gemini app (gemini.google.com, Google Takeout) are a different product and not supported.

```bash
python3 chronicle.py import ~/Downloads/data-…-batch-0000.zip   # a zip, a 7z or an unpacked folder, several at once
python3 chronicle.py import                                     # only rebuild the corpora from the stored exports
```

The source is recognised by its content. The raw export is stored unchanged in the archive, and the chats are rebuilt from all exports stored so far, the newest first, older ones filling the gaps. A chat deleted on the platform therefore stays as long as any stored export has it. Importing the same export twice changes nothing. Import all parts of one Gemini download together; they count as one export.

How sources are recognised, what the converters keep and drop, and how chat IDs are kept stable is in [docs/how-it-works.md](docs/how-it-works.md#importing-exports).

## Using it

### Web interface

`python3 chronicle.py` or the service: a list of all chats with filters by source and project, full-text search, and a page per chat with tool calls collapsed. A chat page reads its file fresh on every load, so a running Claude Code session can be followed by reloading. The list and search refresh the index in the background at most every 15 s.

### Command line

```bash
python3 chronicle.py                         # web interface on 127.0.0.1:5120
python3 chronicle.py --reindex               # drop the index and rebuild it
python3 chronicle.py search docker compose   # search from the command line [--source chatgpt] [--count 20] [--tools]
python3 chronicle.py overview 97634133       # digest and outline (an unambiguous ID prefix is enough) [--chars 6000]
python3 chronicle.py export 97634133 -o chat.md [--no-tools]
python3 chronicle.py backup                  # copy the live sources into the archive now
python3 chronicle.py backup --gone           # list copies whose source is gone
python3 chronicle.py import <export>         # zip, 7z or folder
python3 chronicle.py clutter 97634133        # mark as clutter, see Clean-up
python3 chronicle.py delete 97634133         # delete a clutter chat for good, asks first
python3 chronicle.py digests --count 50      # write missing digests, see Digests
```

### Search syntax

A chat matches when all words occur somewhere in it, not necessarily in the same message (the question often has one term and an answer three messages later the other). Words are matched as prefixes, `"..."` searches a phrase. With `OR` or `NOT` the expression is passed to SQLite FTS5 as written and applies per message. Umlauts and accents are folded (`Schlussel` finds `Schlüssel`, `schluessel` doesn't). Words the tokenizer drops entirely fall back to a substring search and are reported as approximate.

## Clean-up

Nothing in the archive is deleted automatically. Removing a chat takes two decisions, made in the web interface (on a chat's page, in the list, and in the views "Clutter" and "Suggestions") or on the command line:

1. **Mark as clutter.** The chat disappears from the list, the search and the MCP server, but stays in the archive and can be brought back from the "Clutter" view.
2. **Delete for good.** Only for chats marked as clutter, after a confirmation that lists every file that goes. A gravestone stays behind, so the next backup or import doesn't bring the chat back.

```bash
python3 chronicle.py clutter 97634133 eb129377    # mark
python3 chronicle.py clutter                      # list
python3 chronicle.py clutter --restore 97634133   # bring back
python3 chronicle.py delete 97634133              # lists what goes, asks [y/N]; --yes without asking
python3 chronicle.py clutter --deleted            # list the gravestones
```

The "Suggestions" view lists candidates and marks nothing by itself: very short chats, sessions deleted in the desktop app, and chats whose digest says there was no result and no decision. The live sources (`~/.claude`, `~/.config/Claude*`) and the stored raw exports are never touched. What deleting removes for each source is in [docs/how-it-works.md](docs/how-it-works.md#clean-up).

## Digests

A digest is a short description of a chat (goal, result, decisions, open points, keywords), written once per chat by an LLM and stored. The MCP server and the search show it, so a model can decide whether a chat is worth opening without reading it.

```bash
export MISTRAL_API_KEY=...
python3 chronicle.py digests --dry-run --count 0          # list all candidates and estimate the input size, send nothing
python3 chronicle.py digests --count 50                   # Mistral, mistral-small-latest; --count 0 means all
python3 chronicle.py digests --provider ollama --model <name> --url http://host:11434
```

| Provider | Endpoint | Notes |
|---|---|---|
| `mistral` (default) | `https://api.mistral.ai/v1` | key in `MISTRAL_API_KEY` |
| `ollama` | `http://127.0.0.1:11434` or `--url` | native API, so the context length can be set (16k) |
| `openai` | any OpenAI-compatible server | llama.cpp, vLLM, LM Studio; key in `OPENAI_API_KEY` if needed |

Chats under 1,500 characters are skipped (the outline is enough), and so are claude.ai chats that already carry a summary from the export, unless `--include-summarized` is given. The model gets the user messages and the last answer of every turn plus a list of the tools used, at most 24,000 characters; longer chats lose their middle part first. A digest of a running session is considered stale once the session has grown by more than 25 % and 20 kB.

Digests send chat content to the configured model. Obvious secrets are masked before sending (private keys, `sk-…`/`ghp_…`/`mstrl_…`-style tokens, bearer tokens, JWTs, assignments like `DB_PASSWORD=…`); this is a net, not a guarantee. With a local model nothing leaves the machine. On Mistral's free Experiment plan, API requests may be used for training unless that is switched off in the console; according to Mistral, the paid API is not.

A digest is the model's reading of the chat, not a record of it. In a spot check one digest turned "183 passed (start: 106)" into "183 of 106 tests passed" and listed two open questions as decisions. Numbers and commit hashes should be checked in the chat itself.

## Configuration

Everything is set with environment variables; without them, the defaults below apply.

| Variable | Default | What |
|---|---|---|
| `CHRONICLE_ARCHIVE` | `$XDG_DATA_HOME/chat-chronicle/archive` (`~/.local/share/…`) | the archive: copies, exports, corpora, `chronicle.db` |
| `CHRONICLE_INDEX` | `$XDG_CACHE_HOME/chat-chronicle/index.db` (`~/.cache/…`) | the search index, rebuildable |
| `CHRONICLE_CORPUS` | `<archive>/corpus/{claude.ai,chatgpt,gemini}` | corpus folders to read, colon-separated |
| `CHRONICLE_USER` | `You` | label for one's own messages |
| `CHRONICLE_LLM_URL`, `CHRONICLE_LLM_MODEL`, `CHRONICLE_LLM_KEY` | per provider | defaults for digests |

The same values have to reach every entry point: the shell, the systemd units and the MCP server. `install-systemd.sh` writes `CHRONICLE_ARCHIVE`, `CHRONICLE_INDEX` and `CHRONICLE_USER` into the units if they are set when it runs. For the MCP server, pass them with `claude mcp add -e CHRONICLE_ARCHIVE=…` or an `"env"` block in `claude_desktop_config.json`. A symlink at the default location pointing to the real archive avoids all of that.

### The archive

```
~/.local/share/chat-chronicle/archive/
├── live/
│   ├── claude-code/<project>/…     copy of ~/.claude/projects/<project>/…
│   ├── desktop/<instance>/…        copy of ~/.config/<instance>/claude-code-sessions/…
│   ├── cowork/<instance>/…         copy of ~/.config/<instance>/local-agent-mode-sessions/…
│   └── backup.db                   copies whose source is gone
├── exports/<source>/               raw exports, unchanged, never pruned
├── corpus/<source>/                one JSON per chat, built from exports/, rebuildable
└── chronicle.db                    digests, clutter marks, gravestones, pinned chat IDs
```

The archive is the only copy of everything Claude Code has already cleaned up and of every chat deleted on a platform, so it belongs in a backup. `live/` and `exports/` are the source of truth, `corpus/` and the index can be rebuilt from them. `chronicle.db` can't: it holds what cost money or a decision. The archive directory is created with mode 700, and the copies keep the sources' file modes.

To have Claude Code itself keep transcripts longer, raise `cleanupPeriodDays` in `~/.claude/settings.json`. chat-chronicle doesn't depend on it.

## Security

Chats contain whatever went through them: paths, hostnames, sometimes credentials pasted into a shell. The archive is a second copy of all of it, including in every backup of it.

The web server binds to `127.0.0.1` only and rejects requests whose `Host` header isn't localhost (protection against DNS rebinding). `--host` and `--allow-host` exist for access over a VPN such as WireGuard, but there is no authentication: anyone who can reach the port can read everything and mark or delete chats. The forms for that carry a token that changes with every start of the server, and posts from another origin are rejected, so a web page open in the same browser can't submit them.

The MCP server gives the connected model read access to the whole archive, except chats marked as clutter.

## Limitations

- The JSONL format of Claude Code and the desktop metadata are not documented by Anthropic. The parser was written against the files present in October 2026 and skips record types it doesn't know; a format change will more likely show up as missing content than as a crash.
- A session that is created and deleted within the hour while nothing reads the index never reaches the archive.
- Forks made in the CLI (`--fork-session`) are not recognised as forks.
- ChatGPT has reportedly started to export one file per chat instead of a single `conversations.json`; no such export was at hand, so that format isn't recognised.
- Not indexed: subagent transcripts (they are archived, though) and the extracted text of attachments (only file names are shown).

## How it came about

As I understood it from the maintainer, the starting point was the missing `/export` in the Claude desktop app. I suggested a live server over a static generator, because the sessions keep growing and a server can read them on every request without a separate update step. A separate archive of exported chats with its own MCP server already existed; once the two had drifted apart while covering the same ground, the maintainer decided to merge them into one tool with source adapters, and the zoom levels and digests were my answer to models pulling whole chats into their context.

The archive came after the maintainer found two desktop sessions from August missing and had to restore them from a filesystem snapshot. My reading was that Claude Code's 30-day cleanup had removed them; I suggested a copy owned by this tool that only ever grows. Clean-up with a reversible first step, gravestones, and the import of exports with older exports filling gaps followed from the maintainer's wish to have every chat ever held locally and to throw clutter away for good.

For publishing, the German original was translated as a whole, code included; its history was not carried over.
