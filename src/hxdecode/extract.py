"""String and value extraction from decompressed HxStore record data.

Records are LZ4-compressed. After decompression, properties are extracted
using heuristic regex methods (the internal property encoding is not yet
fully decoded as structured TLV).
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timezone

from hxdecode.constants import COCOA_EPOCH_OFFSET, COCOA_TS_MAX, COCOA_TS_MIN

# ---------------------------------------------------------------------------
# Email addresses
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(rb"[\w.+-]+@[\w.]+\.\w{2,6}")


def extract_emails(data: bytes) -> list[str]:
    """Extract email addresses from raw record data.

    Scans for ASCII email-address patterns.  Multiple addresses may be
    present in a single record (sender, recipients, CC, etc.).

    Args:
        data: Raw (compressed) record payload bytes.

    Returns:
        De-duplicated list of email address strings, preserving first-seen
        order.
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _EMAIL_RE.finditer(data):
        addr = match.group().decode("ascii", errors="replace").lower()
        if addr not in seen:
            seen.add(addr)
            result.append(addr)
    return result


# ---------------------------------------------------------------------------
# UTF-16LE strings
# ---------------------------------------------------------------------------

_UTF16LE_RE = re.compile(rb"(?:[\x20-\x7e]\x00){3,}")


def extract_utf16le_strings(data: bytes) -> list[str]:
    """Extract UTF-16LE encoded strings from raw record data.

    Microsoft applications store display names, subjects, and other text
    as UTF-16LE.  Each ASCII character occupies 2 bytes (char + 0x00).
    This function finds runs of 3 or more such character pairs.

    Args:
        data: Raw (compressed) record payload bytes.

    Returns:
        List of decoded strings.  Strings shorter than 3 characters after
        decoding are excluded.
    """
    results: list[str] = []
    for match in _UTF16LE_RE.finditer(data):
        try:
            text = match.group().decode("utf-16-le")
        except UnicodeDecodeError:
            continue
        text = text.strip()
        if len(text) >= 3:
            results.append(text)
    return results


# ---------------------------------------------------------------------------
# ASCII strings
# ---------------------------------------------------------------------------

_ASCII_RE = re.compile(rb"[\x20-\x7e]{5,}")


def extract_ascii_strings(data: bytes) -> list[str]:
    """Extract runs of printable ASCII from raw record data.

    Finds contiguous sequences of 5 or more printable ASCII bytes
    (0x20-0x7E).

    Args:
        data: Raw (compressed) record payload bytes.

    Returns:
        List of ASCII strings found.
    """
    return [m.group().decode("ascii") for m in _ASCII_RE.finditer(data)]


# ---------------------------------------------------------------------------
# Timestamps (Cocoa epoch)
# ---------------------------------------------------------------------------


def extract_timestamps(data: bytes) -> list[datetime]:
    """Extract plausible Cocoa-epoch timestamps from raw record data.

    Scans every 4-byte-aligned offset for uint32_le values that fall
    within the plausible range for dates between 2020 and 2030.

    Timestamps use the Cocoa epoch (2001-01-01 00:00:00 UTC).
    Conversion: ``unix_timestamp = cocoa_timestamp + 978307200``.

    Args:
        data: Raw (compressed) record payload bytes.

    Returns:
        List of timezone-aware UTC datetime objects, sorted chronologically.
    """
    timestamps: list[datetime] = []
    seen: set[int] = set()

    for offset in range(0, len(data) - 3, 4):
        val = struct.unpack_from("<I", data, offset)[0]
        if COCOA_TS_MIN <= val <= COCOA_TS_MAX and val not in seen:
            seen.add(val)
            unix_ts = val + COCOA_EPOCH_OFFSET
            try:
                dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
                timestamps.append(dt)
            except (OSError, OverflowError, ValueError):
                continue

    timestamps.sort()
    return timestamps


# ---------------------------------------------------------------------------
# Record ID
# ---------------------------------------------------------------------------


def extract_record_id(data: bytes) -> int:
    """Extract the record ID from the start of record data.

    The first 8 bytes are read as uint64_le.  If the result is 0 or
    exceeds 0xFFFFFFFF, the first 4 bytes are used as uint32_le instead.

    Args:
        data: Raw record data (starting at the first byte after the slot
              header).

    Returns:
        The record ID as an integer, or 0 if the data is too short.
    """
    if len(data) < 4:
        return 0

    if len(data) >= 8:
        val64 = struct.unpack_from("<Q", data, 0)[0]
        if 0 < val64 <= 0xFFFFFFFF:
            return val64

    return struct.unpack_from("<I", data, 0)[0]


# ---------------------------------------------------------------------------
# Decompression
# ---------------------------------------------------------------------------


def decompress(data: bytes, size_b: int) -> bytes:
    """Decompress an HxStore record payload using LZ4 block decompression.

    Args:
        data: Raw record data (size_a bytes after the slot header).
        size_b: Expected uncompressed size from the slot header.

    Returns:
        The decompressed payload (excluding the 8-byte record ID prefix).
    """
    from hxdecode.decompress import decompress_record
    _, decompressed = decompress_record(data, size_b)
    return decompressed
