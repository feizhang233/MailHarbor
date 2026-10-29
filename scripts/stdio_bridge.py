#!/usr/bin/env python3
"""Absolute-path launcher for desktop MCP clients that do not set cwd."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mailhub.stdio_bridge import main

if __name__ == "__main__":
    raise SystemExit(main())
