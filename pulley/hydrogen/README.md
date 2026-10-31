[![Wednesware](https://github.com/Wednesware/Nitrogen/raw/main/wednesware.png)](https://wednesware.org)

# Hydrogen

Sourcegen-based Distrobase installer.

## Installation

### With `pip`
    
> `pip install wwh`

### With `pipx`

> `pipx install wwh`

### With Nitrogen (library only)

> `n2 get hydrogen`

### With Nitrogen (library and command)

> `n2 get hydrogen && n2 install hydrogen`

## Dependencies

- Python 3.12+
- Optional: Keyring (`pip install keyring`)

# Commands

## Distributions

Hydrogen understands Distrobase addresses, optionally including an author, a registry, and a release version. When the registry is omitted, Hydrogen uses the configured home registry (`wednesware.org` by default). When the version is omitted, Hydrogen selects `latest`.

### `view <address>`

Show distribution release metadata and available artifacts, including sizes and SHA-256 hashes when supplied by the registry.

> `h2 view author.distribution@wednesware.org=1.0`

### `url <address>`

Resolve the URL of a `.tar.gz` or `.tgz` artifact for a distribution, or the path to the matching archive in a local registry.

> `h2 url author.distribution@wednesware.org=1.0`

### `fetch <address>`

Download and extract a distribution into a temporary directory, then print the installed directory path. Online artifacts and local releases use Hydrogen's Distrobase address resolution and Sourcegen's installation engine.

> `h2 fetch author.distribution@wednesware.org=1.0`

### `publish <project> <address>`

Package a project and publish a versioned distribution to a Distrobase registry. Publishing a remote release requires appropriate registry credentials; local registry publishing uses a filesystem path.

> `h2 publish ./myproject author.distribution@wednesware.org=1.0`

### `registry <registry>`

Set the default home registry used when a distribution address does not explicitly name one.

> `h2 registry wednesware.org`

## Users and accounts

### `user create <username> <password>`

Register a user with the configured Distrobase registry.

> `h2 user create exampleuser examplepassword`

### `user view <username>`

Display information about a registered user.

> `h2 user view exampleuser`

### `user list`

List registered users in the registry.

> `h2 user list`

### `user me`

Show the locally authenticated user.

> `h2 user me`

### `user login <username> [password]`

Authenticate against the registry and save credentials for publishing. Omitting the password allows Hydrogen to prompt for it. The optional `keyring` package enables credential storage through the OS keyring.

> `h2 user login exampleuser`

### `user logout`

Remove locally stored authentication information.

> `h2 user logout`

## Documentation and support

### `help`

Display Hydrogen's supported command reference.

> `h2 help`

### `support`

Compose a message to Wednesware support.

> `h2 support`

# Definitions

## Distrobase address

A remote distribution address follows the pattern `author.distribution@registry=version`. The version and registry may be omitted where a default is available.

> `example.project@wednesware.org=1.0`

Local registries use `#` instead of `@` and accept a filesystem location.

> `example.project#/path/to/registry=1.0`

## `hydrogen`

Hydrogen is also a Python module. Its public-facing Distrobase helpers include:

### `hydrogen.getAddressInfo(address: str) -> dict`

Parse a distribution address into its author, distribution name, registry, version, and registry type.

> `getAddressInfo("example.project@wednesware.org=1.0")`

### `hydrogen.findDistroLocation(address: str) -> str`

Resolve a distribution to the URL or local archive path for its release artifact.

> `findDistroLocation("example.project@wednesware.org=1.0")`

### `hydrogen.viewDistro(address: str) -> Result`

Retrieve distribution metadata in a structured result.

> `viewDistro("example.project@wednesware.org=1.0")`

### `hydrogen.fetchDistro(address: str) -> str` *(async)*

Fetch and extract a distribution through Sourcegen's internal caching engine, returning its filesystem path.

> `path = await fetchDistro("example.project@wednesware.org=1.0")`

### `hydrogen.publishDistro(project_path: str, address: str, username: str | None = None, password: str | None = None) -> Result` *(async)*

Build and publish a versioned distribution.

> `result = await publishDistro("./myproject", "example.project@wednesware.org=1.0")`

### `hydrogen.entrypoint() -> None`

Run Hydrogen's command-line interface.

> `entrypoint()`

## Relationship with Sourcegen

Hydrogen retains its Distrobase-specific functionality while using the newer Sourcegen core for package retrieval and caching. Sourcegen's generic `get`, `rm`, `install`, `uninstall`, `list`, `cache`, and documentation commands are **not** exposed as Hydrogen commands. Legacy staging, extension and dependency-file commands are also excluded.
