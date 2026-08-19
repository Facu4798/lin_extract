"""Load golden_record_structure.json and resolve each source field name to
its physical column(s), scanning a folder of query files."""

from __future__ import annotations

import json
import os

from lineage.errors import FieldNotFoundError, LineageError
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


def resolve_source_field(field_name: str, queries_dir: str) -> tuple[list[ResolvedSource], list[str]]:
    """Resolve ``field_name`` against every query file in ``queries_dir``,
    keeping results from whichever file(s) actually define it (mirrors the
    "try each file, skip the ones that don't have it" pattern already used
    for batch resolution — see resolve_lineage.py's IDE-mode loop). Each hit
    is tagged with the file it came from.

    Also returns a list of diagnostic strings, so a field that *looks* like
    it should resolve but doesn't isn't a silent dead end: a
    ``FieldNotFoundError`` from one file is routine (that file just doesn't
    define this field, most files won't) and is only reported if the field
    resolved nowhere at all; any *other* error (a parse failure, an
    unresolved/ambiguous branch surfaced as an exception, etc.) is always
    reported, even when the field did resolve elsewhere — it means that
    file has a real problem, silently masked by the "try every file, skip
    failures" pattern otherwise.

    A dotted (struct sub-field) path deserves special care here: the
    resolver itself never *raises* for a struct member that doesn't exist —
    it reports has_unresolved_branches instead (consistent with how every
    other resolution difficulty is handled, see lineage_resolver.py). So a
    file where the top-level wrapper alias exists but the specific nested
    member doesn't would otherwise succeed here with zero tables and no
    exception, silently contributing nothing while looking like a match —
    the unresolved reasons are surfaced as diagnostics so that isn't
    invisible. This does NOT discard tables that did resolve alongside an
    unresolved reason (e.g. a multi-source SELECT * where every candidate
    table is included but the branch still counts as unresolved) — those
    are kept, just with the reason surfaced too.
    """
    results: list[ResolvedSource] = []
    not_found: list[str] = []
    problems: list[str] = []

    for fname in sorted(os.listdir(queries_dir)):
        path = os.path.join(queries_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            lineage_result = resolve_field_lineage(path, field_name)
        except FieldNotFoundError:
            not_found.append(fname)
            continue
        except LineageError as e:
            problems.append(f"{fname}: {e}")
            continue

        if lineage_result.has_unresolved_branches:
            for reason in lineage_result.unresolved_reasons:
                problems.append(f"{fname}: {reason}")

        for qualified in sorted(lineage_result.tables):
            table, column = _split_qualified(qualified)
            results.append(ResolvedSource(table=table, column=column, query_file=fname))

    diagnostics = list(problems)
    if not results and not_found:
        diagnostics.append(
            f"'{field_name}' not found as an output field in: {', '.join(not_found)}"
        )
    return results, diagnostics
