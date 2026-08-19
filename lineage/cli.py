"""FR8: command-line interface.

    python resolve_lineage.py --file <query_file.txt> --field <CosmosFieldName> [--format json|text]

``--field`` may be repeated to resolve several fields in one run, or
``--all-fields`` resolves every field in the final SELECT (DESIGN.md §8 Q7).
"""

from __future__ import annotations

import argparse
import json
import sys

from lineage.errors import LineageError
from lineage.lineage_resolver import build_chain, resolve_field_lineage
from lineage.models import LineageResult

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"


def format_text(result: LineageResult) -> str:
    lines = [f"{BOLD}Field:{RESET} {result.field}"]
    if result.is_literal_only:
        lines.append("  (literal/NULL-only — no source tables)")
    if result.tables:
        lines.append("  Tables:")
        for t in sorted(result.tables):
            lines.append(f"    - {t}")
    else:
        lines.append("  Tables: <none>")
    if result.has_unresolved_branches:
        lines.append(f"  {BOLD}Unresolved branches:{RESET}")
        for reason in result.unresolved_reasons:
            lines.append(f"    ! {reason}")

    def render_trace(node, indent: int) -> None:
        prefix = "  " * indent
        lines.append(f"{prefix}{DIM}{node.kind}{RESET} {node.label}" + (f"  ({node.detail})" if node.detail else ""))
        for child in node.children:
            render_trace(child, indent + 1)

    lines.append("  Trace:")
    render_trace(result.trace, 2)
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="resolve_lineage.py",
        description="Resolve the source table(s) feeding a Cosmos document field.",
    )
    parser.add_argument("--file", required=True, help="Path to the query file.")
    field_group = parser.add_mutually_exclusive_group(required=True)
    field_group.add_argument(
        "--field",
        action="append",
        dest="fields",
        metavar="COSMOS_FIELD_NAME",
        help="Cosmos field name to resolve. May be repeated.",
    )
    field_group.add_argument(
        "--all-fields",
        action="store_true",
        help="Resolve every output field in the final SELECT.",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format (default: json).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.all_fields:
            ctx = build_chain(args.file)
            field_names = [e.alias for e in ctx.final.projections.values()]
            results = [resolve_field_lineage(args.file, f) for f in field_names]
        else:
            results = [resolve_field_lineage(args.file, f) for f in args.fields]
    except LineageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.format == "json":
        payload = [r.to_dict() for r in results]
        print(json.dumps(payload if len(payload) != 1 else payload[0], indent=2))
    else:
        print("\n\n".join(format_text(r) for r in results))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
