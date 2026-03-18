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
| Timestamp encoding | .NET ticks (int64, 100ns since 0001-01-01) |

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

**Note:** Data records can also appear on index pages — the slot layout is shared. See [Section 11](#11-index-pages-b-tree--cola) for details.

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
| 0x0190 | 400 | **Entity/container records** | Mixed: entity extraction data (URLs, metadata) AND large account container records with folder definitions. Small records (~1-8 KB) contain JSON-like entity data. Large records (~50-100 KB) contain the complete folder hierarchy with name tables. |
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

##### 0x10013 Record Header (Decoded)

All 0x10013 records share a common 64-byte header:

| Offset | Size | Type | Description |
|--------|------|------|-------------|
| 0-7 | 8 | bytes | Zeros |
| 8 | 4 | uint32_le | Entry count (1-10) |
| 12 | 4 | uint32_le | Total size |
| 16 | 2 | uint16_le | Constant `0x0006` |
| 18 | 2 | uint16_le | Constant `0x0570` (Cola store tag) |
| 20 | 4 | uint32_le | Self-referencing record ID (100% confirmed) |
| 28 | 2 | uint16_le | Near ObjectType (0x00C7=199 for emails, close to MessageHeader=201) |
| 34 | 2 | uint16_le | Sub-type tag: `0x0290` (emails + some metadata), `0x01F0` (skeleton objects), `0x00D0`/`0x0070` (small metadata) |
| 44 | 2 | uint16_le | **ObjectType** (see below) |
| 48 | 4 | uint32_le | Account root record ID |

##### ObjectType at Offset 44

The uint16 at offset 44 in decompressed 0x10013 records classifies the record:

| ObjectType | Value | Description | Count (typical) |
|------------|-------|-------------|-----------------|
| Email (MessageHeader) | 0xBF (191) | Email record | ~864 |
| Folder (View) | 0x4D (77) | Mail folder definition | ~45 |
| Contact | 0xE0 (224) | Contact record | ~26 |
| Calendar | 0x68 (104) | Calendar item | ~6 |
| Search | 0x120 (288) | Search session | ~38 |

##### Schema Table (Bytes 64-532)

The 475 bytes from offset 64 to 532 are a **fixed schema table** that is 100% identical across all email records. It contains property definition entries in a 10-byte format: `[uint32 property_id][uint32 type=3][uint16 padding=0]`, with 4-byte sequence counters separating groups.

##### Section Marker Structure

After the schema table and a variable footer, section markers (`00 01 00 00 00 00 01`) divide the record into property groups. Each section begins with a 16-byte header:

| Offset | Size | Type | Description |
|--------|------|------|-------------|
| 0 | 4 | uint32_le | Section data length |
| 4 | 2 | uint16_le | Constant `0x0005` |
| 6 | 2 | uint16_le | Format subtype (e.g. `0x0613` for email) |
| 8 | 4 | uint32_le | Section data length (repeated) |
| 12 | 2 | uint16_le | Padding |
| 14 | 2 | uint16_le | Object type (e.g. `0x00C9`=201 for MessageHeader) |

Records may contain multiple sections for child objects (Recipients, AttachmentHeaders).

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

### Two-Object Model (MessageHeader + MessageData)

Outlook stores emails as two separate Cola objects:

| Object | Type ID | Contents |
|--------|---------|----------|
| **MessageHeader** | 201 | Subject, sender, date, flags, preview, read status, size, serverId |
| **MessageData** | (separate) | `_messageDataHTMLBodyBytes`, `_body`, `_bodyEncoding`, `_bodyType` |

The MessageHeader links to its MessageData via a `_messageDataId` property. In decompressed 0x10013 records, this property has been observed at byte offsets 632, 652, and 848.

**Three-tier body storage:**

| Tier | Condition | Storage Location |
|------|-----------|-----------------|
| Inline | Small bodies | Directly in the MessageData record |
| Object stream | Medium bodies | Key-value store (blob pages) |
| External file | Large bodies (68--152 KB HTML) | `~/Files/S0/3/EFMData/N.dat` (gzip-compressed) |

### Folder/Mailbox Mapping

Email-to-folder membership is now fully decoded for `0x10013` records.

#### Folder Reference in Email Records

Each email record (0x10013, ObjectType 0xBF at offset 44) contains a **16-byte folder reference pattern** at fixed offset **1520** in the decompressed data:

```
Offset 1520:
  [uint32 folder_ref_id]   -- identifies the containing folder
  [uint32 0x00000000]      -- zero padding
  [uint32 0x00000002]      -- constant
  [uint32 account_root_id] -- account container record ID
```

This pattern resolves **100% of standard email records** (863/864 in a typical database). The single exception is a folder record (ObjectType 0x4D) that coincidentally contains email-like content.

#### Folder Name Table (Container Records)

Folder names are stored in **large 0x0190 container records** (typically 50-100 KB). These records contain the complete folder hierarchy with paired entries in a linked-list structure.

The container record uses the same `[ref_id, 0, 2, root_id]` pattern to define folder entries. Each folder appears as two consecutive entries: the first entry's ref_id is the folder's own identifier, and the second entry's ref_id is the next folder's identifier (linked-list forward pointer).

Folder names appear as UTF-16LE strings within each entry, positioned before the ref_id pattern. The `05 00 4D 04` byte sequence acts as a separator between catalog entries, though the catalog keys after this separator are B-tree internal identifiers rather than the folder ref_ids used in email records.

#### Extraction Algorithm

1. Find large 0x0190 container records (>10 KB)
2. The container's own record ID is the account root ID
3. Search for all `[ref_id, 00000000, 02000000, root_id]` patterns
4. For each pattern, find the first UTF-16LE string within 200 bytes before it
5. Allow overwrites (last mapping wins) to handle the linked-list paired structure
6. For email records, read uint32 at offset 1520 as the folder ref_id

#### Example Folder Map

| ref_id | Folder Name | Emails |
|--------|-------------|--------|
| 1182 | Inbox | 304 |
| 1636 | Sent Mail | 208 |
| 4558 | m.johnson@massey.ac.nz | 123 |
| 4569 | elwesties@gmail.com | 97 |
| 4580 | Uni Mass email shit | 93 |
| 1658 | Spam | 15 |
| 1204 | Drafts | 4 |
| 1669 | Archive | 0 |
| 1647 | Trash | 0 |
| 4602 | Call For Papers | 0 |
| 2145 | Conversation History | 0 |
| 4591 | UNI | 0 |

#### Key Findings

- **MAPI property tags are NOT used** for folder linkage. `PidTagParentFolderId` (0x6749) and `PidTagFolderId` (0x6748) do not appear in Cola records. The Cola engine uses its own record-ID reference system.
- **IPF.Note** (folder container class, distinct from IPM.Note message class) was not found in folder records. Folder records are identified by ObjectType 0x4D at offset 44.
- The folder reference at offset 1520 enables **incoming vs outgoing mail filtering**: emails in "Sent Mail" folders are outgoing, all others are incoming.
- Multiple email accounts share a single HxStore file. Each account has its own container record with a distinct root_id.

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

Additionally, **EFMData/*.dat** files store **gzip-compressed HTML email bodies** for large emails that exceed inline storage limits. These files decompress to 68--152 KB of full HTML. Records reference them via paths like `~/Files/S0/3/EFMData/N.dat`.

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

Pages carrying the **secondary store_id** contain **B-tree index nodes** managed by the Cola storage engine. These pages share the same 8-slot-per-page layout as data pages — individual slots may contain either index nodes or data records.

### Mixed Page Layout

Index pages use the same 8 x 512-byte slot structure as data pages. Each slot is independently typed:
- Slots with the **secondary store_id** and type=8 are **index nodes**
- Slots with the **primary store_id** and type=8 are **data records** (emails, metadata)

This means **data records can live on index pages**. In a typical database, ~61 data records are found on index pages that would be missed by scanning only primary-store-ID pages.

### Index Node Header (32 bytes)

| Offset | Size | Type | Description |
|--------|------|------|-------------|
| 0x00 | 8 | uint64_le | Node hash |
| 0x08 | 8 | bytes | Secondary store_id |
| 0x10 | 4 | uint32_le | Node ID (unique per node) |
| 0x14 | 4 | uint32_le | Always 0x200 (512 = slot size) |
| 0x18 | 4 | uint32_le | Entry region end offset (0x20 + entry_count * 20) |
| 0x1C | 4 | uint32_le | B-tree level (1 = leaf, 2 = internal) |

### Index Entries (20 bytes each)

Starting at offset 0x20 within the slot, entries are packed sequentially:

| Offset | Size | Type | Description |
|--------|------|------|-------------|
| 0x00 | 4 | uint32_le | Key (sorted ascending within node) |
| 0x04 | 4 | uint32_le | Type (always 8) |
| 0x08 | 4 | uint32_le | cumulative_size_a — subtree size statistic (NOT a blob page pointer; correlates with size_a at 69%) |
| 0x0C | 4 | uint32_le | cumulative_size_b — subtree size statistic (matches record IDs at 39%) |
| 0x10 | 4 | uint32_le | Reserved (always 0) |

### B-tree Structure

| Property | Value |
|----------|-------|
| Typical index nodes | ~82 (62 internal, 20 leaf) |
| Entries per node | 1--2 (sparse; capacity for ~24) |
| Keys sorted within node | 83% ascending |
| Level 1 (leaf) | val1 may reference child index pages |
| Level 2 (internal) | Contains range keys for subtree navigation |

Entry fields are `[key, type=8, cumulative_size_a, cumulative_size_b, 0]` — these encode **subtree statistics** rather than direct page pointers. The `cumulative_size_a` field correlates with record `size_a` values at 69%, confirming it tracks aggregate compressed sizes. The tree navigation mechanism uses range-based key comparison rather than explicit child page pointers. Full traversal is not yet implemented.

### Statistics

| Metric | Value |
|--------|-------|
| Index pages | ~37 |
| Index nodes across all pages | ~82 |
| Data records found on index pages | ~61 |
| Dense index region | Pages 4573--5489 (all 8 slots are index nodes) |

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

Timestamps are stored as **.NET ticks** — `int64` little-endian values representing 100-nanosecond intervals since **0001-01-01 00:00:00 UTC**. This is the same encoding as C# `DateTime.Ticks`.

**NOT** Cocoa epoch, **NOT** Unix epoch, **NOT** Windows FILETIME.

| Property | Value |
|----------|-------|
| Encoding | int64_le (8 bytes) |
| Epoch | 0001-01-01 00:00:00 UTC |
| Unit | 100-nanosecond intervals (10,000,000 per second) |
| Precision | Whole seconds (sub-second remainder is always 0) |
| Extraction rate | 100% across all 669 email records |

Timestamps are embedded in a **48-byte sentinel block** within the decompressed record:

```
+0x00  8B  Sync/modification timestamp (.NET ticks)
+0x08  8B  Sentinel: FF 3F 37 F4 75 28 CA 2B
+0x10  8B  displayTime (.NET ticks) — the email send date
+0x18  8B  Sentinel (repeated)
+0x20  8B  Sentinel (repeated)
+0x28  8B  Sentinel (repeated)
```

The sentinel value `0x2BCA2875F4373FFF` is a **null/unset marker** (overflows as a date). Its lower 4 bytes (`0x2BCA2875` = 734,668,917) were previously misidentified as a Cocoa "schema date."

Conversion:
```python
from datetime import datetime, timedelta, timezone

DOTNET_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)
DOTNET_TICKS_PER_SECOND = 10_000_000

dt = DOTNET_EPOCH + timedelta(seconds=ticks / DOTNET_TICKS_PER_SECOND)
```

### Property Encoding

Properties within decompressed records are **not** stored in a standard TLV, protobuf, or msgpack format. The data uses a proprietary serialization that interleaves ASCII strings, UTF-16LE strings, and raw binary data. Section boundaries are marked by the byte sequence `00 01 00 00 00 00 01`.

The property format has not been fully decoded. Current extraction relies on heuristic regex methods (see [Current Capabilities & Limitations](#16-current-capabilities--limitations)).

---

## 13. Companion Files

### hxcore.hfl

| Property | Value |
|----------|-------|
| Location | `<Profile>/hxcore.hfl` |
| Format | **Binary log format** (not Nostromoi) |
| Typical size | ~50 MB |
| Purpose | Binary log or auxiliary index for HxCore |

The file does not begin with the Nostromoi magic. First 8 bytes: `08 00 00 00 00 00 01 00`. Binary log format — contains version strings (e.g., `16.0.19819.33806`) and a DEADBEEF marker, but uses a completely different internal structure from HxStore.hxd.

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

### Object Type System

Cola objects are typed by integer IDs. The following types have been identified:

| ObjectType | Value | Description |
|------------|-------|-------------|
| Account | 73 | Email account |
| View | 77 | Mail folder/view |
| Calendar | 104 | Calendar |
| Appointment | 107 | Calendar item |
| MessageHeader | 201 | Email metadata |
| DataReplication | 202 | Sync data |
| AttachmentHeader | 212 | Attachment metadata |
| Contact | 224 | Contact |
| SearchSession | 288 | Search |
| Person | 359 | Person record |

**Key relationships:** A MessageHeader has children (Recipients, CcRecipients, BccRecipients, AttachmentHeaders) and links to a MessageData object via `_messageDataId`.

### Storage Layers

Cola sits on a **KeyValueStore** layer comprising:

| Component | Purpose |
|-----------|---------|
| `KeyValueBlock` | Fixed-size storage blocks |
| `KeyDirectory` | Key-to-block mapping |
| `Blob` | Large value storage |
| `Transaction` | ACID transaction support |

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
DOTNET_TICKS_PER_SECOND = 10_000_000                   # 100-nanosecond intervals per second
DOTNET_SENTINEL     = b"\xff\x3f\x37\xf4\x75\x28\xca\x2b"  # Null/unset timestamp marker
DOTNET_TICKS_MIN    = 633_979_008_000_000_000  # ~2010-01-01
DOTNET_TICKS_MAX    = 642_297_024_000_000_000  # ~2030-01-01
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
- **Timestamp extraction** — .NET ticks decoded from 48-byte sentinel blocks (100% extraction rate)
- **Full-text search** across decompressed records and blob pages (UTF-8 + UTF-16LE)
- **HTML body extraction** from blob pages
- **Record enumeration**, ID-based lookup, and format type classification
- **Blob page decompression** for type-4 (LZ4) blob pages (99.5% success with standard library)
- **Record format type classification** distinguishing emails, metadata, entities, and rich content
- **Folder/mailbox mapping** -- 100% resolution of email-to-folder membership via 16-byte reference pattern at offset 1520 in 0x10013 records. 12 folders discovered including Inbox, Sent Mail, Drafts, Spam, Archive, Trash, and custom folders.
- **ObjectType classification** -- uint16 at offset 44 in 0x10013 records distinguishes emails (0xBF), folders (0x4D), contacts (0xE0), calendar items (0x68), and search sessions (0x120)
- **Incoming/outgoing mail filtering** -- folder membership enables direction-based filtering (Sent Mail = outgoing, all others = incoming)

### What Does Not Work (Yet)

**Body resolution** now works for **59% of emails** via 5 tiers (inline record, blob page, object stream, EFMData .dat file, and heuristic HTML scan). The remaining ~40% are **un-synced bodies** (confirmed by the `_bodyDownloadStatus` property indicating the body was never downloaded from the server). **Timestamps** are now fully decoded (.NET ticks in a 48-byte sentinel block — see [Section 12](#12-data-encoding-details)).

| Gap | Description |
|-----|-------------|
| Structured property decoding | The property format within decompressed records is not decoded as structured TLV. Relies on heuristic regex extraction. |
| ~~Folder/label mapping~~ | **SOLVED** -- see [Folder/Mailbox Mapping](#foldermailbox-mapping). 100% resolution via 16-byte reference pattern at offset 1520. |
| Attachment-to-record association | Attachments exist on the filesystem but cannot be programmatically linked to their parent records. |
| Index page traversal | B-tree index pages are identified but not parsed; lookups require full sequential scan. |
| Calendar event fields | Start/end times, location, attendees are not mapped. |
| Recipient differentiation | To/CC/BCC classification is heuristic only. |

---

## 17. Approach to Further Reverse Engineering

1. **Differential analysis**: Create an email with known content, sync Outlook, compare the HxStore before and after to isolate the new record and map exact byte offsets to known fields.

2. **Index page parsing**: Decode the Cola B-tree structure to enable efficient lookups instead of sequential scans. The `Cola::IndexPage` and `Cola::KvStoreReader` symbols provide clues about the node format.

3. **Property tag identification**: Microsoft Exchange uses MAPI property tags (e.g., `PR_SUBJECT = 0x0037`, `PR_SENDER_EMAIL_ADDRESS = 0x0C1F`). Scanning for known tag values near property data could reveal the encoding scheme.

4. **Section marker analysis**: The `00 01 00 00 00 00 01` section markers divide records into logical groups. Systematic analysis of content between markers across many records could reveal the property layout.

5. **Cross-platform comparison**: Windows Mail uses the same HxStore.hxd format (magic "Nostromoh"). Comparing Mac and Windows files could clarify platform-specific vs. universal structures.

6. **Compaction/WAL analysis**: The file may have write-ahead log mechanics. Observing the file across multiple sync cycles could reveal these patterns.

7. **Dynamic analysis**: Using LLDB or Frida to hook `Hx::Compressor::CopyStreamToCompressed_LZ4` and `Cola::StoreObject` methods at runtime could reveal the exact serialization format used when writing records.

8. **Property offset mapping via `_messageDataId`**: The `_messageDataId` field appears at known offsets (632, 652, 848) in decompressed 0x10013 records. Cross-referencing these positions with other known field values (subject, sender, dates) across many records could build a proper property offset map, replacing heuristic extraction with structured field decoding.

---

## References

- **Boncaldo's Forensics Blog (2018)**: First public research on HxStore.hxd. Identified the "Nostromoh" magic header (Windows variant). Noted the format is undocumented.
- **"Navigating the Windows Mail database"** (ScienceDirect): Academic forensics paper analyzing the format.
- **LZ4 Block Format Specification**: https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md

No other public documentation of the Nostromoi/HxStore internal format is known to exist as of March 2026.
