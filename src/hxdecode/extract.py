"""String and value extraction from decompressed HxStore record data.

Records are LZ4-compressed. After decompression, properties are extracted
using heuristic regex methods (the internal property encoding is not yet
fully decoded as structured TLV).
"""

from __future__ import annotations

import re
import struct
from datetime import datetime, timezone
from typing import Callable

from hxdecode.constants import COCOA_EPOCH_OFFSET, COCOA_TS_MAX, COCOA_TS_MIN

# ---------------------------------------------------------------------------
# Month name lookup for date parsing
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ---------------------------------------------------------------------------
# Plausible year range for content-extracted dates
# ---------------------------------------------------------------------------

_YEAR_MIN = 2015
_YEAR_MAX = 2027

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
    """Extract plausible Cocoa-epoch timestamps from record data.

    Scans for uint32_le values in the Cocoa timestamp range (2010-2030).
    Filters out values that appear at the same position across many records
    (schema/creation dates) by requiring timestamps to not be in a known
    set of common false positives.

    Args:
        data: Decompressed record payload bytes.

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
# Content-based date extraction strategies
# ---------------------------------------------------------------------------

# Strategy 1: 13-digit Unix millisecond timestamps in Message-ID strings.
# Example: <83236174.457198108.1767749130069@email.apple.com>
# The 1767749130069 is Unix ms -> 2026-01-07 01:25:30 UTC.
_MSGID_RE = re.compile(r"<[^>]*?(\d{13})[^>]*?@[^>]+>")


def _extract_msgid_timestamp(
    decompressed: bytes, utf16_strings: list[str]
) -> datetime | None:
    """Extract a date from a 13-digit Unix-ms timestamp in a Message-ID."""
    for s in utf16_strings:
        m = _MSGID_RE.match(s)
        if m:
            ts_ms = int(m.group(1))
            ts_s = ts_ms / 1000.0
            try:
                dt = datetime.fromtimestamp(ts_s, tz=timezone.utc)
                if _YEAR_MIN <= dt.year <= _YEAR_MAX:
                    return dt
            except (OSError, OverflowError, ValueError):
                continue
    return None


# Strategy 2: Date strings in body preview / forwarded headers.
# Matches "Date: Thu, 2 Nov 2017" or "On Mon, 25 Oct 2017".
_BODY_DATE_RE = re.compile(
    r"(?:Date|On)\s*[,:]\s*"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    r"(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
    r"(\d{4})",
    re.IGNORECASE,
)

# Optional time part following the date, e.g. "19:10" or "7:30"
_BODY_TIME_RE = re.compile(
    r"(?:Date|On)\s*[,:]\s*"
    r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    r"\d{1,2}\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
    r"\d{4},?\s+"
    r"(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)


def _extract_body_date(
    decompressed: bytes, utf16_strings: list[str]
) -> datetime | None:
    """Extract a date from 'Date:' or 'On ...' lines in the body preview."""
    all_text = " ".join(utf16_strings)
    m = _BODY_DATE_RE.search(all_text)
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = _MONTH_NAMES.get(m.group(2).lower()[:3])
        year = int(m.group(3))
        if month is None or not (_YEAR_MIN <= year <= _YEAR_MAX):
            return None
        hour, minute = 0, 0
        tm = _BODY_TIME_RE.search(all_text)
        if tm:
            hour, minute = int(tm.group(1)), int(tm.group(2))
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


# Strategy 3: Compact timestamps in subject lines.
# Example: "Version 1.0 (202601070121)" -> YYYYMMDDHHSS format.
_SUBJECT_DATE_RE = re.compile(r"\((\d{4})(\d{2})(\d{2})(\d{2})(\d{2})\)")


def _extract_subject_date(
    decompressed: bytes, utf16_strings: list[str]
) -> datetime | None:
    """Extract a YYYYMMDDHHSS date from parenthesized timestamps in subjects."""
    for s in utf16_strings:
        m = _SUBJECT_DATE_RE.search(s)
        if m:
            try:
                year = int(m.group(1))
                month = int(m.group(2))
                day = int(m.group(3))
                hour = int(m.group(4))
                minute = int(m.group(5))
                if not (_YEAR_MIN <= year <= _YEAR_MAX):
                    continue
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            except (ValueError, OverflowError):
                continue
    return None


# Strategy 4: Date: header in inline HTML (forwarded email headers).
# Same pattern as body date but searched in raw decompressed bytes.
_HTML_DATE_RE = re.compile(
    rb"Date:\s*"
    rb"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    rb"(\d{1,2})\s+"
    rb"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
    rb"(\d{4})",
    re.IGNORECASE,
)

_HTML_TIME_RE = re.compile(
    rb"Date:\s*"
    rb"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    rb"\d{1,2}\s+"
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
    rb"\d{4},?\s+"
    rb"(\d{1,2}):(\d{2})",
    re.IGNORECASE,
)


def _extract_html_date(
    decompressed: bytes, utf16_strings: list[str]
) -> datetime | None:
    """Extract a Date: header from inline HTML in the decompressed data."""
    m = _HTML_DATE_RE.search(decompressed)
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = _MONTH_NAMES.get(m.group(2).decode("ascii").lower()[:3])
        year = int(m.group(3))
        if month is None or not (_YEAR_MIN <= year <= _YEAR_MAX):
            return None
        hour, minute = 0, 0
        tm = _HTML_TIME_RE.search(decompressed)
        if tm:
            hour, minute = int(tm.group(1)), int(tm.group(2))
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


# Strategy 5: ASCII date strings in decompressed data.
# Searches for ISO 8601 (2026-01-07), RFC 2822 (Thu, 02 Nov 2017),
# and compact dates (20261107).
_ISO_DATE_RE = re.compile(rb"(20[12]\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])")
_RFC_DATE_RE = re.compile(
    rb"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+"
    rb"(\d{1,2})\s+"
    rb"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+"
    rb"(20[12]\d)",
    re.IGNORECASE,
)
_COMPACT_DATE_RE = re.compile(rb"(20[12]\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])")


def _extract_ascii_date(
    decompressed: bytes, utf16_strings: list[str]
) -> datetime | None:
    """Extract a date from ASCII date patterns in the decompressed payload."""
    # Try RFC 2822 dates first (most specific)
    for m in _RFC_DATE_RE.finditer(decompressed):
        try:
            day = int(m.group(1))
            month = _MONTH_NAMES.get(m.group(2).decode("ascii").lower()[:3])
            year = int(m.group(3))
            if month is None or not (_YEAR_MIN <= year <= _YEAR_MAX):
                continue
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue

    # Then ISO 8601 dates
    for m in _ISO_DATE_RE.finditer(decompressed):
        try:
            year = int(m.group(1))
            month = int(m.group(2))
            day = int(m.group(3))
            if not (_YEAR_MIN <= year <= _YEAR_MAX):
                continue
            return datetime(year, month, day, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            continue

    # Compact dates are the least specific -- high false positive risk from
    # non-date digit sequences, so skip them (they added very few matches
    # in testing and would need careful filtering).
    return None


# Ordered list of content-date extraction strategies (highest priority first).
_CONTENT_DATE_STRATEGIES: list[
    tuple[str, Callable[[bytes, list[str]], datetime | None]]
] = [
    ("msgid_timestamp", _extract_msgid_timestamp),
    ("body_date", _extract_body_date),
    ("subject_date", _extract_subject_date),
    ("html_date", _extract_html_date),
    ("ascii_date", _extract_ascii_date),
]


def extract_content_date(
    decompressed: bytes, utf16_strings: list[str]
) -> datetime | None:
    """Extract the send/receive date from email record content.

    Tries multiple extraction strategies in priority order, returning
    the first successful result:

    1. **Message-ID timestamp**: 13-digit Unix-ms values embedded in
       Message-ID strings (e.g. ``1767749130069``).
    2. **Body preview dates**: ``Date:`` or ``On ...`` lines with
       RFC 2822-style day-month-year dates.
    3. **Subject line dates**: Compact ``(YYYYMMDDHHSS)`` timestamps
       in subject lines (e.g. from Apple build notifications).
    4. **HTML Date: header**: ``Date:`` headers in inline HTML from
       forwarded emails (0x03B0 records).
    5. **ASCII date strings**: ISO 8601, RFC 2822, or compact date
       patterns found anywhere in the decompressed payload.

    Args:
        decompressed: Decompressed record payload bytes.
        utf16_strings: Pre-extracted UTF-16LE strings from the payload.

    Returns:
        A timezone-aware UTC datetime, or ``None`` if no date could be
        extracted from the content.
    """
    for _name, strategy in _CONTENT_DATE_STRATEGIES:
        result = strategy(decompressed, utf16_strings)
        if result is not None:
            return result
    return None


# ---------------------------------------------------------------------------
# .NET ticks timestamp extraction (definitive)
# ---------------------------------------------------------------------------

# Sentinel value that brackets displayTime in the 48-byte timestamp block.
# Structure: [8B sync_time] [8B sentinel] [8B displayTime] [8B sentinel] [8B sentinel] [8B sentinel]
_DOTNET_SENTINEL = b"\xff\x3f\x37\xf4\x75\x28\xca\x2b"
_TICKS_PER_SECOND = 10_000_000
_DOTNET_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)

# Plausible .NET ticks range: 2010-01-01 to 2030-01-01
_TICKS_MIN = 633_979_008_000_000_000  # 2010-01-01
_TICKS_MAX = 642_297_024_000_000_000  # 2030-01-01


def _ticks_to_datetime(ticks: int) -> datetime:
    """Convert .NET ticks to a datetime."""
    from datetime import timedelta
    return _DOTNET_EPOCH + timedelta(seconds=ticks / _TICKS_PER_SECOND)


def extract_dotnet_timestamp(data: bytes) -> datetime | None:
    """Extract the displayTime from the .NET ticks sentinel block.

    HxStore records contain a 48-byte timestamp block where the
    displayTime is stored as a .NET ticks int64 (100-nanosecond
    intervals since 0001-01-01 UTC). The block is identified by a
    sentinel value ``FF 3F 37 F4 75 28 CA 2B`` that brackets the
    timestamp.

    Args:
        data: Decompressed record payload bytes.

    Returns:
        The email's display/send time, or None if the sentinel block
        is not found.
    """
    pos = 0
    while True:
        idx = data.find(_DOTNET_SENTINEL, pos)
        if idx == -1:
            break

        # The displayTime is the 8 bytes immediately AFTER the sentinel
        ts_offset = idx + 8
        if ts_offset + 8 > len(data):
            break

        ticks = struct.unpack_from("<q", data, ts_offset)[0]

        # Verify it's in plausible range
        if _TICKS_MIN <= ticks <= _TICKS_MAX:
            # Verify: next 8 bytes should also be the sentinel
            verify_offset = ts_offset + 8
            if verify_offset + 8 <= len(data):
                if data[verify_offset : verify_offset + 8] == _DOTNET_SENTINEL:
                    try:
                        return _ticks_to_datetime(ticks)
                    except (OSError, OverflowError, ValueError):
                        pass

        pos = idx + 1

    return None


def extract_display_time(
    data: bytes,
    utf16_strings: list[str] | None = None,
) -> datetime | None:
    """Extract the display/send time for an email record.

    Tries in order:
    1. .NET ticks sentinel block (100% hit rate on email records)
    2. Content-based extraction (message-IDs, body dates, subject dates)
    3. Cocoa median heuristic (least accurate fallback)

    Args:
        data: Decompressed record payload bytes.
        utf16_strings: Optional pre-extracted UTF-16LE strings.

    Returns:
        A single datetime, or None if no plausible timestamp is found.
    """
    # 1. .NET ticks (definitive, 100% hit rate)
    dotnet_ts = extract_dotnet_timestamp(data)
    if dotnet_ts is not None:
        return dotnet_ts

    # 2. Content-based extraction (accurate for emails with parseable dates)
    if utf16_strings is None:
        utf16_strings = extract_utf16le_strings(data)
    content_date = extract_content_date(data, utf16_strings)
    if content_date is not None:
        return content_date

    # 3. Cocoa median heuristic (least accurate)
    return _extract_cocoa_median(data)


def _extract_cocoa_median(data: bytes) -> datetime | None:
    """Extract a date via the Cocoa-epoch median heuristic (fallback).

    Scans for uint32_le values in the Cocoa timestamp range, excludes
    known schema dates, and returns the median.

    Args:
        data: Decompressed record payload bytes.

    Returns:
        A single datetime, or None if no plausible timestamp is found.
    """
    # Well-known false positive: appears in all 0x10013 records at fixed offsets
    SCHEMA_DATE = 734668917  # 2024-04-13 02:41:57

    candidates: list[int] = []
    seen: set[int] = set()

    for offset in range(0, len(data) - 3, 4):
        val = struct.unpack_from("<I", data, offset)[0]
        if COCOA_TS_MIN <= val <= COCOA_TS_MAX and val not in seen:
            seen.add(val)
            if val != SCHEMA_DATE:
                candidates.append(val)

    if not candidates:
        return None

    # Use the median to filter out outlier noise
    candidates.sort()
    median_val = candidates[len(candidates) // 2]
    unix_ts = median_val + COCOA_EPOCH_OFFSET
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


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
