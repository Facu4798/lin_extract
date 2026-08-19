"""Golden-record query builder.

Given a golden_record_structure.json (per-field coalesce of source field
names, each defined in exactly one of a folder of per-collection query
files, see DESIGN.md's ``df_name = SELECT ...`` format) plus that folder of
query files, generates a single SQL query that:

- SELECTs each golden field as ``COALESCE(source1, source2, ..., literal)``
  (or just the bare column, or just the literal, when only one contributor
  exists).
- JOINs the distinct physical source tables together, using each
  cross-collection golden field's own coalesced columns as the join key
  (see ``joins/query_builder.py`` for the exact rule and its limits).

Built on top of the ``lineage`` package — it does not modify it.
"""

from joins.models import GoldenFieldSpec, ResolvedSource, NO_LITERAL
from joins.resolver import load_golden_structure, resolve_source_field
from joins.query_builder import BuildResult, build_query

__all__ = [
    "GoldenFieldSpec",
    "ResolvedSource",
    "NO_LITERAL",
    "load_golden_structure",
    "resolve_source_field",
    "BuildResult",
    "build_query",
]
