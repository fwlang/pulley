"""Preparing a project for publishing: the files a release archive contains."""

from __future__ import annotations

import fnmatch
import shutil
import tarfile
import tomllib
from pathlib import Path

from .config import LOCAL_MODULES, LOCKFILE, PulleyError
from .manifest import NAME, Manifest

ALWAYS_EXCLUDED = {".git", ".hg", ".svn", LOCAL_MODULES, "__pycache__", ".DS_Store", LOCKFILE, "coverage.lcov"}


def _excludes(manifest: Manifest) -> list[str]:
    data = tomllib.loads(manifest.path.read_text(encoding="utf-8")).get("package", {})
    patterns = data.get("exclude", [])
    if not isinstance(patterns, list):
        raise PulleyError("[package] exclude must be a list of glob patterns")
    return [str(p) for p in patterns]


def files(manifest: Manifest) -> list[Path]:
    """Project files that belong in a release, relative to the project directory."""
    patterns = _excludes(manifest)
    root = manifest.directory
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in ALWAYS_EXCLUDED for part in relative.parts):
            continue
        text = relative.as_posix()
        if any(fnmatch.fnmatch(text, p) or fnmatch.fnmatch(text, p.rstrip("/") + "/*") for p in patterns):
            continue
        if path.is_file() and not path.is_symlink():
            out.append(relative)
    return out


def check(manifest: Manifest) -> None:
    """Problems that would make a release unusable."""
    problems = []
    if not manifest.name or not NAME.match(manifest.name):
        problems.append("[package] name must be lowercase letters, digits, '-' and '_'")
    if not manifest.author or not NAME.match(manifest.author):
        problems.append("[package] author must be your Distrobase username (lowercase)")
    if not manifest.version or manifest.version == "latest":
        problems.append("[package] version is required")
    if not (manifest.directory / manifest.main_module()).is_file():
        problems.append(f"the main module {manifest.main_module()} does not exist (set [package] main)")
    for dep in manifest.dependencies.values():
        if dep.kind == "path":
            problems.append(f"dependency '{dep.alias}' is a path; published packages can only depend on registry or git packages")
    if problems:
        raise PulleyError("cannot publish:\n  " + "\n  ".join(problems))


def stage(manifest: Manifest, destination: Path) -> list[Path]:
    """Copies the release files into `destination`; returns them."""
    selected = files(manifest)
    for relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest.directory / relative, target)
    return selected


def pack(manifest: Manifest, archive: Path) -> list[Path]:
    """Writes the release archive (as Hydrogen publishes it) to `archive`."""
    selected = files(manifest)
    with tarfile.open(archive, "w:gz") as tar:
        for relative in selected:
            tar.add(manifest.directory / relative, arcname=relative.as_posix())
    return selected
