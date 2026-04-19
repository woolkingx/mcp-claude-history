#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import sys

from mcp_claude_history.adapter import cli, run_mcp


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("{"):
        cli()
    else:
        asyncio.run(run_mcp())
