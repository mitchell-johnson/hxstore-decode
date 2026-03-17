"""hxdecode - Decoder for Microsoft Outlook for Mac HxStore.hxd binary database.

Parses the proprietary HxStore.hxd page-based binary format, decompresses
LZ4-encoded records, and extracts email metadata (sender, subject, timestamps)
using heuristic pattern matching.
"""

__version__ = "0.1.0"
