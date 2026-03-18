"""Folder mapping for HxStore email records.

Extracts folder definitions from container records (format 0x0190) and
resolves email-to-folder membership using a 16-byte reference pattern
at a fixed offset in decompressed 0x10013 email records.

Architecture
------------
The Cola engine stores folder hierarchy in large container records
(format 0x0190) that contain paired folder entries in a linked-list
structure.  Each email record (format 0x10013, ObjectType 0xBF)
embeds a folder reference at byte offset 1520 as::

    [uint32 folder_ref] [00 00 00 00] [02 00 00 00] [uint32 account_root_id]

The ``FolderIndex`` class scans both container and email records to
build the complete ref_id -> folder_name mapping.
"""

from __future__ import annotations

import re
import struct
from typing import Iterator

from hxdecode.decompress import decompress_record
from hxdecode.parser import HxStoreFile

# Format type for account container records (hold folder definitions)
_FORMAT_CONTAINER = 0x0190

# Minimum container size to contain a folder name table (bytes)
_MIN_CONTAINER_SIZE = 10_000

# UTF-16LE string pattern (runs of 3+ printable ASCII chars)
_UTF16_RE = re.compile(rb"(?:[\x20-\x7e]\x00){3,}")

# Max distance (bytes) to search backward from a ref_id entry for a
# folder name string.
_NAME_SEARCH_RANGE = 200

# Offset in decompressed 0x10013 email records where the folder
# reference pattern starts.
_FOLDER_REF_OFFSET = 1520


def extract_folder_ref(decompressed: bytes) -> int | None:
    """Extract the folder reference ID from a decompressed email record.

    Looks for the 16-byte pattern at offset 1520::

        [uint32 folder_ref] [uint32 0] [uint32 2] [uint32 root_id]

    Args:
        decompressed: Decompressed payload of a 0x10013 email record.

    Returns:
        The folder reference ID, or None if the pattern is not found.
    """
    if len(decompressed) < _FOLDER_REF_OFFSET + 16:
        return None

    ref, zero, two = struct.unpack_from("<III", decompressed, _FOLDER_REF_OFFSET)
    if zero == 0 and two == 2 and ref > 0:
        return ref

    return None


class FolderIndex:
    """Maps email records to their containing folders.

    Usage::

        store = HxStoreFile(path)
        folders = FolderIndex(store)
        for rec in store.iter_data_records():
            ...
            name = folders.get_folder(decompressed)
    """

    def __init__(self, store: HxStoreFile) -> None:
        self._ref_to_name: dict[int, str] = {}
        self._build(store)

    @property
    def folders(self) -> dict[int, str]:
        """Return the complete ref_id -> folder_name mapping."""
        return dict(self._ref_to_name)

    def get_folder(self, decompressed: bytes) -> str | None:
        """Return the folder name for a decompressed email record.

        Args:
            decompressed: Decompressed payload bytes.

        Returns:
            Folder name string, or None if folder cannot be resolved.
        """
        ref = extract_folder_ref(decompressed)
        if ref is None:
            return None
        return self._ref_to_name.get(ref)

    def _build(self, store: HxStoreFile) -> None:
        """Scan container records to build the folder name table."""
        for rec in store.iter_data_records():
            if len(rec.raw_data) < 12:
                continue
            raw_fmt = struct.unpack_from("<I", rec.raw_data, 8)[0]
            if raw_fmt != _FORMAT_CONTAINER:
                continue

            rec_id, decompressed = decompress_record(
                rec.raw_data, rec.slot_header.size_b
            )
            if len(decompressed) < _MIN_CONTAINER_SIZE:
                continue

            # The container's own record ID is the account root ID
            self._extract_folder_names(decompressed, rec_id)

    def _extract_folder_names(self, data: bytes, root_id: int) -> None:
        """Extract folder names from a container record.

        Container records store folder entries in a linked-list structure
        where each folder has a paired entry.  The first entry of each
        pair contains the folder's own ref_id, and the second contains
        the next folder's ref_id.  By processing entries sequentially
        and allowing overwrites, the last mapping per ref_id gives the
        correct folder name.

        Args:
            data: Decompressed container record payload.
            root_id: The account root record ID.
        """
        # Find all [ref, 0, 2, root_id] patterns
        root_pattern = struct.pack("<III", 0, 2, root_id)
        entries: list[tuple[int, int]] = []  # (offset, ref_id)
        pos = 4
        while pos < len(data):
            idx = data.find(root_pattern, pos)
            if idx == -1 or idx < 4:
                break
            ref_id = struct.unpack_from("<I", data, idx - 4)[0]
            if ref_id > 0:
                entries.append((idx - 4, ref_id))
            pos = idx + 1

        # Find all UTF-16LE strings with byte positions
        strings: list[tuple[int, int, str]] = []  # (start, end, text)
        for m in _UTF16_RE.finditer(data):
            try:
                text = m.group().decode("utf-16-le").strip()
            except UnicodeDecodeError:
                continue
            if len(text) >= 2:
                strings.append((m.start(), m.end(), text))

        # For each ref_id entry, find the first UTF-16LE string (by
        # offset) that ends within _NAME_SEARCH_RANGE bytes before it.
        # Allow overwrites so that linked-list second entries get
        # corrected by the next folder's first entry.
        for entry_off, ref_id in entries:
            candidates: list[tuple[int, str]] = []
            for s_start, s_end, text in strings:
                if s_end <= entry_off and entry_off - s_start < _NAME_SEARCH_RANGE:
                    candidates.append((s_start, text))

            if candidates:
                candidates.sort()
                self._ref_to_name[ref_id] = candidates[0][1]
