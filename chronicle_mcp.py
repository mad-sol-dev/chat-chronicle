#!/usr/bin/env python3
"""chat-chronicle: MCP server (code in chat_chronicle/mcp_server.py).

Needs the mcp package (>= 2.0), the rest is standard library:
    <venv>/bin/python chronicle_mcp.py                      stdio (Claude Desktop, Claude Code)
    <venv>/bin/python chronicle_mcp.py --http --port 5121   Streamable HTTP (e.g. Open WebUI)
"""
import os
import sys

# The checkout, even when Python runs with -P or -I and leaves the script's folder out of sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chat_chronicle.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
