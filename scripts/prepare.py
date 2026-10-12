#!/usr/bin/env python3
"""Fetch exact source revisions and assemble an isolated LEDE build tree."""
import json
from pathlib import Path
import shutil
import subprocess

from source_utils import (extract_core, extract_dashboard, extract_tailcat, locked_download,
                          package_version, sha256, write_json)

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


def main():
    if LOCK.get("schema_version") != 3:
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
                               for name in ("openclash", "mihomo", "zashboard", "tailcat")})

    defaults = LEDE / "files/etc/uci-defaults/99-ax6000-build"
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text("""#!/bin/sh
uci -q set system.@system[0].timezone='CST-8'
uci -q set system.@system[0].zonename='Asia/Shanghai'
uci -q set luci.main.lang='zh_cn'
uci -q set luci.main.mediaurlbase='/luci-static/argon'
uci -q set openclash.config.core_version='linux-arm64'
uci -q set openclash.config.core_type='Meta'
uci -q set openclash.config.enable='0'
uci -q commit system
uci -q commit luci
uci -q commit openclash
exit 0
""")
    defaults.chmod(0o755)
    run("./scripts/feeds", "update", "-a", cwd=LEDE)
    run("./scripts/feeds", "install", "-a", cwd=LEDE)
    shutil.copyfile(ROOT / "configs/ax6000.config", LEDE / ".config")
    run("make", "defconfig", cwd=LEDE)
    run("python3", str(ROOT / "scripts/validate.py"), "config", cwd=LEDE)
    print("Prepared pinned LEDE tree:", LEDE)


if __name__ == "__main__":
    main()
