"""Per-attempt workspace: safe input materialization, artifact collection, cleanup.

Relative logical names are the only addressing model code ever sees; they are
validated before any filesystem access and can never escape the workspace root.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path, PurePosixPath

MAX_INPUT_FILES = 50
MAX_INPUT_FILE_BYTES = 1_048_576
MAX_ARTIFACT_FILES = 100
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
# Runtime noise never published as an artifact.
EXCLUDED_PARTS = {"__pycache__", ".tmp"}


class WorkspaceError(Exception):
    pass


def check_logical_name(name: str) -> PurePosixPath:
    """Validate a relative logical path used for files inside one task workspace."""
    if not isinstance(name, str) or not name:
        raise WorkspaceError("file name must be a non-empty string")
    if "\\" in name or "\x00" in name:
        raise WorkspaceError(f"invalid file name: {name!r}")
    pure = PurePosixPath(name)
    if pure.as_posix() != name:
        raise WorkspaceError(f"file name must be a plain relative path: {name!r}")
    if pure.is_absolute() or not pure.parts or any(part in {"..", "."} for part in pure.parts):
        raise WorkspaceError(f"file name must stay inside the workspace: {name!r}")
    return pure


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, state_dir: Path, attempt_id: str) -> Workspace:
        root = state_dir / "workspaces" / attempt_id
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        (root / ".tmp").mkdir(mode=0o700)
        return cls(root)

    def resolve(self, name: str) -> Path:
        pure = check_logical_name(name)
        resolved = (self.root / pure).resolve()
        root_resolved = self.root.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise WorkspaceError(f"path escapes the workspace: {name!r}")
        return resolved

    def materialize_input(self, files: dict[str, str]) -> list[str]:
        """Write the task's initial files as UTF-8 bytes; returns the sorted names."""
        for name in sorted(files):
            content = files[name]
            data = content.encode("utf-8")
            if len(data) > MAX_INPUT_FILE_BYTES:
                raise WorkspaceError(f"initial file too large: {name}")
            target = self.resolve(name)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_bytes(data)
        return sorted(files)

    def read_text(self, name: str, max_bytes: int) -> str:
        target = self.resolve(name)
        if not target.is_file():
            raise WorkspaceError(f"not a file: {name}")
        data = target.read_bytes()
        if len(data) > max_bytes:
            raise WorkspaceError(f"file exceeds {max_bytes} bytes: {name}")
        return data.decode("utf-8", errors="replace")

    def list_files(self, limit: int = 1000) -> list[str]:
        found: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            found.append(relative.as_posix())
            if len(found) >= limit:
                break
        return found

    def collect_artifacts(self) -> list[tuple[str, bytes]]:
        """Actual files written by this run, as (logical name, bytes)."""
        collected: list[tuple[str, bytes]] = []
        for name in self.list_files():
            data = (self.root / name).read_bytes()
            if len(data) > MAX_ARTIFACT_BYTES:
                continue
            collected.append((name, data))
            if len(collected) >= MAX_ARTIFACT_FILES:
                break
        return collected

    def cleanup(self) -> bool:
        try:
            shutil.rmtree(self.root)
            return True
        except OSError:
            return False


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
