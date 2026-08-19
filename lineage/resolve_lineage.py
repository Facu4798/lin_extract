#!/usr/bin/env python
"""Resolve which source table(s) feed a Cosmos document field.

Two ways to run this:

1. **From VSCode / no arguments** — edit QUERY_FILE and FIELD_NAME below,
   then hit Run (F5, or the Python extension's "Run Python File" button).

2. **From the command line** (see DESIGN.md FR8), e.g.:

       python lineage/resolve_lineage.py --file <query_file.txt> --field <CosmosFieldName> [--format json|text]

   Passing any command-line arguments switches this script into CLI mode and
   the QUERY_FILE/FIELD_NAME variables below are ignored.

Also importable programmatically:

    from lineage import resolve_field_lineage
    result = resolve_field_lineage("query_file.txt", "ClaimId")
"""

import json
import os
import sys

# This file lives inside the `lineage` package but is meant to be run
# directly (e.g. VSCode's Run button), which sets sys.path[0] to this file's
# own directory rather than its parent. Add the parent directory so
# `import lineage.xxx` below resolves regardless of how this is launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage.cli import main as cli_main
from lineage.errors import LineageError
from lineage.lineage_resolver import resolve_field_lineage

# ---------------------------------------------------------------------------
# Edit these and hit Run — used only when the script is launched with no
# command-line arguments (e.g. from an IDE).
# ---------------------------------------------------------------------------
QUERY_FILE = "example_query.txt"
FIELD_NAME = "ClaimId"
# ---------------------------------------------------------------------------


def run(query_file: str = QUERY_FILE, field_name: str = FIELD_NAME) -> None:
    """Resolve one field and print the result as JSON (IDE / variables mode)."""
    try:
        result = resolve_field_lineage(query_file, field_name)
    except LineageError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        return
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(cli_main())
    run()
