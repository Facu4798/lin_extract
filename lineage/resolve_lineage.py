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
import subprocess
subprocess.run(["cls"],shell=True)
# This file lives inside the `lineage` package but is meant to be run
# directly (e.g. VSCode's Run button), which sets sys.path[0] to this file's
# own directory rather than its parent. Add the parent directory so
# `import lineage.xxx` below resolves regardless of how this is launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lineage.cli import main as cli_main
from lineage.errors import LineageError
from lineage.lineage_resolver import resolve_field_lineage
from lineage.field_descriptor import resolve_field_description
from lineage.utils import resolve_collections
# ---------------------------------------------------------------------------
# Edit these and hit Run — used only when the script is launched with no
# command-line arguments (e.g. from an IDE).
# ---------------------------------------------------------------------------
QUERY_FOLDER = "queries/claims"
queries = os.listdir(QUERY_FOLDER)
FIELDS = ["ClaimId"]
# ---------------------------------------------------------------------------


lineage = []
for field in FIELDS:
    sources = []
    accepted_queries = []
    for query in queries:
        try:
            result = resolve_field_lineage(
                QUERY_FOLDER + "\\" + query,
                field
            )
        except LineageError:
            # This query file genuinely doesn't define the field — routine,
            # not every file will.
            continue
        if result.has_unresolved_branches:
            # It resolved without raising, but only partially/ambiguously
            # (e.g. a struct member that doesn't exist in this file's
            # version of the field, or an ambiguous SELECT * candidate) —
            # lineage_resolver.py reports that as has_unresolved_branches
            # rather than raising, so it must be checked explicitly here
            # too, or a "no exception" file gets wrongly treated as a real
            # match with nothing to show for it.
            continue
        sources.extend(result.to_dict().get("tables"))
        accepted_queries.append(query)
    lineage.append(
        {
            "fieldName" : field,
            "fieldDescription": resolve_field_description(
                QUERY_FOLDER + "\\" + accepted_queries[0],
                field
            ),
            "domain": QUERY_FOLDER.split("\\")[-1],
            "collections": resolve_collections(accepted_queries),
            "sources" : list(set(sources))
        }
    )
        

print(
    json.dumps(
        lineage,
        indent=4
    )
)