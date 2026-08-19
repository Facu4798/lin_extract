"""FR4-FR6: recursive lineage resolution engine.

Builds the ordered df chain (FR1 + FR2), then walks the dependency graph
described in DESIGN.md §3.4 starting from ``(final_df, cosmos_field_name)``
down to real-table leaves, literals, NULLs, or explicit "unresolved"
branches — never guessing (§6).
"""

from __future__ import annotations

from sqlglot import exp

from lineage.errors import FieldNotFoundError, StructFieldNotFoundError
from lineage.file_parser import parse_file
from lineage.models import DfStatement, LineageResult, SourceRef, TraceNode, normalize_ident
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

    @staticmethod
    def _branch_entries_for(stmt: DfStatement, key: str) -> list[tuple[int, "Branch | None", "ProjectionEntry | None"]] | None:
        """Locate ``key`` as an output column of ``stmt`` and return one
        (branch_index, branch, entry) triple per branch — entry/branch are
        ``None`` for a branch that doesn't actually provide a value at that
        position (e.g. a shorter branch, or one that's a bare ``SELECT *``).

        Matches Spark's real UNION semantics: only the FIRST branch's
        aliases name the result's columns — later branches are matched by
        *position*, not by their own (possibly different, possibly absent)
        aliases. Returns ``None`` if ``key`` isn't one of branch 0's output
        aliases at all.
        """
        branch0 = stmt.branches[0]
        position = None
        for idx, entry in enumerate(branch0.projections.values()):
            if normalize_ident(entry.alias) == normalize_ident(key):
                position = idx
                break
        if position is None:
            return None

        results = []
        for i, branch in enumerate(stmt.branches):
            values = list(branch.projections.values())
            if position < len(values):
                results.append((i, branch, values[position]))
            else:
                results.append((i, branch, None))
        return results

    def resolve_field(self, field_name: str) -> LineageResult:
        final = self.ctx.final
        path = field_name.split(".")
        top_key = path[0]

        located = self._branch_entries_for(final, top_key)
        if located is None:
            available = [e.alias for e in final.branches[0].projections.values()]
            raise FieldNotFoundError(field_name, available)

        matches = [(i, b, e) for i, b, e in located if e is not None]

        n = len(final.branches)
        trace = TraceNode(label=field_name, kind="field")
        container = trace
        if n > 1:
            container = TraceNode(label=f"UNION ({final.name})", kind="union")
            trace.children.append(container)
            matched_indices = {i for i, _, _ in matches}
            for i in range(n):
                if i not in matched_indices:
                    reason = (
                        f"column '{top_key}' is not an output column of df "
                        f"'{final.name}' [branch {i + 1}/{n}]"
                    )
                    self.unresolved.append(reason)
                    container.children.append(
                        TraceNode(label=f"{field_name} [branch {i + 1}/{n}]", kind="unresolved", detail=reason)
                    )

        for i, branch, entry in matches:
            branch_tag = "" if n == 1 else f" [branch {i + 1}/{n}]"
            branch_label = field_name + branch_tag

            # Drill into STRUCT(...) sub-fields for a dotted path like
            # "Address.City" — each remaining path segment must name a
            # member of a STRUCT at that point. With a single branch this
            # fails loudly (matches original behavior); with multiple
            # branches a per-branch failure is reported as an unresolved
            # branch instead of aborting the other branches.
            expr = entry.expr
            failed = False
            for depth_i, part in enumerate(path[1:], start=1):
                if not isinstance(expr, exp.Struct):
                    reason = (
                        f"'{'.'.join(path[:depth_i])}'{branch_tag} is not a "
                        f"STRUCT, so sub-field '{part}' cannot be resolved"
                    )
                    if n == 1:
                        raise StructFieldNotFoundError(field_name, reason)
                    self.unresolved.append(reason)
                    container.children.append(TraceNode(label=branch_label, kind="unresolved", detail=reason))
                    failed = True
                    break
                match = next(
                    (
                        m
                        for m in expr.expressions
                        if isinstance(m, exp.PropertyEQ) and normalize_ident(m.name) == normalize_ident(part)
                    ),
                    None,
                )
                if match is None:
                    available = sorted(
                        m.name for m in expr.expressions if isinstance(m, exp.PropertyEQ)
                    )
                    reason = (
                        f"no sub-field '{part}' in STRUCT{branch_tag}. "
                        f"Available: {', '.join(available) or '<none>'}"
                    )
                    if n == 1:
                        raise StructFieldNotFoundError(field_name, reason)
                    self.unresolved.append(reason)
                    container.children.append(TraceNode(label=branch_label, kind="unresolved", detail=reason))
                    failed = True
                    break
                expr = match.expression
            if failed:
                continue

            branch_view = final.branch_view(i)
            child = container if n == 1 else TraceNode(label=branch_label, kind="column")
            if n > 1:
                container.children.append(child)
            self._resolve_expr(expr, branch_view, child, visited=frozenset(), depth=0)

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

        if isinstance(node, exp.Struct):
            # STRUCT(expr AS field, expr AS field, ...) — nested JSON object
            # construction. sqlglot normalizes every member (explicitly
            # aliased or not) to a PropertyEQ(name, expr); lineage for the
            # struct as a whole is the union of every member's lineage.
            struct_node = TraceNode(label="STRUCT(...)", kind="struct")
            parent.children.append(struct_node)
            for member in node.expressions:
                if isinstance(member, exp.PropertyEQ):
                    member_label = member.name
                    member_expr = member.expression
                else:
                    # Defensive fallback — shouldn't occur with sqlglot's
                    # normalization, but don't guess if it does.
                    member_label = getattr(member, "alias_or_name", "") or "<unnamed>"
                    member_expr = member
                field_node = TraceNode(label=member_label, kind="struct_field")
                struct_node.children.append(field_node)
                self._resolve_expr(member_expr, ctx_stmt, field_node, visited, depth + 1)
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
        # col.parts gives every dot-separated identifier left-to-right. For
        # 3+ parts (e.g. "df_item_0.item.customerCode"), this is Spark's
        # native syntax for reaching into a nested struct-typed *source*
        # column: parts[0] is the alias, parts[1] is the column, and
        # parts[2:] is a struct sub-field path that only narrows which
        # nested value is read — it doesn't change which table/column feeds
        # it, so it's carried through for the trace but not required to
        # resolve further (we drill into it only if it turns out to
        # address one of our own STRUCT(...) expressions).
        parts = [p.name for p in col.parts]
        col_sql = col.sql()

        if len(parts) >= 2:
            table_part = parts[0]
            col_name = parts[1]
            struct_path = parts[2:]
        else:
            table_part = ""
            col_name = parts[0]
            struct_path = []

        if table_part:
            matches = [s for s in ctx_stmt.sources if normalize_ident(s.alias) == normalize_ident(table_part)]
            if not matches:
                reason = (
                    f"alias '{table_part}' referenced by column '{col_sql}' "
                    f"in df '{ctx_stmt.name}' does not match any FROM/JOIN source"
                )
                self.unresolved.append(reason)
                parent.children.append(TraceNode(label=col_sql, kind="unresolved", detail=reason))
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

        node_label = col_sql
        self._resolve_via_source(source, col_name, struct_path, ctx_stmt.name, node_label, parent, visited, depth)

    def _resolve_via_source(
        self,
        source: SourceRef,
        col_name: str,
        struct_path: list,
        referencing_df_name: str,
        node_label: str,
        parent: TraceNode,
        visited: frozenset,
        depth: int,
    ) -> None:
        """Continue resolution once a column reference has been matched to a
        specific FROM/JOIN source (real table, or another df to recurse
        into). Factored out so the ``SELECT *`` single-source passthrough
        below can reuse the exact same real-table/df dispatch logic."""
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
            reason = f"df '{source.ref_name}' referenced by '{referencing_df_name}' was not found in the chain"
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
            return

        dep_key = (normalize_ident(dep_stmt.name), normalize_ident(col_name))
        if dep_key in visited:
            reason = f"cycle detected resolving '{dep_stmt.name}.{col_name}'"
            self.unresolved.append(reason)
            parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
            return

        self._resolve_dep_column(dep_stmt, col_name, struct_path, node_label, parent, visited | {dep_key}, depth)

    def _resolve_dep_column(
        self,
        dep_stmt: DfStatement,
        col_name: str,
        struct_path: list,
        node_label: str,
        parent: TraceNode,
        visited: frozenset,
        depth: int,
    ) -> None:
        """Resolve ``col_name`` as an output column of ``dep_stmt``, which
        may have multiple UNION ALL branches (§ new: UNION support) — each
        branch is resolved independently (own FROM/JOIN scope) and their
        lineage is unioned, same treatment as COALESCE/CONCAT/STRUCT args.

        With multiple branches, matching follows Spark's real UNION
        semantics (only branch 0's aliases name the result — later branches
        line up by *position*, not name); see ``_branch_entries_for``. With
        exactly one branch this is just "look up col_name by name", same as
        before UNION support existed.
        """
        branches = dep_stmt.branches
        n = len(branches)

        if n == 1:
            located = [(0, branches[0], branches[0].projections.get(normalize_ident(col_name)))]
        else:
            found = self._branch_entries_for(dep_stmt, col_name)
            if found is None:
                reason = (
                    f"df '{dep_stmt.name}' is a UNION whose first branch does not "
                    f"define '{col_name}' as an output column (later branches are "
                    f"matched by position, so it can't be located)"
                )
                self.unresolved.append(reason)
                parent.children.append(TraceNode(label=node_label, kind="unresolved", detail=reason))
                return
            located = found

        container = parent
        if n > 1:
            container = TraceNode(label=f"UNION ({dep_stmt.name})", kind="union")
            parent.children.append(container)

        for i, branch, dep_entry in located:
            branch_tag = "" if n == 1 else f" [branch {i + 1}/{n}]"
            branch_label = node_label + branch_tag

            if dep_entry is None:
                if branch.has_star:
                    # A bare SELECT * with exactly one FROM/JOIN source has
                    # no ambiguity about where any given column comes from
                    # — every column it exposes passes straight through
                    # from that one source, whatever it's named, so resolve
                    # directly into it (recursing further if that source is
                    # itself a df, possibly with its own SELECT *). With 2+
                    # sources we can't tell which one actually has the
                    # column without schema info, so that case stays
                    # explicitly unresolved rather than guessing.
                    if len(branch.sources) == 1:
                        passthrough_label = f"{branch_label} (via SELECT * in '{dep_stmt.name}')"
                        self._resolve_via_source(
                            branch.sources[0],
                            col_name,
                            struct_path,
                            dep_stmt.name,
                            passthrough_label,
                            container,
                            visited,
                            depth + 1,
                        )
                        continue
                    reason = (
                        f"df '{dep_stmt.name}'{branch_tag} uses SELECT * over "
                        f"{len(branch.sources)} FROM/JOIN sources — cannot "
                        f"determine which one provides '{col_name}' without "
                        f"table schema information"
                    )
                else:
                    reason = (
                        f"column '{col_name}' is not an output column of df "
                        f"'{dep_stmt.name}'{branch_tag}"
                    )
                self.unresolved.append(reason)
                container.children.append(TraceNode(label=branch_label, kind="unresolved", detail=reason))
                continue

            # Drill the leftover struct sub-field path into the dependency's
            # own expression, but only while it's actually one of our
            # synthesized STRUCT(...) expressions — otherwise (a plain
            # column, a native struct-typed source column, etc.) there's
            # nothing more we can verify without schema info, so just
            # resolve it as-is.
            expr = dep_entry.expr
            for part in struct_path:
                if not isinstance(expr, exp.Struct):
                    break
                match = next(
                    (
                        m
                        for m in expr.expressions
                        if isinstance(m, exp.PropertyEQ) and normalize_ident(m.name) == normalize_ident(part)
                    ),
                    None,
                )
                if match is None:
                    break
                expr = match.expression

            child = TraceNode(label=f"{dep_stmt.name}.{dep_entry.alias}{branch_tag}", kind="column")
            container.children.append(child)
            branch_view = dep_stmt.branch_view(i)
            self._resolve_expr(expr, branch_view, child, visited, depth + 1)


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
