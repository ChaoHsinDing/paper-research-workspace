#!/usr/bin/env python3
"""Export the portable research workspace from an explicit source allowlist."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
NAME = "paper-research-workspace"
MARKER = ".paper-research-export.json"
FORMAT = "paper-research-workspace-export-v1"
REQUIRED = (
    "AGENTS.md",
    ".agents/skills/paper-reader/SKILL.md",
    ".agents/skills/paper-reader/agents/openai.yaml",
    ".agents/skills/paper-reader/references/evidence.md",
    ".agents/skills/paper-discovery/SKILL.md",
    ".agents/skills/paper-discovery/agents/openai.yaml",
    ".agents/skills/paper-discovery/references/operations.md",
    ".codex/config.toml",
    ".codex/agents/paper_evidence.toml",
    "scripts/papers.py",
    "scripts/bootstrap.py",
    "scripts/discover.py",
    "scripts/test_papers.py",
    "scripts/test_discover.py",
    "scripts/package_research.py",
    "scripts/test_package_research.py",
    "requirements.txt",
    "discovery/interests.json",
    "docs/GITHUB.md",
    "docs/PACKAGE_README.md",
)
OPTIONAL = (
    "discovery/weekly-prompt.txt",
)
GITIGNORE = f""".DS_Store
/.venv/
__pycache__/
*.py[cod]
/.pytest_cache/
/library/
/notes/
/output/
/papers/
/tmp/
/discovery/cache/
/discovery/digests/
/discovery/runs/
*.[pP][dD][fF]
*.sqlite3*
/.env*
/.aws/
/.ssh/
/.codex/auth.json
/.codex/sessions/
/{MARKER}
"""


def existing_path(root: Path, relative: str) -> Path:
    """Reject symlinks in an included file path, including its directories."""
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Symlink is not allowed: {current.relative_to(root)}")
    return current


def source_files(root: Path) -> tuple[dict[str, bytes], list[str]]:
    agents = existing_path(root, ".codex/agents")
    agent_files = {p.relative_to(root).as_posix() for p in agents.glob("*.toml")}
    included = set(REQUIRED) | agent_files
    skipped = []
    for relative in OPTIONAL:
        if existing_path(root, relative).exists():
            included.add(relative)
        else:
            skipped.append(relative)

    private_paths = {os.fsencode(path) for path in (root, Path.home()) if path.parent != path}
    files = {}
    for relative in sorted(included):
        path = existing_path(root, relative)
        if not path.is_file():
            raise ValueError(f"Missing required source file: {relative}")
        data = path.read_bytes()
        if any(private_path in data for private_path in private_paths):
            raise ValueError(f"Machine-specific absolute path in source file: {relative}")
        files[relative] = data

    files["README.md"] = files["docs/PACKAGE_README.md"]
    files[".gitignore"] = GITIGNORE.encode("utf-8")
    return files, skipped


def marker_bytes(files: dict[str, bytes]) -> bytes:
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return (json.dumps({"format": FORMAT, "files": manifest}, sort_keys=True, indent=2) + "\n").encode()


def verify_existing_export(destination: Path, archive: Path) -> None:
    if destination.exists() or destination.is_symlink():
        marker = destination / MARKER
        if destination.is_symlink() or marker.is_symlink() or not marker.is_file():
            raise ValueError(f"Will not replace an unmanaged directory: {destination}")
        marker_data = marker.read_bytes()
        try:
            manifest = json.loads(marker_data)
            recorded = manifest["files"]
            if manifest["format"] != FORMAT or not isinstance(recorded, dict):
                raise ValueError("Unrecognized marker")
        except (TypeError, KeyError, ValueError) as error:
            raise ValueError(f"Will not replace an unmanaged directory: {destination}") from error
        directories = {parent.as_posix() for name in recorded for parent in Path(name).parents}
        files = {}
        for path in destination.rglob("*"):
            relative = path.relative_to(destination).as_posix()
            if not path.is_symlink():
                if relative == MARKER or (path.is_dir() and relative in directories):
                    continue
                if path.is_file() and relative in recorded:
                    files[relative] = path.read_bytes()
                    continue
            raise ValueError(f"Will not replace an export containing {relative}")
        if marker_data != marker_bytes(files):
            raise ValueError(f"Will not replace an edited export: {destination}")

    if archive.exists() or archive.is_symlink():
        if archive.is_symlink() or not archive.is_file():
            raise ValueError(f"Will not replace an unmanaged ZIP: {archive}")
        try:
            with ZipFile(archive) as old_zip:
                managed = old_zip.comment == FORMAT.encode("ascii")
        except BadZipFile:
            managed = False
        if not managed:
            raise ValueError(f"Will not replace an unmanaged ZIP: {archive}")


def export(root: Path) -> tuple[Path, Path, list[str]]:
    root = root.resolve()
    files, skipped = source_files(root)
    files[MARKER] = marker_bytes(files)
    output = existing_path(root, "output/packages")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / NAME
    archive = output / f"{NAME}.zip"
    lock = output / f".{NAME}.lock"

    with TemporaryDirectory(prefix=f".{NAME}-", dir=output) as temporary:
        staging = Path(temporary)
        staged_tree = staging / NAME
        staged_zip = staging / archive.name
        with ZipFile(staged_zip, "w", compression=ZIP_DEFLATED) as bundle:
            bundle.comment = FORMAT.encode("ascii")
            for relative, data in sorted(files.items()):
                path = staged_tree / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                member = ZipInfo(f"{NAME}/{relative}")
                member.create_system = 3
                member.external_attr = 0o100644 << 16
                bundle.writestr(member, data, compress_type=ZIP_DEFLATED)

        try:
            lock.mkdir()
        except FileExistsError as error:
            raise ValueError(f"An export is already in progress or left a lock at {lock}") from error
        try:
            verify_existing_export(destination, archive)
            previous_tree = staging / "previous"
            if destination.exists():
                destination.rename(previous_tree)
            try:
                staged_tree.rename(destination)
                os.replace(staged_zip, archive)
            except OSError:
                if destination.exists():
                    destination.rename(staged_tree)
                if previous_tree.exists():
                    previous_tree.rename(destination)
                raise
        finally:
            lock.rmdir()
    return destination, archive, skipped


def main() -> int:
    try:
        destination, archive, skipped = export(ROOT)
    except (OSError, ValueError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    for relative in skipped:
        print(f"Optional file absent, skipped: {relative}")
    print(destination)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
