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
  has no inferable join key from golden fields alone. Before giving up on
  it, ``build_query`` tries one more thing: it scans every query file for
  *any* direct ``real_table JOIN real_table ON a.x = b.y`` condition
  (regardless of whether that field is part of the golden record at all)
  and, if one connects the orphaned table back to the rest of the query
  (possibly by pulling in further tables purely as bridges, not selected
  from), patches it in as a LEFT JOIN — flagged with a warning either way,
  since it wasn't derived from a golden field and deserves a look. Only if
  no such join can be found anywhere is the table left out entirely (still
  warned about). Set ``patch_missing_joins=False`` to disable this and get
  the old strict behavior.
- **Join type.** Every join defaults to LEFT (anchored on the first table
  encountered), since that's the only choice that can never drop a row a
  coalesce might still want data from. If you need a specific join to be
  INNER instead, treat this as a first draft to hand-edit, not a final
  query — the generated SQL is meant to save the mechanical part, not
  replace review.

A multi-source ``SELECT *`` candidate that turns out to be flat wrong (the
column doesn't actually exist in that table — see ``lineage_resolver.py``'s
ambiguous-star handling) can be dropped with ``exclude_sources``: pass a
path to a JSON exclude-list file, or a collection of exclusion strings
directly (see ``joins.exclusions``). Excluded candidates are removed
*before* join-key inference runs, so a bad candidate can't poison a join
condition either, not just the SELECT's COALESCE.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from joins.exclusions import is_excluded, load_exclusions
from joins.join_extractor import extract_bridge_edges
from joins.models import GoldenFieldSpec, ResolvedSource
from joins.resolver import load_golden_structure, resolve_source_field


@dataclass
class BuildResult:
    sql: str
    warnings: list[str] = field(default_factory=list)


def _table_alias(table: str) -> str:
    return table.replace(".", "_")


def _format_join(table: str, alias: str, conditions: list[str]) -> str:
    """Render one LEFT JOIN with each ON/AND condition on its own indented
    line, e.g.::

        LEFT JOIN table2 AS alias2
            ON table1.column = table2.column
            AND table1.column = table2.column
    """
    lines = [f"LEFT JOIN {table} AS {alias}"]
    for i, cond in enumerate(conditions):
        keyword = "ON" if i == 0 else "AND"
        lines.append(f"    {keyword} {cond}")
    return "\n".join(lines)


def _bfs_path(start_set: set[str], target: str, edges: dict[frozenset, list]):
    """Shortest path (list of ``(parent, node, edge_key)``) from any table
    in ``start_set`` to ``target`` over ``edges`` (a table-pair graph, see
    ``full_edges`` in ``build_query``). Returns ``None`` if unreachable.
    Only ``node``s not already in ``start_set`` appear in the returned
    path — i.e. it's exactly the new tables/joins needed to reach
    ``target``."""
    if target in start_set:
        return []
    prev: dict[str, tuple[str, frozenset]] = {}
    visited = set(start_set)
    dq = deque(start_set)
    while dq:
        cur = dq.popleft()
        if cur == target:
            break
        for key in edges:
            if cur not in key:
                continue
            other = next(x for x in key if x != cur)
            if other in visited:
                continue
            visited.add(other)
            prev[other] = (cur, key)
            dq.append(other)
    if target not in visited:
        return None
    path = []
    node = target
    while node not in start_set:
        parent, key = prev[node]
        path.append((parent, node, key))
        node = parent
    path.reverse()
    return path


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def build_query(
    golden_structure_path: str,
    queries_dir: str,
    patch_missing_joins: bool = True,
    exclude_sources: "str | set[str] | list[str] | None" = None,
) -> BuildResult:
    golden_structure = load_golden_structure(golden_structure_path)
    warnings: list[str] = []

    exclusions: set[str] = set()
    if isinstance(exclude_sources, str):
        exclusions = load_exclusions(exclude_sources)
    elif exclude_sources:
        exclusions = set(exclude_sources)

    # golden_field_name -> source_field_name -> [ResolvedSource, ...]
    resolved: dict[str, dict[str, list[ResolvedSource]]] = {}
    for gname, spec in golden_structure.items():
        resolved[gname] = {}
        for sf in spec.source_fields:
            hits, diagnostics = resolve_source_field(sf, queries_dir)

            if exclusions:
                kept, excluded = [], []
                for h in hits:
                    (excluded if is_excluded(h, exclusions) else kept).append(h)
                for h in excluded:
                    warnings.append(
                        f"golden field '{gname}': source field '{sf}': "
                        f"excluded candidate {h.tagged} (user-provided "
                        f"exclude list) — removed before join-key inference "
                        f"and the SELECT's COALESCE"
                    )
                hits = kept

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
                    from_clauses.append(_format_join(other, other_source.alias, on_parts))
                    joined.add(other)
                    next_frontier.append(other)
            frontier = next_frontier

        unreachable = [t for t in table_refs if t not in joined]

        if unreachable and patch_missing_joins:
            # -- patch pass: no golden field alone connects these tables,
            # but maybe some query file's actual SQL already joins them (or
            # joins them via further intermediate tables) directly. Build a
            # combined graph of golden-derived edges + every direct
            # real-table JOIN condition found anywhere in queries_dir, and
            # try to path-find from the tables already joined out to each
            # orphan, adding whatever new tables/joins that path needs. ----
            bridge_edges = extract_bridge_edges(queries_dir)
            full_edges: dict[frozenset, list[tuple[str, str, str, str, str]]] = {}
            for key, conditions in join_edges.items():
                for a, b in conditions:
                    full_edges.setdefault(key, []).append((a.table, a.sql_ref, b.table, b.sql_ref, "golden"))
            for key, bridge_list in bridge_edges.items():
                for be in bridge_list:
                    full_edges.setdefault(key, []).append(
                        (
                            be.table_a,
                            f"{_table_alias(be.table_a)}.{be.col_a}",
                            be.table_b,
                            f"{_table_alias(be.table_b)}.{be.col_b}",
                            f"patched:{be.query_file}",
                        )
                    )

            still_unreachable: list[str] = []
            for t in unreachable:
                path = _bfs_path(joined, t, full_edges)
                if not path:
                    still_unreachable.append(t)
                    continue

                patched_files: set[str] = set()
                for parent, node, key in path:
                    conds = full_edges[key]
                    golden_conds = [c for c in conds if c[4] == "golden"]
                    # golden-field-derived conditions are trusted over
                    # patched ones when both exist for the same table pair.
                    chosen = golden_conds if golden_conds else conds

                    pairs: list[tuple[str, str]] = []
                    for ta, sa, tb, sb, label in chosen:
                        if label != "golden":
                            patched_files.add(label.split(":", 1)[1])
                        pair = (sa, sb) if ta == parent else (sb, sa)
                        if pair not in pairs:
                            pairs.append(pair)

                    if len(pairs) > 1 and not golden_conds:
                        warnings.append(
                            f"multiple different join conditions were found "
                            f"between '{parent}' and '{node}' across query "
                            f"files ({', '.join(sorted(patched_files))}) — "
                            f"using all of them ANDed together; verify this "
                            f"is correct"
                        )

                    parts = [f"{left} = {right}" for left, right in pairs]
                    from_clauses.append(_format_join(node, _table_alias(node), parts))
                    joined.add(node)
                    if node not in all_tables:
                        warnings.append(
                            f"table '{node}' was added to the query only to "
                            f"bridge a join path — it's not itself used by "
                            f"any golden field; verify it's correct and "
                            f"remove it if unnecessary"
                        )

                if patched_files:
                    warnings.append(
                        f"table '{t}' had no join key derivable from any "
                        f"golden field alone — patched in using a join "
                        f"condition found in {', '.join(sorted(patched_files))}; "
                        f"verify this is correct before relying on it"
                    )

            unreachable = still_unreachable

        for t in unreachable:
            warnings.append(
                f"table '{t}' shares no golden field with any other joined "
                f"table, and no direct join to it was found in any query "
                f"file either — it's left out of the generated FROM/JOIN "
                f"entirely; add it manually with the correct join key"
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
            # Each COALESCE arg on its own line, indented one level deeper
            # than the item itself; the closing paren dedents back to the
            # item's own indent (matching the outer "    " every select
            # item gets from the join below).
            inner = ",\n".join(f"        {a}" for a in args)
            select_lines.append(f"COALESCE(\n{inner}\n    ) AS {gname}")

    select_clause = "SELECT\n    " + ",\n    ".join(select_lines)
    from_clause = "\n".join(from_clauses) if from_clauses else "-- no source tables resolved"
    sql = f"{select_clause}\n{from_clause}"

    return BuildResult(sql=sql, warnings=warnings)
