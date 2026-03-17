"""Profile discovery for HxStore.hxd files on macOS."""

from __future__ import annotations

import os
from pathlib import Path

from hxdecode.constants import HXSTORE_MAGIC

# Standard macOS path components for Outlook's HxStore.
_GROUP_CONTAINER = "UBF8T346G9.Office"
_OUTLOOK_SUBPATH = "Outlook/Outlook 15 Profiles"
_DEFAULT_PROFILE = "Main Profile"
_HXSTORE_FILENAME = "HxStore.hxd"


def default_hxstore_path(profile: str = _DEFAULT_PROFILE) -> Path:
    """Return the default HxStore.hxd path for the given Outlook profile.

    The standard location is:
        ~/Library/Group Containers/UBF8T346G9.Office/
            Outlook/Outlook 15 Profiles/<profile>/HxStore.hxd

    Args:
        profile: Outlook profile name. Defaults to ``"Main Profile"``.

    Returns:
        Absolute path to the expected HxStore.hxd file.
    """
    home = Path.home()
    return (
        home
        / "Library"
        / "Group Containers"
        / _GROUP_CONTAINER
        / _OUTLOOK_SUBPATH
        / profile
        / _HXSTORE_FILENAME
    )


def find_hxstore(custom_path: str | Path | None = None) -> Path:
    """Locate a valid HxStore.hxd file.

    If *custom_path* is provided, that path is used directly.
    Otherwise the default macOS location is tried.

    Args:
        custom_path: Optional explicit path to an HxStore.hxd file.

    Returns:
        Path to the HxStore.hxd file.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file exists but does not have valid magic bytes.
    """
    path = Path(custom_path) if custom_path else default_hxstore_path()

    if not path.exists():
        raise FileNotFoundError(f"HxStore.hxd not found at: {path}")

    _validate_magic(path)
    return path


def list_profiles() -> list[str]:
    """Return the names of all Outlook profiles that contain an HxStore.hxd.

    Returns:
        Sorted list of profile directory names (e.g. ``["Main Profile"]``).
    """
    profiles_dir = (
        Path.home()
        / "Library"
        / "Group Containers"
        / _GROUP_CONTAINER
        / _OUTLOOK_SUBPATH
    )

    if not profiles_dir.is_dir():
        return []

    found: list[str] = []
    try:
        for entry in profiles_dir.iterdir():
            if entry.is_dir() and (entry / _HXSTORE_FILENAME).exists():
                found.append(entry.name)
    except PermissionError:
        # Full Disk Access not granted.
        return []

    return sorted(found)


def _validate_magic(path: Path) -> None:
    """Check that *path* starts with the expected magic bytes.

    Raises:
        ValueError: If the first bytes do not match ``HXSTORE_MAGIC``.
    """
    magic_len = len(HXSTORE_MAGIC)
    with open(path, "rb") as fh:
        header = fh.read(magic_len)

    if header != HXSTORE_MAGIC:
        raise ValueError(
            f"Invalid HxStore file: expected magic {HXSTORE_MAGIC!r}, "
            f"got {header!r}"
        )
