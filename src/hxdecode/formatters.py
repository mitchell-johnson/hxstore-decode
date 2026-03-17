"""Output formatters for hxdecode CLI.

Provides table, JSON, and CSV output for record listings,
plus a detailed single-record view with optional hex dump.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Sequence


def format_table(
    records: Sequence[dict[str, Any]],
    columns: Sequence[str],
    *,
    max_col_width: int = 50,
) -> str:
    """Render records as a pretty-printed ASCII table.

    Args:
        records: Sequence of dicts, each representing one row.
        columns: Column keys to display (in order). Keys missing from
                 a record are shown as empty strings.
        max_col_width: Truncate cell values wider than this many characters.

    Returns:
        A multi-line string containing the formatted table.
    """
    if not records:
        return "(no records)"

    # Build string matrix, truncating long values.
    headers = [_truncate(col, max_col_width) for col in columns]
    rows: list[list[str]] = []
    for rec in records:
        row = []
        for col in columns:
            val = rec.get(col, "")
            row.append(_truncate(_format_value(val), max_col_width))
        rows.append(row)

    # Compute column widths.
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # Build output lines.
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header_line = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, widths)) + " |"

    lines = [sep, header_line, sep]
    for row in rows:
        line = "| " + " | ".join(cell.ljust(w) for cell, w in zip(row, widths)) + " |"
        lines.append(line)
    lines.append(sep)

    return "\n".join(lines)


def format_json(records: Sequence[dict[str, Any]]) -> str:
    """Render records as pretty-printed JSON.

    Datetime objects are serialised to ISO-8601 strings.
    Bytes objects are converted to hex strings.

    Returns:
        A JSON string with 2-space indentation.
    """
    return json.dumps(
        [_serialise_record(r) for r in records],
        indent=2,
        ensure_ascii=False,
        default=_json_default,
    )


def format_csv(records: Sequence[dict[str, Any]], columns: Sequence[str] | None = None) -> str:
    """Render records as CSV text.

    Args:
        records: Sequence of dicts.
        columns: If provided, only these keys are included (in order).
                 Otherwise all keys from the first record are used.

    Returns:
        CSV-formatted string including the header row.
    """
    if not records:
        return ""

    if columns is None:
        columns = list(records[0].keys())

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow({k: _format_value(rec.get(k, "")) for k in columns})
    return buf.getvalue()


def format_record_detail(record: dict[str, Any], *, show_hex: bool = False) -> str:
    """Render a detailed view of a single record.

    This is used by ``hxdecode mail show`` and similar detail commands.

    Args:
        record: Dict with record fields.
        show_hex: If True and the record contains a ``raw_data`` bytes key,
                  append a hex dump at the end.

    Returns:
        Multi-line human-readable string.
    """
    lines: list[str] = []
    lines.append("=" * 72)

    # Fixed ordering: show important fields first, then the rest.
    priority_keys = [
        "record_id",
        "format_type",
        "sender",
        "sender_name",
        "subject",
        "date",
        "emails",
        "utf16_strings",
        "size_a",
        "size_b",
    ]

    seen: set[str] = set()
    for key in priority_keys:
        if key in record:
            seen.add(key)
            lines.append(_detail_line(key, record[key]))

    # Remaining keys in alphabetical order, excluding raw_data.
    for key in sorted(record.keys()):
        if key in seen or key == "raw_data":
            continue
        lines.append(_detail_line(key, record[key]))

    lines.append("=" * 72)

    if show_hex and "raw_data" in record:
        lines.append("")
        lines.append("Hex dump (first 512 bytes):")
        lines.append(_hex_dump(record["raw_data"], limit=512))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, width: int) -> str:
    """Truncate text to *width* characters, adding ellipsis if needed."""
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def _format_value(val: Any) -> str:
    """Convert a value to a display string."""
    if val is None:
        return ""
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, (list, tuple)):
        return ", ".join(str(v) for v in val)
    return str(val)


def _serialise_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Prepare a record dict for JSON serialisation."""
    out: dict[str, Any] = {}
    for k, v in rec.items():
        if isinstance(v, bytes):
            out[k] = v.hex()
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, (list, tuple)):
            out[k] = [
                item.hex() if isinstance(item, bytes)
                else item.isoformat() if isinstance(item, datetime)
                else item
                for item in v
            ]
        else:
            out[k] = v
    return out


def _json_default(obj: Any) -> Any:
    """Fallback serialiser for json.dumps."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _detail_line(key: str, value: Any) -> str:
    """Format one key-value line for the detail view."""
    label = key.replace("_", " ").title()
    return f"  {label:20s}: {_format_value(value)}"


def _hex_dump(data: bytes, *, limit: int = 512, bytes_per_line: int = 16) -> str:
    """Produce a classic hex + ASCII dump of *data*.

    Args:
        data: Raw bytes to dump.
        limit: Maximum number of bytes to display.
        bytes_per_line: Bytes per output line (default 16).

    Returns:
        Multi-line hex dump string.
    """
    chunk = data[:limit]
    lines: list[str] = []
    for offset in range(0, len(chunk), bytes_per_line):
        row = chunk[offset : offset + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in row)
        ascii_part = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in row)
        lines.append(f"  {offset:08x}  {hex_part:<{bytes_per_line * 3 - 1}s}  |{ascii_part}|")

    if len(data) > limit:
        lines.append(f"  ... ({len(data) - limit} more bytes)")

    return "\n".join(lines)
