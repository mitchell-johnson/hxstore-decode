"""Click-based CLI for hxdecode.

Entry point: ``hxdecode`` (registered via pyproject.toml console_scripts).

Commands
--------
- ``hxdecode info``          -- database statistics
- ``hxdecode mail``          -- list email records
- ``hxdecode mail show ID``  -- show one email in detail
- ``hxdecode records``       -- list raw records
- ``hxdecode search``        -- full-text search across records and blobs
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Any

import click

from hxdecode.constants import PAGE_SIZE
from hxdecode.decompress import decompress_record
from hxdecode.extract import (
    extract_ascii_strings,
    extract_email_fields,
    extract_emails,
    extract_record_id,
    extract_display_time,
    extract_timestamps,
    extract_utf16le_strings,
    _TEXT_EMAIL_RE as _EMAIL_RE_TEXT,
)
from hxdecode.formatters import (
    format_csv,
    format_json,
    format_record_detail,
    format_table,
)
from hxdecode.parser import HxStoreFile, PageType
from hxdecode.profile import find_hxstore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_path(path: str | None) -> Path:
    if path is not None:
        p = Path(path).expanduser().resolve()
        if not p.is_file():
            click.echo(f"Error: file not found: {p}", err=True)
            sys.exit(1)
        return p
    try:
        return find_hxstore()
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _open_store(path: str | None) -> HxStoreFile:
    resolved = _resolve_path(path)
    try:
        return HxStoreFile(resolved)
    except Exception as exc:
        click.echo(f"Error opening {resolved}: {exc}", err=True)
        sys.exit(1)


def _output(records: list[dict[str, Any]], columns: list[str], fmt: str) -> None:
    if fmt == "json":
        click.echo(format_json(records))
    elif fmt == "csv":
        click.echo(format_csv(records, columns))
    else:
        click.echo(format_table(records, columns))


def _decompress_and_extract(rec) -> dict[str, Any]:
    """Decompress a RawRecord and extract all fields."""
    rec_id, decompressed = decompress_record(
        rec.raw_data, rec.slot_header.size_b
    )

    # Raw format type from compressed stream (correlates with record type)
    raw_fmt = 0
    if len(rec.raw_data) >= 12:
        raw_fmt = struct.unpack_from("<I", rec.raw_data, 8)[0]

    # Extract from decompressed data
    ascii_emails = extract_emails(decompressed)
    utf16 = extract_utf16le_strings(decompressed)
    timestamps = extract_timestamps(decompressed)

    # Also extract emails from UTF-16LE strings (module-level regex)
    all_emails: list[str] = list(ascii_emails)
    seen = set(e.lower() for e in all_emails)
    for s in utf16:
        for m in _EMAIL_RE_TEXT.finditer(s):
            addr = m.group().lower()
            if addr not in seen:
                seen.add(addr)
                all_emails.append(m.group())

    is_email = "IPM.Note" in utf16

    # Use the centralised field extractor
    sender_email, sender_name, subject = extract_email_fields(utf16, raw_fmt)

    if not sender_email and all_emails:
        sender_email = all_emails[0]

    return {
        "record_id": rec_id,
        "format_type": raw_fmt,
        "is_email": is_email,
        "size_a": rec.slot_header.size_a,
        "size_b": rec.slot_header.size_b,
        "decompressed_size": len(decompressed),
        "sender_email": sender_email,
        "sender_name": sender_name,
        "subject": subject,
        "emails": all_emails,
        "utf16_strings": utf16,
        "timestamps": timestamps,
        "timestamp": extract_display_time(decompressed, utf16),
        "decompressed": decompressed,
        "raw_data": rec.raw_data,
    }


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="hxdecode")
def cli() -> None:
    """Decode and inspect Microsoft Outlook for Mac HxStore.hxd files."""


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def info(path: str | None) -> None:
    """Show database statistics."""
    store = _open_store(path)

    click.echo(f"File:              {store.path}")
    click.echo(f"File size:         {store.file_size:,} bytes ({store.file_size / 1_048_576:.1f} MB)")
    click.echo(f"Total pages:       {store.num_pages:,}")
    click.echo(f"Page size:         {PAGE_SIZE} bytes")
    click.echo()
    click.echo(f"Primary store ID:  {store.primary_store_id.hex()}")
    click.echo(f"Secondary store ID:{store.secondary_store_id.hex()}")
    click.echo()

    page_stats = store.stats()
    click.echo(f"Data pages:        {page_stats['data']:,}")
    click.echo(f"Index pages:       {page_stats['index']:,}")
    click.echo(f"Blob pages:        {page_stats['blob']:,}")
    click.echo()

    record_count = 0
    type_counts: dict[int, int] = {}
    for rec in store.iter_data_records():
        record_count += 1
        if len(rec.raw_data) >= 12:
            ft = struct.unpack_from("<I", rec.raw_data, 8)[0]
            type_counts[ft] = type_counts.get(ft, 0) + 1

    click.echo(f"Data records:      {record_count:,}")
    if type_counts:
        click.echo("Record format types:")
        for ft, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            click.echo(f"  0x{ft:04X} ({ft}): {count:,}")


# ---------------------------------------------------------------------------
# mail
# ---------------------------------------------------------------------------

@cli.group(invoke_without_command=True)
@click.option("--limit", "-n", default=50, show_default=True, help="Max records to display.")
@click.option(
    "--format", "fmt", type=click.Choice(["table", "json", "csv"]),
    default="table", show_default=True, help="Output format.",
)
@click.option(
    "--sort", "sort_order", type=click.Choice(["newest", "oldest", "none"]),
    default="newest", show_default=True, help="Sort by timestamp.",
)
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
@click.pass_context
def mail(ctx: click.Context, limit: int, fmt: str, sort_order: str, path: str | None) -> None:
    """List email records with sender, subject, and date."""
    ctx.ensure_object(dict)
    ctx.obj["path"] = path

    if ctx.invoked_subcommand is not None:
        return

    store = _open_store(path)
    columns = ["record_id", "sender_email", "sender_name", "subject", "timestamp"]
    rows: list[dict[str, Any]] = []

    # When sorting, we need to scan all email records first, then limit.
    scan_limit = limit if sort_order == "none" else 0  # 0 = unlimited scan

    for rec in store.iter_data_records():
        if scan_limit and len(rows) >= scan_limit:
            break

        info = _decompress_and_extract(rec)

        # Filter to email records (contain IPM.Note marker)
        if not info["is_email"]:
            continue

        rows.append(info)

    if not rows:
        click.echo("No email records found.")
        return

    # Sort by timestamp if requested
    if sort_order in ("newest", "oldest"):
        from datetime import datetime as _dt
        _epoch = _dt.min.replace(tzinfo=None)

        def _sort_key(row: dict[str, Any]) -> _dt:
            ts = row.get("timestamp")
            if ts is None:
                return _epoch
            # Strip tzinfo for comparison consistency
            return ts.replace(tzinfo=None) if hasattr(ts, "replace") else _epoch

        rows.sort(key=_sort_key, reverse=(sort_order == "newest"))

    # Apply limit after sorting
    if sort_order != "none" and len(rows) > limit:
        rows = rows[:limit]

    _output(rows, columns, fmt)
    click.echo(f"\n{len(rows)} email(s) shown.")


@mail.command("show")
@click.argument("record_id", type=int)
@click.option("--hex", "show_hex", is_flag=True, help="Show hex dump of decompressed data.")
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def mail_show(record_id: int, show_hex: bool, path: str | None) -> None:
    """Show full detail of a single record by ID."""
    store = _open_store(path)

    for rec in store.iter_data_records():
        rid = extract_record_id(rec.raw_data)
        if rid == record_id:
            info = _decompress_and_extract(rec)
            # Remove large binary fields from detail view unless hex requested
            detail = {k: v for k, v in info.items() if k not in ("decompressed", "raw_data")}
            detail["emails"] = ", ".join(info["emails"])
            detail["utf16_strings"] = info["utf16_strings"][:20]
            detail["timestamps"] = [str(t) for t in info["timestamps"][:10]]

            if show_hex:
                detail["raw_data"] = info["decompressed"]

            click.echo(format_record_detail(detail, show_hex=show_hex))
            return

    click.echo(f"Record {record_id} not found.", err=True)
    sys.exit(1)


@mail.command("body")
@click.argument("record_id", type=int)
@click.option("--html", "show_html", is_flag=True, help="Show raw HTML instead of plain text.")
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def mail_body(record_id: int, show_html: bool, path: str | None) -> None:
    """Extract and display the email body for a record."""
    from hxdecode.body import BodyIndex

    store = _open_store(path)
    index = BodyIndex(store)
    body = index.get_body(record_id)

    if body is None:
        click.echo(f"No body found for record {record_id}.", err=True)
        sys.exit(1)

    click.echo(f"Record: {record_id}")
    click.echo(f"Source: {body.source}")
    if body.source == "sibling":
        click.echo(f"Body from record: {body.record_id}")
    click.echo("---")

    if show_html and body.html:
        click.echo(body.html)
    else:
        click.echo(body.text)


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--limit", "-n", default=20, show_default=True, help="Max records to display.")
@click.option(
    "--format", "fmt", type=click.Choice(["table", "json", "csv"]),
    default="table", show_default=True, help="Output format.",
)
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def records(limit: int, fmt: str, path: str | None) -> None:
    """List raw data records with IDs, sizes, and format types."""
    store = _open_store(path)

    columns = ["record_id", "format_type", "size_a", "size_b", "decompressed_size", "emails", "subject"]
    rows: list[dict[str, Any]] = []

    for rec in store.iter_data_records():
        if len(rows) >= limit:
            break

        info = _decompress_and_extract(rec)
        rows.append({
            "record_id": info["record_id"],
            "format_type": f"0x{info['format_type']:04X}",
            "size_a": info["size_a"],
            "size_b": info["size_b"],
            "decompressed_size": info["decompressed_size"],
            "emails": ", ".join(info["emails"][:2]),
            "subject": info["subject"][:60],
        })

    if not rows:
        click.echo("No records found.")
        return

    _output(rows, columns, fmt)
    click.echo(f"\n{len(rows)} record(s) shown.")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("query")
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def search(query: str, path: str | None) -> None:
    """Search for a string across all decompressed records (UTF-8 + UTF-16LE)."""
    store = _open_store(path)

    query_utf8 = query.encode("utf-8")
    query_utf16 = query.encode("utf-16-le")

    click.echo(f'Searching for "{query}" ...')
    click.echo()

    found = 0
    for rec in store.iter_data_records():
        _, decompressed = decompress_record(rec.raw_data, rec.slot_header.size_b)

        if query_utf8 in decompressed or query_utf16 in decompressed:
            info = _decompress_and_extract(rec)

            click.echo(f"Record {info['record_id']} (format=0x{info['format_type']:04X}):")
            if info["sender_email"]:
                click.echo(f"  From: {info['sender_name']} <{info['sender_email']}>")
            if info["subject"]:
                click.echo(f"  Subject: {info['subject']}")
            if info["timestamp"]:
                click.echo(f"  Date: {info['timestamp']}")
            click.echo(f"  size: {info['size_a']} -> {info['decompressed_size']} bytes")
            click.echo()
            found += 1

    # Also search blob pages
    blob_found = 0
    for page_num, ptype in store.iter_pages(PageType.BLOB):
        page_data = store.page_data(page_num)
        if query_utf8 in page_data or query_utf16 in page_data:
            blob_found += 1

    click.echo(f"{found} record(s) matched in data records.")
    if blob_found:
        click.echo(f"{blob_found} blob page(s) also contain the search term.")


# ---------------------------------------------------------------------------
# blob-search
# ---------------------------------------------------------------------------

@cli.command("blob-search")
@click.argument("query")
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def blob_search(query: str, path: str | None) -> None:
    """Search blob pages for text (HTML email bodies, attachments)."""
    store = _open_store(path)

    query_utf8 = query.encode("utf-8")
    query_utf16 = query.encode("utf-16-le")

    click.echo(f'Searching blob pages for "{query}" ...')
    click.echo()

    found = 0
    for page_num, _ in store.iter_pages(PageType.BLOB):
        page_data = store.page_data(page_num)
        if query_utf8 in page_data or query_utf16 in page_data:
            # Try to extract surrounding context
            for encoding, label in [(query_utf8, "UTF-8"), (query_utf16, "UTF-16LE")]:
                idx = page_data.find(encoding)
                if idx >= 0:
                    # Show context around the match
                    start = max(0, idx - 40)
                    end = min(len(page_data), idx + len(encoding) + 80)
                    context = page_data[start:end]
                    # Try to decode as text
                    try:
                        if label == "UTF-16LE":
                            text = context.decode("utf-16-le", errors="replace")
                        else:
                            text = context.decode("utf-8", errors="replace")
                    except Exception:
                        text = repr(context[:60])
                    click.echo(f"  Page {page_num} ({label}): ...{text[:120]}...")
                    found += 1
                    break

    click.echo(f"\n{found} blob page(s) matched.")


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--limit", "-n", default=50, show_default=True, help="Max records to display.")
@click.option(
    "--format", "fmt", type=click.Choice(["table", "json", "csv"]),
    default="table", show_default=True, help="Output format.",
)
@click.option("--path", default=None, type=click.Path(), help="Path to HxStore.hxd file.")
def attachments(limit: int, fmt: str, path: str | None) -> None:
    """List email attachments with filenames and disk paths."""
    store = _open_store(path)

    # Resolve the profile directory for attachment paths
    profile_dir = store.path.parent

    columns = ["record_id", "filename", "disk_path", "exists", "size", "sender_email"]
    rows: list[dict[str, Any]] = []

    for rec in store.iter_data_records():
        if len(rows) >= limit:
            break

        rec_id, decompressed = decompress_record(rec.raw_data, rec.slot_header.size_b)
        utf16 = extract_utf16le_strings(decompressed)

        # Find attachment paths
        paths = [s for s in utf16 if s.startswith("~/Files/") and "/Attachments/" in s]
        if not paths:
            continue

        # Get sender from the record
        raw_ft = struct.unpack_from("<I", rec.raw_data, 8)[0] if len(rec.raw_data) >= 12 else 0
        sender = ""
        if "IPM.Note" in utf16 and raw_ft == 0x03B0:
            ipm_idx = utf16.index("IPM.Note")
            for s in utf16[ipm_idx + 1:]:
                if _EMAIL_RE_TEXT.fullmatch(s):
                    sender = s
                    break

        for attach_path in paths:
            # Get display name (string just before the path)
            try:
                idx = utf16.index(attach_path)
                filename = utf16[idx - 1] if idx > 0 else attach_path.rsplit("/", 1)[-1]
            except ValueError:
                filename = attach_path.rsplit("/", 1)[-1]

            # Resolve to actual disk path
            rel_path = attach_path.replace("~/", "")
            disk_path = profile_dir / rel_path
            exists = disk_path.exists()
            size = disk_path.stat().st_size if exists else 0

            rows.append({
                "record_id": rec_id,
                "filename": filename,
                "disk_path": str(disk_path),
                "exists": "yes" if exists else "NO",
                "size": f"{size:,}" if exists else "-",
                "sender_email": sender,
            })

    if not rows:
        click.echo("No attachments found.")
        return

    _output(rows, columns, fmt)
    click.echo(f"\n{len(rows)} attachment(s) shown.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for console_scripts."""
    cli()


if __name__ == "__main__":
    main()
