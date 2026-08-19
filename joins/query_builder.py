"""Build a single golden-record SQL query from a golden_record_structure.json
and a folder of per-collection query files.

Two things this deliberately does NOT try to be clever about (ask rather
than guess, same policy as the ``lineage`` package):

- **Join keys.** For now (per explicit direction), two source tables are
  only known to be joinable when some golden field coalesces a column from
  each of them — that field's own pair of source columns becomes the join
  condition. When one side of that pairing is itself ambiguous (a source
  field that resolved to 2+ candidate columns, e.g. from a multi-source
  ``SELECT *``), every candidate table gets its own *independent* join
  against the other side, rather than picking one or refusing to join at
  all — equivalent to "whichever candidate actually matches wins", since
  each candidate's condition is separate and unmatched ones just stay NULL.
  A table that never shares a golden field with any other table at all
  has no inferable join key, and is reported as a warning rather than
  silently cross-joined or dropped.
- **Join type.** Every join defaults to LEFT (anchored on the first table
  encountered), since that's the only choice that can never drop a row a
  coalesce might still want data from. If you need a specific join to be
  INNER instead, treat this as a first draft to hand-edit, not a final
  query — the generated SQL is meant to save the mechanical part, not
  replace review.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from joins.models import GoldenFieldSpec, ResolvedSource
from joins.resolver import load_golden_structure, resolve_source_field


@dataclass
class BuildResult:
    sql: str
    warnings: list[str] = field(default_factory=list)


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_query(golden_structure_path: str, queries_dir: str) -> BuildResult:
    golden_structure = load_golden_structure(golden_structure_path)
    warnings: list[str] = []

    # golden_field_name -> source_field_name -> [ResolvedSource, ...]
    resolved: dict[str, dict[str, list[ResolvedSource]]] = {}
    for gname, spec in golden_structure.items():
        resolved[gname] = {}
        for sf in spec.source_fields:
            hits, diagnostics = resolve_source_field(sf, queries_dir)
            if not hits:
                warnings.append(
                    f"golden field '{gname}': source field '{sf}' did not "
                    f"resolve in any query file under '{queries_dir}'"
                )
            # Surfaced unconditionally, even when hits is non-empty — a
            # parse/resolution problem in one file doesn't stop other files
            # from succeeding, but it's still worth knowing about; this is
            # often exactly why a field that's "clearly in the queries"
            # doesn't show up (e.g. a different df in that same file fails
            # to parse, or the field isn't re-exposed by the *last*
            # statement — see DESIGN.md FR4).
            for d in diagnostics:
                warnings.append(f"golden field '{gname}': source field '{sf}': {d}")
            resolved[gname][sf] = hits

    # -- infer join edges: for each golden field, group its resolved hits by
    # source field (one group per source field — a group has 1 entry when
    # unambiguous, 2+ when ambiguous), then connect consecutive groups'
    # DISTINCT TABLES pairwise. For the common unambiguous case each group
    # has exactly one table, so this is exactly one edge per adjacent pair
    # of source fields (same as before). When a group is ambiguous (spans
    # multiple tables), every one of its tables gets its own independent
    # edge against every table in the next group — each is a separate join
    # condition, so an unmatched candidate just contributes NULLs rather
    # than blocking the real match. -----------------------------------------
    # edge key: frozenset({table_a, table_b}) -> list[(ResolvedSource_a, ResolvedSource_b)]
    join_edges: dict[frozenset, list[tuple[ResolvedSource, ResolvedSource]]] = {}
    all_tables: dict[str, ResolvedSource] = {}  # table_ref -> a representative ResolvedSource (for alias/table name)

    for gname, spec in golden_structure.items():
        groups: list[list[ResolvedSource]] = []
        for sf in spec.source_fields:
            hits = resolved[gname][sf]
            if len(hits) > 1:
                warnings.append(
                    f"golden field '{gname}': source field '{sf}' resolved to "
                    f"{len(hits)} columns — ambiguous, so each candidate table "
                    f"gets its own independent join against the other side "
                    f"instead of one trusted join key (still included in the "
                    f"SELECT's COALESCE)"
                )
            for h in hits:
                all_tables.setdefault(h.table, h)
            if hits:
                groups.append(hits)

        # one representative candidate per distinct table, per group (so an
        # ambiguous group contributes one candidate per table it touches)
        table_groups: list[list[ResolvedSource]] = []
        for g in groups:
            per_table: dict[str, ResolvedSource] = {}
            for r in g:
                per_table.setdefault(r.table, r)
            table_groups.append(list(per_table.values()))

        for group_a, group_b in zip(table_groups, table_groups[1:]):
            for a in group_a:
                for b in group_b:
                    if a.table == b.table:
                        continue
                    key = frozenset((a.table, b.table))
                    join_edges.setdefault(key, []).append((a, b))

    # -- build a join order: spanning tree over all_tables using join_edges,
    # anchored on the first table encountered (dict preserves insertion
    # order). Any table with no path back to the anchor via a known edge
    # can't be placed in a single join graph — flagged, not guessed. -------
    table_refs = list(all_tables.keys())
    from_clauses: list[str] = []
    joined: set[str] = set()

    if table_refs:
        anchor = table_refs[0]
        anchor_source = all_tables[anchor]
        from_clauses.append(f"FROM {anchor} AS {anchor_source.alias}")
        joined.add(anchor)

        # simple BFS over join_edges from the anchor
        frontier = [anchor]
        while frontier:
            next_frontier = []
            for t in frontier:
                for key, conditions in join_edges.items():
                    if t not in key:
                        continue
                    other = next(x for x in key if x != t)
                    if other in joined:
                        continue
                    other_source = all_tables[other]
                    on_parts = [f"{a.sql_ref} = {b.sql_ref}" for a, b in conditions]
                    from_clauses.append(
                        f"LEFT JOIN {other} AS {other_source.alias} ON "
                        + " AND ".join(on_parts)
                    )
                    joined.add(other)
                    next_frontier.append(other)
            frontier = next_frontier

        unreachable = [t for t in table_refs if t not in joined]
        for t in unreachable:
            warnings.append(
                f"table '{t}' shares no golden field with any other joined "
                f"table, so no join key could be inferred for it — it's "
                f"left out of the generated FROM/JOIN entirely; add it "
                f"manually with the correct join key"
            )

    # -- build the SELECT list --------------------------------------------
    select_lines: list[str] = []
    for gname, spec in golden_structure.items():
        seen_refs: list[str] = []
        for sf in spec.source_fields:
            for h in resolved[gname][sf]:
                if h.table not in joined:
                    continue  # excluded table — can't reference it, already warned above
                if h.sql_ref not in seen_refs:
                    seen_refs.append(h.sql_ref)

        args = list(seen_refs)
        if spec.has_literal:
            args.append(_sql_literal(spec.literal))

        if not args:
            warnings.append(f"golden field '{gname}': nothing to select (no resolved columns, no literal) — skipped")
            continue
        elif len(args) == 1:
            select_lines.append(f"{args[0]} AS {gname}")
        else:
            select_lines.append(f"COALESCE({', '.join(args)}) AS {gname}")

    select_clause = "SELECT\n    " + ",\n    ".join(select_lines)
    from_clause = "\n".join(from_clauses) if from_clauses else "-- no source tables resolved"
    sql = f"{select_clause}\n{from_clause}"

    return BuildResult(sql=sql, warnings=warnings)
