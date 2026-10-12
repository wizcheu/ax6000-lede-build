#!/usr/bin/env python3
"""Fail the build if Kconfig drops requirements or the image targets another layout."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

from source_utils import write_json

ROOT = Path(__file__).resolve().parents[1]
LEDE = ROOT / "work/lede"
BOARD = "xiaomi,redmi-router-ax6000"
PROFILE = "xiaomi_redmi-router-ax6000"


def validate_config():
    config = (LEDE / ".config").read_text().splitlines()
    selected = set(config)
    errors = []
    for line in (ROOT / "configs/ax6000.config").read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line and line not in selected:
            errors.append(f"Required option lost after defconfig: {line}")
        match = re.fullmatch(r"# (CONFIG_\S+) is not set", line)
        if match and any(f"{match[1]}={value}" in selected for value in ("y", "m")):
            errors.append(f"Excluded option was selected: {match[1]}")
    devices = [x for x in config if re.match(r"CONFIG_TARGET_mediatek_filogic_DEVICE_.+=y$", x)]
    if devices != [f"CONFIG_TARGET_mediatek_filogic_DEVICE_{PROFILE}=y"]:
        errors.append(f"Unexpected device selections: {devices}")
    dts = (LEDE / "target/linux/mediatek/dts/mt7986a-xiaomi-redmi-router-ax6000.dts").read_text()
    if not re.search(r"reg\s*=\s*<0x600000\s+0x6e00000>", dts):
        errors.append("Expected UBI offset/size absent from device tree")
    if errors:
        raise SystemExit("\n".join(errors))
    print("Kconfig requirements, single device and NMBM partition layout verified")


def verify_bundled(image, packages):
    expected_path = LEDE / "files/etc/ax6000-build.json"
    bundled = json.loads(expected_path.read_text())
    version = bundled["openclash"]["version"]
    installed = packages["luci-app-openclash"]
    if installed != version and not installed.startswith(version + "-"):
        raise SystemExit(f"Unexpected compiled OpenClash version: {installed}")
    hashes = {"etc/openclash/core/clash_meta": bundled["mihomo"]["sha256"],
              "etc/ax6000-build.json": hashlib.sha256(expected_path.read_bytes()).hexdigest()}
    hashes.update({"usr/share/openclash/ui/zashboard/" + name: digest
                   for name, digest in bundled["zashboard"]["files"].items()})
    hashes.update(bundled["tailcat"]["files"])
    with tempfile.TemporaryDirectory() as temp:
        rootfs = Path(temp) / "root.squashfs"
        with tarfile.open(image, "r:") as tar:
            roots = [m for m in tar.getmembers() if m.name.endswith("/root") and m.isfile()]
            if len(roots) != 1:
                raise SystemExit("Expected exactly one root filesystem")
            with tar.extractfile(roots[0]) as src, rootfs.open("wb") as dst:
                shutil.copyfileobj(src, dst)
        unsquashfs = LEDE / "staging_dir/host/bin/unsquashfs4"
        listing = subprocess.check_output([str(unsquashfs), "-ls", str(rootfs)], text=True)
        forbidden = ("etc/tailcat", "etc/rc.d/S95tailcat", "etc/rc.d/K10tailcat")
        if any(line.startswith("squashfs-root/" + name) for line in listing.splitlines()
               for name in forbidden):
            raise SystemExit("Generic firmware must contain no Tailcat identity or autostart links")
        for name, digest in hashes.items():
            data = subprocess.check_output([str(unsquashfs), "-cat", str(rootfs), name])
            if hashlib.sha256(data).hexdigest() != digest:
                raise SystemExit(f"Bundled file differs inside firmware: {name}")
    print(f"Verified {len(hashes)} bundled files inside the actual squashfs image")
    return bundled


def validate_image():
    validate_config()
    out = LEDE / "bin/targets/mediatek/filogic"
    images = list(out.glob(f"*{PROFILE}-squashfs-sysupgrade.bin"))
    if len(images) != 1:
        raise SystemExit(f"Expected exactly one AX6000 sysupgrade image; found {images}")
    image = images[0]
    size = image.stat().st_size
    # A conservative ceiling within the 110 MiB UBI partition, not a runtime free-space claim.
    if not 5 * 1024**2 < size < 80 * 1024**2:
        raise SystemExit(f"Unexpected image size: {size} bytes")
    dest = ROOT / "artifacts/firmware"
    dest.mkdir(parents=True, exist_ok=True)
    metadata = dest / "image-metadata.json"
    subprocess.run([str(LEDE / "staging_dir/host/bin/fwtool"), "-i", str(metadata), str(image)], check=True)
    meta = json.loads(metadata.read_text())
    if BOARD not in meta.get("supported_devices", []):
        raise SystemExit(f"Wrong image metadata: {meta.get('supported_devices')}")
    with tarfile.open(image, "r:") as tar:
        names = tar.getnames()
        if not any(n.endswith("/kernel") for n in names) or not any(n.endswith("/root") for n in names):
            raise SystemExit("Missing kernel/root in sysupgrade archive")
    manifest_files = list(out.glob("*.manifest"))
    if len(manifest_files) != 1:
        raise SystemExit("Expected one package manifest")
    packages = dict(line.split(" - ", 1) for line in manifest_files[0].read_text().splitlines())
    required = {"luci-app-openclash", "ruby-yaml", "firewall4", "kmod-mt7915e", "dnsmasq-full"}
    if not required <= packages.keys():
        raise SystemExit(f"Packages missing from image: {required - packages.keys()}")
    bundled = verify_bundled(image, packages)
    write_json(dest / "software-versions.json", {"bundled": bundled, "packages": dict(sorted(packages.items()))})
    for path in [image, *manifest_files, out / "profiles.json"]:
        if path.is_file():
            shutil.copy2(path, dest / path.name)
    shutil.copy2(LEDE / ".config", dest / "config.full")
    shutil.copy2(ROOT / "sources.lock.json", dest / "sources.lock.json")
    diff = subprocess.check_output(["./scripts/diffconfig.sh"], cwd=LEDE)
    (dest / "config.diff").write_bytes(diff)
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    (dest / "SHA256SUMS").write_text(f"{sha}  {image.name}\n")
    result = {"image": image.name, "size_bytes": size, "sha256": sha,
              "supported_devices": meta["supported_devices"], "package_count": len(packages),
              "bundled_versions": {name: bundled[name]["version"]
                                   for name in ("openclash", "mihomo", "zashboard", "tailcat")},
              "bundled_files_verified_in_image": True,
              "runtime_tested": False, "flashed": False}
    (dest / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    {"config": validate_config, "image": validate_image}[sys.argv[1]]()
