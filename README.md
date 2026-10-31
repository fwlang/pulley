# Pulley

Pulley is the package manager for [Flywheel](../fw). It installs Flywheel packages from
[Distrobase](https://dstbs.wednesware.org) registries, using
[Hydrogen](https://github.com/Wednesware/Hydrogen) to resolve, download, and publish
distributions. The registry is `wednesware.org` unless you choose another.

The command is `pulley`, also installed as `ppm`. (Both names are taken on PyPI, so the
distribution is called `fw-pulley`.)

```sh
pipx install ./pulley        # or: pip install ./pulley
pulley new app && cd app
pulley add wednesware.shapes # records shapes = "wednesware.shapes=^1.2.0" and installs it
flywheel run app.fw          # import shapes
```

Requirements: Python 3.12+ and `git` for git dependencies. Hydrogen is bundled with Pulley (as
`pulley.hydrogen`; see `pulley/hydrogen/PULLEY.md`) rather than installed from PyPI. Optionally,
install `keyring` (`pip install "fw-pulley[keyring]"`) to keep registry passwords in the system
keyring.

## Where packages go

| Install | Directory | Used by |
|---|---|---|
| `pulley install` (default) | `./.flywheel-modules/` in the project (next to `pulley.toml`) | programs in that project |
| `pulley install -g` | `modules/` of the Flywheel installation | every program run by that `flywheel` |

The installation directory is the one that holds `bin/flywheel` (or the directory of the
`flywheel` executable when it is not in a `bin` directory). Pulley finds `flywheel` on `PATH`;
`pulley config flywheel /path/to/flywheel`, `PULLEY_FLYWHEEL`, or `FLYWHEEL_HOME` (which the
compiler also honours, as `$FLYWHEEL_HOME/modules`) override it. `pulley where` shows the
directories in use.

The Flywheel compiler searches the nearest `.flywheel-modules` above the importing file, then
the installation's `modules`. Each package is a directory named by its import name, or a
`<name>.link` file holding the path of a path dependency. Pulley owns these entries: it writes a
`.pulley.json` record into each package directory and only removes entries it installed. Add
`.flywheel-modules/` to `.gitignore` (`pulley init` does) and commit `pulley.toml` and
`pulley.lock`.

## pulley.toml

```toml
[package]
name = "app"              # the distribution name
author = "danny"          # your Distrobase username; publishes as danny.app
version = "0.1.0"
description = "An example"
main = "app.fw"           # what `import app` loads (default <name>.fw)
license = "MIT"
exclude = ["drafts/*"]    # files left out of published releases

[registry]
default = "wednesware.org"    # optional, for this project

[dependencies]
shapes = "wednesware.shapes=^1.2"                  # import name = Distrobase address [= constraint]
text = "acme.text@example.org=~2.0"                # from another registry
vendored = "acme.vendored#/srv/registry=1.0"       # a local (filesystem) registry
geometry = { distribution = "acme.geometry", version = ">=1.0, <3", registry = "example.org" }
helpers = { path = "../helpers" }                  # a directory on disk
parser = { git = "https://example.com/parser.git", rev = "v2.1" }

[dev-dependencies]
fixtures = "danny.fixtures"

[scripts]
test = "flywheel test"
```

The key of each dependency is its import name (`import shapes`), so one distribution can be
imported under a different name. Dependencies of dependencies are installed under the names
their packages give them; two packages that want the same import name for different
distributions are reported as a conflict.

### Version constraints

| Constraint | Allows |
|---|---|
| `*`, `latest`, or none | any release |
| `1.2.3` or `=1.2.3` | exactly that release |
| `^1.2.3` | `>=1.2.3, <2.0.0` (`^0.2.3` is `>=0.2.3, <0.3.0`) |
| `~1.2.3` | `>=1.2.3, <1.3.0` (`~1` is `>=1, <2`) |
| `1.2.*` | `>=1.2, <1.3` |
| `>=1.0, <2`, `!=1.4` | every comparison together |

Versions are dotted numbers with an optional pre-release (`2.0.0-beta.1`); pre-releases are
chosen only when a constraint names one. `pulley add` without a constraint records `^` the newest
release.

## Resolution and the lock file

Every distribution resolves to one version for the whole project: the highest release that
satisfies the constraints of the project and of every package that depends on it. When no release
satisfies all of them, Pulley names each requirement and who made it:

```text
error: no version of acme.mathx on wednesware.org satisfies every requirement:
^2.0 (by app); ~1.0 (by acme.geo=1.0.0). Available: 1.0.0, 1.1.0, 2.0.0
```

A failed `add` leaves `pulley.toml` unchanged. `pulley.lock` records the chosen version of every
package with the SHA-256 of its release archive (and the commit of git dependencies). Later
installs reuse the locked versions while they still satisfy `pulley.toml`, and refuse an archive
whose checksum differs from the lock. `pulley update` moves packages to the newest versions their
constraints allow; `pulley install --frozen` fails instead of changing the lock (for CI).

Downloads are verified against the SHA-256 that the registry publishes and cached (see
`pulley cache`), so each release is fetched once per machine.

## Commands

| Command | Does |
|---|---|
| `pulley init [dir]`, `pulley new <dir>` | create `pulley.toml` (and a main module, a test, `.gitignore`) |
| `pulley add <package>...` | add dependencies (`author.name`, `author.name=^1.2`, `alias=author.name`, `--as name`, `--path dir`, `--git url --rev ref`, `-D` for dev) and install |
| `pulley install` (`i`, `get`) | install everything in `pulley.toml`; with packages, same as `add`; `--production` skips dev-dependencies; `--frozen` |
| `pulley install -g <package>` | install for the whole Flywheel installation |
| `pulley remove <name>...` (`rm`, `uninstall`) | remove dependencies (and packages only they needed); `-g` for global ones |
| `pulley update [name...]` (`up`) | re-resolve to the newest allowed versions |
| `pulley list [--tree]` (`ls`), `pulley outdated` | what is installed; what has newer releases |
| `pulley info <address>` (`view`) | a distribution's releases, artifacts, sizes, and checksums |
| `pulley publish [--dry-run]` | publish the project as `author.name=version` |
| `pulley pack [-o file]` | write the release archive without publishing |
| `pulley login [user]`, `logout`, `whoami`, `register <user>` | registry accounts |
| `pulley registry [name]` / `--reset` | show or set the default registry (`whereami` only shows it) |
| `pulley config [key] [value]` | settings: `registry`, `flywheel`, `global_dir` |
| `pulley run [script] [args]` | run a `[scripts]` entry in the project directory |
| `pulley cache [list\|dir\|clean]`, `pulley where` | the download cache; the directories in use |

Every command that talks to a registry accepts `--registry`. `-q` prints only results and errors;
`-v` shows resolution steps.

## Registries

A registry is named as Hydrogen names it: a host (`wednesware.org`, served from
`https://dstbs.wednesware.org`), `localhost:port` or `127.0.0.1:port`, a full `http(s)://`
URL, or `#/path/to/directory` for a local registry on disk (releases are stored as
`<path>/dstbs/<author>/<name>/<version>.tar.gz`, as Hydrogen publishes them). The registry used
is, most specific first: `--registry`, a dependency's own registry, `[registry] default` in
`pulley.toml`, `PULLEY_REGISTRY`, `pulley registry <name>`, and `wednesware.org`.

Pulley always passes Hydrogen fully qualified addresses (`author.name@registry=version`), so
Hydrogen's own home-registry setting does not affect it.

### Publishing

```sh
pulley register danny          # once, if you have no account
pulley login danny
pulley publish --dry-run       # list the files that would be published
pulley publish
```

A release contains the project's files except `.git`, `.flywheel-modules`, `pulley.lock`, and
`[package] exclude` patterns. Publishing checks that the name, author, version, and main module
are set and that no dependency is a local path. Registry credentials are kept per registry: in
the system keyring when `keyring` is installed, otherwise in a file readable only by you
(`credentials.json` in Pulley's configuration directory). `PULLEY_AUTH_USER` and
`PULLEY_AUTH_PASSWORD` (or Hydrogen's `DISTROBASE_AUTH_USER` / `DISTROBASE_AUTH_PASSWORD`)
override them, for CI.

Distrobase has no search endpoint, so Pulley has no `search` command.

A registry that lists only a distribution's `latest` release (rather than every release) still
works: exact versions, and constraints that the latest release satisfies, resolve; other
constraints report which releases are known.

## Files and environment

| Variable | Default |
|---|---|
| `PULLEY_REGISTRY` | the default registry |
| `PULLEY_CONFIG_DIR` | `~/.config/pulley` (`%APPDATA%\pulley` on Windows) |
| `PULLEY_CACHE_DIR` | `~/.cache/pulley` (`%LOCALAPPDATA%\pulley\cache`) |
| `PULLEY_FLYWHEEL` | the `flywheel` executable (default: on `PATH`) |
| `FLYWHEEL_HOME` | the Flywheel installation; global packages go to `$FLYWHEEL_HOME/modules` |
| `NO_COLOR` | plain output |

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e .
FLYWHEEL=../build/flywheel .venv/bin/python -m unittest discover tests
```

The tests use a local registry and an in-memory Distrobase server (`tests/distrobase.py`, with
the same API Hydrogen uses), and compile and run programs against the installed packages when
`FLYWHEEL` points to a compiler.
