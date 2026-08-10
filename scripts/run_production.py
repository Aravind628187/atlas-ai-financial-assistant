#!/usr/bin/env python3
"""Render entry point: FastAPI + Telegram webhook + scheduler in one process."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.production import main  # noqa: E402


if __name__ == "__main__":
    main()
