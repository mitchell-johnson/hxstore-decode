"""LZ4 block decompression for HxStore records.

HxStore records use LZ4 block compression (raw, no frame header).
The record data layout is:
  - record_data[0:8] = record ID (uint64_le, uncompressed)
  - record_data[8:]  = LZ4 block compressed payload
  - size_b - 8       = expected decompressed payload size

The standard lz4.block C library is strict about end-of-stream
handling and fails on ~97% of records. A lenient manual decoder
handles the remaining cases.
"""

from __future__ import annotations

import struct

try:
    import lz4.block as _lz4_block
except ImportError:
    _lz4_block = None


def decompress_record(raw_data: bytes, size_b: int) -> tuple[int, bytes]:
    """Decompress an HxStore record payload.

    Args:
        raw_data: The full record data (size_a bytes starting after
                  the 32-byte slot header).
        size_b: The uncompressed size from the slot header.

    Returns:
        Tuple of (record_id, decompressed_payload).
    """
    if len(raw_data) < 8:
        return 0, raw_data

    record_id = struct.unpack_from("<Q", raw_data, 0)[0]
    if record_id == 0 or record_id > 0xFFFFFFFF:
        record_id = struct.unpack_from("<I", raw_data, 0)[0]

    compressed = raw_data[8:]
    expected_size = max(size_b - 8, 0)

    if expected_size == 0 or len(compressed) == 0:
        return record_id, compressed

    # Try the C library first (fast)
    if _lz4_block is not None:
        try:
            result = _lz4_block.decompress(
                compressed, uncompressed_size=expected_size
            )
            return record_id, result
        except Exception:
            pass

    # Fall back to lenient manual decoder
    result, _, _ = _lz4_block_decompress_lenient(compressed, expected_size)
    return record_id, result


def _lz4_block_decompress_lenient(
    data: bytes, max_output_size: int
) -> tuple[bytes, int, str | None]:
    """Lenient LZ4 block decoder.

    Handles truncated last literals and end-of-stream edge cases
    that the strict C library rejects.

    Args:
        data: LZ4 block compressed bytes (no frame header).
        max_output_size: Maximum bytes to produce.

    Returns:
        Tuple of (output_bytes, consumed_input_bytes, error_or_none).
    """
    output = bytearray()
    i = 0

    while i < len(data) and len(output) < max_output_size:
        token = data[i]
        i += 1

        # Literal length from high nibble
        lit_len = (token >> 4) & 0x0F
        if lit_len == 15:
            while i < len(data):
                extra = data[i]
                i += 1
                lit_len += extra
                if extra != 255:
                    break

        # Copy literals (lenient: copy what's available)
        available = len(data) - i
        actual_lit = min(lit_len, available)
        output.extend(data[i : i + actual_lit])
        i += actual_lit

        if actual_lit < lit_len:
            return bytes(output), i, "truncated_literal"

        # End of input? Last sequence has no match
        if i >= len(data) or len(output) >= max_output_size:
            return bytes(output), i, None

        # Match offset (2 bytes LE)
        if i + 2 > len(data):
            return bytes(output), i, "truncated_offset"

        match_offset = struct.unpack_from("<H", data, i)[0]
        i += 2

        if match_offset == 0:
            return bytes(output), i, "zero_offset"

        # Match length from low nibble + 4
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while i < len(data):
                extra = data[i]
                i += 1
                match_len += extra
                if extra != 255:
                    break

        if match_offset > len(output):
            return bytes(output), i, f"bad_offset:{match_offset}>{len(output)}"

        # Copy match bytes (byte-by-byte for overlapping copies)
        remaining = max_output_size - len(output)
        to_copy = min(match_len, remaining)
        for _ in range(to_copy):
            output.append(output[-match_offset])

    return bytes(output), i, None
