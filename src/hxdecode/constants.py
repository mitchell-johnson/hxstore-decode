"""Constants for the HxStore.hxd binary format."""

# File magic bytes at offset 0x00 (9 bytes: "Nostromoi")
HXSTORE_MAGIC = b"Nostromoi"

# Page size in bytes (4096)
PAGE_SIZE = 0x1000

# Slot size in bytes (512) - each data page has 8 slots
SLOT_SIZE = 0x200

# Slot header size in bytes (32)
# Layout: 8B hash + 8B store_id + 4B type + 4B size_a + 4B size_b + 4B unknown
SLOT_HEADER_SIZE = 32

# Usable data per slot after header
SLOT_DATA_SIZE = SLOT_SIZE - SLOT_HEADER_SIZE  # 480 bytes

# Number of slots per page
SLOTS_PER_PAGE = PAGE_SIZE // SLOT_SIZE  # 8

# Data page type indicator (uint32_le at page+16 or slot+16)
DATA_PAGE_TYPE = 8

# Cocoa epoch offset: seconds between Unix epoch (1970-01-01) and Cocoa epoch (2001-01-01)
COCOA_EPOCH_OFFSET = 978307200

# Header field offsets within Page 0
HEADER_MAGIC_OFFSET = 0x00
HEADER_TOTAL_SIZE_OFFSET = 0x10
HEADER_BITMAP_OFFSET = 0x18
HEADER_DATA_OFFSET = 0x20

# Plausible Cocoa timestamp range (approx 2010-01-01 to 2030-01-01)
COCOA_TS_MIN = 284_083_200   # 2010-01-01
COCOA_TS_MAX = 915_148_800   # 2030-01-01

# ---------------------------------------------------------------------------
# .NET ticks timestamp constants
# ---------------------------------------------------------------------------

# 100-nanosecond intervals per second (C# DateTime.Ticks resolution)
DOTNET_TICKS_PER_SECOND = 10_000_000

# Sentinel value that brackets the displayTime in the 48-byte timestamp block.
# Structure: [8B sync_time] [8B sentinel] [8B displayTime] [8B sentinel] ...
DOTNET_SENTINEL = b"\xff\x3f\x37\xf4\x75\x28\xca\x2b"

# Plausible .NET ticks range: 2010-01-01 to 2030-01-01
DOTNET_TICKS_MIN = 633_979_008_000_000_000  # 2010-01-01
DOTNET_TICKS_MAX = 642_297_024_000_000_000  # 2030-01-01

# ---------------------------------------------------------------------------
# _messageDataId offsets in decompressed 0x10013 records
# ---------------------------------------------------------------------------

MSG_DATA_ID_OFFSETS = (632, 652, 848)

# ---------------------------------------------------------------------------
# Known record format types
# ---------------------------------------------------------------------------

FORMAT_TYPE_A = 0x03B0    # 944 - mail records, calendar items
FORMAT_TYPE_B = 0x10013   # 65555 - metadata, folder entries, contacts
