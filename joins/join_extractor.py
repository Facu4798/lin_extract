"""Discover direct real-table JOIN conditions written in query files, to use
as a fallback "bridge" when golden-field-based join inference alone can't
connect every table a golden record needs (see query_builder.py's patch
pass).

This is deliberately narrow: it only picks up a JOIN whose ON condition
directly compares a column on one real ``analytics_{sor}_cdz.{table}``
source to a column on another real table *within the same SELECT* (e.g.
``... FROM analytics_a_cdz.t1 x JOIN analytics_b_cdz.t2 y ON x.id = y.id``).
A join where one side is an intermediate df reference, a compound/derived
expression, or anything not a bare ``alias.column = alias.column`` equality
is skipped rather than guessed at — same "ask, don't guess" policy as the
rest of this codebase. This is a best-effort bridge, not a lineage
resolution: parse failures here are silently skipped (the lineage resolver
already reports real parse problems when it resolves each golden field's
actual source columns)."""

from __future__ import annotations

import os
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from lineage.file_parser import parse_file
from lineage.models import normalize_ident
from lineage.sql_parser import DIALECT, is_real_table_name


@dataclass
class BridgeEdge:
    table_a: str
    col_a: str
    table_b: str
    col_b: str
    query_file: str


def _flatten_union(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.Union):
        return _flatten_union(node.this) + _flatten_union(node.expression)
    return [node]


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _real_table_ref(table_expr: exp.Table) -> str | None:
    parts = [p for p in (table_expr.catalog, table_expr.db, table_expr.this.name if table_expr.this else None) if p]
    ref = ".".join(parts)
    if "." in ref and is_real_table_name(ref):
        return ref
    return None


def _alias_source_map(select: exp.Select) -> dict[str, str]:
    """alias (normalized) -> real table ref, for every direct real-table
    FROM/JOIN source in this SELECT. Sources that aren't a direct real
    table (a df reference, subquery, table function, etc.) are simply
    absent from the map, so any condition touching them is skipped below."""
    mapping: dict[str, str] = {}
    tables: list[exp.Expression] = []
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None and from_clause.this is not None:
        tables.append(from_clause.this)
    for join in select.args.get("joins", []) or []:
        if join.this is not None:
            tables.append(join.this)
    for t in tables:
        if not isinstance(t, exp.Table):
            continue
        ref = _real_table_ref(t)
        if ref is None:
            continue
        alias = t.alias or t.name
        mapping[normalize_ident(alias)] = ref
    return mapping


def _join_conditions(select: exp.Select, alias_map: dict[str, str], fname: str) -> list[BridgeEdge]:
    edges: list[BridgeEdge] = []
    for join in select.args.get("joins", []) or []:
        on = join.args.get("on")
        if on is None:
            continue
        for cond in _flatten_and(on):
            if not isinstance(cond, exp.EQ):
                continue
            left, right = cond.this, cond.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            if not left.table or not right.table:
                continue
            table_a = alias_map.get(normalize_ident(left.table))
            table_b = alias_map.get(normalize_ident(right.table))
            if not table_a or not table_b or table_a == table_b:
                continue
            edges.append(
                BridgeEdge(table_a=table_a, col_a=left.name, table_b=table_b, col_b=right.name, query_file=fname)
            )
    return edges


def extract_bridge_edges(queries_dir: str) -> dict[frozenset, list[BridgeEdge]]:
    """Scan every query file for direct ``real_table JOIN real_table ON
    a.x = b.y`` conditions (regardless of whether either table's column is
    used by any golden field) and return them grouped by table pair
    (``frozenset({table_a, table_b}) -> [BridgeEdge, ...]``)."""
    edges: dict[frozenset, list[BridgeEdge]] = {}
    for fname in sorted(os.listdir(queries_dir)):
        path = os.path.join(queries_dir, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            statements = parse_file(text)
        except Exception:
            continue
        for _df_name, raw_sql, _line_no in statements:
            try:
                parsed = sqlglot.parse_one(raw_sql, read=DIALECT)
            except Exception:
                continue
            for select in _flatten_union(parsed):
                if not isinstance(select, exp.Select):
                    continue
                alias_map = _alias_source_map(select)
                for be in _join_conditions(select, alias_map, fname):
                    key = frozenset((be.table_a, be.table_b))
                    edges.setdefault(key, []).append(be)
    return edges
