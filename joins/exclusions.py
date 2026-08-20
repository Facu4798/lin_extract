"""An optional exclude-list for known-bad SELECT * candidate columns.

When a df is built from ``SELECT *`` over 2+ joined dfs/tables, the lineage
resolver can't tell which of those tables actually has the requested
column — so, per explicit direction (see ``lineage_resolver.py``'s
ambiguous-star handling), every joined table is included as a candidate
source, flagged unresolved. Most of the time that's harmless (an extra
COALESCE arm that just never matches), but sometimes a candidate is flat
wrong: the column only exists in one of the joined tables, and the other
doesn't have it at all.

This module lets you say so explicitly, so ``build_query`` can drop it —
both from the SELECT's COALESCE *and* from join-key inference, since a
bad candidate can just as easily poison a join condition as a SELECT.

Each exclusion entry is one of:
- ``"<table>.<column>"`` — excludes that candidate everywhere it appears,
  in any query file.
- ``ResolvedSource.tagged`` form, ``"<table>.<column>&<query_file>"`` —
  narrows the exclusion to that one specific file (use this when the same
  table.column is a legitimate candidate elsewhere but wrong in one file).
"""

from __future__ import annotations

import json


def load_exclusions(path: str) -> set[str]:
    """Load a JSON file holding a flat list of exclusion strings."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{path}: expected a JSON list of strings")
    return set(raw)


def is_excluded(resolved_source, exclusions: set[str]) -> bool:
    """Whether ``resolved_source`` (a ``joins.models.ResolvedSource``)
    matches an entry in ``exclusions``, by either its exact per-file
    ``.tagged`` form or its file-agnostic ``.qualified`` form."""
    return resolved_source.tagged in exclusions or resolved_source.qualified in exclusions
