"""Package the project for submission:  python scripts/make_zip.py [output.zip]

Leaves out installed dependencies, caches, secrets and local tooling files.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".venv", "venv", "__pycache__", ".pytest_cache", ".git", ".claude", "node_modules"}
SKIP_FILES = {".env"}
# .db*: a SQLite session store kept in the project folder holds whole conversations.
SKIP_SUFFIXES = {".pyc", ".zip", ".db", ".db-wal", ".db-shm"}


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "sop-agent.zip"
    files = [
        p
        for p in sorted(ROOT.rglob("*"))
        if p.is_file()
        and not (set(p.relative_to(ROOT).parts[:-1]) & SKIP_DIRS)
        and p.name not in SKIP_FILES
        and p.suffix not in SKIP_SUFFIXES
    ]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            zf.write(p, Path("sop-agent") / p.relative_to(ROOT))
    size_kb = out.stat().st_size / 1024
    print(f"{out} — {len(files)} files, {size_kb:.0f} KB")


if __name__ == "__main__":
    main()
