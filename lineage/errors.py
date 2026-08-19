"""Exception types for the lineage resolver.

Per DESIGN.md §6 ("Correctness over cleverness") and edge cases §5 #1/#11, the
resolver must fail loudly and specifically rather than silently returning an
empty/guessed result. These exception types give callers (CLI, tests, other
tooling) something precise to catch and report.
"""

from __future__ import annotations


class LineageError(Exception):
    """Base class for all errors raised by the lineage resolver."""


class FileParseError(LineageError):
    """Raised when the custom query-file format (FR1) cannot be split into
    (df_name, raw_sql) statements — e.g. an unterminated backslash
    continuation at end of file."""


class SqlParseError(LineageError):
    """Raised when a df statement's SQL text cannot be parsed (FR2).

    Carries the df_name and (1-based) source line number of the offending
    statement so failures can be traced back to the query file.
    """

    def __init__(self, df_name: str, line_no: int, message: str):
        self.df_name = df_name
        self.line_no = line_no
        super().__init__(
            f"Failed to parse SQL for df '{df_name}' (line {line_no}): {message}"
        )


class FieldNotFoundError(LineageError):
    """Raised when the requested cosmos field name is not an output alias of
    the final SELECT in the query file (FR4 step 2)."""

    def __init__(self, field_name: str, available: list[str]):
        self.field_name = field_name
        self.available = available
        avail = ", ".join(sorted(available)) or "<none>"
        super().__init__(
            f"Field '{field_name}' not found in the final SELECT's output "
            f"aliases. Available fields: {avail}"
        )


class StructFieldNotFoundError(LineageError):
    """Raised when a dotted field path (e.g. ``Address.City``) can't be
    followed — either an intermediate segment's expression isn't a STRUCT,
    or a named sub-field doesn't exist within one."""

    def __init__(self, field_name: str, message: str):
        self.field_name = field_name
        super().__init__(f"Cannot resolve '{field_name}': {message}")
