"""User configuration, directories, and registry credentials.

The registry is chosen, most specific first, by a ``--registry`` option, the project's
``[registry] default`` in pulley.toml, the ``PULLEY_REGISTRY`` environment variable, the
user configuration (``pulley registry <name>``), and finally ``wednesware.org``.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import stat
from pathlib import Path

DEFAULT_REGISTRY = "wednesware.org"
LOCAL_MODULES = ".flywheel-modules"
MANIFEST = "pulley.toml"
LOCKFILE = "pulley.lock"

try:  # optional, as in Hydrogen
    import keyring  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    keyring = None


class PulleyError(Exception):
    """An error reported to the user without a traceback."""


def config_dir() -> Path:
    if os.environ.get("PULLEY_CONFIG_DIR"):
        return Path(os.environ["PULLEY_CONFIG_DIR"])
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", Path.home())) / "pulley"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pulley"


def cache_dir() -> Path:
    if os.environ.get("PULLEY_CACHE_DIR"):
        return Path(os.environ["PULLEY_CACHE_DIR"])
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "pulley" / "cache"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pulley"


def _config_file() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    try:
        return json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def default_registry(project_registry: str | None = None) -> str:
    return (
        project_registry
        or os.environ.get("PULLEY_REGISTRY")
        or load_config().get("registry")
        or DEFAULT_REGISTRY
    )


def find_flywheel() -> Path | None:
    """The flywheel executable: $PULLEY_FLYWHEEL, the configured path, or flywheel on PATH."""
    candidate = os.environ.get("PULLEY_FLYWHEEL") or load_config().get("flywheel")
    if candidate:
        path = Path(candidate).expanduser()
        return path if path.exists() else None
    found = shutil.which("flywheel")
    return Path(found).resolve() if found else None


def global_modules_dir() -> Path:
    """The installation-wide modules directory that flywheel searches.

    This mirrors the compiler: ``$FLYWHEEL_HOME/modules`` when set, otherwise ``modules``
    in the directory that holds ``bin/flywheel`` (or the executable's own directory).
    """
    if os.environ.get("FLYWHEEL_HOME"):
        return Path(os.environ["FLYWHEEL_HOME"]) / "modules"
    if load_config().get("global_dir"):
        return Path(load_config()["global_dir"]).expanduser()
    flywheel = find_flywheel()
    if not flywheel:
        raise PulleyError(
            "cannot find the flywheel installation for a global install: put flywheel on PATH, "
            "set FLYWHEEL_HOME, or run `pulley config flywheel /path/to/flywheel`"
        )
    directory = flywheel.resolve().parent
    if directory.name == "bin":
        directory = directory.parent
    return directory / "modules"


def find_project(start: Path | None = None) -> Path | None:
    """The nearest directory at or above `start` that has a pulley.toml."""
    directory = (start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / MANIFEST).is_file():
            return candidate
    return None


# Credentials -------------------------------------------------------------------------------

def _credentials_file() -> Path:
    return config_dir() / "credentials.json"


def _read_credentials() -> dict:
    try:
        return json.loads(_credentials_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_credentials(data: dict) -> None:
    path = _credentials_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create the file owner-only from the start, so the passwords are never readable by others.
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(json.dumps(data, indent=2) + "\n")
    os.replace(temporary, path)


def store_credentials(registry: str, username: str, password: str) -> str:
    """Saves credentials for a registry; returns where they were stored."""
    data = _read_credentials()
    entry = {"username": username}
    where = "the system keyring"
    if keyring is not None:
        try:
            keyring.set_password("pulley-distrobase", f"{registry}:{username}", password)
        except Exception:
            entry["password"] = password
            where = str(_credentials_file())
    else:
        entry["password"] = password
        where = str(_credentials_file())
    data[registry] = entry
    _write_credentials(data)
    return where


def credentials(registry: str) -> tuple[str | None, str | None]:
    """Credentials for a registry: environment variables, then stored ones."""
    user = os.environ.get("PULLEY_AUTH_USER") or os.environ.get("DISTROBASE_AUTH_USER")
    password = os.environ.get("PULLEY_AUTH_PASSWORD") or os.environ.get("DISTROBASE_AUTH_PASSWORD")
    if user and password:
        return user, password
    entry = _read_credentials().get(registry)
    if not entry:
        return None, None
    password = entry.get("password")
    if not password and keyring is not None:
        try:
            password = keyring.get_password("pulley-distrobase", f"{registry}:{entry['username']}")
        except Exception:
            password = None
    return entry.get("username"), password


def forget_credentials(registry: str) -> bool:
    data = _read_credentials()
    entry = data.pop(registry, None)
    if entry is None:
        return False
    if keyring is not None:
        try:
            keyring.delete_password("pulley-distrobase", f"{registry}:{entry['username']}")
        except Exception:
            pass
    _write_credentials(data)
    return True
