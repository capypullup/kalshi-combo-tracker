"""Kalshi combo EV tracker."""

from pathlib import Path


def source_files() -> dict[str, tuple[float, int]]:
    """Modification time and size of every module in the package."""
    root = Path(__file__).parent
    return {str(p.relative_to(root)): (p.stat().st_mtime, p.stat().st_size) for p in sorted(root.rglob("*.py"))}


# app.py compares this with the files on disk to spot a deploy that changed the
# code without re-importing it.
LOADED_FILES = source_files()
