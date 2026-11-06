# How it works

> **Text:** written by Claude, not yet reviewed by the maintainer.

The [README](../README.md) covers setting the tool up and using it. This file describes the rules behind it: where each source comes from, what the archive copies and keeps, how exports become chats, and what deleting removes.

## Sources

| Source | Live location | Index reads | Updated |
|---|---|---|---|
| Claude Code | `~/.claude/projects/*/*.jsonl` (CLI and the Code tab of every desktop instance) | `archive/live/claude-code/` | live |
| Desktop metadata | `~/.config/Claude*/claude-code-sessions/**/local_*.json`: title, archive flag, instance, earlier transcripts and forks | `archive/live/desktop/<instance>/` | live |
| Cowork | `~/.config/Claude*/local-agent-mode-sessions/**/.claude/projects/*/*.jsonl` | `archive/live/cowork/<instance>/` | live |
| claude.ai, ChatGPT, Gemini (AI Studio) | raw exports, imported with `chronicle.py import` | `archive/corpus/<source>/`, built from `archive/exports/<source>/` | on import |

The live sources are read only from the tool's own copy (see [Archive](#archive)). Several desktop instances (started with separate `--user-data-dir`, for example `~/.config/Claude` and `~/.config/Claude-work`) all write their sessions to `~/.claude/projects`; the instance is taken from the directory the desktop metadata lives in, and the archive keeps that directory's name. Cowork starts a new CLI session with the whole history on every turn, so only the newest transcript per Cowork session is indexed.

The corpus directories default to `archive/corpus/{claude.ai,chatgpt,gemini}` and can be set with `CHRONICLE_CORPUS` (colon-separated); chats from a corpus outside the archive can be read but not deleted. The claude.ai corpus carries a `metadata.parts` field (length of each text, tool and result section of a message); with it, tool results can be told apart from the answer that follows. Corpora without it still work, the split is then guessed.

Not indexed: subagent transcripts (they are archived, though) and the extracted text of attachments (only file names are shown).

The JSONL format of Claude Code is not documented by Anthropic. The parser was written against the files present in October 2026 and skips record types it doesn't know; a format change will more likely show up as missing content than as a crash.

### Earlier transcripts and forks

When one jumps back to an earlier message in the desktop app, it starts a new transcript with the history up to that point. The old transcript stays on disk and is only listed in the session's metadata (`priorCliSessionIds`, `rewindEdges`). A fork made on purpose (`forkedFromSessionId`) is a session of its own. Both carry the shared beginning a second time.

The index keeps the shared beginning only with the counterpart and indexes the copy from the first message where it differs, so a search doesn't return the same history several times. Earlier transcripts are left out of the list, `recent_chats` and the digests; the session they belong to links to them. Forks stay visible. A term that only occurs in the shared part therefore finds the original, not the fork, while `find_in_chat` inside a fork does cover the shared part.

The relationships come from the desktop metadata, which is as undocumented as the JSONL format. Forks made in the CLI (`--fork-session`) have no such metadata and are not recognised.

## Archive

Claude Code removes transcripts on its own: as far as we can tell 30 days after their last change unless `cleanupPeriodDays` says otherwise, and some time after a session is deleted in the desktop app (it leaves a `.desktop-released.json` marker; Claude Code's settings mention a grace period, its length wasn't measured). The settings of version 2.1.308 exempt desktop and Cowork sessions from the 30-day cleanup (`desktopSessionCleanupPeriodDays`); two desktop sessions of the maintainer's were removed anyway, so the exemption is either newer or doesn't always apply. The tool therefore keeps its own copy of the live sources, and the index reads only that copy. Nothing in it is deleted automatically.

The paths below `live/` mirror the sources, so every copy can be traced back to its original. `chronicle.db` holds what cost money or a decision and can't be rebuilt: the digests, the clutter marks and gravestones from [Clean-up](#clean-up), and pinned chat IDs (see [IDs](#ids)). It sits next to the copies, so a backup of the archive covers both.

### What is copied

| Source | Files |
|---|---|
| Claude Code | `*.jsonl`, each session's folder (`subagents/`, `tool-results/`, `custom-title.json`), the `*.desktop-released.json` markers |
| Desktop | `local_*.json` |
| Cowork | `local_*.json`; inside each `local_*/` only `.claude/projects/`, `outputs/` and `uploads/` |

Never copied: Cowork's `.audit-key`, `.claude.json` and its backups, anything named like a key (`*.pem`, `id_rsa*`, `.credentials.json`), `cowork-*-cache.json`, the `skills-plugin/` and `rpm/` trees, and the `memory/` folders.

### Rules

| Case | What happens |
|---|---|
| new file | copy |
| grown, the old content is the beginning of the new one (JSONL is appended) | overwrite |
| changed otherwise or shrunk | keep the old copy as `<name>.<date>` (date of its last change), then copy |
| desktop metadata changed | overwrite, no old versions (they change constantly and are small) |
| gone at the source | the copy stays; `backup.db` records since when, and "deleted in the app on …" if a `.desktop-released.json` marker says so |
| deleted for good (gravestone) | not copied again, see [Clean-up](#clean-up) |

A note is dropped again when the file reappears at the source, for example after restoring it from a snapshot. No marker is known for deletions in Cowork; those show up only as "gone".

Copies are made with a reflink (`FICLONE`) where the filesystem supports it (btrfs, XFS): original and copy share their blocks until one of them changes. On the maintainer's btrfs the first run copied 425 files (335 MB) in 0.25 s, and `btrfs filesystem du` reported 12 KiB as exclusive to the archive. Elsewhere the files are copied normally. File modes and modification times are kept; the archive directory itself is created with mode 700.

### When it runs

- before every index refresh, which happens at most every 15 s;
- for a single chat right before it is read (chat page, `read_chat`, export), so a running session can still be followed;
- hourly from the systemd timer, so copies are made even when nobody opens the tool.

A lock file keeps the web service, the MCP servers and the timer from copying at the same time. One gap remains: a session that is created and deleted within the hour while nothing reads the index never reaches the archive.

## Importing exports

1. The source is recognised by its content: claude.ai and ChatGPT by `conversations.json` (conversations with `chat_messages` or with `mapping`), Gemini by files with AI Studio JSON (starting with `runSettings`).
2. The raw export is copied unchanged to `archive/exports/<source>/<date>_<name>`, by reflink where possible. `<date>` is the export's day in UTC, taken from its name where it has one, otherwise from the newest change inside it. A zip or 7z is copied as it is. From an unpacked folder only the export's own files are taken; anything else in the folder is listed and left out. If the same export is already there, nothing happens. The check is a SHA-256 over the export's files, so an archive and its unpacked folder count as the same export.
3. `archive/corpus/<source>/` is rebuilt from all exports in `exports/<source>/`.
4. The report lists new and grown chats, chats that are now only in older exports, and chats skipped for a gravestone. Then the index is refreshed.

The Python standard library can't read 7z. A 7z export is unpacked with `7z` into `~/.cache/chat-chronicle/ingest/` for the duration of the run and removed afterwards (not `/tmp`, which is often a small tmpfs). The stored export stays a 7z.

Drive names the zips of a folder download after the folder with a UTC timestamp (`Google AI Studio-20251228T101500Z-001.zip`), as far as known; the date is read from that pattern. Import all parts of a download together; they share the timestamp and count as one export.

### Newest first, older ones fill gaps

A chat comes from the newest export that has it. A chat that appears only in older exports stays and names its export in `metadata.rescued_from`. Old exports are the only source for chats deleted on the platform, which is why `exports/` only grows. The parts of one Drive download all count as the newest.

A claude.ai export still lists chats that were deleted on the platform, but as empty husks: the structure is there, the text is gone. Older exports often still have the content. For claude.ai a chat is therefore taken from the newest export that has it with content. If none has, the husk from the newest one is used, and a chat without any message is left out. ChatGPT exports simply leave deleted chats out.

Chats with a gravestone (see [Clean-up](#clean-up)) are skipped by their ID and by the platform's own ID (the conversation ID, for Gemini the file name), so an older export can't bring them back either.

### Corpus

```
archive/corpus/<source>/
├── <created>_<title>_<id8>.json     one unified JSON per chat
├── attachments/<key>/…              files split out of the chats, referenced from them
└── project_docs/                    claude.ai only: project knowledge as Markdown, with _catalog.json
```

`<key>` is the conversation UUID for claude.ai (extracted text of attached files) and the chat ID for Gemini (files embedded in the chat). The ChatGPT corpus has no attachments.

Each source owns its folder under `corpus/`. A rebuild writes only files whose content changed and removes what it no longer produces. A file keeps its name as long as the chat keeps its title, so after a new export the index re-reads only the chats that changed. `source.exported_at` is the time of the export the chat's current content first came from.

### Converters

**claude.ai.** Tool traffic is compacted rather than dropped, thinking blocks are dropped, tool results are cut to 1,500 characters, search hits stay as title, URL and a short excerpt. The export doesn't link projects to chats, so `metadata.project` stays empty.

**ChatGPT.** Only the text parts of each message are kept, from all branches of the conversation in order of time, so a regenerated answer stands next to the one it replaced. That leaves out a lot: in the maintainer's December 2025 export the dropped parts amounted to about 9.4 million characters of reasoning, 5.3 million of code and 3.6 million of code interpreter output, plus web search results and the transcripts of voice chats, so a voice chat may come out as an empty shell. Images and audio stay in the raw export.

**Gemini (AI Studio).** A chat is a file whose name is the chat's title (Drive replaces characters like `:` with `_`). Folders don't matter: chat and ID depend only on the file name, so sorting a download into folders changes nothing. The messages are the chunks of `chunkedPrompt` that have text, one message per chunk; Gemini's thought summaries stay in the answer text. Beyond that:

- The title is the file name, the time is the file's last change in Drive as the download carries it. AI Studio stores no time per message.
- A chunk with a file becomes a message with `metadata.attachments`. Files embedded in the chat (images, audio, Python files) are written to `attachments/<chat-id>/`. Files from Drive (voice recordings, documents, images) are referenced only by their Drive ID, which a download doesn't keep; they stay in the raw export, and the chat shows them as "Audio from Drive" and the like.
- A chat renamed in AI Studio counts as a new chat in the next download; the old one stays from the older export.

### IDs

A chat's ID is a uuid5 of the platform's own ID (the conversation UUID, for Gemini the file name), so it is the same in every build. The `ids` table in `chronicle.db` can pin a different ID for a platform ID; the maintainer uses it for chats that got random IDs from an older converter, so that digests and marks stay attached to them. A build never changes the ID of a chat that is already in the corpus: if it would, it stops before writing anything.

## Clean-up

### Clutter

A chat marked as clutter disappears from the list, the search, `recent_chats`, `search_chats` and the other MCP tools (also when asked for by ID), and from digest runs. Nothing is deleted: the chat stays in the archive, keeps being backed up, and is listed in the "Clutter" view, from where it can be brought back. The marks live in `chronicle.db`, not in the rebuildable index.

A marked session takes its earlier transcripts with it. A fork of a marked chat stays visible and is then indexed in full, including the beginning it shares with the marked one, so a term from that part still finds the fork.

### Deleting for good

Only chats marked as clutter can be deleted, after a confirmation that lists every file that goes.

| Source | Removed |
|---|---|
| Claude Code | the transcript in `archive/live/claude-code/`, its old versions `<name>.<date>`, its session folder (`subagents/`, `tool-results/`), its `.desktop-released.json` marker and its desktop metadata; the session's earlier transcripts the same way |
| Cowork | the session's whole `local_<id>/` folder (all turns, `outputs/`, `uploads/`) and its `local_<id>.json` |
| claude.ai | the chat's JSON in `archive/corpus/claude.ai/` and its folder in `attachments/` there |
| ChatGPT, Gemini | the chat's JSON in `archive/corpus/<source>/`, for Gemini also its folder in `attachments/` there |

The chat's digest is deleted with it. Forks of a deleted session are sessions of their own and stay; the next refresh indexes them in full. The live sources (`~/.claude`, `~/.config/Claude*`) and the raw exports are never touched: what the app keeps, the app decides.

Each deleted chat leaves a gravestone in `chronicle.db`: ID, title, source, date of deletion, the files that were removed, and the keys that identify the chat elsewhere (the `local_` ID of its desktop or Cowork metadata, the platform's own ID for an exported chat). The backup skips everything a gravestone covers, so a deleted session doesn't come back while the app still has it. The index skips gravestoned chats as well, also when a corpus file with the same ID is written again, and the import doesn't build them again from any export.

One gap: if a deleted desktop session is continued in the app and one jumps back to an earlier message there, the app starts a new transcript with a new ID and the history up to that point. The gravestone doesn't cover it, so it shows up again.

Undoing a deletion means restoring the files from a backup and removing the chat's row from the `gravestones` table by hand; there is no command for it.

### Suggestions

The "Suggestions" view lists candidates for clutter and marks nothing by itself:

- chats under 1,500 characters (they get no digest either), leaving out anything active in the last 24 hours;
- sessions deleted in the desktop app (a `.desktop-released.json` marker, noted in `backup.db`);
- chats whose digest says the result is open and there were no decisions.

## Zoom levels

Reading a whole chat into a model's context is expensive: Claude Code sessions are a few MB of JSONL each, exported chats often 50,000 characters and more. The tool therefore offers a chat in levels, each much smaller than the next.

| Level | What | Size | Made by |
|---|---|---|---|
| 0 Hits | title, date, source, best snippets | ~100 tokens per chat | full-text search |
| 1 Digest | goal, result, decisions, open points, keywords | ~300 tokens | an LLM, once per chat, stored |
| 2 Outline | one line per message with its number, tools as a short list | 1–3k tokens | deterministic, no LLM |
| 3 Excerpt | hits inside one chat with message numbers | small | full-text search |
| 4 Full text | Markdown, page by page | as needed | – |

Where a claude.ai export already carries a summary (about half of them do), that summary stands in for the digest.

## Index

The search index is a SQLite database with FTS5 in `~/.cache/chat-chronicle/index.db` (`CHRONICLE_INDEX`). It can be dropped at any time (`chronicle.py --reindex`) and is rebuilt from the archive; a full build took about 8 s for 3,200 chats on the maintainer's machine, after that only changed files are read again. The web service and the MCP server share it; whichever runs first after a change updates it.
