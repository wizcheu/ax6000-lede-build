"""Regression checks for failed updates, untrusted archives and packaged assets."""
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_utils as utils
import prepare
import update_sources
import validate


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def core(self, machine=183, extra=False):
        path = self.root / "core.tar.gz"
        elf = b"\x7fELF\x02\x01" + b"\0" * 12 + machine.to_bytes(2, "little")
        with tarfile.open(path, "w:gz") as tar:
            member = tarfile.TarInfo("clash")
            member.size = len(elf)
            tar.addfile(member, io.BytesIO(elf))
            if extra:
                tar.addfile(tarfile.TarInfo("unexpected"))
        return path

    def dashboard(self, extra=None, missing=False):
        path = self.root / "dashboard.zip"
        with zipfile.ZipFile(path, "w") as zipped:
            zipped.writestr("dist/index.html", '<script src="./assets/main.js"></script>')
            if not missing:
                zipped.writestr("dist/assets/main.js", "version='1.2.3'")
            if extra is not None:
                zipped.writestr(extra, "bad")
        return path

    def aeris(self, version="1.0.2", extra=None):
        path = self.root / f"aeris-{version}.tar.gz"
        files = {
            "Makefile": f"PKG_NAME:=luci-theme-aeris\nPKG_VERSION:={version}\n",
            "LICENSE": "license", "NOTICE": "notice",
            "htdocs/luci-static/aeris/aeris.css": ":root{color:blue}",
            "ucode/template/themes/aeris/header.ut": "header",
            "ucode/template/themes/aeris/footer.ut": version,
            "ucode/template/themes/aeris/sysauth.ut": "login",
            "root/etc/uci-defaults/30_luci-theme-aeris": "#!/bin/sh\nexit 0\n",
            "preview/example.html": "must not enter build tree",
        }
        with tarfile.open(path, "w:gz") as tar:
            for name, text in files.items():
                data = text.encode()
                member = tarfile.TarInfo("luci-theme-aeris/" + name)
                member.mode = 0o755 if name.startswith("root/etc/uci-defaults/") else 0o644
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            if extra:
                tar.addfile(extra)
        return path

    def tailcat(self, machine=183, program_type=1, name="tailcat", link=False):
        path = self.root / "tailcat.tar.gz"
        elf = bytearray(120)
        elf[:6] = b"\x7fELF\x02\x01"
        elf[18:20] = machine.to_bytes(2, "little")
        elf[32:40] = (64).to_bytes(8, "little")
        elf[54:56] = (56).to_bytes(2, "little")
        elf[56:58] = (1).to_bytes(2, "little")
        elf[64:68] = program_type.to_bytes(4, "little")
        with tarfile.open(path, "w:gz") as tar:
            for filename, content in ((name, elf), ("LICENSE", b"license"), ("README.md", b"readme")):
                member = tarfile.TarInfo(filename)
                member.size = len(content)
                if link and filename == name:
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/bin/sh"
                tar.addfile(member, io.BytesIO(content))
        return path

    def test_tailcat_rejects_wrong_arch_dynamic_and_unsafe_archives(self):
        for args in ({"machine": 62}, {"program_type": 2}, {"program_type": 3},
                     {"name": "../tailcat"}, {"name": "LICENSE"}, {"link": True}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                utils.extract_tailcat(self.tailcat(**args), self.root / "bin/tailcat")
        self.assertFalse((self.root / "bin/tailcat").exists())
        digest = utils.extract_tailcat(self.tailcat(), self.root / "bin/tailcat", self.root / "LICENSE")
        self.assertEqual(digest, utils.sha256(self.root / "bin/tailcat"))
        self.assertEqual((self.root / "LICENSE").read_bytes(), b"license")
        self.assertEqual((self.root / "bin/tailcat").stat().st_mode & 0o777, 0o755)

    def test_bad_download_preserves_previous_file(self):
        dest = self.root / "download"
        dest.write_bytes(b"previous")
        def fake_curl(args, **kwargs):
            Path(args[args.index("--output") + 1]).write_bytes(b"corrupted")
        with patch.object(utils.subprocess, "run", side_effect=fake_curl):
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                utils.fetch("https://example.com/asset", dest, "a" * 64)
        self.assertEqual(dest.read_bytes(), b"previous")

    def test_core_architecture_and_archive_members(self):
        for machine, extra in [(62, False), (183, True)]:
            with self.subTest(machine=machine, extra=extra), self.assertRaises(ValueError):
                utils.extract_core(self.core(machine, extra), self.root / "clash")
        digest = utils.extract_core(self.core(), self.root / "clash")
        self.assertEqual(digest, utils.sha256(self.root / "clash"))

    def test_github_auth_is_scoped_to_api_and_not_in_process_arguments(self):
        calls = []
        token = "ghs_test_only_placeholder"
        def fake_curl(args, **kwargs):
            self.assertFalse(any(token in arg for arg in args))
            uses_auth = "--config" in args
            if uses_auth:
                config = Path(args[args.index("--config") + 1])
                self.assertIn(token, config.read_text())
                self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            calls.append(uses_auth)
            Path(args[args.index("--output") + 1]).write_bytes(b"download")
        with patch.dict(os.environ, {"GITHUB_TOKEN": token}), patch.object(utils.subprocess, "run", side_effect=fake_curl):
            utils.fetch("https://api.github.com/repos/example/repo/releases/latest", self.root / "api")
            utils.fetch("https://github.com/example/repo/releases/download/v1/source.tar.gz", self.root / "asset")
        self.assertEqual(calls, [True, False])

    def test_dashboard_rejects_paths_and_links(self):
        link = zipfile.ZipInfo("dist/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entry in ("dist/../../escape", "/dist/escape", "dist/../escape", "dist\\escape", link):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                utils.extract_dashboard(self.dashboard(extra=entry), self.root / "panel")
        self.assertFalse((self.root / "panel").exists())

    def test_dashboard_requires_referenced_assets(self):
        with self.assertRaisesRegex(ValueError, "Missing or invalid dashboard asset"):
            utils.extract_dashboard(self.dashboard(missing=True), self.root / "missing")
        hashes = utils.extract_dashboard(self.dashboard(), self.root / "panel")
        self.assertEqual(set(hashes), {"index.html", "assets/main.js"})

    def test_release_and_core_snapshot_is_consistent(self):
        core, dashboard, tailcat, aeris = self.core(), self.dashboard(), self.tailcat(), self.aeris()
        original = {
            "lede": {"url": "https://example.com/lede.git", "ref": "HEAD"},
            "feeds": {}, "openclash": {"url": "https://example.com/openclash.git", "ref": "HEAD"},
            "openclash_core": {"repository": "https://example.com/core.git", "ref": "refs/heads/core"},
        }
        release = {"tag_name": "v1.2.3", "draft": False, "prerelease": False, "assets": [{
            "name": "dist-cdn-fonts.zip", "digest": "sha256:" + utils.sha256(dashboard),
            "browser_download_url": "https://github.com/Zephyruso/zashboard/releases/download/v1.2.3/dist-cdn-fonts.zip",
        }]}
        tailcat_release = {"tag_name": "v0.7.0", "draft": False, "prerelease": False, "assets": [{
            "name": "tailcat_0.7.0_linux_arm64.tar.gz", "digest": "sha256:" + utils.sha256(tailcat),
            "browser_download_url": "https://github.com/tailscale/tailcat/releases/download/v0.7.0/tailcat_0.7.0_linux_arm64.tar.gz",
        }]}
        aeris_release = {"tag_name": "v1.0.2", "draft": False, "prerelease": False, "assets": [{
            "name": "luci-theme-aeris-source.tar.gz", "digest": "sha256:" + utils.sha256(aeris),
            "browser_download_url": "https://github.com/wizcheu/luci-theme-aeris/releases/download/v1.0.2/luci-theme-aeris-source.tar.gz",
        }]}
        def text(url):
            if url.endswith("Makefile"):
                return "PKG_VERSION:=0.48.002\n"
            if url.endswith("core_version"):
                return "alpha-g1234567\nalpha-smart-g7654321\n"
            if url.endswith("checksums.txt"):
                return utils.sha256(core) + "  ./clash-linux-arm64.tar.gz\n"
            if "wizcheu/luci-theme-aeris" in url:
                return json.dumps(aeris_release)
            return json.dumps(tailcat_release if "tailscale/tailcat" in url else release)
        # Exercise real hash verification and archive validation with controlled upstream inputs.
        def download(url, dest, expected=None):
            src = (aeris if "wizcheu/luci-theme-aeris" in url else
                   tailcat if "tailscale/tailcat" in url else core if url.endswith(".tar.gz") else dashboard)
            if utils.sha256(src) != expected:
                raise ValueError("SHA256 mismatch")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
        with patch.object(update_sources, "latest", return_value="a" * 40), \
                patch.object(update_sources, "fetch_text", side_effect=text), \
                patch.object(utils, "fetch", side_effect=download):
            result = update_sources.resolve(original, self.root / "cache")
            self.assertEqual(result["openclash_core"]["version"], "alpha-g1234567")
            self.assertEqual(result["zashboard"]["version"], "1.2.3")
            self.assertEqual(result["tailcat"]["version"], "0.7.0")
            self.assertEqual(result["aeris"]["version"], "1.0.2")
            self.assertEqual(result["schema_version"], 4)
            self.assertNotIn("commit", original["lede"])
            for upstream in (release, tailcat_release, aeris_release):
                digest = upstream["assets"][0]["digest"]
                upstream["assets"][0]["digest"] = None
                with self.assertRaisesRegex(ValueError, "SHA256 digest unavailable"):
                    update_sources.resolve(original, self.root / "cache")
                upstream["assets"][0]["digest"] = digest

    def test_failed_refresh_keeps_lock_byte_identical(self):
        path = self.root / "sources.lock.json"
        path.write_text('{"previous": true}\n')
        before = path.read_bytes()
        with patch.object(update_sources, "ROOT", self.root), \
                patch.object(update_sources, "resolve", side_effect=ValueError("upstream unavailable")):
            with self.assertRaises(ValueError):
                update_sources.main()
        self.assertEqual(path.read_bytes(), before)

    def test_aeris_rejects_unsafe_archive_and_version_mismatch(self):
        link = tarfile.TarInfo("luci-theme-aeris/root/link")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        for extra in (tarfile.TarInfo("luci-theme-aeris/../../escape"), link):
            with self.subTest(name=extra.name), self.assertRaisesRegex(ValueError, "Unsafe Aeris"):
                utils.extract_aeris(self.aeris(extra=extra), self.root / "rejected", "1.0.2")
        with self.assertRaisesRegex(ValueError, "version/name"):
            utils.extract_aeris(self.aeris(), self.root / "rejected", "9.9.9")
        self.assertFalse((self.root / "rejected").exists())
        dest = self.root / "theme"
        hashes = utils.extract_aeris(self.aeris(), dest, "1.0.2")
        self.assertIn("www/luci-static/aeris/aeris.css", hashes)
        self.assertEqual((dest / "root/etc/uci-defaults/30_luci-theme-aeris").stat().st_mode & 0o777, 0o755)
        self.assertFalse((dest / "preview").exists())

    def test_every_aeris_refresh_replaces_only_theme_inputs(self):
        lede = self.root / "work/lede"
        metadata = lede / "files/etc/ax6000-build.json"
        utils.write_json(metadata, {"tailcat": {"version": "kept"}, "aeris": {"version": "old"}})
        lock = {"schema_version": 4, "aeris": {"version": "old"}, "lede": {"commit": "b" * 40}}
        utils.write_json(self.root / "sources.lock.json", lock)
        releases = []
        for version in ("1.0.2", "1.0.3"):
            archive = self.aeris(version)
            digest = utils.sha256(archive)
            cache = self.root / "downloads"
            cache.mkdir(exist_ok=True)
            shutil.copyfile(archive, cache / (digest + ".tar.gz"))
            releases.append({"version": version, "tag": "v" + version, "sha256": digest})
        with patch.multiple(prepare, ROOT=self.root, LEDE=lede, LOCK=lock), \
                patch.object(prepare, "resolve_aeris", side_effect=releases) as resolve, \
                patch("sys.stdout", new=io.StringIO()):
            for source in releases:
                prepare.refresh_aeris()
                actual = json.loads(metadata.read_text())
                self.assertEqual(actual["aeris"]["version"], source["version"])
                self.assertEqual(actual["tailcat"], {"version": "kept"})
                self.assertEqual(json.loads((self.root / "sources.lock.json").read_text())["lede"], lock["lede"])
                self.assertEqual(utils.package_version((lede / "package/custom/luci-theme-aeris/Makefile").read_text()), source["version"])
            self.assertEqual(resolve.call_count, 2)
        before = metadata.read_bytes(), (self.root / "sources.lock.json").read_bytes()
        with patch.multiple(prepare, ROOT=self.root, LEDE=lede, LOCK=lock), \
                patch.object(prepare, "resolve_aeris", side_effect=ValueError("release unavailable")):
            with self.assertRaisesRegex(ValueError, "release unavailable"):
                prepare.refresh_aeris()
        self.assertEqual(before, (metadata.read_bytes(), (self.root / "sources.lock.json").read_bytes()))

    def test_old_themes_and_argon_settings_are_rejected(self):
        validate.validate_themes({"luci-theme-aeris", "luci-base"})
        for extra in ("luci-theme-bootstrap", "luci-theme-argon", "luci-app-argon-config"):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                validate.validate_themes({"luci-theme-aeris", extra})
        with self.assertRaises(SystemExit):
            validate.validate_themes({"luci-base"})

    def test_compile_stops_before_make_if_latest_theme_cannot_be_resolved(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(prepare.ROOT / "scripts/build.sh", scripts / "build.sh")
        (self.root / "work/lede").mkdir(parents=True)
        bins = self.root / "bin"
        bins.mkdir()
        (bins / "python3").write_text('#!/bin/sh\nexit 17\n')
        (bins / "make").write_text('#!/bin/sh\ntouch "$MAKE_MARKER"\n')
        for path in bins.iterdir():
            path.chmod(0o755)
        marker = self.root / "make-was-called"
        result = subprocess.run(["bash", str(scripts / "build.sh")],
                                env=dict(os.environ, PATH=str(bins) + os.pathsep + os.environ["PATH"],
                                         MAKE_MARKER=str(marker)), capture_output=True)
        self.assertEqual(result.returncode, 17)
        self.assertFalse(marker.exists())

    def test_defaults_select_aeris_and_remove_stale_theme_entries(self):
        bins = self.root / "bin"
        bins.mkdir()
        uci = bins / "uci"
        uci.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
p = Path(os.environ['UCI_STATE'])
state = json.loads(p.read_text())
args = [a for a in sys.argv[1:] if a != '-q']
if args[0] == 'get':
    if args[1] not in state: sys.exit(1)
    print(state[args[1]])
elif args[0] == 'set':
    key, value = args[1].split('=', 1); state[key] = value
elif args[0] == 'delete':
    state.pop(args[1], None)
p.write_text(json.dumps(state))
''')
        uci.chmod(0o755)
        state = self.root / "uci.json"
        initial = {"luci.main.mediaurlbase": "/luci-static/argon", "unrelated.setting": "keep"}
        initial.update({"luci.themes." + name: name for name in ("Bootstrap", "BootstrapDark", "BootstrapLight", "Argon")})
        utils.write_json(state, initial)
        env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ["PATH"], UCI_STATE=str(state))
        for _ in range(2):
            subprocess.run(["sh", str(prepare.ROOT / "files/etc/uci-defaults/99-ax6000-build")],
                           env=env, capture_output=True, check=True)
        actual = json.loads(state.read_text())
        self.assertEqual(actual["luci.main.mediaurlbase"], "/luci-static/aeris")
        self.assertEqual({k for k in actual if k.startswith("luci.themes.")}, {"luci.themes.Aeris"})
        self.assertEqual(actual["unrelated.setting"], "keep")

    def test_prepare_keeps_tailcat_and_system_defaults_in_shared_directory(self):
        lock = json.loads(json.dumps(prepare.LOCK))
        core, dashboard, tailcat, aeris = self.core(), self.dashboard(), self.tailcat(), self.aeris()
        lock["openclash_core"]["binary_sha256"] = utils.extract_core(core, self.root / "core.bin")
        lock["tailcat"]["binary_sha256"] = utils.extract_tailcat(tailcat, self.root / "tailcat.bin")
        cache = self.root / "downloads"
        cache.mkdir()
        for component, archive, suffix in (("openclash_core", core, ".tar.gz"),
                                            ("zashboard", dashboard, ".zip"),
                                            ("tailcat", tailcat, ".tar.gz"), ("aeris", aeris, ".tar.gz")):
            digest = lock[component]["sha256"] = utils.sha256(archive)
            shutil.copyfile(archive, cache / (digest + suffix))
        shutil.copytree(prepare.ROOT / "files", self.root / "files")
        shutil.copytree(prepare.ROOT / "configs", self.root / "configs")
        work = self.root / "work"
        lede = work / "lede"

        def checkout(source, dest):
            dest.mkdir(parents=True)
            if dest == work / "openclash":
                package = dest / "luci-app-openclash"
                package.mkdir()
                (package / "Makefile").write_text(f"PKG_VERSION:={lock['openclash']['version']}\n")

        # Keep file assembly real; stub only source checkout and external build commands.
        with patch.multiple(prepare, ROOT=self.root, WORK=work, LEDE=lede, LOCK=lock), \
                patch.object(prepare, "checkout", side_effect=checkout), \
                patch.object(prepare, "run") as run, patch("sys.stdout", new=io.StringIO()):
            prepare.main()
        defaults = lede / "files/etc/uci-defaults"
        migration = "98-tailcat-bundle"
        self.assertEqual((defaults / migration).read_bytes(),
                         (self.root / "files/etc/uci-defaults" / migration).read_bytes())
        system_defaults = defaults / "99-ax6000-build"
        self.assertIn("uci -q set openclash.config.enable='0'", system_defaults.read_text())
        self.assertEqual(system_defaults.stat().st_mode & 0o777, 0o755)
        self.assertIn("luci.main.mediaurlbase='/luci-static/aeris'", system_defaults.read_text())
        self.assertTrue((lede / "package/custom/luci-theme-aeris/Makefile").is_file())
        self.assertFalse((lede / "package/custom/luci-theme-aeris/preview").exists())
        self.assertEqual((lede / ".config").read_bytes(),
                         (self.root / "configs/ax6000.config").read_bytes())
        run.assert_called_with("python3", str(self.root / "scripts/validate.py"), "config", cwd=lede)


if __name__ == "__main__":
    unittest.main()
