"""FR2/FR3: parse each df's raw SQL (via sqlglot) into sources + projections."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from lineage.errors import SqlParseError
from lineage.models import Branch, DfStatement, ProjectionEntry, SourceRef, normalize_ident

# FR3: analytics_{sor_name}_cdz.{table_name} — two-part, dot-qualified, first
# part matching analytics_[^.]+_cdz.
REAL_TABLE_RE = re.compile(r"^analytics_[^.\s]+_cdz\.[^.\s]+$", re.IGNORECASE)

DIALECT = "spark"


def is_real_table_name(fully_qualified: str) -> bool:
    return bool(REAL_TABLE_RE.match(fully_qualified))


def _table_ref_name(table: exp.Table) -> str:
    parts = [p for p in (table.catalog, table.db, table.this.name if table.this else None) if p]
    return ".".join(parts)


def _extract_source(table_expr: exp.Expression, known_df_names: set[str]) -> SourceRef:
    if isinstance(table_expr, exp.Table):
        ref_name = _table_ref_name(table_expr)
        alias = table_expr.alias or table_expr.name
        if "." in ref_name:
            if is_real_table_name(ref_name):
                return SourceRef(ref_name=ref_name, alias=alias, is_real_table=True)
            return SourceRef(
                ref_name=ref_name,
                alias=alias,
                is_real_table=True,
                unsupported_reason=(
                    f"'{ref_name}' is dot-qualified but does not match the "
                    f"analytics_{{sor}}_cdz.{{table}} naming convention"
                ),
            )
        # bare identifier: must match a known df_name from the chain (FR3)
        if normalize_ident(ref_name) in known_df_names:
            return SourceRef(ref_name=ref_name, alias=alias, is_real_table=False)
        return SourceRef(
            ref_name=ref_name,
            alias=alias,
            is_real_table=None,
            unsupported_reason=(
                f"'{ref_name}' is not a schema-qualified real table and does "
                f"not match any earlier df_name in the chain"
            ),
        )

    # Anything else (subquery, lateral view, table function, etc.) is out of
    # scope per DESIGN.md — don't guess (§5, §6).
    alias = ""
    try:
        alias = table_expr.alias
    except Exception:
        pass
    return SourceRef(
        ref_name=table_expr.sql(dialect=DIALECT),
        alias=alias or "",
        is_real_table=None,
        unsupported_reason=f"unsupported FROM/JOIN source type: {type(table_expr).__name__}",
    )


def _collect_sources(select: exp.Select, known_df_names: set[str]) -> list[SourceRef]:
    sources: list[SourceRef] = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None and from_clause.this is not None:
        sources.append(_extract_source(from_clause.this, known_df_names))
    for join in select.args.get("joins", []) or []:
        if join.this is not None:
            sources.append(_extract_source(join.this, known_df_names))
    return sources


def _has_star(select: exp.Select) -> bool:
    for e in select.expressions:
        if isinstance(e, exp.Star):
            return True
        if isinstance(e, exp.Column) and isinstance(e.this, exp.Star):
            return True
    return False


def _collect_projections(select: exp.Select) -> dict[str, ProjectionEntry]:
    projections: dict[str, ProjectionEntry] = {}
    for e in select.expressions:
        if isinstance(e, exp.Star):
            continue
        if isinstance(e, exp.Alias):
            alias = e.alias
            body = e.this
        else:
            alias = e.alias_or_name
            body = e
        if not alias:
            continue
        projections[normalize_ident(alias)] = ProjectionEntry(alias=alias, expr=body)
    return projections


def _flatten_union(node: exp.Expression) -> list[exp.Expression]:
    """Flatten a (possibly chained) UNION/UNION ALL tree into its leaf
    SELECT statements, left to right. Union is left-associative in
    sqlglot's AST, so a 3-way union nests as Union(Union(s1, s2), s3)."""
    if isinstance(node, exp.Union):
        return _flatten_union(node.this) + _flatten_union(node.expression)
    return [node]


def parse_statement(df_name: str, raw_sql: str, line_no: int, known_df_names: set[str]) -> DfStatement:
    """Parse one df's raw SQL text into a :class:`DfStatement` (FR2).

    Supports ``SELECT ... UNION [ALL] SELECT ...`` (also DISTINCT/INTERSECT/
    EXCEPT, which sqlglot parses the same way): each branch gets its own
    independent FROM/JOIN scope and projections (see :class:`Branch`).
    """
    try:
        parsed = sqlglot.parse_one(raw_sql, read=DIALECT)
    except Exception as e:  # sqlglot raises its own ParseError subclasses
        raise SqlParseError(df_name, line_no, str(e)) from e

    selects = _flatten_union(parsed)
    if not selects or not all(isinstance(s, exp.Select) for s in selects):
        bad = next((s for s in selects if not isinstance(s, exp.Select)), parsed)
        raise SqlParseError(
            df_name,
            line_no,
            f"expected a SELECT statement (optionally combined with UNION/"
            f"UNION ALL), got {type(bad).__name__}",
        )

    branches = [
        Branch(
            sources=_collect_sources(select, known_df_names),
            projections=_collect_projections(select),
            has_star=_has_star(select),
        )
        for select in selects
    ]

    return DfStatement(name=df_name, raw_sql=raw_sql, line_no=line_no, branches=branches)
