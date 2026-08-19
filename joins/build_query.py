#!/usr/bin/env python
"""Build a golden-record SQL query from golden_record_structure.json and a
folder of per-collection query files.

Edit GOLDEN_STRUCTURE_FILE / QUERIES_DIR below and hit Run (F5, or the
Python extension's "Run Python File" button in VSCode).
"""

import os
import sys

# This file lives inside the `joins` package but is meant to be run
# directly, which sets sys.path[0] to this file's own directory rather than
# its parent. Add the parent directory so `import joins.xxx`/`import
# lineage.xxx` below resolve regardless of how this is launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from joins.query_builder import build_query

# ---------------------------------------------------------------------------
GOLDEN_STRUCTURE_FILE = "joins/golden_record_structure.json"
QUERIES_DIR = "queries"
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = build_query(GOLDEN_STRUCTURE_FILE, QUERIES_DIR)

    print(result.sql)

    if result.warnings:
        print("\n-- warnings --", file=sys.stderr)
        for w in result.warnings:
            print(f"! {w}", file=sys.stderr)
