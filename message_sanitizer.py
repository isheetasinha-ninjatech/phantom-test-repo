"""
Message sanitizer — MOVED to tools/message_sanitizer.py

This file is a compatibility shim. Import from tools.message_sanitizer instead.
"""

from tools.message_sanitizer import _strip_emojis, sanitize

if __name__ == "__main__":
    from tools.message_sanitizer import main

    main()
