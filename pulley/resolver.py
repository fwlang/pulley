"""Dependency resolution.

Every distribution resolves to one version for the whole project (dependencies are installed
side by side in one modules directory). The resolver collects the constraints that every
selected package places on each distribution, picks the highest version satisfying all of
them, and repeats when a choice changes another package's requirements, until nothing
changes. A version recorded in pulley.lock is kept while it still satisfies the constraints,
unless that package is being updated.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import manifest as manifests
from .config import PulleyError, cache_dir
from .registry import Registry, extract
from .versions import Constraint, best

ROOT = ("root",)


@dataclass
class Package:
    """A resolved package: its source, version, and the aliases it is installed under."""

    key: tuple
    kind: str  # registry, path, git
    aliases: set[str] = field(default_factory=set)
    version: str = ""
    registry: str = ""
    author: str = ""
    name: str = ""
    sha256: str = ""
    path: str = ""  # absolute directory (path packages) or the cached contents
    git: str = ""
    rev: str = ""
    commit: str = ""
    dev: bool = True  # only needed by dev-dependencies
    edges: dict[str, tuple] = field(default_factory=dict)  # alias -> key of its dependencies
    manifest: manifests.Manifest | None = None

    @property
    def label(self) -> str:
        if self.kind == "registry":
            return f"{self.author}.{self.name}={self.version}"
        if self.kind == "path":
            return f"path {self.path}"
        return f"git {self.git}@{self.commit[:12]}"


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


class Cache:
    """Downloaded archives and their extracted contents, shared by all projects."""

    def __init__(self, root: Path | None = None):
        self.root = root or cache_dir()

    def registry_package(self, registry: Registry, author: str, name: str, version: str,
                         expected_sha: str = "") -> tuple[Path, str]:
        """The extracted contents of a release and the archive's SHA-256."""
        base = self.root / "packages" / _safe(registry.spec) / author / name / _safe(version)
        marker = base / ".sha256"
        if marker.is_file() and (base / "contents").is_dir():
            sha = marker.read_text().strip()
            if not expected_sha or sha == expected_sha:
                return base / "contents", sha
        release = registry.release(author, name, version)
        if expected_sha and release.sha256 and release.sha256 != expected_sha:
            raise PulleyError(
                f"{author}.{name}={version} on {registry} has checksum {release.sha256}, but "
                f"pulley.lock records {expected_sha}; the release changed after it was locked "
                "(run `pulley update` to accept it)"
            )
        archive = self.root / "archives" / f"{_safe(registry.spec)}-{author}.{name}-{_safe(version)}.tar.gz"
        sha = registry.download(release, archive)
        if expected_sha and sha != expected_sha:
            raise PulleyError(f"{author}.{name}={version} does not match the checksum in pulley.lock")
        if base.exists():
            shutil.rmtree(base)
        extracted = base / "extracted"
        root = extract(archive, extracted)
        root.rename(base / "contents")
        if extracted.exists():
            shutil.rmtree(extracted)
        marker.write_text(sha + "\n")
        return base / "contents", sha

    def git_package(self, url: str, rev: str, commit: str = "") -> tuple[Path, str]:
        """A checkout of a git revision (or a locked commit), without its .git directory."""
        if not shutil.which("git"):
            raise PulleyError("git dependencies need git on PATH")
        clone = self.root / "git" / hashlib.sha256(url.encode()).hexdigest()[:16]

        def git(*args: str, cwd: Path | None = clone) -> str:
            result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
            if result.returncode:
                raise PulleyError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
            return result.stdout.strip()

        def commit_of(name: str) -> str:
            try:
                return git("rev-parse", "--verify", "--quiet", name + "^{commit}")
            except PulleyError:
                return ""

        if not (clone / ".git").is_dir():
            clone.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "--quiet", url, str(clone), cwd=None)
        # A locked commit is used as is; otherwise fetch and read the branch, tag, or commit.
        resolved = commit_of(commit) if commit else ""
        if not resolved:
            git("fetch", "--quiet", "--tags", "--force", "origin")
            target = commit or rev or "HEAD"
            resolved = commit_of(f"origin/{target}") or commit_of(target)
            if not resolved:
                raise PulleyError(f"git revision '{target}' not found in {url}")
        export = self.root / "git-exports" / resolved
        if not export.is_dir():
            temporary = export.with_name(resolved + ".part")
            if temporary.exists():
                shutil.rmtree(temporary)
            temporary.mkdir(parents=True)
            archive = subprocess.run(["git", "archive", "--format=tar", resolved], cwd=clone, capture_output=True)
            if archive.returncode:
                raise PulleyError(f"git archive of {url} failed")
            import io
            import tarfile
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
                tar.extractall(temporary, filter="data")
            temporary.rename(export)
        return export, resolved


class Resolver:
    def __init__(self, project: manifests.Manifest, default_registry: str, lock: dict | None = None,
                 update: set[str] | None = None, update_all: bool = False, include_dev: bool = True,
                 cache: Cache | None = None, log=None):
        self.project = project
        self.default_registry = default_registry
        self.lock = lock or {}
        self.update = update or set()
        self.update_all = update_all
        self.include_dev = include_dev
        self.cache = cache or Cache()
        self.log = log or (lambda message: None)
        self.registries: dict[str, Registry] = {}
        self.version_lists: dict[tuple, list[str]] = {}
        # key -> {requester: (constraint, alias)}
        self.requests: dict[tuple, dict[tuple, tuple[Constraint, str]]] = {}
        self.packages: dict[tuple, Package] = {}

    def registry(self, spec: str) -> Registry:
        if spec not in self.registries:
            self.registries[spec] = Registry(spec)
        return self.registries[spec]

    def _key(self, dependency: manifests.Dependency, base: Path, registry: str) -> tuple:
        if dependency.kind == "path":
            return ("path", str((base / dependency.path).resolve()))
        if dependency.kind == "git":
            return ("git", dependency.git, dependency.rev)
        return ("registry", dependency.registry or registry, dependency.author, dependency.name)

    def _requester(self, package: Package | None) -> tuple:
        return ROOT if package is None else (package.key, package.version or package.commit or "")

    def _add_requests(self, owner: Package | None, deps: list[manifests.Dependency], base: Path, registry: str) -> None:
        requester = self._requester(owner)
        for dep in deps:
            key = self._key(dep, base, registry)
            constraint = Constraint(dep.constraint if dep.kind == "registry" else "*")
            self.requests.setdefault(key, {})[requester] = (constraint, dep.alias)
            if owner is not None:
                owner.edges[dep.alias] = key

    def _drop_requests(self, package: Package) -> None:
        for key, requests in self.requests.items():
            for requester in [r for r in requests if r != ROOT and r[0] == package.key]:
                del requests[requester]
        package.edges.clear()

    def _locked(self, key: tuple) -> dict | None:
        entry = self.lock.get(key)
        if not entry or self.update_all:
            return None
        if entry.get("aliases") and set(entry["aliases"]) & self.update:
            return None
        return entry

    def _versions(self, key: tuple) -> list[str]:
        if key not in self.version_lists:
            _, registry, author, name = key
            self.log(f"resolving {author}.{name} on {registry}")
            self.version_lists[key] = self.registry(registry).versions(author, name)
        return self.version_lists[key]

    def _choose(self, key: tuple) -> str:
        constraints = [c for c, _ in self.requests[key].values()]
        locked = self._locked(key)
        if locked and all(c.allows(locked["version"]) for c in constraints):
            return locked["version"]
        exact = {c.exact() for c in constraints if c.exact()}
        if len(exact) == 1 and all(c.allows(next(iter(exact))) for c in constraints):
            # Taken as is: a registry may list only its latest release; fetching checks it exists.
            return next(iter(exact))
        versions = self._versions(key)
        chosen = best(versions, constraints)
        if chosen is None:
            _, registry, author, name = key
            wanted = "; ".join(f"{c} (by {self._who(r)})" for r, (c, _) in self.requests[key].items())
            raise PulleyError(
                f"no version of {author}.{name} on {registry} satisfies every requirement: {wanted}. "
                f"Available: {', '.join(sorted(versions, key=_sort)) or 'none'}"
            )
        return chosen

    def _who(self, requester: tuple) -> str:
        if requester == ROOT:
            return self.project.name or "this project"
        package = self.packages.get(requester[0])
        return package.label if package else str(requester[0])

    def _load(self, key: tuple, version: str = "") -> Package:
        """Fetches a package (a version of it) and reads its manifest."""
        kind = key[0]
        package = self.packages.get(key) or Package(key, kind)
        if kind == "registry":
            _, registry, author, name = key
            locked = self._locked(key)
            expected = locked.get("sha256", "") if locked and locked.get("version") == version else ""
            self.log(f"fetching {author}.{name}={version}")
            contents, sha = self.cache.registry_package(self.registry(registry), author, name, version, expected)
            package.registry, package.author, package.name, package.version = registry, author, name, version
            package.sha256, package.path = sha, str(contents)
            registry_for_deps = registry
        elif kind == "path":
            directory = Path(key[1])
            if not directory.is_dir():
                raise PulleyError(f"path dependency {directory} does not exist")
            package.path = str(directory)
            contents = directory
            registry_for_deps = self.default_registry
        else:
            _, url, rev = key
            locked = self._locked(key)
            self.log(f"fetching {url}" + (f" ({rev})" if rev else ""))
            contents, commit = self.cache.git_package(url, rev, locked.get("commit", "") if locked else "")
            package.git, package.rev, package.commit, package.path = url, rev, commit, str(contents)
            registry_for_deps = self.default_registry
        package.manifest = manifests.load(contents) if (contents / "pulley.toml").is_file() else None
        self._drop_requests(package)
        self.packages[key] = package
        if package.manifest:
            # A package's dependencies resolve against its own default registry, if it names one.
            own = package.manifest.registry or (registry_for_deps if kind == "registry" else self.default_registry)
            self._add_requests(package, package.manifest.all_dependencies(dev=False), contents, own)
        return package

    def resolve(self) -> dict[tuple, Package]:
        root_deps = self.project.all_dependencies(dev=self.include_dev)
        self._add_requests(None, root_deps, self.project.directory, self.default_registry)
        for _ in range(200):
            changed = False
            for key in list(self.requests):
                if not self.requests[key]:
                    continue
                if key[0] == "registry":
                    version = self._choose(key)
                    current = self.packages.get(key)
                    if current is None or current.version != version:
                        self._load(key, version)
                        changed = True
                elif key not in self.packages:
                    self._load(key)
                    changed = True
            if not changed:
                break
        else:
            raise PulleyError("dependency resolution did not settle; the requirements keep changing each other")
        return self._reachable(root_deps)

    def _reachable(self, root_deps: list[manifests.Dependency]) -> dict[tuple, Package]:
        result: dict[tuple, Package] = {}
        aliases: dict[str, tuple] = {}

        for package in self.packages.values():
            package.aliases.clear()
        # Every package reachable from the project, with the import names it is used under.
        stack = [(dep.alias, self._key(dep, self.project.directory, self.default_registry)) for dep in root_deps]
        while stack:
            alias, key = stack.pop()
            package = self.packages[key]
            if aliases.get(alias, key) != key:
                other = self.packages[aliases[alias]]
                raise PulleyError(
                    f"two different packages want the import name '{alias}': {other.label} and {package.label}"
                )
            aliases[alias] = key
            package.aliases.add(alias)
            if key not in result:
                result[key] = package
                stack.extend(package.edges.items())
        # Packages that only dev-dependencies need are marked dev.
        production: set[tuple] = set()
        stack = [self._key(d, self.project.directory, self.default_registry) for d in root_deps if not d.dev]
        while stack:
            key = stack.pop()
            if key not in production:
                production.add(key)
                stack.extend(self.packages[key].edges.values())
        for key, package in result.items():
            package.dev = key not in production
        return result


def _sort(version: str):
    from .versions import Version
    return Version.parse(version)
