#!/usr/bin/env python
"""Build a golden-record SQL query from golden_record_structure.json and a
folder of per-collection query files.

Edit GOLDEN_STRUCTURE_FILE / QUERIES_DIR below and hit Run (F5, or the
Python extension's "Run Python File" button in VSCode).
"""

import os
import sys
import subprocess
subprocess.run(["cls"],shell=True)
# This file lives inside the `joins` package but is meant to be run
# directly, which sets sys.path[0] to this file's own directory rather than
# its parent. Add the parent directory so `import joins.xxx`/`import
# lineage.xxx` below resolve regardless of how this is launched.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from joins.query_builder import build_query

# ---------------------------------------------------------------------------
GOLDEN_STRUCTURE_FILE = "joins/golden_record_structure.json"
QUERIES_DIR = "queries"
# Path to a JSON exclude-list (see joins/exclusions.py) of known-bad
# SELECT * candidate columns to drop from both the COALESCE and join-key
# inference — e.g. ["analytics_x_cdz.t2.name"] or, to scope it to one
# query file, ["analytics_x_cdz.t2.name&life.txt"]. Leave as None to skip.
EXCLUDE_SOURCES_FILE = None
# Golden field names to dedup the final result on — one row per distinct
# combination of these survives, chosen by DEDUP_ORDER_BY. Both must be
# set together, or left as None/[] to skip deduping entirely.
DEDUP_PARTITION_BY = None  # e.g. ["nationalId"]
# Golden field names to break dedup ties by; each entry may end in " ASC"
# or " DESC" (default ASC if omitted) — e.g. ["updatedAt DESC"].
DEDUP_ORDER_BY = None
# Real source tables that aren't unique on the column they get joined on
# within some golden field's own coalesce (e.g. joining two collections on
# a name, which can repeat) — collapse each to one row per join-key value,
# tie-broken by the given order-by list, *before* that join runs, so a
# non-unique key can't fan out the join itself. Only applies to a table
# used as a non-anchor participant in some field's own spanning-tree join.
# e.g. {"analytics_x_cdz.customer": ["updatedAt DESC"]}. Leave as None/{}
# to skip.
DEDUP_JOIN_SOURCES = None
# If True, emit a sequence of "CREATE OR REPLACE TEMP VIEW ...;" statements
# (one per stage) plus a final "SELECT ...;", instead of one WITH-clause
# query — split on ";" and run/cache each statement to let Spark
# materialize each stage instead of re-deriving the whole upstream chain
# every time. If False, emit the usual single WITH-clause query.
AS_TEMP_VIEWS = False
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = build_query(
        GOLDEN_STRUCTURE_FILE,
        QUERIES_DIR,
        exclude_sources=EXCLUDE_SOURCES_FILE,
        dedup_partition_by=DEDUP_PARTITION_BY,
        dedup_order_by=DEDUP_ORDER_BY,
        dedup_join_sources=DEDUP_JOIN_SOURCES,
        as_temp_views=AS_TEMP_VIEWS,
    )

    print(result.sql)

    if result.warnings:
        print("\n-- warnings --", file=sys.stderr)
        for w in result.warnings:
            print(f"! {w}", file=sys.stderr)
