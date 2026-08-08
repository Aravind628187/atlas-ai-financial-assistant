#!/usr/bin/env python
"""Convenience launcher: `python scripts/run_bot.py` from the project root."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
