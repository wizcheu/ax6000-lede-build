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

    def theme(self, theme, version="1.0.2", extra=None):
        path = self.root / f"{theme}-{version}.tar.gz"
        files = {
            "Makefile": f"PKG_NAME:=luci-theme-{theme}\nPKG_VERSION:={version}\ninclude $(TOPDIR)/feeds/luci/luci.mk\n",
            "LICENSE": "license", "NOTICE": "notice",
            f"htdocs/luci-static/{theme}/{theme}.css": ":root{color:blue}",
            f"ucode/template/themes/{theme}/header.ut": "header",
            f"ucode/template/themes/{theme}/footer.ut": version,
            f"ucode/template/themes/{theme}/sysauth.ut": "login",
            f"root/etc/uci-defaults/30_luci-theme-{theme}": "#!/bin/sh\nexit 0\n",
            "preview/example.html": "must not enter build tree",
        }
        with tarfile.open(path, "w:gz") as tar:
            for name, text in files.items():
                data = text.encode()
                member = tarfile.TarInfo(f"luci-theme-{theme}/" + name)
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
        core, dashboard, tailcat = self.core(), self.dashboard(), self.tailcat()
        aeris, mono = self.theme("aeris"), self.theme("mono")
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
        mono_release = {"tag_name": "v1.0.2", "draft": False, "prerelease": False, "assets": [{
            "name": "luci-theme-mono-source.tar.gz", "digest": "sha256:" + utils.sha256(mono),
            "browser_download_url": "https://github.com/wizcheu/luci-theme-mono/releases/download/v1.0.2/luci-theme-mono-source.tar.gz",
        }]}
        def text(url):
            if url.endswith("Makefile"):
                return "PKG_VERSION:=0.48.002\n"
            if url.endswith("core_version"):
                return "alpha-g1234567\nalpha-smart-g7654321\n"
            if url.endswith("checksums.txt"):
                return utils.sha256(core) + "  ./clash-linux-arm64.tar.gz\n"
            if "wizcheu/luci-theme-mono" in url:
                return json.dumps(mono_release)
            if "wizcheu/luci-theme-aeris" in url:
                return json.dumps(aeris_release)
            return json.dumps(tailcat_release if "tailscale/tailcat" in url else release)
        # Exercise real hash verification and archive validation with controlled upstream inputs.
        def download(url, dest, expected=None):
            src = (mono if "wizcheu/luci-theme-mono" in url else aeris if "wizcheu/luci-theme-aeris" in url else
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
            self.assertEqual(result["mono"]["version"], "1.0.2")
            self.assertEqual(result["schema_version"], 5)
            self.assertNotIn("commit", original["lede"])
            for upstream in (release, tailcat_release, aeris_release, mono_release):
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

    def test_themes_reject_unsafe_archives_and_version_mismatch(self):
        for theme in utils.THEMES:
            link = tarfile.TarInfo(f"luci-theme-{theme}/root/link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            for extra in (tarfile.TarInfo(f"luci-theme-{theme}/../../escape"), link):
                with self.subTest(theme=theme, name=extra.name), self.assertRaisesRegex(ValueError, "Unsafe"):
                    utils.extract_theme(self.theme(theme, extra=extra), self.root / "rejected", theme, "1.0.2")
            with self.assertRaisesRegex(ValueError, "version/name"):
                utils.extract_theme(self.theme(theme), self.root / "rejected", theme, "9.9.9")
            self.assertFalse((self.root / "rejected").exists())
            dest = self.root / theme
            hashes = utils.extract_theme(self.theme(theme), dest, theme, "1.0.2")
            self.assertIn(f"www/luci-static/{theme}/{theme}.css", hashes)
            self.assertEqual((dest / f"root/etc/uci-defaults/30_luci-theme-{theme}").stat().st_mode & 0o777, 0o755)
            self.assertFalse((dest / "preview").exists())

    def test_every_refresh_updates_both_themes_and_migrates_old_build_tree(self):
        lede = self.root / "work/lede"
        metadata = lede / "files/etc/ax6000-build.json"
        utils.write_json(metadata, {"tailcat": {"version": "kept"}, "aeris": {"version": "old"}})
        (lede / ".config").write_text("CONFIG_PACKAGE_luci-theme-aeris=y\nCONFIG_PACKAGE_htop=y\n"
                                      "CONFIG_PACKAGE_luci-theme-material=y\nCONFIG_PACKAGE_luci=y\n")
        shutil.copytree(prepare.ROOT / "files", self.root / "files")
        lock = {"schema_version": 4, "aeris": {"version": "old"}, "lede": {"commit": "b" * 40}}
        utils.write_json(self.root / "sources.lock.json", lock)
        releases = []
        for versions in (("1.0.2", "1.0.2"), ("1.0.3", "2.0.0")):
            sources = {}
            for theme, version in zip(utils.THEMES, versions):
                archive = self.theme(theme, version)
                digest = utils.sha256(archive)
                cache = self.root / "downloads"
                cache.mkdir(exist_ok=True)
                shutil.copyfile(archive, cache / (digest + ".tar.gz"))
                sources[theme] = {"version": version, "tag": "v" + version, "sha256": digest}
            releases.append(sources)
        with patch.multiple(prepare, ROOT=self.root, LEDE=lede, LOCK=lock), \
                patch.object(prepare, "resolve_themes", side_effect=releases) as resolve, \
                patch("sys.stdout", new=io.StringIO()):
            for sources in releases:
                prepare.refresh_themes()
                actual = json.loads(metadata.read_text())
                for theme, source in sources.items():
                    self.assertEqual(actual[theme]["version"], source["version"])
                    self.assertEqual(utils.package_version((lede / f"package/custom/luci-theme-{theme}/Makefile").read_text()), source["version"])
                self.assertEqual(actual["tailcat"], {"version": "kept"})
                self.assertEqual(json.loads((self.root / "sources.lock.json").read_text())["lede"], lock["lede"])
            self.assertEqual(resolve.call_count, 2)
        config = (lede / ".config").read_text()
        self.assertIn("CONFIG_PACKAGE_htop=y", config)
        self.assertIn("CONFIG_PACKAGE_luci-theme-mono=y", config)
        self.assertIn("CONFIG_PACKAGE_luci-theme-aeris=y", config)
        self.assertIn("# CONFIG_PACKAGE_luci-theme-material is not set", config)
        self.assertIn("# CONFIG_PACKAGE_luci is not set", config)
        self.assertIn("luci.main.mediaurlbase='/luci-static/mono'",
                      (lede / "files/etc/uci-defaults/99-ax6000-build").read_text())
        before = metadata.read_bytes(), (self.root / "sources.lock.json").read_bytes()
        with patch.multiple(prepare, ROOT=self.root, LEDE=lede, LOCK=lock), \
                patch.object(prepare, "resolve_themes", side_effect=ValueError("release unavailable")):
            with self.assertRaisesRegex(ValueError, "release unavailable"):
                prepare.refresh_themes()
        self.assertEqual(before, (metadata.read_bytes(), (self.root / "sources.lock.json").read_bytes()))
        # A failure in the second theme must not replace the first theme either.
        sources = json.loads(json.dumps(releases[0]))
        sources["aeris"]["version"] = "9.9.9"
        snapshot = {p.relative_to(lede): p.read_bytes() for p in lede.rglob("*") if p.is_file()}
        with patch.multiple(prepare, ROOT=self.root, LEDE=lede, LOCK=lock), \
                patch.object(prepare, "resolve_themes", return_value=sources):
            with self.assertRaisesRegex(ValueError, "version/name"):
                prepare.refresh_themes()
        self.assertEqual(snapshot, {p.relative_to(lede): p.read_bytes() for p in lede.rglob("*") if p.is_file()})
        self.assertEqual(before, (metadata.read_bytes(), (self.root / "sources.lock.json").read_bytes()))

    def test_old_themes_and_argon_settings_are_rejected(self):
        allowed = {"luci-theme-mono", "luci-theme-aeris", "luci-base"}
        validate.validate_themes(allowed)
        for extra in ("luci-theme-bootstrap", "luci-theme-argon", "luci-theme-material", "luci-app-argon-config"):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                validate.validate_themes(allowed | {extra})
        for theme in utils.THEMES:
            with self.subTest(missing=theme), self.assertRaises(SystemExit):
                validate.validate_themes(allowed - {f"luci-theme-{theme}"})

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

    def test_build_refreshes_and_cleans_both_themes_before_download(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copyfile(prepare.ROOT / "scripts/build.sh", scripts / "build.sh")
        (self.root / "work/lede").mkdir(parents=True)
        bins = self.root / "bin"
        bins.mkdir()
        for name in ("python3", "make"):
            path = bins / name
            path.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$BUILD_CALLS"\n')
            path.chmod(0o755)
        calls = self.root / "calls"
        subprocess.run(["bash", str(scripts / "build.sh")], check=True, capture_output=True,
                       env=dict(os.environ, PATH=str(bins) + os.pathsep + os.environ["PATH"],
                                BUILD_CALLS=str(calls), BUILD_JOBS="1"))
        recorded = calls.read_text().splitlines()
        self.assertEqual(recorded[0], f"{scripts}/prepare.py --refresh-themes")
        self.assertLess(recorded.index("package/luci-theme-mono/clean package/luci-theme-aeris/clean"),
                        recorded.index("download -j8 V=s"))

    def test_final_image_checks_both_themes_versions_templates_and_assets(self):
        lede = self.root / "lede"
        metadata = lede / "files/etc/ax6000-build.json"
        bundled = {"openclash": {"version": "0.48.002"}, "mihomo": {"sha256": ""},
                   "zashboard": {"files": {}}, "tailcat": {"files": {}}}
        packages = {"luci-app-openclash": "0.48.002-r1"}
        contents = {"etc/openclash/core/clash_meta": b"core"}
        templates = []
        for theme in utils.THEMES:
            asset = f"www/luci-static/{theme}/{theme}.css"
            contents[asset] = theme.encode()
            bundled[theme] = {"version": "1.0.2", "files": {asset: utils.hashlib.sha256(contents[asset]).hexdigest()}}
            packages[f"luci-theme-{theme}"] = "1.0.2-r1"
            templates.extend(f"squashfs-root/usr/share/ucode/luci/template/themes/{theme}/{name}.ut"
                             for name in ("header", "footer", "sysauth"))
        bundled["mihomo"]["sha256"] = utils.hashlib.sha256(contents["etc/openclash/core/clash_meta"]).hexdigest()
        utils.write_json(metadata, bundled)
        contents["etc/ax6000-build.json"] = metadata.read_bytes()
        contents["etc/uci-defaults/99-ax6000-build"] = (prepare.ROOT / "files/etc/uci-defaults/99-ax6000-build").read_bytes()
        image = self.root / "image.bin"
        with tarfile.open(image, "w") as tar:
            tar.addfile(tarfile.TarInfo("sysupgrade/root"))
        def unsquashfs(args, **kwargs):
            return "\n".join(templates) if "-ls" in args else contents[args[-1]]
        with patch.object(validate, "LEDE", lede), \
                patch.object(validate.subprocess, "check_output", side_effect=unsquashfs), \
                patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(validate.verify_bundled(image, packages), bundled)
            for theme in utils.THEMES:
                with self.subTest(theme=theme):
                    package = f"luci-theme-{theme}"
                    packages[package] = "0.9.9-r1"
                    with self.assertRaisesRegex(SystemExit, f"Unexpected compiled {theme}"):
                        validate.verify_bundled(image, packages)
                    packages[package] = "1.0.2-r1"
                    template = f"squashfs-root/usr/share/ucode/luci/template/themes/{theme}/sysauth.ut"
                    templates.remove(template)
                    with self.assertRaisesRegex(SystemExit, f"{theme} template missing"):
                        validate.verify_bundled(image, packages)
                    templates.append(template)
                    asset = f"www/luci-static/{theme}/{theme}.css"
                    contents[asset] = b"stale"
                    with self.assertRaisesRegex(SystemExit, "Bundled file differs"):
                        validate.verify_bundled(image, packages)
                    contents[asset] = theme.encode()

    def test_defaults_select_mono_and_keep_exactly_two_theme_entries(self):
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
    state = {k: v for k, v in state.items() if k != args[1] and not k.startswith(args[1] + '.')}
p.write_text(json.dumps(state))
''')
        uci.chmod(0o755)
        state = self.root / "uci.json"
        initial = {"luci.main.mediaurlbase": "/luci-static/argon", "unrelated.setting": "keep"}
        initial.update({"luci.themes." + name: name for name in ("Bootstrap", "BootstrapDark", "BootstrapLight", "Argon", "Material", "old_custom")})
        utils.write_json(state, initial)
        env = dict(os.environ, PATH=str(bins) + os.pathsep + os.environ["PATH"], UCI_STATE=str(state))
        for _ in range(2):
            subprocess.run(["sh", str(prepare.ROOT / "files/etc/uci-defaults/99-ax6000-build")],
                           env=env, capture_output=True, check=True)
        actual = json.loads(state.read_text())
        self.assertEqual(actual["luci.main.mediaurlbase"], "/luci-static/mono")
        self.assertEqual({k for k in actual if k.startswith("luci.themes.")}, {"luci.themes.Mono", "luci.themes.Aeris"})
        self.assertEqual(actual["unrelated.setting"], "keep")

    def test_prepare_keeps_tailcat_and_system_defaults_in_shared_directory(self):
        lock = json.loads(json.dumps(prepare.LOCK))
        core, dashboard, tailcat = self.core(), self.dashboard(), self.tailcat()
        aeris, mono = self.theme("aeris"), self.theme("mono")
        lock["openclash_core"]["binary_sha256"] = utils.extract_core(core, self.root / "core.bin")
        lock["tailcat"]["binary_sha256"] = utils.extract_tailcat(tailcat, self.root / "tailcat.bin")
        cache = self.root / "downloads"
        cache.mkdir()
        for component, archive, suffix in (("openclash_core", core, ".tar.gz"),
                                            ("zashboard", dashboard, ".zip"),
                                            ("tailcat", tailcat, ".tar.gz"), ("aeris", aeris, ".tar.gz"),
                                            ("mono", mono, ".tar.gz")):
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
        self.assertIn("luci.main.mediaurlbase='/luci-static/mono'", system_defaults.read_text())
        for theme in utils.THEMES:
            package = lede / f"package/custom/luci-theme-{theme}"
            self.assertTrue((package / "Makefile").is_file())
            self.assertIn("# call BuildPackage", (package / "Makefile").read_text())
            self.assertIn("LUCI_MINIFY_JS:=0\nLUCI_MINIFY_CSS:=0", (package / "Makefile").read_text())
            self.assertTrue((package / f"root/usr/share/luci-theme-{theme}/LICENSE").is_file())
            self.assertFalse((package / "preview").exists())
        self.assertEqual((lede / ".config").read_bytes(),
                         (self.root / "configs/ax6000.config").read_bytes())
        run.assert_called_with("python3", str(self.root / "scripts/validate.py"), "config", cwd=lede)


if __name__ == "__main__":
    unittest.main()
