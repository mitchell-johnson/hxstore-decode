"""Data models for decoded HxStore records."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Record:
    """A raw decoded record from the HxStore database.

    Contains the record ID, format type indicator, the raw (compressed)
    payload bytes, and any strings extracted heuristically from the
    compressed stream.
    """

    record_id: int
    format_type: int
    raw_data: bytes
    strings: list[str] = field(default_factory=list)

    @property
    def size_compressed(self) -> int:
        return len(self.raw_data)


@dataclass
class Email:
    """An email record extracted from HxStore.

    Fields are populated on a best-effort basis from heuristic extraction
    of the compressed record data.  Any field may be empty if the value
    was not recoverable from the compressed stream.
    """

    record_id: int
    sender_email: str = ""
    sender_name: str = ""
    subject: str = ""
    timestamp: datetime | None = None
    body_preview: str = ""
    raw_data: bytes = b""
