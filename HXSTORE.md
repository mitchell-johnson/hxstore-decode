# HxStore.hxd Binary Database Format

A reverse-engineering reference for the proprietary binary database used by Microsoft Outlook for Mac and Windows Mail.

---

## Table of Contents

1. [Overview](#1-overview)
2. [File Layout](#2-file-layout)
3. [Header Page](#3-header-page)
4. [Store IDs](#4-store-ids)
5. [Data Pages & Slot Headers](#5-data-pages--slot-headers)
6. [Record Compression (LZ4)](#6-record-compression-lz4)
7. [Record Format Types](#7-record-format-types)
8. [Decompressed Record Structure](#8-decompressed-record-structure)
9. [Attachment Storage](#9-attachment-storage)
10. [Blob Pages](#10-blob-pages)
11. [Index Pages (B-tree / Cola)](#11-index-pages-b-tree--cola)
12. [Data Encoding Details](#12-data-encoding-details)
13. [Companion Files](#13-companion-files)
14. [Internal Architecture (Cola Engine, HxCore.framework)](#14-internal-architecture-cola-engine-hxcoreframework)
15. [Key Constants](#15-key-constants)
16. [Current Capabilities & Limitations](#16-current-capabilities--limitations)
17. [Approach to Further Reverse Engineering](#17-approach-to-further-reverse-engineering)

---

## 1. Overview

**HxStore.hxd** is the proprietary binary database used by **Microsoft Outlook for Mac** (the native Cocoa/AppKit/SwiftUI app -- NOT Electron-based) and the **Windows Mail** app to store emails, calendar events, contacts, and other mailbox data locally. It replaces the legacy `Outlook.sqlite` + `.olk15*` file system on Mac.

There is no public documentation of this format. Everything in this document was discovered through binary analysis of live HxStore.hxd files and reverse engineering of Outlook's application frameworks.

| Property | Value |
|----------|-------|
| Magic bytes | `Nostromoi` (Mac), `Nostromoh` (Windows) |
| Storage engine | **Cola** (`Hx::Storage::Cola` namespace in HxCore.framework) |
| Compression | **LZ4 Block** (raw, no frame header) |
| Page size | 4096 bytes (0x1000) |
| Typical file size | 50--300 MB |
| Safe to read while Outlook is running | Yes (read-only) |

### File Location

**macOS:**
```
~/Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles/<Profile>/HxStore.hxd
```

The default profile name is `Main Profile`.

**Windows:**
```
%LOCALAPPDATA%\Packages\microsoft.windowscommunicationsapps_8wekyb3d8bbwe\LocalState\
```

### Access Requirements (macOS)

**Full Disk Access** is required. Enable it in System Settings > Privacy & Security > Full Disk Access for the terminal or application reading the file. Without FDA, access to the Group Containers directory raises `PermissionError`.

---

## 2. File Layout

The file is organized as a sequence of **4096-byte (0x1000) pages**. A 164 MB file contains approximately 40,000 pages.

```
Offset
0x00000  ┌──────────────────────────────────────┐
         │  Header Page (4096 bytes)             │
0x01000  ├──────────────────────────────────────┤
         │  Bitmap / Allocation Pages            │
         ├──────────────────────────────────────┤
         │  Data Pages (type 0x08)               │  -- structured records (8 slots each)
         ├──────────────────────────────────────┤
         │  Index Pages (B-tree)                 │  -- lookup indices (secondary store_id)
         ├──────────────────────────────────────┤
         │  Blob Pages                           │  -- email bodies, HTML, large content
         └──────────────────────────────────────┘
```

### Page Type Distribution (Typical)

| Page Type | Typical Count | Purpose |
|-----------|--------------|---------|
| Data (0x08) with primary store_id | ~4,500 | Structured record storage |
| Index with secondary store_id | ~1,100 | B-tree index pages |
| Blob pages (no store_id match) | ~34,000 | Raw content (HTML bodies, etc.) |

---

## 3. Header Page

The first 4096 bytes (page 0) contain the file header.

### Known Header Fields

| Offset | Size | Type | Description |
|--------|------|------|-------------|
| 0x00 | 9 | ASCII | Magic: `Nostromoi\0` (null-terminated) |
| 0x08 | 1 | uint8 | Format version (observed: 0x69 = 105) |
| 0x10 | 8 | uint64_le | Logical file size (may differ from physical file size) |
| 0x18 | 8 | uint64_le | First section offset (bitmap / B-tree root) |
| 0x20 | 8 | uint64_le | Second section offset (data / Cola area) |
| 0x28 | 4 | uint32_le | Record count (tentative) |
| 0x2C | 4 | uint32_le | Flags |
| 0x30 | 4 | uint32_le | Checksum |
| 0x38 | 8 | uint64_le | Page size (always 0x1000 = 4096) |
| 0x40 | 8 | uint64_le | Free page map offset |
| 0x48 | 4 | uint32_le | Max pages (observed: 65521) |
| 0x4C | 4 | uint32_le | Flags |
| 0x50 | 4 | uint32_le | `0xDEADBEEF` sentinel (marks unused section pointer) |
| 0x58 | 8 | uint64_le | Section 3 offset |
| 0x68 | 4 | uint32_le | Count 1 (observed: 3611) |
| 0x6C | 4 | uint32_le | Count 2 (observed: 221) |
| 0x78 | 8 | uint64_le | Section 4 offset |
| 0xC0 | 8 | uint64_le | Data area start offset (tentative) |

`0xDEADBEEF` sentinel values appear extensively in unused header slots, indicating uninitialized or absent section pointers. The remaining header bytes (through offset 0xFFF) contain additional metadata whose structure has not been fully decoded.

---

## 4. Store IDs

Every data page and index page carries an **8-byte store identifier** at offset +8 within the page. A single HxStore file contains at least two store IDs:

| Store ID | Location | Purpose |
|----------|----------|---------|
| **Primary** | Data pages (type 0x08) | Most frequently occurring store_id; identifies record storage pages |
| **Secondary** | Index pages | Identifies B-tree / Cola index pages |

### Discovery Method

The primary store_id is discovered by frequency analysis: scan the first ~2 MB of data pages and select the most common 8-byte value at `page_offset + 8` among pages where the uint32 at `page_offset + 16` equals `DATA_PAGE_TYPE (8)`.

Store IDs appear to be randomly generated on account creation and remain stable across syncs.

---

## 5. Data Pages & Slot Headers

### Identifying Data Pages

A page is a data page when:
1. The **primary store_id** matches the 8 bytes at `page_offset + 8`
2. The **type field** is `0x08` (uint32_le) at `page_offset + 16`

### Slot Layout

Each data page is divided into **8 slots of 512 bytes (0x200)** each:

```
Page (4096 bytes = 8 x 512-byte slots)
┌─────────────────────────────────────────┐
│ Slot 0  (page header + first record)    │  offset + 0x000
│ Slot 1  (record)                        │  offset + 0x200
│ Slot 2  (record)                        │  offset + 0x400
│ Slot 3  (record)                        │  offset + 0x600
│ Slot 4  (record)                        │  offset + 0x800
│ Slot 5  (record)                        │  offset + 0xA00
│ Slot 6  (record)                        │  offset + 0xC00
│ Slot 7  (record)                        │  offset + 0xE00
└─────────────────────────────────────────┘
```

Not all slots contain valid records. Empty slots have zeroed store_id or non-matching type fields.

### Slot Header (32 bytes)

Each 512-byte slot begins with a 32-byte header:

| Offset (within slot) | Size | Type | Description |
|----------------------|------|------|-------------|
| 0x00 | 8 | uint64_le | Hash (indexing / lookup key) |
| 0x08 | 8 | bytes | Store ID (must match primary store_id) |
| 0x10 | 4 | uint32_le | Page/record type (must be `8` for data) |
| 0x14 | 4 | uint32_le | `size_a` -- compressed payload size in bytes |
| 0x18 | 4 | uint32_le | `size_b` -- uncompressed payload size in bytes |
| 0x1C | 4 | uint32_le | Unknown field (observed values: 2, 4, 6) |

After the 32-byte header, the remaining bytes (up to `size_a` bytes) contain the **record data** (an 8-byte record ID followed by LZ4-compressed payload).

### Multi-Slot Records

When `size_a` exceeds the usable space in a single slot (480 bytes = 512 - 32), the record data continues contiguously into subsequent bytes in the page. The data is read as a contiguous block starting at `slot_offset + 32` for `size_a` bytes, potentially crossing slot boundaries.

---

## 6. Record Compression (LZ4)

Records are compressed using **LZ4 Block format** (raw block, no frame header).

### Confirmation

The compression algorithm was confirmed through two independent methods:

1. **Binary analysis**: The `HxCore.framework` binary contains the C++ symbol `Hx::Compressor::CopyStreamToCompressed_LZ4(IStream*, IStream*)`. The `mso20.framework` statically links 100+ LZ4 symbols (LZ4 core, LZ4HC high compression, LZ4F frame, and streaming APIs).

2. **Empirical decompression**: 100% success rate across all tested records using a lenient LZ4 block decoder.

### Record Data Layout

```
Record data (size_a bytes, starting after the 32-byte slot header):
  [8 bytes: record_id (uint64_le)]  -- NOT compressed
  [size_a - 8 bytes: LZ4 block]    -- compressed payload

size_a = 8 + len(compressed_payload)
size_b = 8 + len(decompressed_payload)
```

### Decompression

```python
import struct
import lz4.block

record_id = struct.unpack_from("<Q", record_data, 0)[0]
compressed = record_data[8:]
uncompressed_size = size_b - 8
decompressed = lz4.block.decompress(compressed, uncompressed_size=uncompressed_size)
```

### C Library Compatibility

The standard `lz4.block` C library (Python `lz4` package) successfully decompresses approximately 3% of records. It is strict about end-of-stream conditions and rejects records where:
- The LZ4 stream terminates with a truncated final literal run
- Trailing bytes exist after the compressed stream
- The decompressed output is slightly short of `size_b - 8` (typically 7--17 bytes)

A **lenient manual LZ4 block decoder** that gracefully handles truncated literals and non-standard stream endings achieves **100% decompression success**. The decompressed output from the lenient decoder is correct -- verified by extracting readable email content (sender, subject, body, message IDs) from decompressed records.

### Compression Statistics

| Metric | Value |
|--------|-------|
| Min compression ratio (size_b / size_a) | 1.3x |
| Max compression ratio | 13.2x |
| Median ratio | ~2.5x |
| Most common range | 2.0x--5.0x |
| Average space saving | ~74% |

### Algorithms Ruled Out

zlib, zstd, brotli, LZ4 Frame, LZMA/XZ, MS-XPRESS (LZ77 plain and Huffman), LZNT1, LZX, LZMS, snappy, LZF, LZO, and ESE 7-bit compression were all tested and ruled out. zlib is linked by HxCore for HTTP gzip transport, not for storage compression.

---

## 7. Record Format Types

The first 4 bytes of the LZ4 compressed stream (i.e., the uint32_le at `record_data[8]` before decompression, which becomes the first 4 bytes after decompression of the non-record-ID portion) identify the record format type. In practice, these are the first 4 bytes of the decompressed payload, acting as a version/flags field.

| Format Type | Decimal | Description | Content Profile |
|-------------|---------|-------------|-----------------|
| 0x03B0 | 944 | **Email records** (primary) | 100% contain IPM.Note. Full email metadata, HTML bodies. Best quality. |
| 0x03B1 | 945 | **Email records** (variant) | Email records, similar structure to 0x03B0 |
| 0x10013 | 65,555 | **Mixed records** | 41% emails, 59% metadata/folders/settings |
| 0x0191 | 401 | **Email summary records** | 48% contain IPM.Note. Sender, subject, body preview. |
| 0x0190 | 400 | **Entity extraction data** | URLs, anchor text, structured metadata extracted from emails. JSON-like content. |
| 0x310F4 | 200,948 | **Rich email records** | Full HTML bodies, ~90% readable content |
| 0x30FF1 | 200,689 | **Rich email records** | Full HTML bodies, ~90% readable content |
| 0x0150 | 336 | **Small metadata records** | ~6% readable, compact binary metadata |
| 0x07B0 | 1,968 | Rare, purpose unknown | |
| 0x04B0 | 1,200 | Rare, purpose unknown | |
| 0x06B0 | 1,712 | Rare, purpose unknown | |
| 0x09B0 | 2,480 | Rare, purpose unknown | |
| 0x01B0 | 432 | Rare, purpose unknown | |

### Typical Distribution (1,302 records)

| Format | Count | Avg Compression Ratio | Avg Readable % |
|--------|-------|----------------------|----------------|
| 0x10013 | 631 | 2.4x | 10.6% |
| 0x03B0 | 243 | 3.4x | 43.9% |
| 0x0190 | 198 | 4.4x | 40.3% |
| 0x0191 | 185 | 4.8x | 19.6% |
| 0x0150 | 15 | -- | 6.1% |
| 0x310F4 | 13 | 6.1x | 89.9% |
| 0x30FF1 | 3 | -- | 90.5% |

Higher compression ratios correlate with records containing more repetitive UTF-16LE text and boilerplate HTML.

---

## 8. Decompressed Record Structure

After LZ4 decompression, the record payload has a general structure:

```
Decompressed data:
  [0x00] uint32_le  -- version/flags field (0x03 for email records, 0x00/0x01 for metadata)
  [0x04] varies     -- format-specific header and property data
  ...
  Sections divided by marker bytes: 00 01 00 00 00 00 01
  ...
  UTF-16LE strings, ASCII strings, binary data
```

### Common Header Patterns

| Format | First Bytes (decompressed) | Description |
|--------|---------------------------|-------------|
| 0x03B0, 0x310F4 | `03 00 00 00` | Email records |
| 0x0190, 0x0191 | `01 00 00 00` or `00 00 00 00` | Summary / entity records |
| 0x10013 | `00 00 00 00 00 00 00 00 01 00 00 00` | Metadata records |

### Section Markers

Records are divided into sections by the byte sequence:
```
00 01 00 00 00 00 01
```

These markers separate logical groups of properties within a record.

### String Sequences by Format Type

The following sequences describe the order of UTF-16LE strings found in decompressed email records. Strings are extracted by scanning for runs of printable-ASCII-range bytes interleaved with `\x00` (the UTF-16LE encoding pattern).

#### Format 0x03B0 (Primary Email Records)

```
IPM.Note
sender_email          (e.g., "sender@example.com")
sender_name           (e.g., "Jane Doe")
recipient_email       (e.g., "recipient@example.com")
recipient_name        (e.g., "John Smith")
[... additional recipient_email / recipient_name pairs ...]
IPM.Note              (repeated)
message_ids           (Internet Message-ID and Exchange identifiers)
body_preview          (first ~200 chars of email body as plain text)
subject               (email subject line)
combined_preview      (subject + body preview combined)
```

In 0x03B0 records, the HTML body (ASCII, terminated by `</html>`) immediately follows the first `IPM.Note` string. A GUID or content hash (16 bytes) precedes the first `IPM.Note` at a fixed offset (~0x1A2 in the decompressed data).

#### Format 0x10013 (Mixed / Metadata Records)

For records that contain email data (41% of 0x10013 records):

```
IPM.Note
message_ids
body_preview
subject
subject               (repeated)
```

The remaining 59% contain folder metadata, settings, and configuration data without the IPM.Note marker.

#### Format 0x0191 (Email Summary Records)

```
IPM.Note
sender_email          (e.g., "sender@example.com")
sender_email          (repeated)
body_preview
sender_name_combined  (display name, possibly with email)
subject
subject               (repeated)
IPM.Note              (repeated)
```

#### Format 0x0190 (Entity Extraction Data)

Contains structured metadata extracted from emails: URLs, anchor text, link destinations, and timestamps. The content is JSON-like with fields such as URLs (`https://...`) and ISO 8601 timestamps (`2026-03-17T08:11:17`). Does not follow the IPM.Note string sequence pattern.

#### Format 0x310F4 / 0x30FF1 (Rich Email Records)

These contain full HTML email bodies and are approximately 90% readable text after decompression. Structure is similar to 0x03B0 but with larger HTML payloads.

---

## 9. Attachment Storage

Attachments are **NOT stored inside HxStore.hxd**. They are stored as real files on the filesystem at:

```
<Profile>/Files/S0/3/Attachments/0/<filename>[<id>].<ext>
```

Where:
- `<Profile>` is the Outlook profile directory
- `<id>` is a numeric identifier in square brackets
- Example: `report[12345].pdf`

Records within HxStore reference attachments via paths like:
```
~/Files/S0/3/Attachments/0/<filename>[<id>].<ext>
```

Additionally, **EFMData/*.dat** files store email body data for large emails that exceed inline storage limits.

A typical Outlook profile contains 300--400 attachment files.

---

## 10. Blob Pages

The majority of pages (~34,000 in a 164 MB file) do not match either store_id and lack the standard data page header structure. These are **blob pages** containing:

- HTML email bodies (detectable by `<html` markers)
- Plain text email bodies
- Inline images (base64 or raw binary)
- Calendar event data (ICS/vCalendar fragments)
- Other large content

### Blob Page Header (Type 4 / LZ4 Compressed Blobs)

Some blob pages carry their own header indicating LZ4-compressed content:

| Offset | Size | Type | Description |
|--------|------|------|-------------|
| 0x00 | 16 | bytes | Hash/checksum (possibly MD5 of content) |
| 0x10 | 4 | uint32_le | Header version/flags (typically 8) |
| 0x14 | 4 | uint32_le | Compressed size (bytes) |
| 0x18 | 4 | uint32_le | Uncompressed size (bytes) |
| 0x1C | 4 | uint32_le | Compression type (4 = LZ4) |
| 0x20 | 8 | bytes | Additional metadata |
| 0x28 | ... | bytes | LZ4 compressed data (raw block, no frame header) |

When `compressed_size` exceeds the page data area (4096 - 0x28 = 4056 bytes), the compressed data spans contiguous pages. Reading from `page_start + 0x28` for `compressed_size` bytes allows successful decompression.

### Compression Type Distribution in Blob Pages

| Type | Count (typical) | Meaning |
|------|-----------------|---------|
| 4 | ~616 | LZ4 block compression (confirmed) |
| 2 | ~24 | Unknown (different header structure, possibly uncompressed or zlib) |
| 1 | ~13 | Unknown (different header structure) |

### Searching Blob Pages

Blob pages can be searched with full-text scanning:
```python
# UTF-8 search
file_data.find(query.encode("utf-8"))

# UTF-16LE search
file_data.find(query.encode("utf-16-le"))

# HTML body extraction: search for <html and read until </html>
```

---

## 11. Index Pages (B-tree / Cola)

Pages carrying the **secondary store_id** are **B-tree index pages** managed by the Cola storage engine.

| Property | Value |
|----------|-------|
| Typical count | ~1,100 pages |
| Store ID | Secondary (distinct from data pages) |
| Internal structure | B-tree nodes |
| Status | **Not yet decoded** |

These pages likely contain lookup indices mapping record IDs, email addresses, or timestamps to data page locations. The Cola engine's `IndexPage`, `IndexPageCache`, and `KvStoreReader` classes (visible in HxCore's symbol table) manage these structures.

Because the index format is not decoded, all record lookups currently require a full sequential scan of data pages.

---

## 12. Data Encoding Details

### Email Addresses

Stored as **plain ASCII** within decompressed record data. Extractable with:
```python
re.finditer(rb"[\w.+-]+@[\w.]+\.\w{2,6}", decompressed_data)
```

Multiple addresses may appear in a single record (sender, recipients, CC, BCC).

### Display Names and Subject Lines

Stored as **UTF-16LE** (Little Endian UTF-16). Each ASCII character occupies 2 bytes: the character byte followed by `\x00`.

Detection pattern:
```python
re.finditer(rb"(?:[\x20-\x7e]\x00){3,}", decompressed_data)
```

### Message Class

Email records contain the string `IPM.Note` encoded as UTF-16LE. This is the MAPI message class (`PR_MESSAGE_CLASS`, tag 0x001A) identifying the record as an email message.

### Timestamps

Timestamps use the **Cocoa epoch**: seconds since **2001-01-01 00:00:00 UTC** (the standard `NSDate` reference date in Apple frameworks).

| Property | Value |
|----------|-------|
| Encoding | uint32_le (4 bytes) |
| Epoch | 2001-01-01 00:00:00 UTC |
| Offset to Unix epoch | +978,307,200 seconds |
| Plausible range (2020--2030) | 599,616,000 -- 915,148,800 |

Conversion:
```python
from datetime import datetime, timezone

COCOA_EPOCH_OFFSET = 978307200
unix_ts = cocoa_timestamp + COCOA_EPOCH_OFFSET
dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
```

Timestamps are located by scanning every 4-byte-aligned offset in the decompressed record and checking if the resulting uint32 falls within the plausible Cocoa range.

### Property Encoding

Properties within decompressed records are **not** stored in a standard TLV, protobuf, or msgpack format. The data uses a proprietary serialization that interleaves ASCII strings, UTF-16LE strings, and raw binary data. Section boundaries are marked by the byte sequence `00 01 00 00 00 00 01`.

The property format has not been fully decoded. Current extraction relies on heuristic regex methods (see [Current Capabilities & Limitations](#16-current-capabilities--limitations)).

---

## 13. Companion Files

### hxcore.hfl

| Property | Value |
|----------|-------|
| Location | `<Profile>/hxcore.hfl` |
| Format | **NOT a Nostromoi database** |
| Typical size | ~50 MB |
| Purpose | Binary log or auxiliary index for HxCore |

The file does not begin with the Nostromoi magic. First 8 bytes: `08 00 00 00 00 00 01 00`. Contains version strings (e.g., `16.0.19819.33806`) and a DEADBEEF marker, but uses a completely different internal format from HxStore.hxd.

### Outlook.sqlite

| Property | Value |
|----------|-------|
| Location | `<Profile>/Data/Outlook.sqlite` |
| Format | Standard SQLite database |
| Purpose | **Legacy** storage format |

Contains tables for `Mail`, `CalendarEvents`, `Contacts`, `Folders`, `Blocks`, `Conversations`, `Tasks`, `Notes`, `Settings`, and others. The `Main` table links record IDs to `.olk15Main` files in the `Data/Main/` directory. This is the pre-HxStore storage format; HxStore.hxd is the current format used by the Cola engine.

### HxStore.lock

File-level lock used by Outlook to prevent concurrent writes.

### ExternalCounters.ctr

Counter file for internal bookkeeping.

---

## 14. Internal Architecture (Cola Engine, HxCore.framework)

### Application Bundle

| Component | Path | Role |
|-----------|------|------|
| HxCore.framework | `.../Frameworks/HxCore.framework/` | Core database engine (Cola storage, compression, sync) |
| mso20.framework | `.../Frameworks/mso20.framework/` | Microsoft Office shared library; contains full statically-linked LZ4 |
| HxPlorer.framework | `.../Frameworks/HxPlorer.framework/` | SwiftUI browser layer for HxStore (no compression code) |
| mbukernel.framework | `.../Frameworks/mbukernel.framework/` | Base utility kernel; uses Apple Compression framework |

Outlook for Mac is a **native Cocoa application** (AppKit/SwiftUI). It is not Electron-based. No Electron, asar, or node_modules components are present.

### Cola Storage Engine

The HxCore binary contains an extensive `Hx::Storage::Cola` namespace implementing a page-based database:

| Class | Purpose |
|-------|---------|
| `Cola::Store` / `IStore` | Top-level database store |
| `Cola::StoreObject` / `IStoreObject` | Individual stored objects |
| `Cola::StoreCollection` / `IStoreCollection` | Object collections |
| `Cola::IndexPage` | B-tree index page management |
| `Cola::CollectionPage` | Collection data page management |
| `Cola::IndexPageCache` | LRU cache for index pages |
| `Cola::PersistenceGroup` | Persistence / transaction groups |
| `Cola::KvStoreReader` | Key-value store reader |
| `Cola::GlobalLRUEvictor` | LRU eviction policy |

### Compression Symbols

**HxCore.framework** exports:
- `Hx::Compressor::CopyStreamToCompressed_LZ4(IStream*, IStream*)` -- the primary compression entry point
- `Hx::GzipCompressStreamImpl` -- used for HTTP transport, not storage

**mso20.framework** contains 100 exported LZ4 symbols:

| API Family | Example Symbols | Count |
|-----------|----------------|-------|
| LZ4 core | `LZ4_compress_default`, `LZ4_decompress_safe`, `LZ4_compress_fast` | ~40 |
| LZ4HC | `LZ4_compress_HC`, `LZ4_compress_HC_extStateHC` | ~20 |
| LZ4F (frame) | `LZ4F_compressBegin`, `LZ4F_decompress`, `LZ4F_getFrameInfo` | ~25 |
| LZ4 streaming | `LZ4_createStream`, `LZ4_createStreamDecode` | ~15 |

### Telemetry

The string `streamDataFileCompressionType` appears in HxCore at two offsets, adjacent to other telemetry event names. This confirms the compression type is a tracked/configurable property (type 4 = LZ4).

### zlib Usage

HxCore dynamically links `/usr/lib/libz.1.dylib` and imports `compress2`, `deflate`, `inflate`, etc. These are used exclusively for **HTTP gzip transport** (`GzipCompressStreamImpl`), NOT for database storage compression.

---

## 15. Key Constants

```python
HXSTORE_MAGIC       = b"Nostromoi"     # Mac; b"Nostromoh" on Windows
PAGE_SIZE           = 0x1000           # 4096 bytes
SLOT_SIZE           = 0x200            # 512 bytes
SLOTS_PER_PAGE      = 8               # PAGE_SIZE / SLOT_SIZE
SLOT_HEADER_SIZE    = 32               # 8B hash + 8B store_id + 4x uint32
SLOT_DATA_SIZE      = 480              # SLOT_SIZE - SLOT_HEADER_SIZE
DATA_PAGE_TYPE      = 8               # uint32 at page+16 for data pages
RECORD_ID_SIZE      = 8               # Uncompressed record ID prefix
COCOA_EPOCH_OFFSET  = 978307200       # Seconds between Unix (1970) and Cocoa (2001) epochs
COCOA_TS_MIN        = 599_616_000     # ~2020-01-01
COCOA_TS_MAX        = 915_148_800     # ~2030-01-01
DEADBEEF_SENTINEL   = 0xDEADBEEF     # Marks unused header section pointers
BLOB_COMPRESS_LZ4   = 4               # Blob page compression type for LZ4
SECTION_MARKER      = b'\x00\x01\x00\x00\x00\x00\x01'  # Record section delimiter

# Record format types
FORMAT_EMAIL        = 0x03B0           # 944: Primary email records
FORMAT_EMAIL_ALT    = 0x03B1           # 945: Email variant
FORMAT_MIXED        = 0x10013          # 65555: Mixed metadata/email
FORMAT_SUMMARY      = 0x0191           # 401: Email summary records
FORMAT_ENTITY       = 0x0190           # 400: Entity extraction data
FORMAT_RICH_EMAIL   = 0x310F4          # 200948: Rich email with full HTML
FORMAT_RICH_EMAIL2  = 0x30FF1          # 200689: Rich email variant
FORMAT_METADATA     = 0x0150           # 336: Small metadata records
```

---

## 16. Current Capabilities & Limitations

### What Works

- **LZ4 decompression** of all data records (100% success rate with lenient decoder)
- **Email record identification** via IPM.Note marker in decompressed data
- **Sender extraction** -- email address and display name (UTF-16LE)
- **Subject line extraction** (UTF-16LE)
- **Body preview extraction** (UTF-16LE)
- **Approximate timestamp extraction** (Cocoa epoch scanning)
- **Full-text search** across decompressed records and blob pages (UTF-8 + UTF-16LE)
- **HTML body extraction** from blob pages
- **Record enumeration**, ID-based lookup, and format type classification
- **Blob page decompression** for type-4 (LZ4) blob pages (99.5% success with standard library)
- **Record format type classification** distinguishing emails, metadata, entities, and rich content

### What Does Not Work (Yet)

| Gap | Description |
|-----|-------------|
| Structured property decoding | The property format within decompressed records is not decoded as structured TLV. Relies on heuristic regex extraction. |
| Folder/label mapping | No known way to determine which folder a record belongs to. |
| Read/unread status | Not identified in the record data. |
| Attachment-to-record association | Attachments exist on the filesystem but cannot be programmatically linked to their parent records. |
| Index page traversal | B-tree index pages are identified but not parsed; lookups require full sequential scan. |
| Calendar event fields | Start/end times, location, attendees are not mapped. |
| Recipient differentiation | To/CC/BCC classification is heuristic only. |
| Exact timestamp field offsets | Timestamps found by range scanning, not by known field offset. |

---

## 17. Approach to Further Reverse Engineering

1. **Differential analysis**: Create an email with known content, sync Outlook, compare the HxStore before and after to isolate the new record and map exact byte offsets to known fields.

2. **Index page parsing**: Decode the Cola B-tree structure to enable efficient lookups instead of sequential scans. The `Cola::IndexPage` and `Cola::KvStoreReader` symbols provide clues about the node format.

3. **Property tag identification**: Microsoft Exchange uses MAPI property tags (e.g., `PR_SUBJECT = 0x0037`, `PR_SENDER_EMAIL_ADDRESS = 0x0C1F`). Scanning for known tag values near property data could reveal the encoding scheme.

4. **Section marker analysis**: The `00 01 00 00 00 00 01` section markers divide records into logical groups. Systematic analysis of content between markers across many records could reveal the property layout.

5. **Cross-platform comparison**: Windows Mail uses the same HxStore.hxd format (magic "Nostromoh"). Comparing Mac and Windows files could clarify platform-specific vs. universal structures.

6. **Compaction/WAL analysis**: The file may have write-ahead log mechanics. Observing the file across multiple sync cycles could reveal these patterns.

7. **Dynamic analysis**: Using LLDB or Frida to hook `Hx::Compressor::CopyStreamToCompressed_LZ4` and `Cola::StoreObject` methods at runtime could reveal the exact serialization format used when writing records.

---

## References

- **Boncaldo's Forensics Blog (2018)**: First public research on HxStore.hxd. Identified the "Nostromoh" magic header (Windows variant). Noted the format is undocumented.
- **"Navigating the Windows Mail database"** (ScienceDirect): Academic forensics paper analyzing the format.
- **LZ4 Block Format Specification**: https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md

No other public documentation of the Nostromoi/HxStore internal format is known to exist as of March 2026.
