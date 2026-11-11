"""The checkout identity captured when an Edge Trader process starts."""
from __future__ import annotations

import subprocess
from pathlib import Path


def checkout_identity(root: Path) -> dict:
    """Return a commit only when Git can inspect this checkout safely."""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5).strip()
        changes = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"commit": None, "clean": False}
    return {"commit": commit or None, "clean": bool(commit) and not changes}
