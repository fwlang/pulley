"""pulley.toml: package metadata and dependencies.

::

    [package]
    name = "app"              # the distribution name on Distrobase
    author = "danny"          # the Distrobase author (account) that publishes it
    version = "0.1.0"
    description = "An example"
    main = "app.fw"           # the module `import app` loads; default <name>.fw
    license = "MIT"

    [registry]
    default = "wednesware.org"   # optional: the registry for dependencies without one

    [dependencies]
    shapes = "wednesware.shapes=^1.2"              # alias = Distrobase address [= constraint]
    text = "acme.text@example.org=~2.0"            # on another registry
    vendored = "acme.vendored#/srv/registry=1.0"   # a local (filesystem) registry
    geometry = { distribution = "acme.geometry", version = ">=1.0, <3", registry = "example.org" }
    helpers = { path = "../helpers" }              # a local directory
    parser = { git = "https://example.com/parser.git", rev = "v2.1" }

    [dev-dependencies]
    fixtures = "danny.fixtures"

    [scripts]
    test = "flywheel test"

The dependency key is the import name: ``import shapes``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import MANIFEST, PulleyError
from .versions import Constraint, ConstraintError

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Dependency:
    alias: str
    kind: str = "registry"  # registry, path, or git
    author: str = ""
    name: str = ""
    registry: str | None = None  # None: the default registry
    constraint: str = "*"
    path: str = ""
    git: str = ""
    rev: str = ""
    dev: bool = False

    @property
    def distribution(self) -> str:
        return f"{self.author}.{self.name}"

    def describe(self) -> str:
        if self.kind == "path":
            return f"path {self.path}"
        if self.kind == "git":
            return f"git {self.git}" + (f" ({self.rev})" if self.rev else "")
        where = f"@{self.registry}" if self.registry and not self.registry.startswith("#") else (self.registry or "")
        return f"{self.distribution}{where} {self.constraint}"

    def toml(self) -> str:
        """The manifest value for this dependency."""
        if self.kind == "path":
            return "{ path = " + _quote(self.path) + " }"
        if self.kind == "git":
            return "{ git = " + _quote(self.git) + (", rev = " + _quote(self.rev) if self.rev else "") + " }"
        text = self.distribution
        if self.registry:
            text += self.registry if self.registry.startswith("#") else "@" + self.registry
        if self.constraint and self.constraint not in ("*", "latest"):
            text += "=" + self.constraint
        return _quote(text)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_address(text: str) -> tuple[str, str, str | None, str]:
    """Splits ``author.name[@registry|#path][=constraint]`` (or ``author.name constraint``).

    Returns (author, name, registry or None, constraint).
    """
    text = text.strip()
    registry = None
    constraint = "*"
    match = re.match(r"^([^@#=\s]+)(.*)$", text)
    if not match:
        raise PulleyError(f"invalid distribution address '{text}'")
    package, rest = match.group(1), match.group(2).strip()
    if rest.startswith(("@", "#")):
        local = rest[0] == "#"
        end = rest.find("=")
        registry = rest[: end if end >= 0 else len(rest)].strip()
        if not local:
            registry = registry[1:]
        rest = rest[end:] if end >= 0 else ""
        if not registry.strip("#"):
            raise PulleyError(f"missing registry in '{text}'")
    if rest.startswith("="):
        constraint = rest[1:].strip() or "*"
    elif rest:
        constraint = rest.strip()
    if "." not in package:
        raise PulleyError(f"'{text}' needs an author: author.{package}")
    author, name = package.lower().split(".", 1)
    for part in (author, name):
        if not NAME.match(part):
            raise PulleyError(
                f"invalid distribution name '{author}.{name}': use lowercase letters, digits, '-' and '_'"
            )
    try:
        Constraint(constraint)
    except ConstraintError as exc:
        raise PulleyError(str(exc)) from None
    return author, name, registry, constraint


def _dependency(alias: str, value, dev: bool) -> Dependency:
    if not ALIAS.match(alias):
        raise PulleyError(f"dependency name '{alias}' must be a Flywheel identifier (it is the import name)")
    if isinstance(value, str):
        author, name, registry, constraint = parse_address(value)
        return Dependency(alias, "registry", author, name, registry, constraint, dev=dev)
    if not isinstance(value, dict):
        raise PulleyError(f"dependency '{alias}' must be a string or a table")
    kinds = [key for key in ("distribution", "path", "git") if key in value]
    if len(kinds) != 1:
        raise PulleyError(f"dependency '{alias}' needs exactly one of distribution, path, or git")
    unknown = set(value) - {"distribution", "version", "registry", "path", "git", "rev", "tag", "branch"}
    if unknown:
        raise PulleyError(f"dependency '{alias}' has unknown field(s): {', '.join(sorted(unknown))}")
    if "path" in value:
        return Dependency(alias, "path", path=str(value["path"]), dev=dev)
    if "git" in value:
        rev = value.get("rev") or value.get("tag") or value.get("branch") or ""
        return Dependency(alias, "git", git=str(value["git"]), rev=str(rev), dev=dev)
    author, name, registry, constraint = parse_address(str(value["distribution"]))
    if "version" in value:
        constraint = str(value["version"])
        try:
            Constraint(constraint)
        except ConstraintError as exc:
            raise PulleyError(str(exc)) from None
    if "registry" in value:
        registry = str(value["registry"])
    return Dependency(alias, "registry", author, name, registry, constraint, dev=dev)


@dataclass
class Manifest:
    directory: Path
    name: str = ""
    author: str = ""
    version: str = "0.1.0"
    description: str = ""
    main: str = ""
    license: str = ""
    registry: str | None = None
    dependencies: dict[str, Dependency] = field(default_factory=dict)
    dev_dependencies: dict[str, Dependency] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.directory / MANIFEST

    def all_dependencies(self, dev: bool = True) -> list[Dependency]:
        return list(self.dependencies.values()) + (list(self.dev_dependencies.values()) if dev else [])

    def main_module(self) -> str:
        return self.main or f"{self.name}.fw"


def load(directory: Path) -> Manifest:
    path = Path(directory) / MANIFEST
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PulleyError(f"no {MANIFEST} in {directory}; run `pulley init`") from None
    except tomllib.TOMLDecodeError as exc:
        raise PulleyError(f"{path}: {exc}") from None
    package = data.get("package", {})
    manifest = Manifest(
        directory=Path(directory).resolve(),
        name=str(package.get("name", "")),
        author=str(package.get("author", "")),
        version=str(package.get("version", "0.1.0")),
        description=str(package.get("description", "")),
        main=str(package.get("main", "")),
        license=str(package.get("license", "")),
        registry=(data.get("registry") or {}).get("default"),
        scripts={str(k): str(v) for k, v in (data.get("scripts") or {}).items()},
    )
    for table, dev in (("dependencies", False), ("dev-dependencies", True)):
        target = manifest.dev_dependencies if dev else manifest.dependencies
        for alias, value in (data.get(table) or {}).items():
            target[alias] = _dependency(alias, value, dev)
    both = set(manifest.dependencies) & set(manifest.dev_dependencies)
    if both:
        raise PulleyError(f"{path}: {', '.join(sorted(both))} listed in both dependencies and dev-dependencies")
    return manifest


def template(name: str, author: str, main: str) -> str:
    return (
        "[package]\n"
        f"name = {_quote(name)}\n"
        f"author = {_quote(author)}\n"
        'version = "0.1.0"\n'
        'description = ""\n'
        f"main = {_quote(main)}\n"
        "\n[dependencies]\n"
        "\n[dev-dependencies]\n"
        "\n[scripts]\n"
        'test = "flywheel test"\n'
    )


# Editing: changes keep the rest of the file (comments, order, formatting) as written. --------

def _table_bounds(lines: list[str], table: str) -> tuple[int, int] | None:
    start = None
    for i, line in enumerate(lines):
        header = re.match(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$", line)
        if header:
            if start is not None:
                return start, i
            if header.group(1).strip() == table:
                start = i + 1
    return (start, len(lines)) if start is not None else None


def _key_line(lines: list[str], bounds: tuple[int, int], key: str) -> int | None:
    pattern = re.compile(r"^\s*(" + re.escape(key) + r'|"' + re.escape(key) + r'")\s*=')
    for i in range(*bounds):
        if pattern.match(lines[i]):
            return i
    return None


def set_value(path: Path, table: str, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    bounds = _table_bounds(lines, table)
    entry = f"{key} = {value}"
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"[{table}]", entry]
    else:
        index = _key_line(lines, bounds, key)
        if index is not None:
            lines[index] = entry
        else:
            # After the table's last non-blank line.
            end = bounds[1]
            while end > bounds[0] and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_value(path: Path, table: str, key: str) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines()
    bounds = _table_bounds(lines, table)
    if bounds is None:
        return False
    index = _key_line(lines, bounds, key)
    if index is None:
        return False
    del lines[index]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True
