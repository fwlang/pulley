# Hydrogen — Wednesware Distrobase client, derived from Sourcegen 26.66.
# Sourcegen powers package fetching, caching and optional pipx installation.
import sys, os, json, asyncio, re, shutil, zipfile, tarfile, tempfile, platform, urllib, urllib.request, urllib.error, logging, base64, getpass, webbrowser, socket, pathlib, subprocess, traceback, importlib.util, threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.request import urlretrieve
try:
    import keyring # type: ignore
except ImportError:
    keyring = None

SOURCEGEN_VERSION = "26.66"  # Upstream Sourcegen version (not Hydrogen's version)
NAME = "Hydrogen"
DESCRIPTION = "Wednesware Distrobase registry client and distribution installer."
VERSION = "26.7"  # Hydrogen release; update when publishing a new release
COMMAND = "h2"
INTERNAL_PACKAGES_DIR = os.path.join(os.path.dirname(__file__), "packages")
INTERNAL_TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
INTERNAL_WW_DIR = INTERNAL_PACKAGES_DIR
INSTALLER_PACKAGE_DEPENDENCY = None
PUBLICATION_CACHE = {}
REVERSE_PUBLICATION_CACHE = {}
CLI_RESET = "\033[0m"
CLI_BOLD = "\033[1m"
CLI_DIM = "\033[90m"
CLI_INFO = "\033[94m"
CLI_SUCCESS = "\033[92m"
CLI_WARNING = "\033[93m"
CLI_ERROR = "\033[91m"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.pyon")
DISTRO_NOT_FOUND_TEXT = "Distribution not found"
SUPPORT_EMAIL = "support@wednesware.org"
DSTBS_DIR = {"linux": "/var/lib", "windows": r"C:\ProgramData\Distrobase", "darwin": "/Library/Application Support/Distrobase"}.get(platform.system().lower(), os.path.expanduser("~/.distrobase"))
logger = logging.getLogger(__name__)
running_installs = {}

def getconf(key: str, default=None, *, config_path=None):
    try:
        with open(str(config_path or CONFIG_PATH), encoding="utf-8") as handle:
            return json.load(handle).get(key, default)
    except (OSError, ValueError, AttributeError):
        return default

class SourcegenDependencyError(RuntimeError):
    pass

def _read_raw_sourcepkg(path: str) -> dict:
    pkg_dir = os.path.abspath(path)
    if not os.path.isdir(pkg_dir):
        raise ValueError(f"Only directories can be installed: {path}")
    pkg_file = os.path.join(pkg_dir, ".sourcepkg")
    if not os.path.isfile(pkg_file):
        raise ValueError(f"Only package directories with a JSON .sourcepkg file can be installed: {path}")
    try:
        with open(pkg_file, "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f".sourcepkg is not valid JSON: {pkg_file}") from exc
    if not isinstance(metadata, dict):
        raise ValueError(f".sourcepkg must contain a JSON object: {pkg_file}")
    return dict(metadata)

def _load_sourcepkg(path: str) -> dict:
    metadata = _read_raw_sourcepkg(path)
    metadata.pop("dependency", None)
    metadata.pop("name", None)
    metadata.pop("command", None)
    return metadata

def _resolve_sourcepkg_entry(path: str, metadata: dict) -> str:
    entry = metadata.get("entry") or metadata.get("main") or metadata.get("script")
    if entry:
        target = os.path.join(path, entry)
        if os.path.isfile(target):
            return target
    for candidate in ("__main__.py", "main.py", "app.py", "run.py"):
        target = os.path.join(path, candidate)
        if os.path.isfile(target):
            return target
    raise ValueError(f"No valid entry script found in package directory '{path}'")

def _as_path_list(root: str) -> list[str]:
    root_abs = os.path.abspath(root)
    parent_abs = os.path.dirname(root_abs)
    entries: list[str] = []
    for candidate in (parent_abs, root_abs):
        if candidate and candidate not in entries:
            entries.append(candidate)
    for relative in ("packages", "libraries", os.path.join("libraries", "packages")):
        candidate = os.path.join(root_abs, relative)
        if os.path.isdir(candidate) and candidate not in entries:
            entries.append(candidate)
    for relative in ("packages", "libraries", os.path.join("libraries", "packages")):
        candidate = os.path.join(parent_abs, relative)
        if os.path.isdir(candidate) and candidate not in entries:
            entries.append(candidate)
    return entries

def _module_name_from_root(root: str) -> str | None:
    package_init = os.path.join(root, "__init__.py")
    if os.path.isfile(package_init):
        package_name = os.path.basename(root)
        normalized = package_name.replace("-", "_").replace(".", "_")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
            return normalized
    return None

def _sanitize_python_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    sanitized = sanitized.strip("-_.")
    return sanitized or "sourcegen-app"

def _module_name_from_metadata(metadata: dict | None, fallback: str) -> str:
    candidates: list[str] = []
    if metadata:
        for key in ("package_name", "name", "command", "publication"):
            value = metadata.get(key)
            if value:
                candidates.append(str(value))
    candidates.append(fallback)
    for candidate in candidates:
        sanitized = re.sub(r"[^A-Za-z0-9_]+", "_", str(candidate).strip())
        sanitized = sanitized.strip("_")
        if not sanitized:
            continue
        if not sanitized[0].isalpha() and sanitized[0] != "_":
            sanitized = f"package_{sanitized}"
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sanitized):
            return sanitized
    return "sourcegen_app"

def _toml_list(values: object) -> str:
    if values is None:
        return "[]"
    if isinstance(values, str):
        values = [values]
    normalized = [str(item).strip() for item in values if str(item).strip()]
    if not normalized:
        return "[]"
    return ", ".join(json.dumps(item) for item in normalized)

def _dependency_list(values: object | None, *, include_installer: bool = False) -> list[str]:
    if values is None:
        values = []
    if isinstance(values, str):
        values = [values]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        candidate = str(item).strip()
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(candidate)
    if include_installer and INSTALLER_PACKAGE_DEPENDENCY:
        nitro_key = INSTALLER_PACKAGE_DEPENDENCY
        if not any(item.lower() == nitro_key for item in normalized):
            normalized.insert(0, nitro_key)
    return normalized

def _build_entry_wrapper(entry: str, pkg_dir: str, metadata: dict | None = None) -> str:
    entry_value = entry.replace("\\", "/")
    pkg_root = os.path.abspath(pkg_dir)
    fallback_name = os.path.basename(pkg_root).replace("-", "_").replace(".", "_") or "sourcegen_app"
    package_name = _module_name_from_metadata(metadata, fallback_name)
    module_path = os.path.join(pkg_root, "__main__.py") if os.path.isfile(os.path.join(pkg_root, "__main__.py")) else os.path.join(pkg_root, "main.py")
    wrapper = f"""

import importlib.util
import os

import runpy

import sys

ROOT = {pkg_root!r}

ENTRY = {entry_value!r}

PACKAGE_NAME = {package_name!r}

PACKAGE_INIT = os.path.join(ROOT, "__init__.py")

MODULE_PATH = {module_path!r}


def _ensure_package_module() -> None:
    if not os.path.isfile(PACKAGE_INIT):
        return
    if PACKAGE_NAME in sys.modules and getattr(sys.modules[PACKAGE_NAME], "__file__", None) == PACKAGE_INIT:
        return
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_INIT,
        submodule_search_locations=[ROOT],
    )
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)


def main() -> None:
    entry_path = os.path.join(ROOT, ENTRY)
    if os.path.isfile(entry_path):
        if os.path.isfile(PACKAGE_INIT):
            sys.path.insert(0, os.path.dirname(ROOT))
            _ensure_package_module()
            basename = os.path.basename(entry_path)
            if basename == "__main__.py":
                runpy.run_module(PACKAGE_NAME, run_name="__main__")
                return
            stem = os.path.splitext(basename)[0]
            if stem:
                runpy.run_module(f"{{PACKAGE_NAME}}.{{stem}}", run_name="__main__")
                return
        runpy.run_path(entry_path, run_name="__main__")
        return
    if os.path.isfile(MODULE_PATH):
        sys.path.insert(0, os.path.dirname(ROOT))
        _ensure_package_module()
        basename = os.path.basename(MODULE_PATH)
        if basename == "__main__.py":
            runpy.run_module(PACKAGE_NAME, run_name="__main__")
            return
        stem = os.path.splitext(basename)[0]
        if stem:
            runpy.run_module(f"{{PACKAGE_NAME}}.{{stem}}", run_name="__main__")
            return
    raise FileNotFoundError(f"No entry script found for package at {{ROOT!r}}")

"""
    return wrapper

def _infer_package_metadata(pkg_dir: str, cmd_name: str | None = None, entry: str | None = None) -> dict:
    pkg_name = os.path.basename(os.path.abspath(pkg_dir)) or "sourcegen-app"
    resolved_entry = entry or next((candidate for candidate in ("__main__.py", "main.py", "app.py", "run.py") if os.path.isfile(os.path.join(pkg_dir, candidate))), None)
    return {
        "name": cmd_name or pkg_name,
        "command": cmd_name or pkg_name,
        "entry": resolved_entry,
        "version": "0.1.0",
        "description": f"Package {pkg_name}",
        "requires-python": ">=3.12",
    }

def _resolve_cached_publication_entry(publication_dir: str, subpath: str | None = None) -> str:
    if subpath:
        cleaned = subpath.replace("\\", "/").strip("/")
        if cleaned.endswith(".py"):
            candidate = os.path.join(publication_dir, *cleaned.split("/"))
            if os.path.isfile(candidate):
                return candidate
        pieces = [part for part in cleaned.split("/") if part]
        if pieces:
            candidate = os.path.join(publication_dir, *pieces)
            if os.path.isfile(candidate):
                return candidate
            package_candidate = os.path.join(candidate, "__main__.py")
            if os.path.isfile(package_candidate):
                return package_candidate
            file_candidate = candidate + ".py"
            if os.path.isfile(file_candidate):
                return file_candidate
    candidates = [
        os.path.join(publication_dir, "__main__.py"),
        os.path.join(publication_dir, "main.py"),
        os.path.join(publication_dir, "app.py"),
        os.path.join(publication_dir, "run.py"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise ValueError(f"No executable entry point was found in cached publication '{publication_dir}'.")

def _write_pyproject_toml(pkg_dir: str, cmd_name: str, metadata: dict | None = None) -> str:
    metadata = dict(metadata or _infer_package_metadata(pkg_dir, cmd_name))
    package_name = metadata.get("package_name") or metadata.get("publication") or metadata.get("name") or cmd_name or os.path.basename(pkg_dir)
    project_name = _sanitize_python_name(package_name)
    entry = metadata.get("entry") or metadata.get("main") or metadata.get("script") or next((candidate for candidate in ("__main__.py", "main.py", "app.py", "run.py") if os.path.isfile(os.path.join(pkg_dir, candidate))), None)
    if entry is None:
        raise ValueError(f"No valid entry script found in package directory '{pkg_dir}'")
    package_dir = os.path.abspath(pkg_dir)
    projected_root = os.path.basename(package_dir) or "sourcegen-app"
    projected_name = _sanitize_python_name(projected_root)
    dependency_entries = metadata.get("dependencies") or []
    dependencies_line = f"dependencies = [{_toml_list(dependency_entries)}]\n" if dependency_entries else ""
    wrapper_path = os.path.join(package_dir, "sourcegen_entry.py")
    with open(wrapper_path, "w", encoding="utf-8") as handle:
        handle.write(_build_entry_wrapper(entry, package_dir, metadata))
    pyproject_path = os.path.join(package_dir, "pyproject.toml")
    with open(pyproject_path, "w", encoding="utf-8") as handle:
        handle.write(
            "[build-system]\n"
            "requires = [\"setuptools>=68\"]\n"
            "build-backend = \"setuptools.build_meta\"\n\n"
            "[project]\n"
            f"name = \"{project_name}\"\n"
            f"version = \"{metadata.get('version', '0.1.0')}\"\n"
            f"description = \"{metadata.get('description', metadata.get('name', projected_name))}\"\n"
            f"requires-python = \"{metadata.get('requires-python', '>=3.12')}\"\n"
            + (dependencies_line + "\n" if dependencies_line else "") +
            "\n[project.scripts]\n"
            f"{cmd_name} = \"sourcegen_entry:main\"\n\n"
            "[tool.setuptools]\n"
            "py-modules = [\"sourcegen_entry\"]\n"
        )
    return pyproject_path

def _install_via_pipx(pkg_dir: str, command_name: str | None = None, metadata: dict | None = None) -> dict:
    if shutil.which("pipx") is None:
        raise RuntimeError("pipx is not installed or not on PATH")
    if metadata is None:
        metadata = _load_sourcepkg(pkg_dir) if os.path.isfile(os.path.join(pkg_dir, ".sourcepkg")) else _infer_package_metadata(pkg_dir, command_name)
    else:
        metadata = dict(metadata)
    resolved_name = command_name or metadata.get("command") or metadata.get("name") or os.path.basename(pkg_dir)
    metadata["command"] = resolved_name
    metadata["name"] = resolved_name
    pyproject_path = _write_pyproject_toml(pkg_dir, resolved_name, metadata)
    env = os.environ.copy()
    try:
        subprocess.run(["pipx", "uninstall", resolved_name], capture_output=True, text=True, env=env, check=False)
        install_result = subprocess.run(["pipx", "install", pkg_dir, "--force"], capture_output=True, text=True, env=env, check=False)
        if install_result.returncode != 0:
            output = (install_result.stderr or install_result.stdout or "").strip()
            lowered = output.lower()
            if any(token in lowered for token in ("already installed", "already exists", "already installed in pipx", "use 'pipx reinstall")):
                reinstall_result = subprocess.run(["pipx", "reinstall", resolved_name], capture_output=True, text=True, env=env, check=False)
                if reinstall_result.returncode != 0:
                    reinstall_output = (reinstall_result.stderr or reinstall_result.stdout or "").strip()
                    lowered = reinstall_output.lower()
                    if "nothing to reinstall" in lowered or "not installed" in lowered or "no app" in lowered:
                        return {
                            "command_name": resolved_name,
                            "source_path": pkg_dir,
                            "root": pkg_dir,
                            "target": os.path.join(pkg_dir, metadata.get("entry") or "__main__.py"),
                            "metadata": metadata,
                        }
                    if not reinstall_output:
                        reinstall_output = f"pipx exited with status {reinstall_result.returncode} while reinstalling '{resolved_name}'."
                    raise RuntimeError(
                        f"Could not reinstall '{resolved_name}' via pipx. pipx reported: {reinstall_output}"
                    )
                return {
                    "command_name": resolved_name,
                    "source_path": pkg_dir,
                    "root": pkg_dir,
                    "target": os.path.join(pkg_dir, metadata.get("entry") or "__main__.py"),
                    "metadata": metadata,
                }
            if not output:
                output = f"pipx exited with status {install_result.returncode} while installing '{resolved_name}'."
            raise RuntimeError(f"Could not install '{resolved_name}' via pipx. pipx reported: {output}")
    finally:
        for cleanup_path in (pyproject_path, os.path.join(pkg_dir, "sourcegen_entry.py")):
            if os.path.exists(cleanup_path):
                os.remove(cleanup_path)
    if os.path.isfile(os.path.join(pkg_dir, ".sourcepkg")):
        target = _resolve_sourcepkg_entry(pkg_dir, metadata)
    else:
        entry_name = metadata.get("entry") or metadata.get("main") or metadata.get("script")
        fallback = "__main__.py" if os.path.isfile(os.path.join(pkg_dir, "__main__.py")) else "main.py"
        target = os.path.join(pkg_dir, entry_name or fallback)
    return {
        "command_name": resolved_name,
        "source_path": pkg_dir,
        "root": pkg_dir,
        "target": target,
        "metadata": metadata,
    }

async def install_target(path: str, command_name: str | None = None, no_deps: bool = False) -> dict:
    pkg_dir = os.path.abspath(path)
    install_root = os.path.abspath(INTERNAL_PACKAGES_DIR)
    try:
        common_root = os.path.commonpath([install_root, pkg_dir])
    except ValueError:
        common_root = ""
    if common_root != install_root:
        raise ValueError(
            "Only cached installer-managed directories may be installed via pipx. "
            "Direct local package installs are disabled by default."
        )
    publication_dir_name = os.path.basename(pkg_dir)
    parsed = _parse_installed_publication_dir(publication_dir_name)
    if parsed is None:
        raise ValueError(
            "Only cached installer-managed directories may be installed via pipx. "
            "Direct local package installs are disabled by default."
        )
    publication_name, release = parsed
    return install_cached_publication(publication_name, release, command_name=command_name)

def install_cached_publication(pub: str, rel: str = "latest", *, command_name: str | None = None, cache_root: str | None = None) -> dict:
    publication_target = pub.strip().strip("/")
    subpath: str | None = None
    if "/" in publication_target:
        publication_name, subpath = publication_target.split("/", 1)
    else:
        publication_name = publication_target
    resolved_pub = parsepub(publication_name)
    target_root = cache_root or INTERNAL_PACKAGES_DIR
    publication_dir = os.path.join(target_root, _publication_leaf(resolved_pub, rel))
    if not os.path.isdir(publication_dir):
        raise FileNotFoundError(f"Publication '{resolved_pub}' release '{rel}' is not installed in the internal cache at '{publication_dir}'.")
    entry_target = _resolve_cached_publication_entry(publication_dir, subpath)
    relative_entry = os.path.relpath(entry_target, publication_dir).replace(os.sep, "/")
    raw_sourcepkg = _read_raw_sourcepkg(publication_dir) if os.path.isfile(os.path.join(publication_dir, ".sourcepkg")) else {}
    sourcepkg_metadata = _load_sourcepkg(publication_dir) if os.path.isfile(os.path.join(publication_dir, ".sourcepkg")) else {}
    publication_command_name = command_name or raw_sourcepkg.get("command") or raw_sourcepkg.get("name") or sourcepkg_metadata.get("command") or sourcepkg_metadata.get("name") or getAddressInfo(resolved_pub)["distro"].lower()
    metadata = _infer_package_metadata(publication_dir, publication_command_name, entry=relative_entry)
    metadata.update(sourcepkg_metadata)
    metadata["command"] = publication_command_name
    metadata["name"] = publication_command_name
    metadata["package_name"] = getAddressInfo(resolved_pub)["distro"].lower()
    metadata["publication"] = resolved_pub
    metadata["dependencies"] = _dependency_list(metadata.get("dependencies"), include_installer=bool(INSTALLER_PACKAGE_DEPENDENCY))
    result = _install_via_pipx(publication_dir, publication_command_name, metadata=metadata)
    result["publication"] = resolved_pub
    result["release"] = rel
    result["entry_path"] = relative_entry
    return result

def uninstall_target(command_name: str) -> dict:
    requested_name = (command_name or "").strip()
    package_name = parsepub(requested_name)
    if shutil.which("pipx") is None:
        raise RuntimeError("pipx is not installed or not on PATH.")
    result = subprocess.run(["pipx", "uninstall", package_name], capture_output=True, text=True)
    if result.returncode == 0:
        return {
            "command_name": requested_name,
            "package_name": package_name,
            "removed": True,
            "path": None,
        }
    output = (result.stderr or result.stdout or "").strip()
    if not output:
        output = f"The package '{package_name}' is not installed or could not be removed."
    if any(token in output.lower() for token in ("not installed", "not found", "no app")):
        raise RuntimeError(f"The command '{requested_name}' is not installed in pipx, so there was nothing to remove.")
    raise RuntimeError(f"The package '{package_name}' could not be removed. pipx reported: {output}")

@dataclass(slots=True)

class InstallResult:
    status: str
    lines: list[str]
    exit_code: int = 0

def _cli(text: str, color: str = "", bold: bool = False) -> str:
    prefix: str = f"{CLI_BOLD if bold else ''}{color}"
    return f"{prefix}{text}{CLI_RESET if prefix else ''}"

def _print_status(label: str, message: str, tone: str = "info") -> None:
    palette: dict[str, str] = {
        "info": CLI_INFO,
        "success": CLI_SUCCESS,
        "warning": CLI_WARNING,
        "error": CLI_ERROR,
        "muted": CLI_DIM
    }
    color: str = palette.get(tone, "")
    print(f"{_cli(f'[{label}]', color, bold=True)} {message}")

def _print_section(title: str) -> None:
    print(_cli(title, CLI_BOLD))

def _print_command(signature: str, description: str) -> None:
    print(f"  {_cli(signature, CLI_INFO)} {_cli('-', CLI_DIM)} {description}")

def _print_help() -> None:
    print(_cli(f"{NAME} v{VERSION}", CLI_INFO, bold=True))
    print(_cli(DESCRIPTION, CLI_DIM))
    print()
    _print_section("Usage")
    print(f"  {COMMAND} <command> [args]")
    print()
    _print_section("General")
    _print_command("get <publication> [release]", "Download a package using the configured source.")
    _print_command("rm <publication> [release]", "Delete one release or all installed releases for a publication.")
    _print_command("install <publication> [release]", "Install a cached package via pipx.")
    _print_command("uninstall <publication> [release]", "Uninstall an installed package by its publication name and optional release from the pipx cache.")
    _print_command("list", "List installed publications in the installer's internal cache.")
    _print_command("cache", "Show the total cache size and per-publication cache usage.")
    print()
    _print_section("Documentation")
    _print_command("readme", "Show the installer README.")
    _print_command("license", "Show the installer license.")
    _print_command("help", "Show this help message.")

def parsepub(pub: str) -> str:
    value = (pub or "").strip()
    if not value:
        return value
    lowered = value.lower()
    if lowered in PUBLICATION_CACHE:
        return PUBLICATION_CACHE[lowered]
    if lowered in REVERSE_PUBLICATION_CACHE:
        return lowered
    return value

def _publication_dirname(pub: str, rel: str, root: str = "packages") -> str:
    return os.path.join(root, _publication_leaf(pub, rel))

def _release_token(rel: str) -> str:
    return rel.replace(".", "_").replace("-", "_")

def _print_install_result(result: InstallResult, color: bool = True) -> None:
    labels: dict[str, str] = {
        "info": "skip",
        "success": "done",
        "error": "fail",
    }
    palette: dict[str, str] = {
        "info": CLI_INFO,
        "success": CLI_SUCCESS,
        "error": CLI_ERROR,
    }
    prefix: str = palette.get(result.status, "") if color else ""
    label: str = labels.get(result.status, "info")
    for line in result.lines:
        if prefix:
            print(f"{_cli(f'[{label}]', prefix, bold=True)} {line}")
        else:
            print(f"[{label}] {line}")

def _remove_publication_versions(install_root: str, pub: str, rel: str | None = None) -> int:
    if not os.path.isdir(install_root):
        return 0
    resolved_pub = parsepub(pub).lower()
    deleted = 0
    for name in os.listdir(install_root):
        full_path = os.path.join(install_root, name)
        if not os.path.isdir(full_path):
            continue
        marker = os.path.join(full_path, ".sourcegen-package.json")
        if os.path.isfile(marker):
            try:
                with open(marker, encoding="utf-8") as handle:
                    metadata = json.load(handle)
                package_name, package_release = str(metadata["package"]).lower(), str(metadata["release"])
            except (ValueError, KeyError, OSError, TypeError):
                continue
        else:
            # Do not delete unknown, non-Sourcegen directories.
            continue
        if package_name == resolved_pub and (rel is None or rel == package_release):
            shutil.rmtree(full_path)
            deleted += 1
    return deleted

def _parse_installed_publication_dir(dirname: str) -> tuple[str, str] | None:
    # A metadata marker permits arbitrary product names and release formats.
    marker = os.path.join(INTERNAL_PACKAGES_DIR, dirname, ".sourcegen-package.json")
    if os.path.isfile(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                info = json.load(f)
            return str(info["package"]), str(info["release"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
    # Fallback for archives cached before marker metadata existed.
    for symbol, publication in sorted(PUBLICATION_CACHE.items(), key=lambda p: -len(p[0])):
        if dirname.lower().startswith(symbol):
            suffix = dirname[len(symbol):]
            return publication, (suffix.replace("_", ".") if suffix else "latest")
    return dirname, "latest"  # Unversioned generic cache directory

def _queue_install_to_root(pub: str, rel: str, install_root: str, reinstall: bool = True, work_dir: str = ".", emit: bool = True, error: bool = False) -> asyncio.Task:
    resolved_pub: str = parsepub(pub)
    key: tuple[str, str, str] = (resolved_pub.lower(), rel, os.path.realpath(install_root))
    if key in running_installs:
        if emit:
            _print_status("wait", f"Already queued {resolved_pub.lower()} {rel} -> {install_root}", "muted")
        return running_installs[key]
    if emit:
        _print_status("queue", f"{resolved_pub.lower()} {rel} -> {install_root}", "info")
    task: asyncio.Task = asyncio.create_task(asyncio.to_thread(_install_publication_to_root, resolved_pub, rel, install_root, reinstall, work_dir, emit, error))
    running_installs[key] = task
    def cleanup(completed_task: asyncio.Task, install_key: tuple[str, str, str] = key) -> None:
        if running_installs.get(install_key) is completed_task:
            running_installs.pop(install_key, None)
    task.add_done_callback(cleanup)
    return task

async def _reinstall_project_libraries(project: str) -> None:
    install_root: str = os.path.join(project, "libraries", "packages")
    if not os.path.isdir(install_root):
        _print_status("miss", f"No library directory found at '{install_root}'.", "warning")
        raise SystemExit(1)
    entries: list[str] = [item for item in os.listdir(install_root) if os.path.isdir(os.path.join(install_root, item))]
    if not entries:
        _print_status("info", f"No installed libraries found in '{install_root}'.", "muted")
        return
    tasks: list[asyncio.Task] = []
    ignored: int = 0
    for entry in entries:
        parsed: tuple[str, str] | None = _parse_installed_publication_dir(entry)
        if parsed is None:
            ignored += 1
            _print_status("skip", f"Could not parse installed library directory '{entry}'.", "warning")
            continue
        pub, rel = parsed
        tasks.append(_queue_install_to_root(pub, rel, install_root, True))
    if not tasks:
        _print_status("miss", "No reinstallable libraries were found.", "warning")
        if ignored:
            _print_status("info", f"Ignored {ignored} unrecognized directory(ies).", "muted")
        return
    results: list[InstallResult] = await asyncio.gather(*tasks)
    failures: int = 0
    for result in results:
        _print_install_result(result)
        failures += int(bool(result.exit_code))
    if failures:
        _print_status("fail", f"Library reinstall finished with {failures} failure{'s' if failures != 1 else ''}.", "error")
        raise SystemExit(1)
    _print_status("done", f"Reinstalled {len(results)} librar{'y' if len(results) == 1 else 'ies'} from {install_root}.", "success")

def _install_publication(pub: str, rel: str, reinstall: bool = True) -> InstallResult:
    return _install_publication_to_root(pub, rel, "packages", reinstall)

def _install_publication_to_root(pub: str, rel: str, install_root: str, reinstall: bool = True, work_dir: str = ".", emit: bool = True, error: bool = False) -> InstallResult:
    pub = parsepub(pub)
    pub_lower: str = pub.lower()
    dirname: str = os.path.join(install_root, _publication_leaf(pub, rel))
    release_token: str = _release_token(rel)
    archive_path: str = os.path.join(work_dir, f"{pub_lower}-{release_token}.zip")
    extract_dir: str = os.path.join(work_dir, f"{pub_lower}-repo-{release_token}")
    custom_result = custom_install_package(pub, rel, install_root, work_dir)
    if custom_result is not None:
        if not custom_result.exit_code:
            os.makedirs(dirname, exist_ok=True)
            with open(os.path.join(dirname, ".sourcegen-package.json"), "w", encoding="utf-8") as marker:
                json.dump({"package": pub, "release": rel}, marker)
        return custom_result
    url: str = resolve_download_url(pub, rel)  # TODO: configure resolver above
    not_found: bool = False
    try:
        if os.path.exists(dirname) and not reinstall:
            if emit:
                _print_status("info", f"{pub_lower} {rel}: Publication is already installed.", "muted")
            return InstallResult("info", [f"{pub_lower} {rel}: Publication is already installed."])
        os.makedirs(work_dir, exist_ok=True)
        try:
            urlretrieve(
                url,
                archive_path,
            )
        except urllib.error.HTTPError:
            not_found = True
            return InstallResult(
                "error",
                [f"{pub_lower} {rel}: Could not find this release. Are you sure you spelled it right?"],
                1,
            )
        safeExtractZip(archive_path, extract_dir)  # Pulley: was an unchecked extractall
        os.makedirs(install_root, exist_ok=True)
        if os.path.exists(dirname):
            shutil.rmtree(dirname)
        source_root: str = resolve_extracted_source(extract_dir, pub, rel)
        if not os.path.isdir(source_root):
            raise ValueError(f"Download resolver returned invalid source directory: {source_root}")
        shutil.move(source_root, dirname)
        with open(os.path.join(dirname, ".sourcegen-package.json"), "w", encoding="utf-8") as marker:
            json.dump({"package": pub, "release": rel}, marker)
        if emit:
            _print_status("done", f"{pub_lower} {rel}: Installation complete!", "success")
        return InstallResult("success", [f"{pub_lower} {rel}: Installation complete!"])
    except Exception:
        return InstallResult(
            "error",
            [line for line in traceback.format_exc().split("\n") if line.strip()],
            1,
        )
    finally:
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir)
        if os.path.exists(archive_path):
            os.remove(archive_path)
        if error and not_found:
            raise FileNotFoundError(f"Could not find release '{rel}' for publication '{pub_lower}'.")

def _queue_install(pub: str, rel: str, reinstall: bool = True, install_root: str = "packages", work_dir: str = ".", emit: bool = True, error: bool = False) -> asyncio.Task:
    return _queue_install_to_root(pub, rel, install_root, reinstall, work_dir, emit, error=error)

async def install_async(pub: str, rel: str, reinstall: bool = True, color: bool = True, emit: bool = True, fatal: bool = True, install_root: str = "packages", work_dir: str = ".", error: bool = False) -> InstallResult:
    result: InstallResult = await _queue_install(pub, rel, reinstall, install_root, work_dir, emit, error)
    if emit:
        _print_install_result(result, color)
    if fatal and result.exit_code:
        raise SystemExit(result.exit_code)
    return result

def _run_coroutine_from_sync(coro):
    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}
    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - exercised via tests
            error["value"] = exc
    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result["value"]

class InvalidDistributionNameError(ValueError):
    """Raised when an address contains an invalid distribution name."""

class InvalidVersionError(ValueError):
    """Raised when an address contains an invalid release version."""

def getAddressInfo(address: str) -> dict:
    address = address.strip()
    if not address:
        raise ValueError("Address is empty.")
    home_registry: str = getconf("registry", "wednesware.org", config_path=CONFIG_PATH)
    addr_type: str = "registry" if "@" in address else ("local" if "#" in address else "")
    if not addr_type:
        addr_type = "local" if home_registry.startswith("#") else "registry"
    separator: str = "@" if addr_type == "registry" else "#"
    parts: list[str] = address.split(separator, maxsplit=1)
    if len(parts) == 1:
        parts.append(home_registry.removeprefix("#") if addr_type == "local" else home_registry)
    package_name: str = parts[0].strip()
    registry: str = parts[1].strip()
    version: str = "latest"
    if "=" in registry:
        registry, version = registry.split("=", maxsplit=1)
    elif "=" in package_name:
        package_name, version = package_name.split("=", maxsplit=1)
    package_parts: list[str] = package_name.split(".", maxsplit=1)
    if len(package_parts) == 2:
        author, distro = package_parts
    else:
        distro = package_parts[0]
        author = registry.split(".")[0].split(":")[0] if addr_type == "registry" else "localhost"
    if addr_type == "local" and not registry:
        registry = DSTBS_DIR
    return {
        "address": address,
        "author": author.lower(),
        "distro": distro.lower(),
        "registry": registry if addr_type == "local" else registry.lower(),
        "version": version,
        "type": addr_type
    }

@dataclass(slots=True)
class Result:
    status: str = "info"
    lines: list[str] = field(default_factory=list)
    exit_code: int = 0
    success: bool = False
    message: str = ""

def cli(text: str, color: str = "", bold: bool = False) -> str:
    prefix: str = f"{CLI_BOLD if bold else ''}{color}"
    return f"{prefix}{text}{CLI_RESET if prefix else ''}"

def printStatus(label: str, message: str, tone: str = "info") -> None:
    palette: dict[str, str] = {
        "info": CLI_INFO,
        "success": CLI_SUCCESS,
        "warning": CLI_WARNING,
        "error": CLI_ERROR,
        "muted": CLI_DIM
    }
    color: str = palette.get(tone, "")
    print(f"{cli(f'[{label}]', color, bold=True)} {message}")

def printSection(title: str) -> None:
    print(cli(title, CLI_BOLD))

def printCommand(signature: str, description: str) -> None:
    print(f"  {cli(signature, CLI_INFO)} {cli('-', CLI_DIM)} {description}")

def printResult(result: Result, color: bool = True) -> None:
    labels: dict[str, str] = {
        "info": "skip",
        "success": "done",
        "error": "fail",
    }
    palette: dict[str, str] = {
        "info": CLI_INFO,
        "success": CLI_SUCCESS,
        "error": CLI_ERROR
    }
    prefix: str = palette.get(result.status, "") if color else ""
    label: str = labels.get(result.status, "info")
    lines: list[str] = list(result.lines) if result.lines else ([result.message] if result.message else [])
    for line in lines:
        if line.startswith("Hint:"):
            line = line.removeprefix("Hint:").strip()
            label = "hint"
            prefix = palette.get("info", "") if color else ""
        if prefix:
            print(f"{cli(f'[{label}]', prefix, bold=True)} {line}")
        else:
            print(f"[{label}] {line}")

def printDistroLocationError(address: str, exc: Exception) -> None:
    """Print a friendly CLI error and retain diagnostics in the debug log."""
    logger.debug("Could not locate distribution %r.", address, exc_info=exc)
    message: str = " ".join(
        line.strip() for line in str(exc).splitlines() if line.strip()
    ) or f"Could not locate distribution '{address}'."
    printStatus("fail", message, "error")

def localRegistryPath(registry: str) -> str:
    path: str = os.path.expanduser(registry)
    if not os.path.isabs(path):
        path = os.path.abspath(os.path.join(os.getcwd(), path))
    return os.path.join(path, "dstbs")

def safeExtractZip(archive: str, destination: str) -> None:
    destination_abs: str = os.path.abspath(destination)
    with zipfile.ZipFile(archive) as archive_file:
        for member in archive_file.infolist():
            target: str = os.path.abspath(os.path.join(destination, member.filename))
            if os.path.commonpath((destination_abs, target)) != destination_abs:
                raise RuntimeError(f"Unsafe archive member: {member.filename}")
        archive_file.extractall(destination)

def safeExtractTar(archive: str, destination: str) -> None:
    destination_abs: str = os.path.abspath(destination)
    with tarfile.open(archive, "r:*") as archive_file:
        for member in archive_file.getmembers():
            target: str = os.path.abspath(os.path.join(destination, member.name))
            if os.path.commonpath((destination_abs, target)) != destination_abs:
                raise RuntimeError(f"Unsafe archive member: {member.name}")
            if member.issym() or member.islnk():
                raise RuntimeError(f"Links are not allowed in archives: {member.name}")
        archive_file.extractall(destination)

def findLocalRelease(local_root: str, author: str, distro: str, version: str) -> tuple[str, str]:
    candidates: list[str] = []
    if version != "latest":
        candidates.extend([
            os.path.join(local_root, author, distro, version),
            os.path.join(local_root, author, distro, f"{version}.zip"),
            os.path.join(local_root, author, distro, f"{version}.tar"),
            os.path.join(local_root, author, distro, f"{version}.tar.gz"),
            os.path.join(local_root, author, distro, f"{version}.tgz"),
            os.path.join(local_root, f"{author}.{distro}", version),
        ])
    if version == "latest":
        candidates.extend([
            os.path.join(local_root, author, distro),
            os.path.join(local_root, f"{author}.{distro}"),
        ])
    for candidate in candidates:
        if not os.path.exists(candidate):
            continue
        if version == "latest" and os.path.isdir(candidate):
            versions: list[str] = sorted(
                item for item in os.listdir(candidate)
                if os.path.isdir(os.path.join(candidate, item))
                or item.endswith((".zip", ".tar", ".tar.gz", ".tgz"))
            )
            if versions:
                selected: str = versions[-1]
                resolved: str = selected
                for suffix in (".tar.gz", ".tgz", ".zip", ".tar"):
                    resolved = resolved.removesuffix(suffix)
                return os.path.join(candidate, selected), resolved
        return candidate, version
    raise FileNotFoundError(
        f"Local distribution '{author}.{distro}={version}' was not found in '{local_root}'."
    )

def registryBaseUrl(registry: str) -> str:
    """Build the API base URL for a registry address."""
    normalized: str = registry.strip()
    if normalized.startswith(("http://", "https://")):
        return normalized.rstrip("/")
    if normalized.startswith(("127.0.0.1", "localhost")):
        return f"http://{normalized}".rstrip("/")
    return f"https://dstbs.{normalized}".rstrip("/")

def registryNetworkError(address: str, registry: str, exc: Exception) -> Result:
    error_text: str = str(exc)
    reason = getattr(exc, "reason", None)
    reason_text = str(reason or exc).lower()
    if isinstance(reason, socket.gaierror) or any(
        token in reason_text
        for token in (
            "name or service not known",
            "temporary failure in name resolution",
            "no address associated with hostname",
            "nodename nor servname provided",
        )
    ):
        message: str = (
            f"Registry '{registryBaseUrl(registry)}' could not be resolved. "
            f"Check DNS, internet access, or configure another registry with "
            f"'{COMMAND} registry <registry>'."
        )
    elif "not found" in reason_text:
        address_info: dict = getAddressInfo(address)
        message = f"Distribution '{address_info['author']}.{address_info['distro']}' was not found in registry '{address_info['registry']}'."
    else:
        message = f"Failed to contact registry '{registryBaseUrl(registry)}': {error_text}"
    return Result(
        status="error",
        success=False,
        lines=[f"Failed to install {address}: {message}"],
        message=message,
        exit_code=1,
    )

def findDistroLocation(address: str) -> str:
    info: dict = getAddressInfo(address)
    author: str = info["author"]
    distro: str = info["distro"]
    version: str = info["version"]
    name_pattern: str = r"[a-z0-9][a-z0-9_-]*"
    invalid_names: list[str] = [
        name for name in (author, distro)
        if not re.fullmatch(name_pattern, name)
    ]
    if invalid_names:
        raise InvalidDistributionNameError(
            f"Invalid distribution name '{author}.{distro}'."
        )
    if (
        not version
        or version != "latest"
        and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", version)
    ):
        raise InvalidVersionError(
            f"Invalid version '{version}'. Use 'latest' or a version containing only letters, numbers, '.', '_' and '-'."
        )
    display_name: str = f"{author}.{distro}={version}"
    if info["type"] == "local":
        local_root: str = localRegistryPath(info["registry"])
        try:
            source_path, resolved_version = findLocalRelease(
                local_root, author, distro, version
            )
        except FileNotFoundError as exc:
            distribution_paths: list[str] = [
                os.path.join(local_root, author, distro),
                os.path.join(local_root, f"{author}.{distro}"),
            ]
            if not any(os.path.exists(path) for path in distribution_paths):
                raise FileNotFoundError(
                    f"Could not find distribution '{author}.{distro}#'."
                ) from exc
            if version != "latest":
                raise FileNotFoundError(
                    f"Could not find version '{version}' of distribution '{author}.{distro}#'."
                ) from exc
            raise FileNotFoundError(
                f"Distribution: {display_name}\n"
                f"Registry: {local_root}\n"
                "Reason: no matching release was found."
            ) from exc
        if os.path.isdir(source_path):
            raise FileNotFoundError(
                f"Distribution: {author}.{distro}={resolved_version}\n"
                f"Found: {source_path}\n"
                "Reason: a release archive is required.\n"
                "Expected: .tar.gz or .tgz"
            )
        if not source_path.endswith((".tar.gz", ".tgz")):
            raise FileNotFoundError(
                f"Distribution: {author}.{distro}={resolved_version}\n"
                f"Found: {source_path}\n"
                "Reason: the release format is unsupported.\n"
                "Expected: .tar.gz or .tgz"
            )
        return os.path.abspath(source_path)
    base_url: str = registryBaseUrl(info["registry"])
    distribution_url: str = (
        f"{base_url}/v1/{urllib.parse.quote(author)}/"
        f"{urllib.parse.quote(distro)}"
    )
    lookup_stage: str = "distribution"
    try:
        with urllib.request.urlopen(distribution_url) as response:
            distribution: dict = json.load(response)
        resolved_version = version if version != "latest" else distribution["latest"]
        release_url: str = f"{distribution_url}/{urllib.parse.quote(resolved_version)}"
        lookup_stage = "release"
        with urllib.request.urlopen(release_url) as response:
            release: dict = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            if lookup_stage == "release":
                raise FileNotFoundError(
                    f"Version '{resolved_version}' was not found for distribution '{author}.{distro}'."
                ) from exc
            raise FileNotFoundError(
                f"Distribution '{author}.{distro}' was not found in registry '{info['registry']}'."
            ) from exc
        raise RuntimeError(
            f"Registry '{info['registry']}' returned HTTP {exc.code} while looking up '{display_name}'."
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Could not contact registry '{registryBaseUrl(info['registry'])}' while looking up '{display_name}': {exc.reason}"
        ) from exc
    artifacts: list = release.get("artifacts", [])
    for artifact in artifacts:
        artifact_name: str = artifact.get("name", "")
        if artifact_name.endswith((".tar.gz", ".tgz")):
            return f"{release_url}/{urllib.parse.quote(artifact_name)}"
    raise FileNotFoundError(
        f"Could not find an archive for '{author}.{distro}={resolved_version}' in registry '{info['registry']}'.\n"
        "  Expected: a .tar.gz or .tgz artifact"
    )

def viewDistro(address: str) -> Result:
    info = getAddressInfo(address)
    author = info["author"]
    distro = info["distro"]
    version = info["version"]
    try:
        if info["type"] == "local":
            source_path, resolved_version = findLocalRelease(
                localRegistryPath(info["registry"]), author, distro, version
            )
            artifact_name = "<directory>" if os.path.isdir(source_path) else os.path.basename(source_path)
            artifact_size = "directory" if os.path.isdir(source_path) else f"{os.path.getsize(source_path)} bytes"
            lines = [
                f"Distribution: {author}.{distro}",
                f"Release: {resolved_version}",
                f"Registry: {info['registry']}",
                f"Artifact: {artifact_name}",
                f"Size: {artifact_size}",
            ]
        else:
            base_url = registryBaseUrl(info["registry"])
            distribution_url = (
                f"{base_url}/v1/{urllib.parse.quote(author)}/"
                f"{urllib.parse.quote(distro)}"
            )
            with urllib.request.urlopen(distribution_url) as response:
                distribution = json.load(response)
            resolved_version = version if version != "latest" else distribution["latest"]
            release_url = f"{distribution_url}/{urllib.parse.quote(resolved_version)}"
            with urllib.request.urlopen(release_url) as response:
                release = json.load(response)
            artifacts = release.get("artifacts", [])
            lines = [
                f"Distribution: {release.get('author', author)}.{release.get('distribution', distro)}",
                f"Release: {release.get('release', resolved_version)}",
                f"Registry: {info['registry']}",
                f"Artifacts: {len(artifacts)}",
            ]
            for artifact in artifacts:
                lines.extend([
                    f"Artifact: {artifact.get('name', '<unnamed>')}",
                    f"Size: {artifact.get('size', 0)} bytes",
                    f"SHA-256: {artifact.get('sha256', '')}",
                ])
        return Result(
            status="success",
            success=True,
            lines=lines,
            message=f"Viewed {author}.{distro} release {resolved_version}",
            exit_code=0,
        )
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        if DISTRO_NOT_FOUND_TEXT in detail:
            message = f"Distribution '{author}.{distro}' was not found in registry '{info['registry']}'."
        else:
            message = f"Registry returned HTTP {exc.code}: {detail}"
        return Result(
            status="error",
            success=False,
            lines=[message],
            message=message,
            exit_code=1,
        )
    except urllib.error.URLError as exc:
        network_error = registryNetworkError(address, info["registry"], exc)
        return Result(
            status="error",
            success=False,
            lines=[f"Failed to view {address}: {network_error.message}"],
            message=network_error.message,
            exit_code=1,
        )
    except Exception as exc:
        message = f"Failed to view {address}: {exc}"
        return Result(status="error", success=False, lines=[message], message=message, exit_code=1)

def updateHomeRegistry(registry: str) -> None:
    config_data: dict = {}
    if os.path.isfile(str(CONFIG_PATH)):
        with open(str(CONFIG_PATH)) as config_file:
            config_data = json.load(config_file)
    config_data["registry"] = registry
    with open(str(CONFIG_PATH), "w") as config_file:
        json.dump(config_data, config_file, indent=4)
        config_file.write("\n")

def normalizeUsername(username: str | None) -> str | None:
    if username is None:
        return None
    username = username.strip()
    return username.lower() if username else None


def getStoredAuth() -> tuple[str | None, str | None]:
    raw_username = os.environ.get("DISTROBASE_AUTH_USER")
    if not raw_username:
        try:
            raw_username = getconf("auth_user", None, config_path=CONFIG_PATH)
        except Exception:
            raw_username = None
    if not raw_username:
        return None, None
    username = normalizeUsername(raw_username)
    if not username:
        return None, None
    password = os.environ.get("DISTROBASE_AUTH_PASSWORD")
    if not password and keyring is not None:
        for candidate in (raw_username, username):
            if not candidate:
                continue
            try:
                password = keyring.get_password("hydrogen-distrobase", candidate)
            except Exception:
                password = None
            if password:
                break
    if not password:
        password = _getFallbackPassword(username) or _getFallbackPassword(raw_username)
    return (username, password) if username and password else (None, None)

def _fallbackAuthKey() -> bytes:
    """Load or create a local, file-permission-protected key used to obscure stored passwords."""
    if os.path.isfile(FALLBACK_AUTH_KEY_FILE):
        with open(FALLBACK_AUTH_KEY_FILE, "rb") as key_file:
            key: bytes = key_file.read()
        if key:
            return key
    key = os.urandom(32)
    with open(FALLBACK_AUTH_KEY_FILE, "wb") as key_file:
        key_file.write(key)
    try:
        os.chmod(FALLBACK_AUTH_KEY_FILE, 0o600)
    except OSError:
        pass
    return key

def _xorBytes(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[i % len(key)] for i, byte in enumerate(data))

def _setFallbackPassword(username: str, password: str) -> None:
    key: bytes = _fallbackAuthKey()
    obscured: str = base64.b64encode(_xorBytes(password.encode("utf-8"), key)).decode("ascii")
    creds: dict = {}
    if os.path.isfile(FALLBACK_AUTH_FILE):
        try:
            with open(FALLBACK_AUTH_FILE) as creds_file:
                creds = json.load(creds_file)
        except Exception:
            creds = {}
    creds[username] = obscured
    with open(FALLBACK_AUTH_FILE, "w") as creds_file:
        json.dump(creds, creds_file)
    try:
        os.chmod(FALLBACK_AUTH_FILE, 0o600)
    except OSError:
        pass

def _getFallbackPassword(username: str) -> str | None:
    if not os.path.isfile(FALLBACK_AUTH_FILE):
        return None
    try:
        with open(FALLBACK_AUTH_FILE) as creds_file:
            creds: dict = json.load(creds_file)
        obscured = creds.get(username)
        if not obscured:
            return None
        return _xorBytes(base64.b64decode(obscured), _fallbackAuthKey()).decode("utf-8")
    except Exception:
        return None

def _deleteFallbackPassword(username: str) -> None:
    if not os.path.isfile(FALLBACK_AUTH_FILE):
        return
    try:
        with open(FALLBACK_AUTH_FILE) as creds_file:
            creds: dict = json.load(creds_file)
        creds.pop(username, None)
        with open(FALLBACK_AUTH_FILE, "w") as creds_file:
            json.dump(creds, creds_file)
    except Exception:
        pass

def _saveConfig(mutate) -> None:
    config_data: dict = {}
    if os.path.isfile(str(CONFIG_PATH)):
        with open(str(CONFIG_PATH)) as config_file:
            config_data = json.load(config_file)
    mutate(config_data)
    with open(str(CONFIG_PATH), "w") as config_file:
        json.dump(config_data, config_file, indent=4)
        config_file.write("\n")

async def loginUser(username: str, password: str | None) -> Result:
    username = normalizeUsername(username)
    if not username:
        return Result(
            status="error",
            success=False,
            lines=["A username is required."],
            message="A username is required.",
            exit_code=1,
        )
    if not password:
        try:
            password = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return Result(
                status="error",
                success=False,
                lines=["Login cancelled."],
                message="Login cancelled.",
                exit_code=1,
            )
    if not password:
        return Result(
            status="error",
            success=False,
            lines=["A password is required."],
            message="A password is required.",
            exit_code=1,
        )
    registry = getconf("registry", "wednesware.org", config_path=CONFIG_PATH).strip()
    base_url = registryBaseUrl(registry)
    auth_url = f"{base_url}/v1/users/{urllib.parse.quote(username)}"
    auth_req = urllib.request.Request(auth_url, method="GET")
    auth_req.add_header(
        "Authorization",
        "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii"),
    )
    try:
        with urllib.request.urlopen(auth_req) as response:
            if response.status != 200:
                raise urllib.error.HTTPError(
                    url=response.geturl() if hasattr(response, "geturl") else auth_url,
                    code=response.status,
                    msg="Authentication failed",
                    hdrs=response.headers,
                    fp=None,
                )
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        message = f"Authentication failed for '{username}'"
        if exc.code == 401:
            message = f"Incorrect password for '{username}'."
        return Result(
            status="error",
            success=False,
            lines=[message if not detail else f"{message}: {detail}"],
            message=message,
            exit_code=1,
        )
    except urllib.error.URLError as exc:
        return Result(
            status="error",
            success=False,
            lines=[f"Could not verify credentials for '{username}': {exc}"],
            message=f"Could not verify credentials for '{username}': {exc}",
            exit_code=1,
        )
    lines: list[str] = []
    keyring_error: Exception | None = None
    if keyring is not None:
        try:
            keyring.set_password("hydrogen-distrobase", username, password)
        except Exception as exc:
            keyring_error = exc
    try:
        _setFallbackPassword(username, password)
    except Exception as exc:
        if keyring_error is not None:
            return Result(
                status="error",
                success=False,
                lines=[f"Could not store credentials securely or in the local fallback store: {exc}"],
                message=f"Could not store credentials securely or in the local fallback store: {exc}",
                exit_code=1,
            )
        lines.append(
            f"Warning: could not write the local fallback credential cache: {exc}"
        )
    if keyring_error is not None:
        lines.append(
            "Hint: Install the 'keyring' package (`pip install keyring`) to store credentials in your OS's secure "
            "credential store instead of a locally protected file."
        )
    def mutate(config_data: dict) -> None:
        config_data["auth_user"] = username
        config_data.pop("auth_password", None)
    _saveConfig(mutate)
    lines.insert(0, f"Logged in as '{username}'.")
    return Result(
        status="success",
        success=True,
        lines=lines,
        message=f"Logged in as '{username}'.",
        exit_code=0,
    )

def logoutUser() -> Result:
    username, _ = getStoredAuth()
    if not username:
        return Result(
            status="info",
            success=True,
            lines=["Not logged in."],
            message="Not logged in.",
            exit_code=0,
        )
    if keyring is not None:
        try:
            keyring.delete_password("hydrogen-distrobase", username)
        except Exception:
            pass
    _deleteFallbackPassword(username)
    def mutate(config_data: dict) -> None:
        config_data.pop("auth_user", None)
        config_data.pop("auth_password", None)
    _saveConfig(mutate)
    return Result(
        status="success",
        success=True,
        lines=[f"Logged out of '{username}'."],
        message=f"Logged out of '{username}'.",
        exit_code=0,
    )

def whoami() -> Result:
    username, password = getStoredAuth()
    if not username or not password:
        return Result(
            status="info",
            success=True,
            lines=["Not logged in."],
            message="Not logged in.",
            exit_code=1,
        )
    registry: str = getconf("registry", "wednesware.org", config_path=CONFIG_PATH)
    return Result(
        status="success",
        success=True,
        lines=[f"Logged in as '{username}' (home registry: '{registry}')."],
        message=f"Logged in as '{username}'.",
        exit_code=0,
    )

def supportCommand() -> None:
    printSection("Support")
    print("Describe your issue or feedback in a single line:")
    try:
        message: str = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        printStatus("cancel", "Support message cancelled.", "warning")
        return
    if not message:
        printStatus("cancel", "No message entered.", "warning")
        return
    try:
        confirm: str = input(f"Send this message to {SUPPORT_EMAIL}? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        printStatus("cancel", "Support message cancelled.", "warning")
        return
    if confirm not in ("y", "yes"):
        printStatus("cancel", "Support message not sent.", "warning")
        return
    mailto_url: str = (
        f"mailto:{SUPPORT_EMAIL}"
        f"?subject={urllib.parse.quote(f'{NAME} Support Request')}"
        f"&body={urllib.parse.quote(message)}"
    )
    if webbrowser.open(mailto_url):
        printStatus("done", "Opened your email client to send the message.", "success")
    else:
        printStatus("fail", f"Could not open an email client. Please email {SUPPORT_EMAIL} manually.", "error")

async def publishDistro(
    project_path: str,
    address: str,
    username: str | None = None,
    password: str | None = None,
) -> Result:
    try:
        project_path = os.path.abspath(
            os.path.expanduser(project_path)
        )
        if not os.path.isdir(project_path):
            return Result(
                status="error",
                success=False,
                lines=[
                    f"Project directory not found: {project_path}"
                ],
                message=(
                    f"Project directory not found: "
                    f"{project_path}"
                ),
                exit_code=1,
            )
        info = getAddressInfo(address)
        if info["type"] != "registry" and info["type"] != "local":
            return Result(
                status="error",
                success=False,
                lines=[
                    f"Unsupported publish address: {address}"
                ],
                message=(
                    f"Unsupported publish address: {address}"
                ),
                exit_code=1,
            )
        author = info["author"]
        distro = info["distro"]
        version = info["version"]
        if version == "latest":
            return Result(
                status="error",
                success=False,
                lines=[
                    "A release version is required when publishing."
                ],
                message=(
                    "A release version is required when publishing."
                ),
                exit_code=1,
            )
        temp_dir = tempfile.mkdtemp(
            prefix="hydrogen-publish-"
        )
        try:
            # ---------------------------------------------------------
            # Build artifact
            # ---------------------------------------------------------
            archive = os.path.join(
                temp_dir,
                f"{version}.tar.gz",
            )
            printStatus(
                "build",
                f"Building project into {archive}...",
                "info",
            )
            with tarfile.open(
                archive,
                "w:gz",
            ) as tar:
                for root, dirs, files in os.walk(
                    project_path
                ):
                    dirs[:] = [
                        directory
                        for directory in dirs
                        if directory not in {
                            ".git",
                            "__pycache__",
                            ".venv",
                            "venv",
                            "node_modules",
                        }
                    ]
                    for file_name in files:
                        file_path = os.path.join(
                            root,
                            file_name,
                        )
                        if os.path.abspath(
                            file_path
                        ) == os.path.abspath(
                            archive
                        ):
                            continue
                        arcname = os.path.relpath(
                            file_path,
                            project_path,
                        )
                        printStatus(
                            "pack",
                            f"Packing {arcname}",
                            "info",
                        )
                        tar.add(
                            file_path,
                            arcname=arcname,
                        )
            printStatus(
                "done",
                f"Build complete in {archive}",
                "success",
            )
            if not os.path.isfile(archive):
                raise RuntimeError(
                    "Build did not produce an archive."
                )
            # ---------------------------------------------------------
            # Local registry
            # ---------------------------------------------------------
            if info["type"] == "local":
                local_root = localRegistryPath(
                    info["registry"]
                )
                # TODO: does local when it shouldn't
                release_dir = os.path.join(
                    local_root,
                    author,
                    distro,
                )
                os.makedirs(
                    release_dir,
                    exist_ok=True,
                )
                destination = os.path.join(
                    release_dir,
                    f"{version}.tar.gz",
                )
                if os.path.exists(destination):
                    raise RuntimeError(
                        f"Release {version} already exists."
                    )
                shutil.copy2(
                    archive,
                    destination,
                )
                return Result(
                    status="success",
                    success=True,
                    lines=[
                        (
                            f"Published "
                            f"{author}.{distro} "
                            f"release {version} "
                            f"to {release_dir}"
                        ),
                        (
                            f"Artifact: "
                            f"{os.path.basename(destination)}"
                        ),
                        (
                            f"Size: "
                            f"{os.path.getsize(destination)} bytes"
                        ),
                    ],
                    message=(
                        f"Published "
                        f"{author}.{distro} "
                        f"release {version} "
                        f"to {release_dir}"
                    ),
                    exit_code=0,
                )
            # ---------------------------------------------------------
            # Remote registry
            # ---------------------------------------------------------
            registry = info["registry"]
            base_url = registryBaseUrl(
                registry
            )
            url = (
                f"{base_url}/v1/"
                f"{urllib.parse.quote(author)}/"
                f"{urllib.parse.quote(distro)}/"
                f"{urllib.parse.quote(version)}"
            )
            boundary = (
                "----HydrogenPublish"
                + os.urandom(16).hex()
            )
            filename = os.path.basename(
                archive
            )
            with open(
                archive,
                "rb",
            ) as file:
                file_data = file.read()
            body = bytearray()
            body.extend(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; '
                    f'name="artifact"; '
                    f'filename="{filename}"\r\n'
                    f"Content-Type: application/gzip\r\n"
                    f"\r\n"
                ).encode()
            )
            body.extend(
                file_data
            )
            body.extend(
                (
                    f"\r\n"
                    f"--{boundary}--\r\n"
                ).encode()
            )
            username = normalizeUsername(username) if username else getStoredAuth()[0]
            password = password or getStoredAuth()[1]
            headers = {
                "Content-Type": (
                    "multipart/form-data; "
                    f"boundary={boundary}"
                ),
                "Content-Length": str(
                    len(body)
                ),
            }
            if username and password:
                auth_value = base64.b64encode(
                    f"{username}:{password}".encode("utf-8")
                ).decode("ascii")
                headers["Authorization"] = f"Basic {auth_value}"
            request = urllib.request.Request(
                url,
                data=bytes(body),
                method="POST",
                headers=headers,
            )
            printStatus(
                "send",
                (
                    f"Publishing "
                    f"{author}.{distro} "
                    f"release {version} "
                    f"to {registry}..."
                ),
                "info",
            )
            with urllib.request.urlopen(
                request
            ) as response:
                response_data = json.load(
                    response
                )
            artifact_name = response_data.get(
                "name",
                filename,
            )
            artifact_size = response_data.get(
                "size",
                os.path.getsize(archive),
            )
            artifact_sha256 = response_data.get(
                "sha256",
                "",
            )
            return Result(
                status="success",
                success=True,
                lines=[
                    (
                        f"Published "
                        f"{author}.{distro} "
                        f"release {version} "
                        f"to {registry}"
                    ),
                    (
                        f"Artifact: "
                        f"{artifact_name}"
                    ),
                    (
                        f"Size: "
                        f"{artifact_size} bytes"
                    ),
                    (
                        f"SHA-256: "
                        f"{artifact_sha256}"
                    ),
                ],
                message=(
                    f"Published "
                    f"{author}.{distro} "
                    f"release {version} "
                    f"to {registry}"
                ),
                exit_code=0,
            )
        finally:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True,
            )
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            )
        except Exception:
            detail = str(exc)
        return Result(
            status="error",
            success=False,
            lines=[
                (
                    f"Registry returned "
                    f"HTTP {exc.code}: {detail}"
                )
            ],
            message=(
                f"Registry returned "
                f"HTTP {exc.code}: {detail}"
            ),
            exit_code=1,
        )
    except urllib.error.URLError as exc:
        return Result(
            status="error",
            success=False,
            lines=[
                f"Could not reach registry: {exc}"
            ],
            message=(
                f"Could not reach registry: {exc}"
            ),
            exit_code=1,
        )
    except Exception as exc:
        return Result(
            status="error",
            success=False,
            lines=[
                f"Failed to publish {address}: {exc}"
            ],
            message=(
                f"Failed to publish {address}: {exc}"
            ),
            exit_code=1,
        )

async def registerUser(username: str, password: str, registry: str | None = None) -> Result:
    registry = (registry or getconf("registry", "wednesware.org", config_path=CONFIG_PATH)).strip()
    username = normalizeUsername(username)
    if not username or not password:
        return Result(
            status="error",
            success=False,
            lines=["A username and password are required."],
            message="A username and password are required.",
            exit_code=1,
        )
    base_url = registryBaseUrl(registry)
    req = urllib.request.Request(
        f"{base_url}/v1/users",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as response:
            payload = json.load(response)
        config_data: dict = {}
        if os.path.isfile(str(CONFIG_PATH)):
            with open(str(CONFIG_PATH)) as config_file:
                config_data = json.load(config_file)
        config_data["auth_user"] = username
        config_data.pop("auth_password", None)
        keyring_error: Exception | None = None
        if keyring is not None:
            try:
                keyring.set_password("hydrogen-distrobase", username, password)
            except Exception as exc:
                keyring_error = exc
        try:
            _setFallbackPassword(username, password)
        except Exception as exc:
            if keyring_error is not None:
                return Result(
                    status="error",
                    success=False,
                    lines=[f"Could not store credentials securely or in the local fallback store: {exc}"],
                    message=f"Could not store credentials securely or in the local fallback store: {exc}",
                    exit_code=1,
                )
        with open(str(CONFIG_PATH), "w") as config_file:
            json.dump(config_data, config_file, indent=4)
            config_file.write("\n")
        return Result(
            status="success",
            success=True,
            lines=[f"Created Distrobase account for '{username}' on '{registry}'."],
            message=f"Created Distrobase account for '{username}' on '{registry}'.",
            exit_code=0,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return Result(
            status="error",
            success=False,
            lines=[f"Registry rejected user creation: {detail}"],
            message=f"Registry rejected user creation: {detail}",
            exit_code=1,
        )
    except urllib.error.URLError as exc:
        return Result(
            status="error",
            success=False,
            lines=[f"Could not reach registry: {exc}"],
            message=f"Could not reach registry: {exc}",
            exit_code=1,
        )

async def viewUser(username: str, registry: str | None = None) -> Result:
    registry = (registry or getconf("registry", "wednesware.org", config_path=CONFIG_PATH)).strip()
    if not username or not username.strip():
        return Result(
            status="error",
            success=False,
            lines=["A username is required."],
            message="A username is required.",
            exit_code=1,
        )
    base_url = registryBaseUrl(registry)
    req = urllib.request.Request(
        f"{base_url}/v1/users/{urllib.parse.quote(username.strip())}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as response:
            payload = json.load(response)
        created_at = payload.get("createdAt", None)
        if created_at:
            dt: datetime = datetime.fromisoformat(created_at)
            seconds = (datetime.now(timezone.utc) - dt).total_seconds()
            time_str: str = dt.strftime("%H:%M, %d %B %Y") if seconds < 86400 else dt.strftime("%d %B %Y")
            if seconds < 60:
                time_since: str = f"{int(seconds)} seconds"
            elif seconds < 3600:
                time_since: str = f"{int(seconds // 60)} minutes"
            elif seconds < 86400:
                time_since: str = f"{int(seconds // 3600)} hours"
            elif seconds < 2592000:
                time_since: str = f"{int(seconds // 86400)} days"
            elif seconds < 31536000:
                time_since: str = f"{int(seconds // 2592000)} months"
            else:
                time_since: str = f"{int(seconds // 31536000)} years"
            lines = [
                f"User {payload.get('username', username)} - member since {time_str} ({time_since})",
            ]
        else:
            lines = [
                f"User {payload.get('username', username)} (account exists - unknown join date)",
            ]
        return Result(
            status="success",
            success=True,
            lines=lines,
            message=f"User '{payload.get('username', username)}' found.",
            exit_code=0,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return Result(
            status="error",
            success=False,
            lines=[f"Could not find user '{username}': {detail}"],
            message=f"Could not find user '{username}': {detail}",
            exit_code=1,
        )
    except urllib.error.URLError as exc:
        return Result(
            status="error",
            success=False,
            lines=[f"Could not reach registry: {exc}"],
            message=f"Could not reach registry: {exc}",
            exit_code=1,
        )

async def listUsers(registry: str | None = None) -> Result:
    registry = (registry or getconf("registry", "wednesware.org", config_path=CONFIG_PATH)).strip()
    base_url = registryBaseUrl(registry)
    req = urllib.request.Request(
        f"{base_url}/v1/users",
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as response:
            payload = json.load(response)
        users = payload if isinstance(payload, list) else []
        lines = [f"{item.get('username', '<unknown>')}" for item in users]
        if not lines:
            lines = ["No registered users."]
        return Result(
            status="success",
            success=True,
            lines=lines,
            message=f"Found {len(users)} registered user(s).",
            exit_code=0,
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return Result(
            status="error",
            success=False,
            lines=[f"Could not list users: {detail}"],
            message=f"Could not list users: {detail}",
            exit_code=1,
        )
    except urllib.error.URLError as exc:
        return Result(
            status="error",
            success=False,
            lines=[f"Could not reach registry: {exc}"],
            message=f"Could not reach registry: {exc}",
            exit_code=1,
        )

FALLBACK_AUTH_FILE = os.path.join(os.path.dirname(CONFIG_PATH), ".hydrogen_auth")
FALLBACK_AUTH_KEY_FILE = os.path.join(os.path.dirname(CONFIG_PATH), ".hydrogen_auth_key")

def _parse_distro_package(package: str, release: str) -> tuple[str, str]:
    info = getAddressInfo(package)
    if release != "latest":
        info["version"] = release
    # Preserve registry/local info in the address instead of discarding it.
    addr = package if release == "latest" else package + ("=" + release if "=" not in package else "")
    return addr, info["distro"]

def _publication_leaf(pub: str, rel: str) -> str:
    # Filesystem-safe canonical cache name for full Distrobase addresses.
    info = getAddressInfo(pub)
    name = f"{info['author']}.{info['distro']}@{info['registry']}={info['version'] if rel == 'latest' else rel}"
    return re.sub(r"[^a-zA-Z0-9._-]", "_", name)

def custom_install_package(package: str, release: str, install_root: str, work_dir: str) -> "InstallResult | None":
    # Use Sourcegen's install/cache machinery, with Hydrogen's own Distrobase
    # resolver for local and remote .tar.gz artifacts.
    dest = os.path.join(install_root, _publication_leaf(package, release))
    try:
        address, _ = _parse_distro_package(package, release)
        source = findDistroLocation(address)
        os.makedirs(work_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="hydrogen-fetch-", dir=work_dir) as temp:
            archive_path = os.path.join(temp, "artifact.tar.gz")
            if os.path.isfile(source):
                shutil.copyfile(source, archive_path)
            else:
                urlretrieve(source, archive_path)
            extracted = os.path.join(temp, "extracted")
            os.makedirs(extracted)
            safeExtractTar(archive_path, extracted)
            entries = os.listdir(extracted)
            root = os.path.join(extracted, entries[0]) if len(entries) == 1 and os.path.isdir(os.path.join(extracted, entries[0])) else extracted
            os.makedirs(install_root, exist_ok=True)
            staged = tempfile.mkdtemp(prefix=".hydrogen-install-", dir=install_root)
            try:
                for item in os.listdir(root):
                    a, b = os.path.join(root, item), os.path.join(staged, item)
                    if os.path.isdir(a): shutil.copytree(a,b,symlinks=True)
                    else: shutil.copy2(a,b)
                if os.path.exists(dest): shutil.rmtree(dest)
                os.replace(staged, dest)
            finally:
                if os.path.exists(staged): shutil.rmtree(staged)
        return InstallResult("success", [f"Installed {address} to {dest}"])
    except Exception as exc:
        return InstallResult("error", [f"Could not fetch {package}: {exc}"], 1)

def resolve_download_url(package: str, release: str) -> str:
    # The Distrobase flow uses custom_install_package instead of ZIP downloads.
    return findDistroLocation(package)

def resolve_extracted_source(extract_root: str, package: str, release: str) -> str:
    return extract_root

async def fetchDistro(address: str) -> str:
    # Fetching uses the Sourcegen installation engine without exposing its shared CLI.
    directory = tempfile.mkdtemp(prefix="hydrogen-fetch-")
    cache = os.path.join(directory, "distributions")
    result = await install_async(address, "latest", install_root=cache, work_dir=os.path.join(directory, "temp"), emit=False, fatal=False)
    if result.exit_code:
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError("; ".join(result.lines))
    return os.path.abspath(os.path.join(cache, _publication_leaf(address, "latest")))

# Hydrogen-specific commands retained from the original Sourcegen fork.
HYDROGEN_INSTALL_ROOT = "distrobase"

def _hydrogen_dir(root: str, address: str) -> str:
    return os.path.join(root, _publication_leaf(address, "latest"))

def removeAddressVersions(install_root: str, address: str) -> int:
    if not os.path.isdir(install_root):
        return 0
    if address.lower() == "all":
        targets = [os.path.join(install_root, name) for name in os.listdir(install_root)]
    else:
        info = getAddressInfo(address)
        prefix = re.sub(r"[^a-zA-Z0-9._-]", "_", f"{info['author']}.{info['distro']}@{info['registry']}=")
        exact = _publication_leaf(address, "latest")
        targets = [os.path.join(install_root, name) for name in os.listdir(install_root)
                   if name == exact or (info['version'] == 'latest' and name.startswith(prefix))]
    deleted = 0
    for target in targets:
        if os.path.isdir(target):
            shutil.rmtree(target)
            deleted += 1
    return deleted

async def installDistro(address: str, reinstall: bool = True, install_root: str = HYDROGEN_INSTALL_ROOT,
                        work_dir: str = ".", emit: bool = True) -> InstallResult:
    return await install_async(address, "latest", reinstall=reinstall, install_root=install_root,
                               work_dir=work_dir, emit=emit, fatal=False)

# Command compatibility audit: keep current Sourcegen commands and Hydrogen-only commands.
# Deprecated original-Sourcegen commands (getlib/getdep/forcegetdep/updlibs,
# getinternal/rminternal/getdepinternal/stage/compat/extensions) are omitted.
HYDROGEN_INSTALL_ROOT = "distrobase"

def _cache_entries() -> list[tuple[str, str]]:
    if not os.path.isdir(INTERNAL_PACKAGES_DIR):
        return []
    return sorted((name, os.path.join(INTERNAL_PACKAGES_DIR, name))
                  for name in os.listdir(INTERNAL_PACKAGES_DIR)
                  if os.path.isdir(os.path.join(INTERNAL_PACKAGES_DIR, name)) and name != "temp")

def _tree_size(path: str) -> int:
    total = 0
    for folder, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total

def _human_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"

def _remove_cached(address: str, release: str | None = None) -> int:
    if not os.path.isdir(INTERNAL_PACKAGES_DIR):
        return 0
    if address.lower() == "all":
        names = [name for name, _ in _cache_entries()]
    else:
        info = getAddressInfo(address)
        # An explicit release only targets that version; otherwise remove all releases.
        chosen = release or (info["version"] if info["version"] != "latest" else None)
        prefix = re.sub(r"[^a-zA-Z0-9._-]", "_", f"{info['author']}.{info['distro']}@{info['registry']}=")
        names = ([ _publication_leaf(address, chosen) ] if chosen else
                 [name for name, _ in _cache_entries() if name.startswith(prefix)])
    removed = 0
    for name in names:
        path = os.path.join(INTERNAL_PACKAGES_DIR, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
            removed += 1
    return removed

def printHelp() -> None:
    print(cli(f"{NAME} v{VERSION}", CLI_INFO, bold=True))
    print(DESCRIPTION)
    print(f"\nUsage: {COMMAND} <command> [args]\n")
    commands = (
        ("get <address> [release]", "Download a Distrobase distribution into the local Sourcegen cache"),
        ("rm <address|all> [release]", "Remove cached distribution releases"),
        ("install <address> [release]", "Install a cached distribution via pipx"),
        ("uninstall <command>", "Uninstall a pipx-installed application"),
        ("list", "List cached distributions"),
        ("cache", "Show per-distribution and total cache disk usage"),
        ("view <address>", "Show distribution and artifact details"),
        ("url <address>", "Resolve a release archive URL or local path"),
        ("fetch <address>", "Fetch and extract to a temporary directory"),
        ("publish <project> <address>", "Publish a release to a Distrobase registry"),
        ("registry <registry>", "Set the default Distrobase registry"),
        ("user create|view|list|me|login|logout", "Manage Distrobase accounts"),
        ("support", "Contact Wednesware support"),
        ("readme", "Show bundled README"),
        ("license", "Show bundled license"),
        ("help", "Show commands"),
    )
    for cmd, desc in commands:
        printCommand(cmd, desc)

async def main() -> None:
    if len(sys.argv) == 1:
        print(cli(f"{NAME} v{VERSION}", CLI_INFO, bold=True))
        print(DESCRIPTION)
        print(f"\nUsage: {COMMAND} <command> [args]\n")
        print(f"Run {_cli('h2 help', CLI_INFO)} for a full command list.")
        return
    cmd, args = sys.argv[1].lower(), sys.argv[2:]
    try:
        if cmd == "help" and not args:
            printHelp()
        elif cmd == "get" and len(args) in (1, 2):
            result = await install_async(args[0], args[1] if len(args) == 2 else "latest",
                                         install_root=INTERNAL_PACKAGES_DIR, work_dir=INTERNAL_TEMP_DIR,
                                         fatal=False)
            if result.exit_code:
                raise RuntimeError("; ".join(result.lines))
        elif cmd == "rm" and len(args) in (1, 2):
            removed = _remove_cached(args[0], args[1] if len(args) == 2 else None)
            printStatus("done", f"Removed {removed} cached distribution(s).", "success")
        elif cmd == "install" and len(args) in (1, 2):
            address, release = args[0], args[1] if len(args) == 2 else "latest"
            if not os.path.isdir(os.path.join(INTERNAL_PACKAGES_DIR, _publication_leaf(address, release))):
                raise FileNotFoundError(f"Distribution is not cached; run '{COMMAND} get {address} {release}' first.")
            result = install_cached_publication(address, release)
            printStatus("done", f"Installed '{result['command_name']}' via pipx.", "success")
        elif cmd == "uninstall" and len(args) == 1:
            result = uninstall_target(args[0])
            printStatus("done", f"Uninstalled '{result['package_name']}' from pipx.", "success")
        elif cmd == "list" and not args:
            entries = _cache_entries()
            if not entries:
                printStatus("empty", "No cached distributions.", "muted")
            for name, path in entries:
                print(f"{name}  {path}")
        elif cmd == "cache" and not args:
            total = 0
            for name, path in _cache_entries():
                size = _tree_size(path)
                total += size
                print(f"{name}: {_human_size(size)}")
            print(f"Total cache: {_human_size(total)}")
        elif cmd == "registry" and len(args) == 1:
            updateHomeRegistry(args[0])
            printStatus("done", f"Home registry set to '{args[0]}'", "success")
        elif cmd == "view" and len(args) == 1:
            result = viewDistro(args[0])
            printResult(result)
            if result.exit_code:
                raise SystemExit(result.exit_code)
        elif cmd == "url" and len(args) == 1:
            print(findDistroLocation(args[0]))
        elif cmd == "fetch" and len(args) == 1:
            print(await fetchDistro(args[0]))
        elif cmd == "publish" and len(args) == 2:
            result = await publishDistro(args[0], args[1])
            printResult(result)
            if result.exit_code:
                raise SystemExit(result.exit_code)
        elif cmd == "support" and not args:
            supportCommand()
        elif cmd in ("readme", "license") and not args:
            path = os.path.join(os.path.dirname(__file__), "README.md" if cmd == "readme" else "LICENSE")
            with open(path, encoding="utf-8") as handle:
                print(handle.read())
        elif cmd == "user":
            if args[:1] == ["create"] and len(args) == 3:
                result = await registerUser(args[1], args[2])
            elif args[:1] == ["view"] and len(args) == 2:
                result = await viewUser(args[1])
            elif args == ["list"]:
                result = await listUsers()
            elif args == ["me"]:
                result = whoami()
            elif args[:1] == ["login"] and len(args) in (2, 3):
                result = await loginUser(args[1], args[2] if len(args) == 3 else None)
            elif args == ["logout"]:
                result = logoutUser()
            else:
                raise ValueError("Usage: h2 user create|view|list|me|login|logout ...")
            printResult(result)
            if result.exit_code:
                raise SystemExit(result.exit_code)
        else:
            raise ValueError(f"Unknown command or incorrect arguments: {cmd}. Run '{COMMAND} help'.")
    except (ValueError, RuntimeError, FileNotFoundError, OSError, urllib.error.URLError) as exc:
        printStatus("fail", str(exc), "error")
        raise SystemExit(1) from exc

def entrypoint() -> None:
    asyncio.run(main())

if __name__ == "__main__":
    entrypoint()
