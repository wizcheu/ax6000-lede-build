#!/usr/bin/env python3
"""Fetch exact source revisions and assemble an isolated LEDE build tree."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from source_utils import (THEMES, extract_theme, extract_core, extract_dashboard, extract_tailcat, locked_download,
                          package_version, sha256, write_json)
from update_sources import resolve_themes

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work"
LEDE = WORK / "lede"
LOCK = json.loads((ROOT / "sources.lock.json").read_text())


def run(*args, cwd=ROOT):
    subprocess.run(args, cwd=cwd, check=True)


def checkout(source, dest):
    sha = source["commit"]
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    if dest.exists():
        raise SystemExit(f"Refusing to overwrite {dest}; use a fresh work directory")
    dest.mkdir(parents=True)
    run("git", "init", "-q", str(dest))
    run("git", "remote", "add", "origin", source["url"], cwd=dest)
    run("git", "fetch", "--depth=1", "origin", sha, cwd=dest)
    run("git", "checkout", "--detach", "FETCH_HEAD", cwd=dest)
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dest, text=True).strip()
    if actual != sha:
        raise SystemExit(f"Source revision mismatch: {dest}")


def install_bundled(lock, lede, cache):
    package = lede / "package/custom/luci-app-openclash"
    actual_version = package_version((package / "Makefile").read_text())
    if actual_version != lock["openclash"]["version"]:
        raise ValueError("OpenClash source version does not match lock")
    core = lock["openclash_core"]
    core_sha = extract_core(locked_download(core, cache, ".tar.gz"),
                            lede / "files/etc/openclash/core/clash_meta")
    if core_sha != core["binary_sha256"]:
        raise ValueError("Extracted Mihomo binary does not match lock")
    dashboard = lock["zashboard"]
    ui = package / "root/usr/share/openclash/ui/zashboard"
    # Replace the package's bundled panel so the IPK and firmware both contain it.
    if ui.exists():
        shutil.rmtree(ui)
    dashboard_hashes = extract_dashboard(locked_download(dashboard, cache, ".zip"), ui)
    tailcat = lock["tailcat"]
    tailcat_sha = extract_tailcat(locked_download(tailcat, cache, ".tar.gz"),
                                  lede / "files/usr/bin/tailcat",
                                  lede / "files/usr/share/licenses/tailcat/LICENSE")
    if tailcat_sha != tailcat["binary_sha256"]:
        raise ValueError("Extracted Tailcat binary does not match lock")
    # Copy only explicit public templates, never a device identity or backup.
    tailcat_files = ["etc/init.d/tailcat", "etc/uci-defaults/98-tailcat-bundle",
                     "lib/upgrade/keep.d/tailcat"]
    for name in tailcat_files:
        dest = lede / "files" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "files" / name, dest)
    tailcat_files += ["usr/bin/tailcat", "usr/share/licenses/tailcat/LICENSE"]
    bundled = {
        "resolved_at": lock["resolved_at"],
        **install_themes(lock, lede, cache),
        "openclash": {"version": actual_version, "commit": lock["openclash"]["commit"]},
        "mihomo": {"version": core["version"], "channel": core["channel"], "sha256": core_sha},
        "zashboard": {"version": dashboard["version"], "tag": dashboard["tag"],
                      "archive_sha256": dashboard["sha256"], "files": dashboard_hashes},
        "tailcat": {"version": tailcat["version"], "tag": tailcat["tag"],
                    "archive_sha256": tailcat["sha256"], "sha256": tailcat_sha,
                    "files": {name: sha256(lede / "files" / name) for name in tailcat_files}},
    }
    write_json(lede / "files/etc/ax6000-build.json", bundled)
    return bundled


def install_themes(sources, lede, cache):
    """Validate both themes before replacing any previously prepared package."""
    bundled = {}
    with tempfile.TemporaryDirectory() as temp:
        for theme in THEMES:
            source = sources[theme]
            staged = Path(temp) / theme
            hashes = extract_theme(locked_download(source, cache, ".tar.gz"), staged, theme, source["version"])
            # LEDE scans for a literal BuildPackage signature. Keep runtime assets
            # unchanged so modern CSS and the final image's file hashes survive.
            makefile = staged / "Makefile"
            include = "include $(TOPDIR)/feeds/luci/luci.mk"
            text = makefile.read_text()
            if text.count(include) != 1:
                raise ValueError(f"Unexpected {theme} LuCI build include")
            settings = (f"LUCI_NAME:=luci-theme-{theme}\n"
                        "LUCI_MINIFY_JS:=0\nLUCI_MINIFY_CSS:=0\n"
                        "# call BuildPackage - OpenWrt buildroot scan signature\n")
            makefile.write_text(text.replace(include, settings + include))
            license_dir = staged / f"root/usr/share/luci-theme-{theme}"
            license_dir.mkdir(parents=True, exist_ok=True)
            for name in ("LICENSE", "NOTICE"):
                shutil.copyfile(staged / name, license_dir / name)
                hashes[f"usr/share/luci-theme-{theme}/{name}"] = sha256(license_dir / name)
            bundled[theme] = {"version": source["version"], "tag": source["tag"],
                              "archive_sha256": source["sha256"], "files": hashes}
        for theme in THEMES:
            package = lede / f"package/custom/luci-theme-{theme}"
            if package.exists():
                shutil.rmtree(package)
            shutil.copytree(Path(temp) / theme, package)
    return bundled


def configure_themes(lede):
    """Apply the theme policy to existing build trees as well as fresh ones."""
    config = lede / ".config"
    lines = config.read_text().splitlines()
    excluded = {"luci", "luci-light", "luci-app-argon-config", "luci-theme-bootstrap", "luci-theme-argon"}
    selected = {f"luci-theme-{theme}" for theme in THEMES}
    kept = []
    for line in lines:
        option = line.removeprefix("# ").split("=", 1)[0].split(" ", 1)[0]
        package = option.removeprefix("CONFIG_PACKAGE_")
        if package.startswith("luci-theme-") or package in excluded:
            if package not in selected:
                excluded.add(package)
        else:
            kept.append(line)
    kept.extend(f"CONFIG_PACKAGE_{name}=y" for name in sorted(selected))
    kept.extend(f"# CONFIG_PACKAGE_{name} is not set" for name in sorted(excluded))
    config.write_text("\n".join(kept) + "\n")
    defaults = lede / "files/etc/uci-defaults/99-ax6000-build"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "files/etc/uci-defaults/99-ax6000-build", defaults)
    defaults.chmod(0o755)


def refresh_themes():
    """Every compilation, including a reused build tree, resolves both themes anew."""
    metadata = LEDE / "files/etc/ax6000-build.json"
    bundled = json.loads(metadata.read_text())
    sources = resolve_themes(ROOT / "downloads")
    bundled.update(install_themes(sources, LEDE, ROOT / "downloads"))
    configure_themes(LEDE)
    lock = dict(LOCK, **sources, schema_version=5)
    write_json(metadata, bundled)
    write_json(ROOT / "sources.lock.json", lock)
    for theme, source in sources.items():
        print(f"{theme} selected for this compilation: {source['tag']} ({source['sha256']})")


def main():
    if LOCK.get("schema_version") != 5:
        raise SystemExit("Run update_sources.py to migrate this old lock before preparing sources")
    WORK.mkdir(exist_ok=True)
    checkout(LOCK["lede"], LEDE)
    feeds = []
    for name, source in LOCK["feeds"].items():
        dest = WORK / "feeds" / name
        checkout(source, dest)
        feeds.append(f"src-link {name} {dest}\n")
    (LEDE / "feeds.conf").write_text("".join(feeds))
    checkout(LOCK["openclash"], WORK / "openclash")
    shutil.copytree(WORK / "openclash/luci-app-openclash", LEDE / "package/custom/luci-app-openclash")

    bundled = install_bundled(LOCK, LEDE, ROOT / "downloads")
    print("Bundled versions:", {name: bundled[name]["version"]
                               for name in (*THEMES, "openclash", "mihomo", "zashboard", "tailcat")})

    defaults = LEDE / "files/etc/uci-defaults/99-ax6000-build"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "files/etc/uci-defaults/99-ax6000-build", defaults)
    defaults.chmod(0o755)
    run("./scripts/feeds", "update", "-a", cwd=LEDE)
    run("./scripts/feeds", "install", "-a", cwd=LEDE)
    shutil.copyfile(ROOT / "configs/ax6000.config", LEDE / ".config")
    run("make", "defconfig", cwd=LEDE)
    run("python3", str(ROOT / "scripts/validate.py"), "config", cwd=LEDE)
    print("Prepared pinned LEDE tree:", LEDE)


if __name__ == "__main__":
    if sys.argv[1:] == ["--refresh-themes"]:
        refresh_themes()
    elif not sys.argv[1:]:
        main()
    else:
        raise SystemExit("Usage: prepare.py [--refresh-themes]")
