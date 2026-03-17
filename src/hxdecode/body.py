"""Email body extraction from HxStore records.

Provides functions to extract HTML and plain-text email bodies from
decompressed record data, and to link records via message-IDs to find
the best available body content.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Iterator

from hxdecode.decompress import decompress_record
from hxdecode.extract import extract_utf16le_strings
from hxdecode.parser import HxStoreFile, RawRecord


@dataclass
class EmailBody:
    """Extracted email body content."""

    record_id: int
    html: str
    text: str
    source: str  # "inline", "sibling", "preview"


# Formats that carry inline HTML bodies
_HTML_FORMATS = {0x03B0, 0x03B1, 0x310F4, 0x30FF1}


def extract_html_body(decompressed: bytes) -> str | None:
    """Extract the HTML body from decompressed record data.

    Returns the HTML string, or None if no HTML body is found.
    """
    start = decompressed.find(b"<html")
    if start == -1:
        start = decompressed.find(b"<HTML")
    if start == -1:
        return None

    end = decompressed.find(b"</html>", start)
    if end == -1:
        end = decompressed.find(b"</HTML>", start)
    if end == -1:
        # Truncated HTML — take what we have
        end = len(decompressed)
    else:
        end += 7  # include </html>

    return decompressed[start:end].decode("utf-8", errors="replace")


def html_to_text(html: str) -> str:
    """Simple HTML-to-text conversion by stripping tags."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</div>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_body_preview(utf16_strings: list[str], format_type: int) -> str:
    """Extract the body preview text from UTF-16LE strings.

    The body preview is a short plain-text excerpt stored in the record.
    """
    if "IPM.Note" not in utf16_strings:
        return ""

    ipm_idx = utf16_strings.index("IPM.Note")
    after = utf16_strings[ipm_idx + 1:]

    if format_type in (0x03B0, 0x03B1):
        # After 2nd IPM.Note: msg_ids -> body_preview -> subject
        ipm_positions = [i for i, s in enumerate(utf16_strings) if s == "IPM.Note"]
        if len(ipm_positions) >= 2:
            after2 = utf16_strings[ipm_positions[1] + 1:]
            pos = 0
            for s in after2:
                if s.startswith("<") and "@" in s:
                    pos += 1
                else:
                    break
            remaining = after2[pos:]
            if remaining:
                return remaining[0]
    elif format_type == 0x0191:
        # IPM.Note -> sender_email -> sender_email -> body_preview
        if len(after) >= 3:
            return after[2]
    else:
        # 0x10013: IPM.Note -> msg_ids -> body_preview
        pos = 0
        for s in after:
            if s.startswith("<") and "@" in s:
                pos += 1
            else:
                break
        remaining = after[pos:]
        if remaining:
            return remaining[0]

    return ""


class BodyIndex:
    """Index for efficient email body lookup.

    Builds an in-memory index mapping message-IDs to records
    with HTML bodies, enabling cross-record body resolution.
    """

    def __init__(self, store: HxStoreFile) -> None:
        self._store = store
        # message_id -> (record_id, decompressed_bytes)
        self._html_by_msgid: dict[str, tuple[int, bytes]] = {}
        # record_id -> decompressed_bytes (for records with inline HTML)
        self._html_by_rid: dict[int, bytes] = {}
        # record_id -> (format_type, decompressed_bytes, utf16_strings)
        self._all_records: dict[int, tuple[int, bytes, list[str]]] = {}
        self._built = False

    def build(self) -> None:
        """Scan all records and build the body index."""
        if self._built:
            return

        for rec in self._store.iter_data_records():
            raw_ft = struct.unpack_from("<I", rec.raw_data, 8)[0]
            rid, decompressed = decompress_record(
                rec.raw_data, rec.slot_header.size_b
            )
            utf16 = extract_utf16le_strings(decompressed)
            self._all_records[rid] = (raw_ft, decompressed, utf16)

            has_html = b"<html" in decompressed or b"<HTML" in decompressed
            if has_html and raw_ft in _HTML_FORMATS:
                self._html_by_rid[rid] = decompressed
                # Index by message-ID
                for s in utf16:
                    if s.startswith("<") and "@" in s and s.endswith(">"):
                        self._html_by_msgid[s] = (rid, decompressed)

        self._built = True

    def get_body(self, record_id: int) -> EmailBody | None:
        """Get the best available body for a record.

        Tries in order:
        1. Inline HTML body in this record
        2. HTML body from a sibling record (same message-ID)
        3. Body preview text from UTF-16LE strings
        """
        self.build()

        if record_id not in self._all_records:
            return None

        raw_ft, decompressed, utf16 = self._all_records[record_id]

        # 1. Inline HTML
        if record_id in self._html_by_rid:
            html = extract_html_body(decompressed)
            if html:
                return EmailBody(
                    record_id=record_id,
                    html=html,
                    text=html_to_text(html),
                    source="inline",
                )

        # 2. Sibling via message-ID
        for s in utf16:
            if s.startswith("<") and "@" in s and s.endswith(">"):
                if s in self._html_by_msgid:
                    sibling_rid, sibling_data = self._html_by_msgid[s]
                    html = extract_html_body(sibling_data)
                    if html:
                        return EmailBody(
                            record_id=sibling_rid,
                            html=html,
                            text=html_to_text(html),
                            source="sibling",
                        )

        # 3. Body preview
        preview = extract_body_preview(utf16, raw_ft)
        if preview:
            return EmailBody(
                record_id=record_id,
                html="",
                text=preview,
                source="preview",
            )

        return None

    def iter_bodies(self) -> Iterator[tuple[int, EmailBody]]:
        """Yield (record_id, EmailBody) for all email records."""
        self.build()
        for rid in self._all_records:
            raw_ft, decompressed, utf16 = self._all_records[rid]
            if "IPM.Note" not in utf16:
                continue
            body = self.get_body(rid)
            if body:
                yield rid, body
