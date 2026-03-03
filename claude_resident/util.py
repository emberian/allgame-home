"""Shared utility functions."""


def safe_filename(name: str) -> str:
    """Convert a display name to a safe filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).lower()
