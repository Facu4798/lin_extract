"""Build a golden-record SQL query, one CTE per golden field, from a
golden_record_structure.json and a folder of per-collection query files.

Instead of merging every golden field's join keys into one flat FROM/JOIN
chain (which risks compounding unrelated fields' fan-outs into one
multiplied row set — field A's join fanning out 2x and field B's fanning
out 3x used to combine into up to 6x, even though neither field's own
join is individually that bad), each golden field is built in its own
CTE first, self-contained, and only then folded into a growing "entity"
CTE:

- The first golden field with any resolved source tables becomes the
  entity's anchor: its own join (shared source tables within *that one
  field's* own coalesce list only — same inference as before) produces
  the entity's first column, plus a "connector" column for every table
  it touched (see below).
- Every later golden field is ALSO built as its own self-contained CTE
  first, then connected back onto the growing entity via a FULL OUTER
  JOIN, using whichever physical table(s) the two happen to share. If a
  field's own tables were never touched by any earlier field,
  ``build_query`` tries to *bridge* them together (see below) before
  giving up on it.

Every join that combines real source data — a field's own internal
multi-table join, and every entity-merge join, direct or bridged — is a
FULL OUTER JOIN, not a LEFT JOIN: an entity that only exists in the
*second*-listed source table of a coalesce (or only in a *later* golden
field's tables) would otherwise be silently dropped just because it's
missing from whichever table happened to be listed/processed first.
``COALESCE`` still picks a value from whichever side actually has one;
FULL OUTER JOIN just makes sure the *row* survives regardless of which
side that turns out to be. (The bridge lookup CTE's own internal joins
are the one exception — an unmatched hop there means "no known
relationship for this particular row", which correctly excludes it from
the *mapping*, not from any real entity.)

**Connector columns.** The first golden field to touch a physical table T
decides T's "identity" for every later field: whatever raw column T
contributed to *that* field's own coalesce becomes T's canonical
connector column from then on — every later CTE that also touches T
re-exposes that same column (not necessarily the one it actually
coalesces) purely so it can be joined back to the entity by it. This
means **field order in golden_record_structure.json matters**: put your
most trustworthy/unique field first (e.g. a national ID) so it becomes
the key every other field links against, rather than a field whose match
logic is looser (e.g. a name).

This scopes each field's own join graph to only the tables *it* needs, so
an ambiguous/fan-out-prone join in one field can no longer compound with
an unrelated join in another field the way a single flat FROM/JOIN would.
It does **not** eliminate fan-out *within* one field's own CTE — if that
field's own join isn't 1:1, its CTE can still produce more than one row
per entity, and that duplication still propagates through the join used
to fold it into the entity. Use ``exclude_sources`` (below) to drop a
candidate you know is causing that.

**Bridging.** When a golden field's tables share nothing with any earlier
field, ``build_query`` scans every query file for any direct
``real_table JOIN real_table ON a.x = b.y`` (see ``joins.join_extractor``)
and tries to path-find a connection — even through further tables that
aren't part of any golden field at all. A successful bridge becomes its
own small lookup CTE (``entity_side_key``/``field_side_key``) so it
doesn't disturb either side's own query. Flagged with a warning either
way, since it isn't something any golden field itself vouches for. Set
``patch_missing_joins=False`` to disable this and require every field to
share a table with the entity outright.

A multi-source ``SELECT *`` candidate that turns out to be flat wrong (the
column doesn't actually exist in that table — see ``lineage_resolver.py``'s
ambiguous-star handling) can be dropped with ``exclude_sources``: pass a
path to a JSON exclude-list file, or a collection of exclusion strings
directly (see ``joins.exclusions``). Excluded candidates are removed
*before* any join-key inference runs.

**Deduplication.** The FULL OUTER JOINs above are deliberately row-
preserving, not row-collapsing — a non-1:1 join anywhere in the chain
(an ambiguous ``SELECT *`` candidate, a loose bridge, ...) can still
leave more than one row per real-world entity. Pass ``dedup_partition_by``
(golden field names identifying the entity, e.g. ``["nationalId"]``) and
``dedup_order_by`` (golden field names to break ties by, each optionally
suffixed with ``"ASC"``/``"DESC"``, e.g. ``["updatedAt DESC"]``) to keep
exactly one row per partition — implemented as
``ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...) = 1`` in one final
wrapping CTE. Both must be given together (a partition with no
deterministic order has no principled way to pick which row survives);
every field named in either must be one of the golden fields that
actually made it into the result, or ``build_query`` raises ``ValueError``
rather than silently ignoring a typo.

That final dedup only cleans up *after* the join has already generated
however many duplicate rows a non-1:1 key produced — for a single golden
field whose own coalesce join key just isn't unique per entity in the
source data (e.g. joining on a name, which can repeat, rather than a
real id), the fan-out happens *inside* that field's own CTE, before any
final dedup ever runs. Two ways to fix that at the source, both only
covering *non-anchor* participants in a field's own spanning-tree join
(the anchor — the root of that tree — has no single incoming join
condition to dedup against; if the anchor itself isn't unique,
``dedup_partition_by``/``dedup_order_by`` on the final result is still
the tool for that):

- ``dedup_join_keys=True`` applies this automatically to *every*
  non-anchor table in *every* field's own spanning tree — no need to
  name tables individually. Without an explicit tie-break order, the
  surviving row per join-key value is arbitrary (still deterministic
  within one query execution, just not something you chose).
- ``dedup_join_sources`` (e.g. ``{"analytics_x_cdz.customer":
  ["updatedAt DESC"]}``) gives a *specific* table an explicit tie-break
  order — and works whether or not ``dedup_join_keys`` is set; a table
  named here always uses its given order, even under the blanket
  ``dedup_join_keys=True``.

Either way, the table is collapsed to one row per join-key value
*before* it's joined, so the join itself can never fan out from that
table's side — this is currently scoped to joins *within* a single
golden field's own coalesce only, not to entity-merge or bridge joins
across different fields.

**Output format.** ``.sql`` is a sequence of standalone
``CREATE OR REPLACE TEMP VIEW <name> AS (...);`` statements, one per
stage, followed by the final ``SELECT ...;`` — semicolon-terminated so a
caller can ``sql.split(";")`` and run/cache each statement individually
(e.g. ``spark.sql(stmt); spark.catalog.cacheTable(name)`` after each
``CREATE VIEW``). That's what actually lets Spark materialize each stage
instead of re-deriving the whole upstream chain every time — a single
``WITH``-clause query (each CTE referenced exactly once) would just get
inlined by Spark into one query anyway, no different from nesting the
same thing as subqueries, with nothing to cache.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from itertools import count

from joins.exclusions import is_excluded, load_exclusions
from joins.join_extractor import extract_bridge_edges
from joins.models import ResolvedSource
from joins.resolver import load_golden_structure, resolve_source_field


@dataclass
class BuildResult:
    sql: str
    warnings: list[str] = field(default_factory=list)


def _table_alias(table: str) -> str:
    return table.replace(".", "_")


def _connector_alias(table: str) -> str:
    return f"{_table_alias(table)}__key"


def _safe_ident(name: str) -> str:
    """A CTE-name-safe version of an arbitrary golden field name."""
    safe = re.sub(r"[^0-9A-Za-z_]", "_", name)
    return safe or "field"


def _format_join(table: str, alias: str, conditions: list[str], keyword: str) -> str:
    """Render one JOIN with each ON/AND condition on its own indented
    line, e.g.::

        FULL OUTER JOIN table2 AS alias2
            ON table1.column = table2.column
            AND table1.column = table2.column
    """
    lines = [f"{keyword} {table} AS {alias}"]
    for i, cond in enumerate(conditions):
        kw = "ON" if i == 0 else "AND"
        lines.append(f"    {kw} {cond}")
    return "\n".join(lines)


def _bfs_path(start_set: set[str], target: str, edges: dict[frozenset, list]):
    """Shortest path (list of ``(parent, node, edge_key)``) from any table
    in ``start_set`` to ``target`` over ``edges`` (a table-pair graph).
    Returns ``None`` if unreachable. Only ``node``s not already in
    ``start_set`` appear in the returned path — i.e. it's exactly the new
    tables/joins needed to reach ``target``."""
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


def _emit_cte(name: str, sql: str) -> str:
    """One stage's text, as a standalone ``CREATE OR REPLACE TEMP VIEW
    ...;`` statement — see the module docstring's "Output format" note."""
    return f"CREATE OR REPLACE TEMP VIEW {name} AS (\n{sql}\n);"


def _select_item(args: list[str], alias: str) -> str:
    if len(args) == 1:
        return f"{args[0]} AS {alias}"
    # Each COALESCE arg on its own line, indented one level deeper than the
    # item itself; the closing paren dedents back to the item's own indent.
    inner = ",\n".join(f"        {a}" for a in args)
    return f"COALESCE(\n{inner}\n    ) AS {alias}"


def _parse_order_by_entry(entry: str, context: str) -> str:
    """Parse one "field" or "field ASC"/"field DESC" entry, returning
    "field DIRECTION". Raises ValueError (with ``context`` in the
    message) on an unrecognized direction."""
    field, _, direction = entry.strip().partition(" ")
    direction = direction.strip().upper() or "ASC"
    if direction not in ("ASC", "DESC"):
        raise ValueError(f"{context}: invalid direction '{direction}' for '{field}' — use 'ASC' or 'DESC'")
    return field, direction


def _dedup_wrap_source(table: str, partition_cols: list[str], order_by: list[str]) -> str:
    """A ``(...)`` subquery that reduces ``table`` to exactly one row per
    ``partition_cols`` combination, tie-broken by ``order_by`` if given —
    used to pre-dedup a table on its own join key *before* it's joined,
    so a non-unique key can't fan out the join itself. See
    ``dedup_join_keys``/``dedup_join_sources`` on ``build_query``.

    ``order_by`` may be empty — Spark allows ``ROW_NUMBER()`` with no
    ``ORDER BY``, at the cost of the surviving row being arbitrary (still
    deterministic within one query execution, not stable across runs).
    Only used when a table is deduped automatically via
    ``dedup_join_keys`` and wasn't also given an explicit tie-break via
    ``dedup_join_sources``.
    """
    lines = [
        "(",
        "        SELECT * FROM (",
        "            SELECT *, ROW_NUMBER() OVER (",
        f"                PARTITION BY {', '.join(partition_cols)}",
    ]
    if order_by:
        order_parts = [
            f"{field} {direction}"
            for field, direction in (_parse_order_by_entry(e, f"dedup_join_sources[{table!r}]") for e in order_by)
        ]
        lines.append(f"                ORDER BY {', '.join(order_parts)}")
    lines += [
        "            ) AS _dedup_join_rn",
        f"            FROM {table}",
        "        )",
        "        WHERE _dedup_join_rn = 1",
        "    )",
    ]
    return "\n".join(lines)


def build_query(
    golden_structure_path: str,
    queries_dir: str,
    patch_missing_joins: bool = True,
    exclude_sources: "str | set[str] | list[str] | None" = None,
    dedup_partition_by: "list[str] | None" = None,
    dedup_order_by: "list[str] | None" = None,
    dedup_join_sources: "dict[str, list[str]] | None" = None,
    dedup_join_keys: bool = False,
) -> BuildResult:
    golden_structure = load_golden_structure(golden_structure_path)
    warnings: list[str] = []
    dedup_join_sources = dedup_join_sources or {}

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

    # -- per golden field: its own table set + its own self-contained join
    # edges (shared source tables within *that field's* coalesce list only
    # — never mixed with another field's). Every field's own graph is
    # guaranteed fully connected (each source field only ever creates
    # edges to its immediate neighbor in the source_fields list, forming a
    # path graph), so there's no "unreachable table" case *within* one
    # field. --------------------------------------------------------------
    field_tables: dict[str, dict[str, ResolvedSource]] = {}  # gname -> table -> representative hit
    field_join_edges: dict[str, dict[frozenset, list[tuple[ResolvedSource, ResolvedSource]]]] = {}

    for gname, spec in golden_structure.items():
        groups: list[list[ResolvedSource]] = []
        local_tables: dict[str, ResolvedSource] = {}
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
                local_tables.setdefault(h.table, h)
            if hits:
                groups.append(hits)

        table_groups: list[list[ResolvedSource]] = []
        for g in groups:
            per_table: dict[str, ResolvedSource] = {}
            for r in g:
                per_table.setdefault(r.table, r)
            table_groups.append(list(per_table.values()))

        local_edges: dict[frozenset, list[tuple[ResolvedSource, ResolvedSource]]] = {}
        for group_a, group_b in zip(table_groups, table_groups[1:]):
            for a in group_a:
                for b in group_b:
                    if a.table == b.table:
                        continue
                    key = frozenset((a.table, b.table))
                    local_edges.setdefault(key, []).append((a, b))

        field_tables[gname] = local_tables
        field_join_edges[gname] = local_edges

    if dedup_join_sources:
        all_known_tables = {t for lt in field_tables.values() for t in lt}
        unknown_tables = [t for t in dedup_join_sources if t not in all_known_tables]
        if unknown_tables:
            raise ValueError(
                f"dedup_join_sources references table(s) never used by any "
                f"golden field: {', '.join(sorted(unknown_tables))} "
                f"(known tables: {', '.join(sorted(all_known_tables))})"
            )
        for table, order_by in dedup_join_sources.items():
            if not order_by:
                raise ValueError(f"dedup_join_sources[{table!r}] needs a non-empty order-by list to break ties deterministically")
            for entry in order_by:
                _parse_order_by_entry(entry, f"dedup_join_sources[{table!r}]")  # validates early, result unused here

    # -- build one CTE per golden field, folding each into a growing
    # "entity" CTE chain via whichever physical table(s) it shares with
    # what's been built so far (or a bridged connection). ------------------
    ctes: list[str] = []
    connector_col: dict[str, str] = {}  # table -> the raw column that identifies it from now on
    entity_name: str | None = None
    entity_tables: set[str] = set()
    entity_gnames: list[str] = []
    pending_literal_selects: dict[str, str] = {}
    bridge_edges = None  # lazily computed (extract_bridge_edges), at most once
    full_edges: dict[frozenset, list[tuple[str, str, str, str, str]]] | None = None  # lazily built, at most once
    cte_seq = count(1)

    for gname, spec in golden_structure.items():
        local_tables = field_tables[gname]

        seen_refs: list[str] = []
        for sf in spec.source_fields:
            for h in resolved[gname][sf]:
                if h.sql_ref not in seen_refs:
                    seen_refs.append(h.sql_ref)

        args = list(seen_refs)
        if spec.has_literal:
            args.append(_sql_literal(spec.literal))

        if not args:
            warnings.append(f"golden field '{gname}': nothing to select (no resolved columns, no literal) — skipped")
            continue

        this_tables = set(local_tables.keys())

        if not this_tables:
            # Literal-only (either no source fields at all, or none of them
            # resolved) — not tied to any row, so it needs no join; folded
            # into the final SELECT once the entity is assembled below.
            # args is always exactly [literal] here: this_tables empty means
            # no hits contributed to seen_refs, and `if not args` above
            # already ruled out zero args, so has_literal must be the sole
            # contributor.
            pending_literal_selects[gname] = args[0]
            continue

        # -- this field's own self-contained FROM/JOIN --------------------
        local_edges = field_join_edges[gname]
        local_table_refs = list(local_tables.keys())
        anchor = local_table_refs[0]
        from_lines = [f"FROM {anchor} AS {_table_alias(anchor)}"]
        local_joined = {anchor}
        frontier = [anchor]
        while frontier:
            next_frontier = []
            for t in frontier:
                for key, conditions in local_edges.items():
                    if t not in key:
                        continue
                    other = next(x for x in key if x != t)
                    if other in local_joined:
                        continue
                    on_parts = [f"{a.sql_ref} = {b.sql_ref}" for a, b in conditions]

                    source = other
                    explicit_order_by = dedup_join_sources.get(other)
                    if dedup_join_keys or explicit_order_by:
                        # This table might not be unique on the column it's
                        # being joined on here — collapse it to one row per
                        # that column *before* the join runs, so the join
                        # itself can't fan out from a non-unique key. With
                        # dedup_join_keys=True this applies to every
                        # non-anchor table in every field's own spanning
                        # tree automatically; dedup_join_sources still gives
                        # a specific table an explicit tie-break order
                        # (falling back to none — an arbitrary but
                        # deterministic-per-run survivor — otherwise).
                        partition_cols: list[str] = []
                        for a, b in conditions:
                            join_col = a.column if a.table == other else b.column
                            if join_col not in partition_cols:
                                partition_cols.append(join_col)
                        source = _dedup_wrap_source(other, partition_cols, explicit_order_by or [])

                    from_lines.append(_format_join(source, _table_alias(other), on_parts, keyword="FULL OUTER JOIN"))
                    local_joined.add(other)
                    next_frontier.append(other)
            frontier = next_frontier

        select_items = [_select_item(args, gname)]
        for t in sorted(this_tables):
            if t not in connector_col:
                connector_col[t] = local_tables[t].column
            select_items.append(f"{_table_alias(t)}.{connector_col[t]} AS {_connector_alias(t)}")

        cte_sql = "SELECT\n    " + ",\n    ".join(select_items) + "\n" + "\n".join(from_lines)
        cte_name = f"cte_{next(cte_seq)}_{_safe_ident(gname)}"
        # Appended right away (even before we know whether this field can be
        # connected to the entity) so CTE numbering matches text order, and
        # so a field that ends up disconnected still leaves its own
        # self-contained join sitting in the output for manual wiring.
        ctes.append(_emit_cte(cte_name, cte_sql))

        if entity_name is None:
            entity_name = cte_name
            entity_tables = set(this_tables)
            entity_gnames = [gname]
            continue

        shared = this_tables & entity_tables
        bridge_cte_name = None
        bridge_start_table = None
        bridge_target_table = None

        if not shared and patch_missing_joins:
            if bridge_edges is None:
                bridge_edges = extract_bridge_edges(queries_dir)
            if full_edges is None:
                full_edges = {}
                for edges2 in field_join_edges.values():
                    for key, conditions in edges2.items():
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

            path = None
            for candidate in sorted(this_tables):
                p = _bfs_path(entity_tables, candidate, full_edges)
                if p:
                    path = p
                    bridge_target_table = candidate
                    break

            if path is not None:
                bridge_start_table = path[0][0]
                bridge_from = [f"FROM {bridge_start_table} AS {_table_alias(bridge_start_table)}"]
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
                    bridge_from.append(_format_join(node, _table_alias(node), parts, keyword="JOIN"))

                if bridge_target_table not in connector_col:
                    connector_col[bridge_target_table] = local_tables[bridge_target_table].column

                bridge_select = [
                    f"{_table_alias(bridge_start_table)}.{connector_col[bridge_start_table]} AS entity_side_key",
                    f"{_table_alias(bridge_target_table)}.{connector_col[bridge_target_table]} AS field_side_key",
                ]
                bridge_sql = "SELECT\n    " + ",\n    ".join(bridge_select) + "\n" + "\n".join(bridge_from)
                bridge_cte_name = f"cte_{next(cte_seq)}_bridge_{_safe_ident(gname)}"
                ctes.append(_emit_cte(bridge_cte_name, bridge_sql))

                warnings.append(
                    f"golden field '{gname}' shares no table with the entity "
                    f"built so far — bridged via a join condition found in "
                    f"{', '.join(sorted(patched_files)) or 'the resolved golden fields'} "
                    f"connecting '{bridge_start_table}' to '{bridge_target_table}'; "
                    f"verify this is correct before relying on it"
                )

        if not shared and bridge_cte_name is None:
            warnings.append(
                f"golden field '{gname}' shares no table with the entity "
                f"built so far, and no join path could be found to connect "
                f"it either — its column is left out of the final result; "
                f"add the join manually"
            )
            continue

        new_tables = this_tables - entity_tables
        merged_select = [f"{entity_name}.{g} AS {g}" for g in entity_gnames]
        merged_select += [f"{entity_name}.{_connector_alias(t)} AS {_connector_alias(t)}" for t in sorted(entity_tables)]
        merged_select.append(f"{cte_name}.{gname} AS {gname}")
        merged_select += [f"{cte_name}.{_connector_alias(t)} AS {_connector_alias(t)}" for t in sorted(new_tables)]

        if shared:
            on_parts = [f"{entity_name}.{_connector_alias(t)} = {cte_name}.{_connector_alias(t)}" for t in sorted(shared)]
            join_lines = [f"FULL OUTER JOIN {cte_name}"]
            for i, cond in enumerate(on_parts):
                join_lines.append(f"    {'ON' if i == 0 else 'OR'} {cond}")
            merged_from = [f"FROM {entity_name}", "\n".join(join_lines)]
        else:
            merged_from = [
                f"FROM {entity_name}",
                f"FULL OUTER JOIN {bridge_cte_name}\n    ON {entity_name}.{_connector_alias(bridge_start_table)} = {bridge_cte_name}.entity_side_key",
                f"FULL OUTER JOIN {cte_name}\n    ON {bridge_cte_name}.field_side_key = {cte_name}.{_connector_alias(bridge_target_table)}",
            ]

        merged_sql = "SELECT\n    " + ",\n    ".join(merged_select) + "\n" + "\n".join(merged_from)
        new_entity_name = f"entity_{next(cte_seq)}"
        ctes.append(_emit_cte(new_entity_name, merged_sql))

        entity_name = new_entity_name
        entity_tables = entity_tables | this_tables
        entity_gnames = entity_gnames + [gname]

    # -- final field expressions, in golden_record_structure.json's own
    # field order (bare, not yet aliased — dedup below needs the raw
    # expressions to reference in PARTITION BY/ORDER BY) -------------------
    entity_gname_set = set(entity_gnames)
    field_exprs: dict[str, str] = {}
    for gname in golden_structure:
        if gname in pending_literal_selects:
            field_exprs[gname] = pending_literal_selects[gname]
        elif gname in entity_gname_set:
            field_exprs[gname] = f"{entity_name}.{gname}"
        # else: already warned about (nothing resolved, or couldn't connect)

    final_from = entity_name
    where_clause = ""

    if dedup_partition_by or dedup_order_by:
        if entity_name is None:
            raise ValueError(
                "dedup_partition_by/dedup_order_by need at least one "
                "resolved, table-backed golden field to dedup rows of — "
                "nothing here is tied to any row"
            )
        if not dedup_partition_by or not dedup_order_by:
            raise ValueError(
                "dedup requires both dedup_partition_by and dedup_order_by "
                "— a partition alone has no deterministic way to pick "
                "which row survives"
            )

        available = set(field_exprs.keys())
        unknown_partition = [f for f in dedup_partition_by if f not in available]
        if unknown_partition:
            raise ValueError(
                f"dedup_partition_by references field(s) not present in "
                f"the final result: {', '.join(unknown_partition)} "
                f"(available: {', '.join(sorted(available))})"
            )

        order_parts: list[str] = []
        for entry in dedup_order_by:
            field, _, direction = entry.strip().partition(" ")
            direction = direction.strip().upper() or "ASC"
            if field not in available:
                raise ValueError(
                    f"dedup_order_by references field '{field}' not "
                    f"present in the final result (available: "
                    f"{', '.join(sorted(available))})"
                )
            if direction not in ("ASC", "DESC"):
                raise ValueError(
                    f"dedup_order_by: invalid direction '{direction}' for "
                    f"field '{field}' — use 'ASC' or 'DESC'"
                )
            order_parts.append(f"{field_exprs[field]} {direction}")

        partition_parts = [field_exprs[f] for f in dedup_partition_by]

        ranked_select = [f"{expr} AS {gname}" for gname, expr in field_exprs.items()]
        ranked_select.append(
            "ROW_NUMBER() OVER (\n"
            "        PARTITION BY " + ", ".join(partition_parts) + "\n"
            "        ORDER BY " + ", ".join(order_parts) + "\n"
            "    ) AS _dedup_row_number"
        )
        ranked_sql = "SELECT\n    " + ",\n    ".join(ranked_select) + f"\nFROM {entity_name}"
        ranked_name = f"cte_{next(cte_seq)}_ranked"
        ctes.append(_emit_cte(ranked_name, ranked_sql))

        final_from = ranked_name
        where_clause = "\nWHERE _dedup_row_number = 1"
        select_lines = list(field_exprs.keys())  # already aliased inside the ranked CTE
    else:
        select_lines = [f"{expr} AS {gname}" for gname, expr in field_exprs.items()]

    select_clause = "SELECT\n    " + (",\n    ".join(select_lines) if select_lines else "-- nothing resolved")

    if final_from is None:
        sql = f"{select_clause};" if select_lines else f"{select_clause}\n-- no source tables resolved"
        return BuildResult(sql=sql, warnings=warnings)

    final_stmt = f"{select_clause}\nFROM {final_from}{where_clause}"
    sql = "\n\n".join(ctes + [f"{final_stmt};"])

    return BuildResult(sql=sql, warnings=warnings)
