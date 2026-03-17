# hxdecode

A command-line tool to decode and inspect Microsoft Outlook for Mac's HxStore.hxd binary database.

## What is HxStore.hxd?

Outlook for Mac (the "new" Outlook, version 16+) stores email metadata, contacts, calendar items, and other records in a proprietary binary database called `HxStore.hxd`. This file uses a page-based storage format with LZ4-compressed records, and is not documented by Microsoft.

**hxdecode** parses this binary format and lets you list emails, search across records, inspect attachments, and export data as JSON or CSV -- all from the command line.

## Installation

```bash
pip install .
```

Or install in development mode:

```bash
pip install -e .
```

This installs the `hxdecode` command-line tool.

### Requirements

- Python 3.10 or later
- macOS (for auto-discovery of the HxStore.hxd file; you can also pass a path manually)
- **Full Disk Access** must be granted to your terminal application in System Settings > Privacy & Security for auto-discovery to work, since the database lives inside `~/Library/Group Containers/`
- Dependencies: `click`, `lz4` (installed automatically)

## Quick Start

**View database statistics:**

```
$ hxdecode info
File:              /Users/you/Library/Group Containers/.../HxStore.hxd
File size:         524,288,000 bytes (500.0 MB)
Total pages:       128,000
Page size:         4096 bytes

Data pages:        89,231
Index pages:       12,445
Blob pages:        26,323

Data records:      42,817
Record format types:
  0x03B0 (944): 28,105
  0x10013 (65555): 14,712
```

**List recent emails:**

```
$ hxdecode mail -n 5
+-------+-----------------------+----------------+---------------------+---------------------+
| rec   | sender_email          | sender_name    | subject             | timestamp           |
+-------+-----------------------+----------------+---------------------+---------------------+
| 10042 | alice@example.com     | Alice Johnson  | Meeting notes       | 2025-03-15 09:22:01 |
| 10051 | bob@example.com       | Bob Smith      | Re: Project update  | 2025-03-15 10:45:33 |
+-------+-----------------------+----------------+---------------------+---------------------+
```

**Search for a string across all records:**

```
$ hxdecode search "invoice"
Searching for "invoice" ...

Record 15234 (format=0x03B0):
  From: accounting@example.com
  Subject: Invoice #1042 attached
  Date: 2025-03-10 14:22:00
  size: 412 -> 1836 bytes

3 record(s) matched in data records.
1 blob page(s) also contain the search term.
```

**Export email list as JSON:**

```
$ hxdecode mail --format json -n 2
[
  {
    "record_id": 10042,
    "sender_email": "alice@example.com",
    "sender_name": "Alice Johnson",
    "subject": "Meeting notes",
    "timestamp": "2025-03-15T09:22:01+00:00"
  }
]
```

## Commands

| Command | Description |
|---|---|
| `hxdecode info` | Show database statistics: file size, page counts, record counts by format type |
| `hxdecode mail` | List email records with sender, subject, and date |
| `hxdecode mail show ID` | Show full detail of a single record by its numeric ID |
| `hxdecode records` | List raw data records with IDs, sizes, and format types |
| `hxdecode search QUERY` | Full-text search across all decompressed records (UTF-8 and UTF-16LE) |
| `hxdecode blob-search QUERY` | Search blob pages for text (HTML email bodies, attachments) |
| `hxdecode attachments` | List email attachments with filenames and disk paths |

### Common Options

- `--path PATH` -- Specify the HxStore.hxd file explicitly (otherwise auto-discovered)
- `--format table|json|csv` -- Output format (default: `table`)
- `--limit N` / `-n N` -- Maximum number of records to display
- `--version` -- Show version number
- `--help` -- Show help for any command

## How It Works

HxStore.hxd is a page-based binary database. The file is divided into 4096-byte pages, each containing up to 8 slots of 512 bytes. hxdecode processes the file in several stages:

1. **Page classification** -- Each page is classified as data, index, or blob by examining its store ID and type fields. Two store IDs are discovered automatically by frequency analysis.

2. **Slot and record parsing** -- Data pages contain slotted records. Each slot has a 32-byte header with a hash, store ID, type, compressed size, and uncompressed size. Records larger than 480 bytes span multiple contiguous slots.

3. **LZ4 decompression** -- Record payloads are LZ4 block-compressed (raw blocks, no frame header). The first 8 bytes of each record are an uncompressed record ID, followed by the compressed payload. A lenient decoder handles the truncated-literal edge cases that the standard LZ4 library rejects.

4. **Heuristic extraction** -- Email fields (sender, subject, timestamps) are extracted using pattern matching over the decompressed data. UTF-16LE strings, ASCII email addresses, and Cocoa-epoch timestamps are identified by regex and range checks. The internal property encoding is not yet fully decoded as structured TLV.

For the full binary format specification, see [HXSTORE.md](HXSTORE.md).

## Limitations

- **Read-only** -- hxdecode only reads the database; it cannot modify it.
- **Heuristic extraction** -- Email fields are extracted using pattern matching, not by decoding a fully understood schema. Some fields may be missing or incorrect, particularly for less common record format types.
- **No body text** -- Full email bodies are stored in blob pages and are not yet associated back to their parent email records.
- **macOS only for auto-discovery** -- The auto-discovery path is macOS-specific, but you can pass any HxStore.hxd file path with `--path` on any platform.
- **Memory usage** -- The entire file is read into memory. For very large databases (multiple GB), this may require significant RAM.

## Contributing

Contributions are welcome. Some areas where help would be particularly useful:

- Decoding the internal TLV/property structure of records
- Linking blob pages back to their parent records
- Supporting additional record format types
- Adding integration tests with synthetic HxStore files

Please open an issue to discuss larger changes before submitting a pull request.

## License

MIT License. See [LICENSE](LICENSE) for details.
