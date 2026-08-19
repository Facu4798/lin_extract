"""FR4-FR6: recursive lineage resolution engine.

Builds the ordered df chain (FR1 + FR2), then walks the dependency graph
described in DESIGN.md §3.4 starting from ``(final_df, cosmos_field_name)``
down to real-table leaves, literals, NULLs, or explicit "unresolved"
branches — never guessing (§6).
"""

from __future__ import annotations

from sqlglot import exp

from lineage.errors import FieldNotFoundError
from lineage.file_parser import parse_file
from lineage.models import DfStatement, LineageResult, TraceNode, normalize_ident
from lineage.sql_parser import parse_statement

MAX_DEPTH = 200  # defensive guard against cycles in malformed input (§3.4)


class ChainContext:
    """Holds the fully-parsed, ordered df chain and provides name lookup."""

    def __init__(self, statements: list[DfStatement]):
        self.statements = statements
        self.by_name: dict[str, DfStatement] = {
            normalize_ident(s.name): s for s in statements
        }

    @property
    def final(self) -> DfStatement:
        return self.statements[-1]

    def get(self, name: str) -> DfStatement | None:
        return self.by_name.get(normalize_ident(name))


def build_chain(file_path: str) -> ChainContext:
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return build_chain_from_text(text)


def build_chain_from_text(text: str) -> ChainContext:
    raw_statements = parse_file(text)
    if not raw_statements:
        from lineage.errors import FileParseError

        raise FileParseError("No df assignments found in query file.")

    statements: list[DfStatement] = []
    known_df_names: set[str] = set()
    for df_name, raw_sql, line_no in raw_statements:
        stmt = parse_statement(df_name, raw_sql, line_no, known_df_names)
        statements.append(stmt)
        known_df_names.add(normalize_ident(df_name))

    return ChainContext(statements)


class _Resolver:
    def __init__(self, ctx: ChainContext):
        self.ctx = ctx
        self.tables: set[str] = set()
        self.unresolved: list[str] = []

    def resolve_field(self, field_name: str) -> LineageResult:
        final = self.ctx.final
        key = normalize_ident(field_name)
        entry = final.projections.get(key)
        if entry is None:
            raise FieldNotFoundError(
                field_name, [p.alias for p in final.projections.values()]
            )

        trace = TraceNode(label=field_name, kind="field")
        self._resolve_expr(entry.expr, final, trace, visited=frozenset(), depth=0)

        is_literal_only = len(self.tables) == 0 and len(self.unresolved) == 0
        return LineageResult(
            field=field_name,
            tables=self.tables,
            is_literal_only=is_literal_only,
            has_unresolved_branches=bool(self.unresolved),
            trace=trace,
            unresolved_reasons=self.unresolved,
        )

    # -- expression dispatch (FR5) -------------------------------------

    def _resolve_expr(
        self,
        node: exp.Expression,
        ctx_stmt: DfStatement,
        parent: TraceNode,
        visited: frozenset,
        depth: int,
    ) -> None:
        if depth > MAX_DEPTH:
            reason = f"max recursion depth ({MAX_DEPTH}) exceeded — possible cycle"
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label="<depth-exceeded>", kind="unresolved", detail=reason))
            return

        # transparent unwrap: parens / redundant alias nesting
        if isinstance(node, exp.Paren):
            self._resolve_expr(node.this, ctx_stmt, parent, visited, depth)
            return
        if isinstance(node, exp.Alias):
            self._resolve_expr(node.this, ctx_stmt, parent, visited, depth)
            return

        if isinstance(node, exp.Null):
            parent.children.append(TraceNode(label="NULL", kind="null"))
            return

        if isinstance(node, exp.Boolean):
            parent.children.append(TraceNode(label=node.sql(), kind="literal"))
            return

        if isinstance(node, exp.Literal):
            kind = "literal"
            label = node.sql()
            parent.children.append(TraceNode(label=label, kind=kind))
            return

        if isinstance(node, exp.Column):
            self._resolve_column(node, ctx_stmt, parent, visited, depth)
            return

        if isinstance(node, (exp.Coalesce, exp.Concat)):
            fn_node = TraceNode(label=node.sql(), kind="function")
            parent.children.append(fn_node)
            for arg in node.flatten():
                self._resolve_expr(arg, ctx_stmt, fn_node, visited, depth + 1)
            return

        # Anything else (CASE, CAST, window funcs, subqueries, arbitrary
        # function calls, arithmetic, ...) is explicitly out of scope.
        # Surface it rather than guessing (FR5 last bullet, §6).
        reason = (
            f"unsupported expression type '{type(node).__name__}' "
            f"in df '{ctx_stmt.name}': {node.sql()}"
        )
        self.unresolved.append(reason)
        parent.children.append(TraceNode(label=node.sql(), kind="unresolved", detail=reason))

    # -- column reference resolution (§3.3, FR6) -------------------------

    def _resolve_column(
        self,
        col: exp.Column,
        ctx_stmt: DfStatement,
        parent: TraceNode,
        visited: frozenset,
        depth: int,
    ) -> None:
        col_name = col.name
        table_part = col.table  # '' if unqualified

        if table_part:
            matches = [s for s in ctx_stmt.sources if normalize_ident(s.alias) == normalize_ident(table_part)]
            if not matches:
                reason = (
                    f"alias '{table_part}' referenced by column '{col.sql()}' "
                    f"in df '{ctx_stmt.name}' does not match any FROM/JOIN source"
                )
                self.unresolved.append(reason)
                parent.children.append(TraceNode(label=col.sql(), kind="unresolved", detail=reason))
                return
            source = matches[0]
        else:
            candidates = ctx_stmt.sources
            if len(candidates) == 1:
                source = candidates[0]
            else:
                reason = (
                    f"unqualified column '{col_name}' in df '{ctx_stmt.name}' "
                    f"is ambiguous across {len(candidates)} FROM/JOIN sources "
                    f"({', '.join(s.alias or s.ref_name for s in candidates)}) — schema "
                    f"information is not available to disambiguate"
                )
                self.unresolved.append(reason)
                parent.children.append(TraceNode(label=col_name, kind="ambiguous", detail=reason))
                return

        node_label = f"{table_part}.{col_name}" if table_part else col_name

        if source.is_real_table is None:
            reason = source.unsupported_reason or f"could not classify source '{source.ref_name}'"
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
            return

        if source.is_real_table:
            self.tables.add(source.ref_name)
            parent.children.append(TraceNode(label=node_label, kind="table", detail=source.ref_name))
            return

        # source is another df in the chain — recurse (§3.3(b), edge case #4)
        dep_stmt = self.ctx.get(source.ref_name)
        if dep_stmt is None:
            reason = f"df '{source.ref_name}' referenced by '{ctx_stmt.name}' was not found in the chain"
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
            return

        dep_key = (normalize_ident(dep_stmt.name), normalize_ident(col_name))
        if dep_key in visited:
            reason = f"cycle detected resolving '{dep_stmt.name}.{col_name}'"
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
            return

        dep_entry = dep_stmt.projections.get(normalize_ident(col_name))
        if dep_entry is None:
            if dep_stmt.has_star:
                reason = (
                    f"df '{dep_stmt.name}' uses SELECT * — cannot determine which "
                    f"source column maps to '{col_name}' without table schema information"
                )
            else:
                reason = (
                    f"column '{col_name}' is not an output column of df '{dep_stmt.name}'"
                )
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
            return

        child = TraceNode(label=f"{dep_stmt.name}.{dep_entry.alias}", kind="column")
        parent.children.append(child)
        self._resolve_expr(
            dep_entry.expr, dep_stmt, child, visited | {dep_key}, depth + 1
        )


def resolve_field_lineage(file_path: str, field_name: str) -> LineageResult:
    """Public entry point (FR8): resolve one Cosmos field's lineage from a
    query file on disk."""
    ctx = build_chain(file_path)
    return _Resolver(ctx).resolve_field(field_name)


def resolve_field_lineage_from_text(text: str, field_name: str) -> LineageResult:
    """Same as :func:`resolve_field_lineage` but takes the file contents
    directly (used by tests / programmatic callers that don't have a file on
    disk)."""
    ctx = build_chain_from_text(text)
    return _Resolver(ctx).resolve_field(field_name)


def resolve_all_fields(file_path: str) -> dict[str, LineageResult]:
    """Resolve lineage for every output field in the final SELECT (helper
    for batch use, e.g. lineage-documenting a whole collection — DESIGN.md §8 Q7)."""
    ctx = build_chain(file_path)
    results: dict[str, LineageResult] = {}
    for entry in ctx.final.projections.values():
        results[entry.alias] = _Resolver(ctx).resolve_field(entry.alias)
    return results
