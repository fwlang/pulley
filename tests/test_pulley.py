"""Pulley tests: versions, manifests, and the command line against local and HTTP registries.

Run with `python -m unittest discover tests`. Set FLYWHEEL to a flywheel executable to also
compile and run programs that import the installed packages.
"""

from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from distrobase import Registry as MockRegistry  # noqa: E402
from pulley import manifest as manifests  # noqa: E402
from pulley.config import PulleyError  # noqa: E402
from pulley.versions import Constraint, Version, best  # noqa: E402

FLYWHEEL = os.environ.get("FLYWHEEL") or shutil.which("flywheel")
SOURCE = str(Path(__file__).resolve().parent.parent)


class VersionTests(unittest.TestCase):
    def allowed(self, constraint, versions):
        return [v for v in versions if Constraint(constraint).allows(v)]

    def test_constraints(self):
        self.assertEqual(self.allowed("^1.2", ["1.1", "1.2", "1.9.9", "2.0"]), ["1.2", "1.9.9"])
        self.assertEqual(self.allowed("^0.2.3", ["0.2.3", "0.2.9", "0.3"]), ["0.2.3", "0.2.9"])
        self.assertEqual(self.allowed("~1.2.3", ["1.2.2", "1.2.9", "1.3"]), ["1.2.9"])
        self.assertEqual(self.allowed(">= 1.0, <2", ["0.9", "1.5", "2.0"]), ["1.5"])
        self.assertEqual(self.allowed("1.2.*", ["1.2.5", "1.3"]), ["1.2.5"])
        self.assertEqual(self.allowed("*", ["1.0", "2.0-rc1"]), ["1.0"])
        self.assertEqual(self.allowed("=2.0-rc1", ["2.0-rc1", "2.0"]), ["2.0-rc1"])
        self.assertEqual(self.allowed("nightly", ["nightly", "1.0"]), ["nightly"])
        self.assertEqual(self.allowed("!=1.1", ["1.0", "1.1"]), ["1.0"])

    def test_ordering(self):
        self.assertEqual(best(["1.0", "1.10", "1.9"], [Constraint("^1")]), "1.10")
        ordered = sorted(["1.0-beta.10", "1.0", "1.0-beta.2", "1.0-alpha"], key=Version.parse)
        self.assertEqual(ordered, ["1.0-alpha", "1.0-beta.2", "1.0-beta.10", "1.0"])
        self.assertEqual(Version.parse("1.2"), Version.parse("1.2.0"))


class ManifestTests(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(manifests.parse_address("a.b"), ("a", "b", None, "*"))
        self.assertEqual(manifests.parse_address("a.b=^1.2"), ("a", "b", None, "^1.2"))
        self.assertEqual(manifests.parse_address("a.b@example.org=>=1, <2"), ("a", "b", "example.org", ">=1, <2"))
        self.assertEqual(manifests.parse_address("a.b#/srv/reg=1.0"), ("a", "b", "#/srv/reg", "1.0"))
        self.assertEqual(manifests.parse_address("A.B ~2"), ("a", "b", None, "~2"))
        for bad in ("noauthor", "a.b=^^", "a.B c=1"):
            with self.assertRaises(PulleyError):
                manifests.parse_address(bad)

    def test_editing_keeps_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pulley.toml"
            path.write_text('# top\n[package]\nname = "x"\n\n[dependencies]\n# keep me\nold = "a.old=1"\n\n[scripts]\n')
            manifests.set_value(path, "dependencies", "new", '"a.new=^2"')
            manifests.set_value(path, "dependencies", "old", '"a.old=^1"')
            manifests.set_value(path, "dev-dependencies", "t", '"a.t"')
            self.assertTrue(manifests.remove_value(path, "dependencies", "missing") is False)
            text = path.read_text()
            self.assertIn('# keep me\nold = "a.old=^1"\nnew = "a.new=^2"\n\n[scripts]', text)
            m = manifests.load(Path(directory))
            self.assertEqual(sorted(m.dependencies), ["new", "old"])
            self.assertEqual(list(m.dev_dependencies), ["t"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pulley-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = dict(os.environ, PULLEY_CONFIG_DIR=str(self.root / "config"),
                        PULLEY_CACHE_DIR=str(self.root / "cache"), NO_COLOR="1",
                        FLYWHEEL_HOME=str(self.root / "flywheel-home"),
                        PYTHONPATH=os.pathsep.join(filter(None, [SOURCE, os.environ.get("PYTHONPATH")])))
        self.env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
        for variable in ("PULLEY_REGISTRY", "PULLEY_AUTH_USER", "PULLEY_AUTH_PASSWORD",
                         "DISTROBASE_AUTH_USER", "DISTROBASE_AUTH_PASSWORD"):
            self.env.pop(variable, None)
        self.local = "#" + str(self.root / "registry")

    def pulley(self, *args, cwd=None, check=True, input=None):
        result = subprocess.run([sys.executable, "-m", "pulley", *map(str, args)], cwd=cwd or self.root,
                                env=self.env, capture_output=True, text=True, input=input,
                                timeout=120)
        if check and result.returncode:
            self.fail(f"pulley {' '.join(map(str, args))} failed:\n{result.stdout}\n{result.stderr}")
        return result

    def package(self, name, version, source, deps=(), registry=None):
        directory = self.root / "work" / f"{name}-{version}"
        self.pulley("new", directory, "--author", "acme", "--name", name)
        manifest = directory / "pulley.toml"
        manifest.write_text(manifest.read_text().replace('version = "0.1.0"', f'version = "{version}"'))
        (directory / f"{name}.fw").write_text(source)
        for dep in deps:
            self.pulley("add", dep, "--no-install", cwd=directory)
        self.pulley("publish", "--registry", registry or self.local, cwd=directory)
        return directory

    def run_flywheel(self, program: Path) -> str:
        result = subprocess.run([FLYWHEEL, "run", program], env=self.env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def publish_math(self, registry=None):
        for version in ("1.0.0", "1.1.0", "2.0.0"):
            self.package("mathx", version, "function double_it(int n) returns int\n    return n * 2\nend\n"
                         f'function version returns string\n    return "{version}"\nend\n', registry=registry)
        self.package("geo", "1.0.0", "import mathx\nfunction area(int w, int h) returns int\n"
                     "    return mathx.double_it(w * h) // 2\nend\n", ["acme.mathx=~1.0"], registry=registry)

    def test_local_registry_workflow(self):
        self.pulley("registry", self.local)
        self.publish_math()
        app = self.root / "app"
        self.pulley("new", app, "--author", "acme")
        self.pulley("add", "acme.geo", cwd=app)
        self.assertIn('geo = "acme.geo=^1.0.0"', (app / "pulley.toml").read_text())
        lock = (app / "pulley.lock").read_text()
        self.assertIn('distribution = "acme.mathx"\nversion = "1.0.0"', lock)  # ~1.0 from geo
        self.assertTrue((app / ".flywheel-modules" / "geo" / "geo.fw").is_file())
        self.assertTrue((app / ".flywheel-modules" / "mathx" / ".pulley.json").is_file())

        # A requirement that conflicts with geo's is refused and not recorded.
        result = self.pulley("add", "acme.mathx=^2", cwd=app, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no version of acme.mathx", result.stderr)
        self.assertIn("~1.0 (by acme.geo=1.0.0)", result.stderr)
        self.assertNotIn("mathx", (app / "pulley.toml").read_text())

        tree = self.pulley("list", "--tree", cwd=app).stdout
        self.assertIn("geo acme.geo=1.0.0", tree)
        self.assertIn("└── mathx acme.mathx=1.0.0", tree)
        outdated = self.pulley("outdated", cwd=app).stdout
        self.assertIn("2.0.0", outdated)

        # Path, git, and dev dependencies.
        helpers = self.root / "helpers"
        helpers.mkdir()
        (helpers / "pulley.toml").write_text('[package]\nname = "helpers"\n')
        (helpers / "helpers.fw").write_text('function hi returns string\n    return "hi"\nend\n')
        repo = self.root / "gitpkg"
        repo.mkdir()
        (repo / "gitpkg.fw").write_text('function tag returns string\n    return "git"\nend\n')
        git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-qm", "one"], cwd=repo, check=True)
        subprocess.run(["git", "tag", "v1"], cwd=repo, check=True)
        self.pulley("add", "--path", "../helpers", cwd=app)
        self.pulley("add", "--git", repo, "--rev", "v1", cwd=app)
        self.pulley("add", "-D", "acme.mathx=^1", "--as", "mtest", cwd=app)
        self.assertEqual((app / ".flywheel-modules" / "helpers.link").read_text().strip(), str(helpers.resolve()))
        self.assertIn("commit = ", (app / "pulley.lock").read_text())
        self.pulley("install", "--production", cwd=app)
        self.assertFalse((app / ".flywheel-modules" / "mtest").exists())
        self.pulley("install", cwd=app)
        self.assertTrue((app / ".flywheel-modules" / "mtest").exists())
        self.pulley("install", "--frozen", cwd=app)

        if FLYWHEEL:
            program = app / "app.fw"
            program.write_text("import geo\nimport mathx\nimport helpers\nimport gitpkg\n"
                               "print geo.area(3, 4), mathx.version(), helpers.hi(), gitpkg.tag()\n")
            self.assertEqual(self.run_flywheel(program), "12 1.0.0 hi git\n")

        # Removing a dependency removes what only it needed.
        self.pulley("remove", "geo", "helpers", "gitpkg", "mtest", cwd=app)
        self.assertEqual(sorted(p.name for p in (app / ".flywheel-modules").iterdir()), [])

    def test_lock_and_checksums(self):
        self.pulley("registry", self.local)
        self.publish_math()
        app = self.root / "app"
        self.pulley("new", app, "--author", "acme")
        self.pulley("add", "acme.mathx=^1", cwd=app)
        self.assertIn('version = "1.1.0"', (app / "pulley.lock").read_text())
        # A newer compatible release does not move a locked project until `update`.
        self.package("mathx", "1.2.0", "function version returns string\n    return \"1.2.0\"\nend\n")
        self.pulley("install", cwd=app)
        self.assertIn('version = "1.1.0"', (app / "pulley.lock").read_text())
        self.assertIn("1.1.0 -> 1.2.0", self.pulley("update", cwd=app).stdout)
        # A release whose contents changed after locking is refused.
        archive = self.root / "registry" / "dstbs" / "acme" / "mathx" / "1.2.0.tar.gz"
        original = archive.read_bytes()
        archive.write_bytes(original + b"\0")
        shutil.rmtree(self.root / "cache")
        shutil.rmtree(app / ".flywheel-modules")
        result = self.pulley("install", cwd=app, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("pulley.lock records", result.stderr)
        archive.write_bytes(original)
        self.pulley("install", cwd=app)
        # --frozen refuses a manifest that the lock does not match.
        manifest = app / "pulley.toml"
        manifest.write_text(manifest.read_text().replace("[dependencies]", '[dependencies]\nextra = "acme.mathx=^1"'))
        result = self.pulley("install", "--frozen", cwd=app, check=False)
        self.assertIn("disagree", result.stderr)

    def test_global_install(self):
        self.pulley("registry", self.local)
        self.publish_math()
        self.pulley("install", "-g", "acme.geo")
        modules = self.root / "flywheel-home" / "modules"
        self.assertTrue((modules / "geo" / "geo.fw").is_file())
        self.assertTrue((modules / "mathx").is_dir())
        if FLYWHEEL:
            script = self.root / "loose" / "script.fw"
            script.parent.mkdir()
            script.write_text("import geo\nprint geo.area(2, 5)\n")
            self.assertEqual(self.run_flywheel(script), "10\n")
        self.assertIn("geo acme.geo=1.0.0", self.pulley("list", "-g").stdout)
        self.pulley("uninstall", "-g", "geo")
        self.assertFalse((modules / "geo").exists())
        self.assertFalse((modules / "mathx").exists())

    def test_http_registry(self):
        for list_releases in (True, False):
            with self.subTest(list_releases=list_releases), MockRegistry(list_releases) as server:
                shutil.rmtree(self.root / "cache", ignore_errors=True)
                shutil.rmtree(self.root / "work", ignore_errors=True)
                shutil.rmtree(self.root / "app", ignore_errors=True)
                # Accounts and authentication.
                self.pulley("register", "acme", "--password-stdin", "--registry", server.address, input="s3cret\n")
                self.pulley("logout", "--registry", server.address)
                bad = self.pulley("login", "acme", "--password-stdin", "--registry", server.address,
                                  input="wrong\n", check=False)
                self.assertIn("incorrect username or password", bad.stderr)
                self.pulley("login", "acme", "--password-stdin", "--registry", server.address, input="s3cret\n")
                self.assertIn("acme @ " + server.address, self.pulley("whoami", "--registry", server.address).stdout)
                self.publish_math(registry=server.address)
                self.assertEqual(list(server.releases[("acme", "mathx")]), ["1.0.0", "1.1.0", "2.0.0"])
                info = self.pulley("info", "acme.mathx", "--registry", server.address).stdout
                self.assertIn("SHA-256:", info)

                app = self.root / "app"
                self.pulley("new", app, "--author", "acme")
                manifest = app / "pulley.toml"
                manifest.write_text(manifest.read_text() + f'\n[registry]\ndefault = "{server.address}"\n')
                if list_releases:
                    self.pulley("add", "acme.geo", cwd=app)
                    self.assertIn('version = "1.0.0"', (app / "pulley.lock").read_text())
                else:
                    # A registry that reports only `latest`: exact versions still resolve.
                    self.pulley("add", "acme.geo=1.0.0", "acme.mathx=1.0.0", cwd=app)
                    result = self.pulley("add", "acme.mathx=^1.1", cwd=app, check=False)
                    self.assertIn("Available: 2.0.0", result.stderr)
                self.assertTrue((app / ".flywheel-modules" / "mathx" / "mathx.fw").is_file())
                if FLYWHEEL:
                    program = app / "app.fw"
                    program.write_text("import geo\nprint geo.area(3, 4)\n")
                    self.assertEqual(self.run_flywheel(program), "12\n")
                # Publishing to another author is refused by the registry.
                other = self.root / "work" / "other"
                self.pulley("new", other, "--author", "someone")
                result = self.pulley("publish", "--registry", server.address, cwd=other, check=False)
                self.assertEqual(result.returncode, 1)

    def test_publish_checks_and_pack(self):
        project = self.root / "lib"
        self.pulley("new", project, "--author", "acme")
        (project / "notes.tmp").write_text("x")
        manifest = project / "pulley.toml"
        manifest.write_text(manifest.read_text().replace('main = "lib.fw"', 'main = "lib.fw"\nexclude = ["*.tmp"]'))
        dry = self.pulley("publish", "--dry-run", "--registry", self.local, cwd=project).stdout
        self.assertIn("lib.fw", dry)
        self.assertNotIn("notes.tmp", dry)
        self.assertFalse((self.root / "registry").exists())
        self.pulley("pack", "-o", self.root / "lib.tar.gz", cwd=project)
        self.assertTrue((self.root / "lib.tar.gz").is_file())
        (project / "lib.fw").unlink()
        result = self.pulley("publish", "--registry", self.local, cwd=project, check=False)
        self.assertIn("main module lib.fw does not exist", result.stderr)

    def test_remove_keeps_shared_dependencies(self):
        self.pulley("registry", self.local)
        self.publish_math()
        app = self.root / "app"
        self.pulley("new", app, "--author", "acme")
        self.pulley("add", "acme.geo", "acme.mathx=~1.0", cwd=app)
        self.pulley("remove", "geo", cwd=app)
        modules = app / ".flywheel-modules"
        self.assertFalse((modules / "geo").exists())
        self.assertTrue((modules / "mathx" / "mathx.fw").is_file())  # still a direct dependency
        self.assertNotIn("acme.geo", (app / "pulley.lock").read_text())
        result = self.pulley("remove", "nosuch", cwd=app, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("'nosuch' is not a dependency", result.stderr)

    def test_update_named_package(self):
        self.pulley("registry", self.local)
        self.publish_math()
        self.package("textx", "1.0.0", 'function v returns string\n    return "1.0.0"\nend\n')
        app = self.root / "app"
        self.pulley("new", app, "--author", "acme")
        self.pulley("add", "acme.mathx=^1", "acme.textx=^1", cwd=app)
        self.package("mathx", "1.2.0", 'function version returns string\n    return "1.2.0"\nend\n')
        self.package("textx", "1.1.0", 'function v returns string\n    return "1.1.0"\nend\n')
        output = self.pulley("update", "mathx", cwd=app).stdout
        self.assertIn("1.1.0 -> 1.2.0", output)
        lock = (app / "pulley.lock").read_text()
        self.assertIn('distribution = "acme.mathx"\nversion = "1.2.0"', lock)
        self.assertIn('distribution = "acme.textx"\nversion = "1.0.0"', lock)  # not named, not moved
        result = self.pulley("update", "nosuch", cwd=app, check=False)
        self.assertIn("not installed: nosuch", result.stderr)

    def test_unsafe_archives_are_refused(self):
        def publish_raw(name, add_members):
            archive = self.root / "registry" / "dstbs" / "acme" / name / "1.0.0.tar.gz"
            archive.parent.mkdir(parents=True)
            with tarfile.open(archive, "w:gz") as tar:
                add_members(tar)

        def regular(tar, path, data=b"x"):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        def symlink(tar):
            regular(tar, "pkg/link.fw")
            info = tarfile.TarInfo("pkg/escape")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc"
            tar.addfile(info)

        publish_raw("traversal", lambda tar: regular(tar, "../../outside.fw"))
        publish_raw("absolute", lambda tar: regular(tar, str(self.root / "absolute.fw")))
        publish_raw("symlink", symlink)
        app = self.root / "app"
        self.pulley("new", app, "--author", "acme")
        for name in ("traversal", "absolute", "symlink"):
            with self.subTest(name=name):
                result = self.pulley("add", f"acme.{name}", "--registry", self.local, cwd=app, check=False)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("cannot extract", result.stderr)
                self.assertNotIn(name, (app / "pulley.toml").read_text())
        self.assertFalse((self.root / "outside.fw").exists())
        self.assertFalse((self.root.parent / "outside.fw").exists())
        self.assertFalse((self.root / "absolute.fw").exists())

    def test_credentials(self):
        with MockRegistry() as server:
            self.pulley("register", "acme", "--password-stdin", "--registry", server.address, input="s3cret\n")
            self.pulley("login", "acme", "--password-stdin", "--registry", server.address, input="s3cret\n")
            stored = self.root / "config" / "credentials.json"
            self.assertIn('"password": "s3cret"', stored.read_text())
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            # Environment variables take precedence (for CI).
            self.env.update(PULLEY_AUTH_USER="ci", PULLEY_AUTH_PASSWORD="token")
            self.assertIn("ci @ ", self.pulley("whoami", "--registry", server.address).stdout)
            del self.env["PULLEY_AUTH_USER"], self.env["PULLEY_AUTH_PASSWORD"]
            self.assertIn("acme @ ", self.pulley("whoami", "--registry", server.address).stdout)
            # Credentials are per registry.
            self.assertEqual(self.pulley("whoami", "--registry", "other.example", check=False).returncode, 1)
            self.pulley("logout", "--registry", server.address)
            self.assertNotIn("s3cret", stored.read_text())
            self.assertEqual(self.pulley("whoami", "--registry", server.address, check=False).returncode, 1)
            result = self.pulley("login", "acme", "--password-stdin", "--registry", self.local, check=False)
            self.assertIn("local registries need no login", result.stderr)

    def test_run_and_cache(self):
        app = self.root / "app"
        self.pulley("new", app, "--author", "acme")
        manifest = app / "pulley.toml"
        script = f'{sys.executable} -c "import sys; print(*sys.argv[1:])"'
        text = manifest.read_text()
        manifest.write_text(text.replace("[scripts]", f"[scripts]\necho = '{script}'") if "[scripts]" in text
                            else text + f"\n[scripts]\necho = '{script}'\n")
        self.assertEqual(self.pulley("run", "echo", "a b", "c", cwd=app).stdout.splitlines()[-1], "a b c")
        result = self.pulley("run", "nosuch", cwd=app, check=False)
        self.assertIn("no script 'nosuch'", result.stderr)

        self.pulley("registry", self.local)
        self.publish_math()
        self.pulley("add", "acme.mathx=1.0.0", cwd=app)
        self.assertIn("1 release(s)", self.pulley("cache").stdout)
        self.pulley("cache", "clean")
        self.assertFalse((self.root / "cache").exists())
        # A clean cache does not stop installs: releases are fetched again.
        shutil.rmtree(app / ".flywheel-modules")
        self.pulley("install", cwd=app)
        self.assertTrue((app / ".flywheel-modules" / "mathx" / "mathx.fw").is_file())

    def test_registry_selection(self):
        self.assertIn("wednesware.org", self.pulley("registry").stdout)
        self.pulley("registry", "example.org")
        self.assertIn("example.org", self.pulley("registry").stdout)
        self.env["PULLEY_REGISTRY"] = "env.example"
        self.assertIn("env.example", self.pulley("registry").stdout)
        del self.env["PULLEY_REGISTRY"]
        self.pulley("registry", "--reset")
        self.assertIn("wednesware.org", self.pulley("registry").stdout)
        self.assertIn("global modules:", self.pulley("where").stdout)


if __name__ == "__main__":
    unittest.main()
