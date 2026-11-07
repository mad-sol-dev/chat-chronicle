#!/usr/bin/env python3
"""chat-chronicle: web interface and command line (code in chat_chronicle/app.py).

    python3 chronicle.py            web interface on http://127.0.0.1:5120/
    python3 chronicle.py --help     all commands
"""
import os
import sys

# The checkout, even when Python runs with -P or -I and leaves the script's folder out of sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chat_chronicle.app import main  # noqa: E402

if __name__ == "__main__":
    main()
