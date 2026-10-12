#!/usr/bin/env python3
"""Fetch exact source revisions and assemble an isolated LEDE build tree."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

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


def main():
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

    core = LOCK["openclash_core"]
    archive = WORK / "clash-linux-arm64.tar.gz"
    run("curl", "--fail", "--location", "--retry", "5", "--connect-timeout", "30",
        "--output", str(archive), core["url"])
    if hashlib.sha256(archive.read_bytes()).hexdigest() != core["sha256"]:
        raise SystemExit("OpenClash core SHA256 mismatch")
    core_dest = LEDE / "files/etc/openclash/core/clash_meta"
    core_dest.parent.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember("clash")
        if not member.isfile() or member.size > 100 * 1024 * 1024:
            raise SystemExit("Unexpected core archive contents")
        with tar.extractfile(member) as src, core_dest.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    # ELF64, little endian, AArch64 (e_machine=183).
    elf = core_dest.read_bytes()[:20]
    if elf[:6] != b"\x7fELF\x02\x01" or int.from_bytes(elf[18:20], "little") != 183:
        raise SystemExit("Core is not an AArch64 ELF executable")
    core_dest.chmod(0o755)

    defaults = LEDE / "files/etc/uci-defaults/99-ax6000-build"
    defaults.parent.mkdir(parents=True)
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
