"""Load golden_record_structure.json and resolve each source field name to
its physical column(s), scanning a folder of query files."""

from __future__ import annotations

import json
import os

from lineage.errors import LineageError
from lineage.lineage_resolver import resolve_field_lineage
from joins.models import NO_LITERAL, GoldenFieldSpec, ResolvedSource


def load_golden_structure(path: str) -> dict[str, GoldenFieldSpec]:
    """Parse golden_record_structure.json into ``{name: GoldenFieldSpec}``,
    normalizing both the shorthand list form and the ``{"fields": ...,
    "literal": ...}`` form."""
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    specs: dict[str, GoldenFieldSpec] = {}
    for name, value in raw.items():
        if isinstance(value, list):
            specs[name] = GoldenFieldSpec(name=name, source_fields=list(value))
        elif isinstance(value, dict):
            specs[name] = GoldenFieldSpec(
                name=name,
                source_fields=list(value.get("fields", [])),
                literal=value.get("literal", NO_LITERAL),
            )
        else:
            raise ValueError(
                f"golden record field '{name}' has an unsupported spec type "
                f"{type(value).__name__} (expected a list or an object)"
            )
    return specs


def _split_qualified(qualified: str) -> tuple[str, str]:
    """Split a lineage-resolved 'analytics_{sor}_cdz.{table}.{column}[.sub...]'
    string into (table_ref, column_path). The schema.table part is always
    exactly two dot-separated segments (enforced by lineage's own
    REAL_TABLE_RE), so the first two segments are the table and everything
    after is the column path."""
    parts = qualified.split(".")
    return ".".join(parts[:2]), ".".join(parts[2:])


def resolve_source_field(field_name: str, queries_dir: str) -> list[ResolvedSource]:
    """Resolve ``field_name`` against every query file in ``queries_dir``,
    keeping results from whichever file(s) actually define it (mirrors the
    "try each file, skip the ones that don't have it" pattern already used
    for batch resolution — see resolve_lineage.py's IDE-mode loop). Each hit
    is tagged with the file it came from."""
    results: list[ResolvedSource] = []
    for fname in sorted(os.listdir(queries_dir)):
        path = os.path.join(queries_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            lineage_result = resolve_field_lineage(path, field_name)
        except LineageError:
            continue
        for qualified in sorted(lineage_result.tables):
            table, column = _split_qualified(qualified)
            results.append(ResolvedSource(table=table, column=column, query_file=fname))
    return results
