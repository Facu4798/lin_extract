"""Thin wrapper for the "one Cosmos collection, multiple independent query
files" case (DESIGN.md §1.1) — e.g. a collection populated by both a life
and a dental query file, appended together. Each file is resolved
independently and the results are unioned per field.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lineage.lineage_resolver import resolve_field_lineage
from lineage.models import LineageResult


@dataclass
class CollectionFieldLineage:
    field: str
    tables: set[str]
    is_literal_only: bool  # true only if literal-only in every file
    has_unresolved_branches: bool  # true if any file had unresolved branches
    per_file: dict[str, LineageResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "tables": sorted(self.tables),
            "is_literal_only": self.is_literal_only,
            "has_unresolved_branches": self.has_unresolved_branches,
            "per_file": {
                path: result.to_dict() for path, result in self.per_file.items()
            },
        }


def resolve_field_lineage_for_collection(
    file_paths: list[str], field_name: str
) -> CollectionFieldLineage:
    """Resolve ``field_name`` independently against every file in
    ``file_paths`` and union the results (§1.1). Each file must itself
    define the field in its final SELECT, or resolution for that file raises
    (per-file errors are not swallowed — same "fail loudly" policy as a
    single file)."""
    tables: set[str] = set()
    per_file: dict[str, LineageResult] = {}
    all_literal_only = True
    any_unresolved = False

    for path in file_paths:
        result = resolve_field_lineage(path, field_name)
        per_file[path] = result
        tables |= result.tables
        all_literal_only = all_literal_only and result.is_literal_only
        any_unresolved = any_unresolved or result.has_unresolved_branches

    return CollectionFieldLineage(
        field=field_name,
        tables=tables,
        is_literal_only=all_literal_only,
        has_unresolved_branches=any_unresolved,
        per_file=per_file,
    )
