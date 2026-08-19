"""FR1: split a query file into an ordered list of (df_name, raw_sql, line_no).

Grammar (DESIGN.md §2.1):
- An assignment starts at true column 0: ``<identifier><ws>=<ws><SQL>``.
- The statement's SQL may continue across multiple physical lines via a
  trailing backslash (Python-style line continuation). The logical statement
  ends at the first line that does NOT end in a (real, non-string-literal)
  backslash.
- There is no statement terminator; a statement ends at the next top-level
  ``df_name =`` line or end-of-file.
- Blank lines, ``#``/``--`` comment lines, and other non-assignment
  boilerplate lines occurring *between* statements are ignored.
"""

from __future__ import annotations

import re

from lineage.errors import FileParseError

_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]?(.*)$")


def _is_comment_line(stripped: str) -> bool:
    return stripped.startswith("#") or stripped.startswith("--")


def _ends_with_continuation(line: str, in_single: bool, in_double: bool) -> tuple[bool, bool, bool, str]:
    """Scan one physical line's characters, tracking whether we're inside a
    quoted string literal (carried in from a prior line via ``in_single`` /
    ``in_double``).

    Returns ``(is_continuation, new_in_single, new_in_double, line_without_trailing_backslash)``.

    A trailing backslash is only treated as a continuation marker when it is
    NOT inside an open string literal at that point (DESIGN.md §5 edge case
    #9 — a backslash that is part of a string's content, e.g. a literal
    value ending in ``\\``, must not be misread as continuation). Doubled
    quotes (``''``) are treated as an escaped quote per SQL convention;
    ``\\'`` / ``\\"`` are also treated as an escaped quote.
    """
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_single:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == "'":
                if i + 1 < n and line[i + 1] == "'":
                    i += 2
                    continue
                in_single = False
                i += 1
                continue
            i += 1
            continue
        if in_double:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                if i + 1 < n and line[i + 1] == '"':
                    i += 2
                    continue
                in_double = False
                i += 1
                continue
            i += 1
            continue
        # not currently inside a string
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        i += 1

    if (in_single or in_double) and line.endswith("\\"):
        # Trailing backslash is inside an (as-yet-unclosed) string literal —
        # it's string content (an escape), not a continuation marker.
        return False, in_single, in_double, line
    if not in_single and not in_double and line.endswith("\\"):
        return True, in_single, in_double, line[:-1].rstrip()
    return False, in_single, in_double, line


def parse_file(text: str) -> list[tuple[str, str, int]]:
    """Split ``text`` into an ordered list of ``(df_name, raw_sql, line_no)``.

    ``line_no`` is the 1-based line on which the assignment starts, used for
    error reporting.
    """
    lines = text.splitlines()
    n = len(lines)
    i = 0
    result: list[tuple[str, str, int]] = []

    while i < n:
        raw_line = lines[i]
        stripped = raw_line.strip()

        if stripped == "" or _is_comment_line(stripped):
            i += 1
            continue

        match = _ASSIGNMENT_RE.match(raw_line)
        if not match or not match.group(2).lstrip().upper().startswith("SELECT"):
            # Not a `df_name = SELECT ...` assignment (DESIGN.md §2.1) — e.g.
            # non-assignment boilerplate, or a config/variable assignment
            # such as `SPARK_CONF = {}`. Tolerate and ignore it.
            i += 1
            continue

        df_name = match.group(1)
        start_line_no = i + 1
        sql_lines = [match.group(2)]

        in_single = False
        in_double = False
        is_cont, in_single, in_double, sql_lines[-1] = _ends_with_continuation(
            sql_lines[-1], in_single, in_double
        )

        while is_cont:
            i += 1
            if i >= n:
                raise FileParseError(
                    f"Unterminated line continuation in df '{df_name}' "
                    f"starting at line {start_line_no}: file ended while a "
                    f"trailing '\\' was still expecting a continuation line."
                )
            next_line = lines[i]
            if next_line.strip() == "" and not in_single and not in_double:
                # A blank line inside a continued statement (the previous
                # line's trailing '\' already signaled more is coming) —
                # skip it without ending the continuation. Only skip when
                # not mid-string, since a blank line could conceivably be
                # meaningful content inside an open string literal.
                continue
            is_cont, in_single, in_double, cleaned = _ends_with_continuation(
                next_line, in_single, in_double
            )
            sql_lines.append(cleaned)

        raw_sql = "\n".join(sql_lines).strip()
        if raw_sql == "":
            raise FileParseError(
                f"df '{df_name}' at line {start_line_no} has an empty SQL body."
            )
        result.append((df_name, raw_sql, start_line_no))
        i += 1

    return result
