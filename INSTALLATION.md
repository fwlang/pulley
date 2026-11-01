# Installing Pulley

Pulley is the Flywheel package manager. It is a Python program. Installing it gives you two commands, `pulley` and its alias `ppm`. Its distribution name is `fw-pulley`.

## Requirements

| | Linux | Windows |
|---|---|---|
| Python | 3.12 or later | 3.12 or later (python.org installer or the Microsoft Store) |
| Flywheel | [installed](../INSTALLATION.md) and on `PATH` | [installed](../INSTALLATION.md) and on `PATH` |
| Git | Only for `git` dependencies | Only for `git` dependencies |

Pulley's registry client, Hydrogen, comes bundled with it. Nothing else is downloaded at install time.

The recommended way to install it is **pipx**, which gives Pulley its own isolated environment and puts its commands on your `PATH`.

## Linux

### 1. Install Python and pipx

| Distribution | Command |
|---|---|
| Arch | `sudo pacman -S python python-pipx` |
| Debian / Ubuntu | `sudo apt install python3 pipx` |
| Fedora | `sudo dnf install python3 pipx` |

Then run this once so that pipx's command directory (`~/.local/bin`) is on `PATH`:

```sh
pipx ensurepath
```

Check that `python3 --version` reports 3.12 or later. If your distribution's Python is older, install a newer one and pass it to pipx with `pipx install --python python3.12 ...`.

### 2. Install Pulley

From this directory:

```sh
pipx install .
```

To keep registry passwords in the system keyring instead of a private file, install the `keyring` extra:

```sh
pipx install ".[keyring]"
```

### 3. Check it works

Open a new terminal, then run:

```sh
pulley --version
pulley where      # shows the Flywheel installation and package directories it found
```

## Windows

### 1. Install Python and pipx

Install Python 3.12 or later from <https://www.python.org/downloads/>, and tick **Add python.exe to PATH**. Then, in PowerShell:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
```

Open a new terminal so the `PATH` change takes effect.

### 2. Install Pulley

From this directory:

```powershell
pipx install .
```

Or, with system-keyring support (Windows Credential Manager):

```powershell
pipx install ".[keyring]"
```

### 3. Check it works

```powershell
pulley --version
pulley where
```

## Without pipx

You can also install Pulley into any virtual environment with plain pip:

```sh
python3 -m venv ~/.venvs/pulley
~/.venvs/pulley/bin/pip install .
```

On Windows, the equivalent is `py -m venv %USERPROFILE%\.venvs\pulley`, then `%USERPROFILE%\.venvs\pulley\Scripts\pip install .`. Afterwards, add the environment's `bin` (Linux) or `Scripts` (Windows) directory to `PATH`.

## Connecting Pulley to Flywheel

Pulley uses the `flywheel` it finds on `PATH`. It installs global packages (`pulley install -g`) into the `modules` directory of that Flywheel installation. If Flywheel isn't on `PATH`, or you have more than one installation, tell Pulley which one to use:

```sh
pulley config flywheel ~/.local/flywheel/bin/flywheel
```

On Windows:

```powershell
pulley config flywheel "$env:LOCALAPPDATA\Programs\Flywheel\bin\flywheel.exe"
```

Alternatively, set `PULLEY_FLYWHEEL` (the compiler) or `FLYWHEEL_HOME` (the installation directory). The compiler also uses `FLYWHEEL_HOME` to find global packages.

## First project

```sh
pulley new app
cd app
pulley add wednesware.shapes
flywheel run app.fw
```

See [README.md](README.md) for the full command reference.

## Updating and uninstalling

To update, pull the new sources and run:

```sh
pipx install --force .
```

To uninstall:

```sh
pipx uninstall fw-pulley
```

Settings, saved credentials and the download cache stay behind. Delete them by hand if you want:

| | Linux | Windows |
|---|---|---|
| Settings and credentials | `~/.config/pulley` | `%APPDATA%\pulley` |
| Download cache | `~/.cache/pulley` | `%LOCALAPPDATA%\pulley\cache` |

## Troubleshooting

| Problem | Fix |
|---|---|
| `pulley: command not found` | Run `pipx ensurepath` and open a new terminal |
| `requires a different Python` | Install Python 3.12+ and use `pipx install --python <that python> .` |
| `pulley where` shows no Flywheel installation | Put `flywheel` on `PATH`, or run `pulley config flywheel <path>` |
| Programs can't find a globally installed package | Make sure Pulley and your shell use the same `flywheel` (check with `pulley where`) |
| Git dependencies fail | Install Git and make sure `git` is on `PATH` |
