"""Data structures for the golden-record query builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

NO_LITERAL = object()  # sentinel: this golden field has no literal fallback


@dataclass
class GoldenFieldSpec:
    """One golden-record field's definition from golden_record_structure.json.

    Two input shapes are normalized to this:
    - ``"name": ["life_name_field", "car_name_field"]`` -> fields only.
    - ``"nationalId": {"fields": [...], "literal": null}`` -> fields + literal.
    - ``"role": {"literal": "customer"}`` -> literal only, no fields.
    """

    name: str
    source_fields: list[str] = field(default_factory=list)
    literal: Any = NO_LITERAL

    @property
    def has_literal(self) -> bool:
        return self.literal is not NO_LITERAL


@dataclass
class ResolvedSource:
    """One physically-resolved source column feeding a golden field's source
    field name, tagged with the query file it was found in (per-file
    resolution — see ``joins.resolver.resolve_source_field``)."""

    table: str  # analytics_{sor}_cdz.{table}
    column: str  # dotted column path, e.g. "claim_id" or "item.customerCode"
    query_file: str  # basename of the query file this was resolved from

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def tagged(self) -> str:
        """The '<table>.<column>&<query_file>' form requested for tracing
        which collection/file each resolved column came from."""
        return f"{self.qualified}&{self.query_file}"

    @property
    def alias(self) -> str:
        """A SQL-safe, always-unique table alias for the generated query
        (schema+table are unique together, unlike the bare table name)."""
        return self.table.replace(".", "_")

    @property
    def sql_ref(self) -> str:
        """How to reference this column in the generated SQL, using this
        table's alias. Spark supports dot-notation straight into a native
        struct column (``alias.item.customerCode``), which is exactly what
        ``self.column`` already is when a struct sub-field path is
        involved, so it's used as-is."""
        return f"{self.alias}.{self.column}"
