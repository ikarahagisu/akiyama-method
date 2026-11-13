"""Append-only store for raw source files.

Every downloaded file is kept under a path that includes its source version
(the provider's LastModified / lastUpdated), so a re-published file never
overwrites the copy an earlier analysis used. ``manifest.jsonl`` records
what was fetched, when, its size and sha256.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


@dataclass
class RawEntry:
    kind: str  # "bulk" | "td_bulk" | "td_doc" | "api"
    key: str  # provider key (bulk key, discNo/type, ...)
    version: str  # provider version stamp (LastModified etc.)
    path: str  # relative to raw root, posix style
    size: int
    sha256: str
    fetched_at: str  # UTC ISO8601


def version_slug(version: str) -> str:
    """Filesystem-safe, sortable form of a version stamp."""
    return re.sub(r"[^0-9A-Za-z]", "", version) or "unversioned"


class RawStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.manifest = self.root / "manifest.jsonl"
        self._index: dict[tuple[str, str, str], RawEntry] | None = None

    # ------------------------------------------------------------ manifest
    def entries(self) -> Iterator[RawEntry]:
        if not self.manifest.exists():
            return
        with open(self.manifest, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield RawEntry(**json.loads(line))

    def _idx(self) -> dict[tuple[str, str, str], RawEntry]:
        if self._index is None:
            self._index = {(e.kind, e.key, e.version): e for e in self.entries()}
        return self._index

    def has(self, kind: str, key: str, version: str) -> bool:
        e = self._idx().get((kind, key, version))
        return e is not None and (self.root / e.path).exists()

    def latest(self, kind: str) -> dict[str, RawEntry]:
        """Newest stored version per key for a kind."""
        out: dict[str, RawEntry] = {}
        for (k, key, _), e in self._idx().items():
            if k != kind or not (self.root / e.path).exists():
                continue
            cur = out.get(key)
            if cur is None or version_slug(e.version) > version_slug(cur.version):
                out[key] = e
        return out

    # --------------------------------------------------------------- write
    def path_for(self, kind: str, key: str, version: str, suffix: str) -> Path:
        key_path = key.strip("/").replace(":", "_")
        return self.root / kind / key_path / f"{version_slug(version)}{suffix}"

    def record(self, kind: str, key: str, version: str, path: Path, size: int, sha256: str) -> RawEntry:
        entry = RawEntry(
            kind=kind,
            key=key,
            version=version,
            path=Path(path).relative_to(self.root).as_posix(),
            size=size,
            sha256=sha256,
            fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.manifest, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
        self._idx()[(kind, key, version)] = entry
        return entry
