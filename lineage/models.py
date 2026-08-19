"""Data structures shared across the lineage resolver (DESIGN.md §7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


def normalize_ident(name: str) -> str:
    """Normalize an identifier for case-insensitive comparison.

    Spark SQL identifiers are case-insensitive by default. We normalize
    consistently to lowercase for all matching (alias lookup, df-name lookup,
    column lookup) per DESIGN.md §5 edge case #8. This is a deliberate
    simplification: quoted/backtick identifiers are not treated specially.
    """
    return name.strip().strip("`").strip('"').lower()


@dataclass
class SourceRef:
    """One entry from a statement's FROM/JOIN clause.

    ``ref_name`` is either a fully-qualified real table name
    (``analytics_{sor}_cdz.{table}``) or the name of an earlier df in the
    chain. ``is_real_table`` is ``None`` when the reference could not be
    classified (FR3) — e.g. a bare identifier that doesn't match any known
    df_name, or an unsupported source such as a subquery.
    """

    ref_name: str
    alias: str
    is_real_table: Optional[bool]
    unsupported_reason: Optional[str] = None


@dataclass
class ProjectionEntry:
    """One output column of a df's SELECT list: its declared/implicit alias
    and the expression AST (a sqlglot Expression) that produces it."""

    alias: str  # original-case alias as written in the SQL
    expr: Any  # sqlglot.exp.Expression


@dataclass
class DfStatement:
    """Parsed representation of one ``df_name = SELECT ...`` assignment."""

    name: str
    raw_sql: str
    line_no: int
    sources: list[SourceRef] = field(default_factory=list)
    # keyed by normalize_ident(alias) -> ProjectionEntry
    projections: dict[str, ProjectionEntry] = field(default_factory=dict)
    has_star: bool = False  # SELECT * (or t.*) present — FR/edge case #6


@dataclass
class TraceNode:
    """One node in the human-readable resolution trace (FR7)."""

    label: str
    kind: str  # field | column | table | literal | null | function | unresolved | ambiguous
    detail: str = ""
    children: list["TraceNode"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "kind": self.kind,
            "detail": self.detail,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class LineageResult:
    """Result of resolving one Cosmos field's lineage (FR7)."""

    field: str
    tables: set[str]
    is_literal_only: bool
    has_unresolved_branches: bool
    trace: TraceNode
    unresolved_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "tables": sorted(self.tables),
            "is_literal_only": self.is_literal_only,
            "has_unresolved_branches": self.has_unresolved_branches,
            "unresolved_reasons": list(self.unresolved_reasons),
            "trace": self.trace.to_dict(),
        }
