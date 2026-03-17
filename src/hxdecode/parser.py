"""Core binary parser for HxStore.hxd files.

Provides page iteration, slot header decoding, multi-slot record assembly,
and store ID discovery.
"""

from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterator

from hxdecode.constants import (
    DATA_PAGE_TYPE,
    HXSTORE_MAGIC,
    PAGE_SIZE,
    SLOT_DATA_SIZE,
    SLOT_HEADER_SIZE,
    SLOT_SIZE,
    SLOTS_PER_PAGE,
    HEADER_BITMAP_OFFSET,
    HEADER_DATA_OFFSET,
    HEADER_TOTAL_SIZE_OFFSET,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class PageType(Enum):
    """Classification of a page based on its store ID and type field."""

    HEADER = "header"
    DATA = "data"
    INDEX = "index"
    BLOB = "blob"


@dataclass(frozen=True, slots=True)
class SlotHeader:
    """Decoded 32-byte slot header.

    Attributes:
        hash: 8-byte indexing/lookup key.
        store_id: 8-byte store identifier (must match primary for data).
        type: Page/record type (8 for data pages).
        size_a: Compressed payload size in bytes.
        size_b: Uncompressed payload size in bytes.
        unknown: Unknown field (observed values: 2, 4, 6).
        file_offset: Absolute byte offset of this slot in the file.
    """

    hash: int
    store_id: bytes
    type: int
    size_a: int
    size_b: int
    unknown: int
    file_offset: int

    @classmethod
    def from_bytes(cls, data: bytes, file_offset: int) -> SlotHeader:
        """Parse a SlotHeader from 32 raw bytes.

        Args:
            data: At least 32 bytes starting at the slot boundary.
            file_offset: Absolute offset in the file for this slot.

        Raises:
            ValueError: If *data* is shorter than 32 bytes.
        """
        if len(data) < SLOT_HEADER_SIZE:
            raise ValueError(
                f"Need {SLOT_HEADER_SIZE} bytes for slot header, got {len(data)}"
            )
        hash_val = struct.unpack_from("<Q", data, 0)[0]
        store_id = data[8:16]
        rec_type = struct.unpack_from("<I", data, 16)[0]
        size_a = struct.unpack_from("<I", data, 20)[0]
        size_b = struct.unpack_from("<I", data, 24)[0]
        unknown = struct.unpack_from("<I", data, 28)[0]
        return cls(
            hash=hash_val,
            store_id=store_id,
            type=rec_type,
            size_a=size_a,
            size_b=size_b,
            unknown=unknown,
            file_offset=file_offset,
        )


@dataclass(frozen=True, slots=True)
class RawRecord:
    """A raw record extracted from one or more contiguous slots.

    Attributes:
        slot_header: The header from the first slot of this record.
        raw_data: The ``size_a`` bytes of compressed payload, potentially
                  spanning multiple slots.
    """

    slot_header: SlotHeader
    raw_data: bytes


@dataclass(frozen=True, slots=True)
class FileHeader:
    """Decoded fields from the HxStore header page (page 0).

    Attributes:
        magic: The 9-byte magic string (should be ``b"Nostromoi"``).
        total_size: Value of the total-size field (may not match actual file size).
        bitmap_offset: Byte offset of the bitmap/allocation region.
        data_offset: Byte offset of the data region.
    """

    magic: bytes
    total_size: int
    bitmap_offset: int
    data_offset: int


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


class HxStoreFile:
    """Read-only parser for an HxStore.hxd file.

    Usage::

        hx = HxStoreFile("/path/to/HxStore.hxd")
        for rec in hx.iter_data_records():
            print(rec.slot_header.size_a, rec.raw_data[:20])
        hx.close()

    Or as a context manager::

        with HxStoreFile(path) as hx:
            ...
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: bytes = b""
        self._header: FileHeader | None = None
        self._primary_store_id: bytes | None = None
        self._secondary_store_id: bytes | None = None

        self._load()

    # -- lifecycle -----------------------------------------------------------

    def _load(self) -> None:
        """Read the entire file into memory and validate the magic bytes."""
        self._data = self._path.read_bytes()
        if len(self._data) < PAGE_SIZE:
            raise ValueError(
                f"File too small ({len(self._data)} bytes); "
                f"need at least one page ({PAGE_SIZE} bytes)"
            )
        magic = self._data[:len(HXSTORE_MAGIC)]
        if magic != HXSTORE_MAGIC:
            raise ValueError(
                f"Bad magic: expected {HXSTORE_MAGIC!r}, got {magic!r}"
            )
        self._header = self._parse_header()

    def close(self) -> None:
        """Release the in-memory file data."""
        self._data = b""

    def __enter__(self) -> HxStoreFile:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- properties ----------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def file_size(self) -> int:
        return len(self._data)

    @property
    def num_pages(self) -> int:
        return len(self._data) // PAGE_SIZE

    @property
    def header(self) -> FileHeader:
        if self._header is None:
            raise RuntimeError("File not loaded")
        return self._header

    @property
    def primary_store_id(self) -> bytes:
        """The primary store ID (data pages). Discovered lazily."""
        if self._primary_store_id is None:
            self._discover_store_ids()
        assert self._primary_store_id is not None
        return self._primary_store_id

    @property
    def secondary_store_id(self) -> bytes:
        """The secondary store ID (index pages). Discovered lazily."""
        if self._secondary_store_id is None:
            self._discover_store_ids()
        assert self._secondary_store_id is not None
        return self._secondary_store_id

    # -- header parsing ------------------------------------------------------

    def _parse_header(self) -> FileHeader:
        """Decode the header page (page 0)."""
        d = self._data
        magic = d[:len(HXSTORE_MAGIC)]
        total_size = struct.unpack_from("<Q", d, HEADER_TOTAL_SIZE_OFFSET)[0]
        bitmap_offset = struct.unpack_from("<Q", d, HEADER_BITMAP_OFFSET)[0]
        data_offset = struct.unpack_from("<Q", d, HEADER_DATA_OFFSET)[0]
        return FileHeader(
            magic=magic,
            total_size=total_size,
            bitmap_offset=bitmap_offset,
            data_offset=data_offset,
        )

    # -- store ID discovery --------------------------------------------------

    def _discover_store_ids(self) -> None:
        """Scan pages to find the primary and secondary store IDs.

        The primary store ID is the most common 8-byte value at page+8
        among pages where the uint32 at page+16 equals DATA_PAGE_TYPE.

        The secondary store ID is the most common 8-byte value at page+8
        among non-data pages that have a non-zero store ID, excluding
        the primary.
        """
        data_ids: Counter[bytes] = Counter()
        other_ids: Counter[bytes] = Counter()
        zero = b"\x00" * 8

        # Scan first 2 MB worth of pages (or all pages if file is smaller)
        scan_pages = min(self.num_pages, (2 * 1024 * 1024) // PAGE_SIZE)

        for page_num in range(1, self.num_pages):
            offset = page_num * PAGE_SIZE
            store_id = self._data[offset + 8 : offset + 16]
            if store_id == zero:
                continue
            page_type = struct.unpack_from("<I", self._data, offset + 16)[0]

            if page_type == DATA_PAGE_TYPE:
                data_ids[store_id] += 1
            else:
                other_ids[store_id] += 1

        if not data_ids:
            raise ValueError(
                "Could not discover primary store ID: no data pages found"
            )

        self._primary_store_id = data_ids.most_common(1)[0][0]

        # Secondary: most common non-primary ID among non-data pages
        for sid, _ in other_ids.most_common():
            if sid != self._primary_store_id:
                self._secondary_store_id = sid
                break

        if self._secondary_store_id is None:
            # Fallback: use a zeroed placeholder
            self._secondary_store_id = zero

    # -- page iteration ------------------------------------------------------

    def classify_page(self, page_num: int) -> PageType:
        """Classify a page by its store ID and type field.

        Args:
            page_num: Zero-based page number.

        Returns:
            The page classification.
        """
        if page_num == 0:
            return PageType.HEADER

        offset = page_num * PAGE_SIZE
        store_id = self._data[offset + 8 : offset + 16]
        page_type = struct.unpack_from("<I", self._data, offset + 16)[0]

        if store_id == self.primary_store_id and page_type == DATA_PAGE_TYPE:
            return PageType.DATA
        if store_id == self.secondary_store_id and store_id != b"\x00" * 8:
            return PageType.INDEX
        return PageType.BLOB

    def iter_pages(
        self, page_type: PageType | None = None
    ) -> Iterator[tuple[int, PageType]]:
        """Yield ``(page_number, page_type)`` tuples.

        Args:
            page_type: If given, only yield pages of this type.
                       If ``None``, yield all pages.
        """
        for page_num in range(self.num_pages):
            ptype = self.classify_page(page_num)
            if page_type is None or ptype == page_type:
                yield page_num, ptype

    # -- slot / record parsing -----------------------------------------------

    def read_slot_header(self, page_num: int, slot_num: int) -> SlotHeader:
        """Read the 32-byte header of a specific slot.

        Args:
            page_num: Zero-based page number.
            slot_num: Slot index within the page (0-7).

        Returns:
            Decoded SlotHeader.
        """
        offset = page_num * PAGE_SIZE + slot_num * SLOT_SIZE
        return SlotHeader.from_bytes(self._data[offset : offset + SLOT_HEADER_SIZE], offset)

    def _is_valid_data_slot(self, header: SlotHeader) -> bool:
        """Check whether a slot header indicates a valid data record."""
        return (
            header.store_id == self.primary_store_id
            and header.type == DATA_PAGE_TYPE
            and header.size_a > 0
        )

    def read_record(self, page_num: int, slot_num: int) -> RawRecord | None:
        """Read a complete record starting at the given slot.

        For records where ``size_a > 480``, the payload continues
        contiguously past the slot boundary.  The data is read as a flat
        block of ``size_a`` bytes starting at ``slot_offset + 32``.

        Args:
            page_num: Zero-based page number.
            slot_num: Slot index within the page (0-7).

        Returns:
            A RawRecord if the slot contains a valid data record, otherwise
            ``None``.
        """
        header = self.read_slot_header(page_num, slot_num)
        if not self._is_valid_data_slot(header):
            return None

        data_start = header.file_offset + SLOT_HEADER_SIZE
        data_end = data_start + header.size_a

        # Guard against reading past end of file
        if data_end > len(self._data):
            return None

        raw = self._data[data_start:data_end]
        return RawRecord(slot_header=header, raw_data=raw)

    def iter_data_records(self) -> Iterator[RawRecord]:
        """Yield every valid data record in the file.

        Iterates over all data pages, checks each slot, and yields
        records.  Multi-slot records are handled transparently.

        Slots whose data has already been consumed by a preceding
        multi-slot record are skipped automatically.
        """
        for page_num, _ in self.iter_pages(PageType.DATA):
            skip_until: int = -1

            for slot_num in range(SLOTS_PER_PAGE):
                if slot_num <= skip_until:
                    continue

                record = self.read_record(page_num, slot_num)
                if record is None:
                    continue

                yield record

                # If this record spanned multiple slots, skip the
                # continuation slots.
                if record.slot_header.size_a > SLOT_DATA_SIZE:
                    total_bytes = SLOT_HEADER_SIZE + record.slot_header.size_a
                    slots_used = (total_bytes + SLOT_SIZE - 1) // SLOT_SIZE
                    skip_until = slot_num + slots_used - 1

    # -- convenience ---------------------------------------------------------

    def page_data(self, page_num: int) -> bytes:
        """Return the raw 4096 bytes of a page.

        Args:
            page_num: Zero-based page number.

        Returns:
            PAGE_SIZE bytes.
        """
        start = page_num * PAGE_SIZE
        return self._data[start : start + PAGE_SIZE]

    def stats(self) -> dict[str, int]:
        """Return a summary of page counts by type.

        Returns:
            Dict with keys ``"data"``, ``"index"``, ``"blob"``, ``"total"``.
        """
        counts: dict[str, int] = {"data": 0, "index": 0, "blob": 0, "total": 0}
        for _, ptype in self.iter_pages():
            counts["total"] += 1
            if ptype == PageType.DATA:
                counts["data"] += 1
            elif ptype == PageType.INDEX:
                counts["index"] += 1
            elif ptype == PageType.BLOB:
                counts["blob"] += 1
        return counts
