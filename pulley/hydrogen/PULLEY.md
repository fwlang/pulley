# Pulley Integration with Hydrogen

This Hydrogen release was, with permission from Wednesware, bundled in with standard pulley installations to provide seamless integration and management of Hydrogen distributions within the Pulley ecosystem.

## Bundled release

Hydrogen 26.7 (Sourcegen 26.66), from https://github.com/Wednesware/Hydrogen. Pulley imports it
as `pulley.hydrogen`; it is not installed as the separate `wwh` package.

Changes from upstream:

- `_install_publication_to_root` extracts zip archives with `safeExtractZip` instead of an
  unchecked `extractall`.
- `config.pyon` is removed. Hydrogen keeps its settings (`config.pyon`, `.hydrogen_auth`) in its
  own package directory, so a copy shipped inside Pulley would be shared by every user of that
  installation. Pulley passes Hydrogen fully qualified addresses and explicit credentials, so it
  never relies on those settings.

`.nitropkg` is the metadata Nitrogen (`wwn`) reads when it installs Hydrogen; pip and Pulley
ignore it.

To update: replace this directory with the new release, keep this file, `LICENSE.md`, and the
changes above, and run the tests.
