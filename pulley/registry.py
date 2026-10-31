"""Distrobase registries, through Hydrogen.

A registry is named as Hydrogen names it: a host (``wednesware.org``, served from
``https://dstbs.wednesware.org``), a ``host:port`` on localhost, a full ``http(s)://`` URL,
or ``#/path`` for a local registry on the filesystem. Pulley always gives Hydrogen fully
qualified addresses (``author.name@registry=version``), so Hydrogen's own home registry
setting never changes which registry Pulley uses.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import os
import shutil
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import PulleyError
from .hydrogen import (
    VERSION as hydrogen_version,
    registryBaseUrl,
    localRegistryPath,
    findDistroLocation,
    viewDistro,
    publishDistro,
)

USER_AGENT = f"pulley/{__version__} (hydrogen/{hydrogen_version})"


@dataclass
class Release:
    author: str
    name: str
    version: str
    location: str  # URL or local archive path
    sha256: str = ""
    size: int = 0


def _request(url: str, data: bytes | None = None, headers: dict | None = None, method: str | None = None):
    request = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=60)


class Registry:
    def __init__(self, spec: str):
        spec = spec.strip()
        if not spec:
            raise PulleyError("empty registry name")
        self.spec = spec
        self.local = spec.startswith("#")

    def __str__(self) -> str:
        return self.spec

    @property
    def base_url(self) -> str:
        return registryBaseUrl(self.spec)

    @property
    def local_root(self) -> Path:
        return Path(localRegistryPath(self.spec[1:]))

    def address(self, author: str, name: str, version: str | None = None) -> str:
        """A fully qualified Hydrogen address."""
        where = self.spec if self.local else "@" + self.spec
        return f"{author}.{name}{where}" + (f"={version}" if version else "")

    def _json(self, url: str):
        try:
            with _request(url) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise PulleyError(f"registry {self.spec} returned HTTP {exc.code} for {url}") from None
        except urllib.error.URLError as exc:
            raise PulleyError(f"cannot reach registry {self.spec} ({self.base_url}): {exc.reason}") from None
        except ValueError:
            raise PulleyError(f"registry {self.spec} returned invalid JSON for {url}") from None

    def _distribution_url(self, author: str, name: str) -> str:
        return f"{self.base_url}/v1/{urllib.parse.quote(author)}/{urllib.parse.quote(name)}"

    def distribution(self, author: str, name: str) -> dict | None:
        """Registry metadata of a distribution, or None if it does not exist."""
        if self.local:
            directory = self.local_root / author / name
            if not directory.is_dir():
                return None
            return {"author": author, "distribution": name, "releases": self._local_versions(directory)}
        return self._json(self._distribution_url(author, name))

    @staticmethod
    def _local_versions(directory: Path) -> list[str]:
        versions = []
        for item in directory.iterdir():
            for suffix in (".tar.gz", ".tgz"):
                if item.is_file() and item.name.endswith(suffix):
                    versions.append(item.name[: -len(suffix)])
        return versions

    def versions(self, author: str, name: str) -> list[str]:
        """Every published version of a distribution (raises if it does not exist)."""
        data = self.distribution(author, name)
        if data is None:
            raise PulleyError(f"distribution '{author}.{name}' was not found in registry {self.spec}")
        found: list[str] = []
        for key in ("releases", "versions"):
            for item in data.get(key) or []:
                if isinstance(item, dict):
                    item = item.get("release") or item.get("version") or item.get("name")
                if item and str(item) not in found:
                    found.append(str(item))
        if data.get("latest") and str(data["latest"]) not in found:
            found.append(str(data["latest"]))
        return found

    def latest(self, author: str, name: str) -> str | None:
        data = self.distribution(author, name)
        return str(data["latest"]) if data and data.get("latest") else None

    def release(self, author: str, name: str, version: str) -> Release:
        """Where a release's archive is and its published checksum."""
        address = self.address(author, name, version)
        try:
            location = findDistroLocation(address)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            raise PulleyError(" ".join(str(exc).split())) from None
        sha256, size = "", 0
        if self.local:
            sha256 = _sha256_file(Path(location))
            size = os.path.getsize(location)
        else:
            data = self._json(f"{self._distribution_url(author, name)}/{urllib.parse.quote(version)}") or {}
            artifact_name = urllib.parse.unquote(location.rsplit("/", 1)[-1])
            for artifact in data.get("artifacts", []):
                if artifact.get("name") == artifact_name:
                    sha256 = str(artifact.get("sha256") or "")
                    size = int(artifact.get("size") or 0)
        return Release(author, name, version, location, sha256.lower(), size)

    def download(self, release: Release, destination: Path) -> str:
        """Copies or downloads a release archive and returns its SHA-256."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            if self.local or os.path.isfile(release.location):
                shutil.copyfile(release.location, temporary)
            else:
                try:
                    with _request(release.location) as response, open(temporary, "wb") as out:
                        shutil.copyfileobj(response, out)
                except urllib.error.HTTPError as exc:
                    raise PulleyError(f"downloading {release.location} failed: HTTP {exc.code}") from None
                except urllib.error.URLError as exc:
                    raise PulleyError(f"downloading {release.location} failed: {exc.reason}") from None
            digest = _sha256_file(temporary)
            if release.sha256 and digest != release.sha256:
                raise PulleyError(
                    f"checksum mismatch for {release.author}.{release.name}={release.version}: "
                    f"registry says {release.sha256}, the download is {digest}"
                )
            os.replace(temporary, destination)
            return digest
        finally:
            if temporary.exists():
                temporary.unlink()

    def view(self, author: str, name: str, version: str = "latest") -> list[str]:
        result = viewDistro(self.address(author, name, version))
        if not result.success:
            raise PulleyError(result.message or "; ".join(result.lines))
        return list(result.lines)

    def publish(self, directory: Path, author: str, name: str, version: str,
                username: str | None, password: str | None, verbose: bool = False) -> list[str]:
        """Publishes a prepared directory as author.name=version (Hydrogen packs and uploads it)."""
        output = io.StringIO()
        # Hydrogen reports each packed file; show that only when asked.
        with contextlib.redirect_stdout(output) if not verbose else contextlib.nullcontext():
            result = asyncio.run(publishDistro(str(directory), self.address(author, name, version), username, password))
        if not result.success:
            message = result.message or "; ".join(result.lines)
            if "401" in message or "403" in message or "nauthori" in message:
                message += " (run `pulley login` for this registry)"
            raise PulleyError(message)
        return list(result.lines)

    def login(self, username: str, password: str) -> None:
        """Checks credentials the way Hydrogen does: an authenticated user lookup."""
        if self.local:
            return
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        url = f"{self.base_url}/v1/users/{urllib.parse.quote(username)}"
        try:
            with _request(url, headers={"Authorization": f"Basic {token}"}):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PulleyError(f"incorrect username or password for {self.spec}") from None
            if exc.code == 404:
                raise PulleyError(f"user '{username}' does not exist on {self.spec}; create it with `pulley register`") from None
            raise PulleyError(f"registry {self.spec} returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            raise PulleyError(f"cannot reach registry {self.spec}: {exc.reason}") from None

    def register(self, username: str, password: str) -> None:
        if self.local:
            raise PulleyError("local registries have no user accounts")
        body = json.dumps({"username": username, "password": password}).encode()
        try:
            with _request(f"{self.base_url}/v1/users", body, {"Content-Type": "application/json"}, "POST"):
                pass
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise PulleyError(f"could not create user '{username}' on {self.spec}: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise PulleyError(f"cannot reach registry {self.spec}: {exc.reason}") from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive: Path, destination: Path) -> Path:
    """Extracts a release archive safely and returns the package root."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                parts = member.name.replace("\\", "/").split("/")
                if member.name.startswith(("/", "\\")) or ":" in parts[0] or ".." in parts:
                    raise PulleyError(f"cannot extract {archive.name}: unsafe path {member.name!r}")
                # Packages are plain files and directories: no links, devices, or FIFOs.
                if not (member.isfile() or member.isdir()):
                    raise PulleyError(f"cannot extract {archive.name}: unsupported member {member.name!r}")
            # The "data" filter re-checks paths against the destination and drops unsafe
            # permission bits.
            tar.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise PulleyError(f"cannot extract {archive.name}: {exc}") from None
    entries = [e for e in destination.iterdir()]
    # Archives with a single top-level directory unpack that directory, as Hydrogen does.
    if len(entries) == 1 and entries[0].is_dir() and not (destination / "pulley.toml").exists():
        return entries[0]
    return destination
