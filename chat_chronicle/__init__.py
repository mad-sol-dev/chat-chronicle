"""chat-chronicle: the code behind chronicle.py and chronicle_mcp.py in the checkout.

    app             web interface and command line
    mcp_server      MCP server
    sources         where chats live on disk and how they are read
    backup          own copy of the live sources in the archive
    ingest          raw exports into the archive, the corpus built from them
    export_*        one converter per platform (claude.ai, ChatGPT, Gemini)
    chat_index      index, search and zoom levels
    chronicle_db    chronicle.db: what can't be rebuilt
    cleanup         clutter, deleting for good, suggestions
    digests         a few lines per chat, generated once by an LLM
"""
