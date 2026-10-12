"""Regression checks for failed updates, untrusted archives and packaged assets."""
import io
import json
from pathlib import Path
import shutil
import stat
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
        core, dashboard, tailcat = self.core(), self.dashboard(), self.tailcat()
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
        def text(url):
            if url.endswith("Makefile"):
                return "PKG_VERSION:=0.48.002\n"
            if url.endswith("core_version"):
                return "alpha-g1234567\nalpha-smart-g7654321\n"
            if url.endswith("checksums.txt"):
                return utils.sha256(core) + "  ./clash-linux-arm64.tar.gz\n"
            return json.dumps(tailcat_release if "tailscale/tailcat" in url else release)
        # Exercise real hash verification and archive validation with controlled upstream inputs.
        def download(url, dest, expected=None):
            src = tailcat if "tailscale/tailcat" in url else core if url.endswith(".tar.gz") else dashboard
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
            self.assertEqual(result["schema_version"], 3)
            self.assertNotIn("commit", original["lede"])
            for upstream in (release, tailcat_release):
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

    def test_prepare_keeps_tailcat_and_system_defaults_in_shared_directory(self):
        lock = json.loads(json.dumps(prepare.LOCK))
        core, dashboard, tailcat = self.core(), self.dashboard(), self.tailcat()
        lock["openclash_core"]["binary_sha256"] = utils.extract_core(core, self.root / "core.bin")
        lock["tailcat"]["binary_sha256"] = utils.extract_tailcat(tailcat, self.root / "tailcat.bin")
        cache = self.root / "downloads"
        cache.mkdir()
        for component, archive, suffix in (("openclash_core", core, ".tar.gz"),
                                            ("zashboard", dashboard, ".zip"),
                                            ("tailcat", tailcat, ".tar.gz")):
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
        self.assertEqual((lede / ".config").read_bytes(),
                         (self.root / "configs/ax6000.config").read_bytes())
        run.assert_called_with("python3", str(self.root / "scripts/validate.py"), "config", cwd=lede)


if __name__ == "__main__":
    unittest.main()
