#!/usr/bin/env python3
"""Recheck downloaded build artifacts and describe the exact firmware being released."""
import json
import os
from pathlib import Path
import sys

from source_utils import sha256
from validate import BOARD, validate_themes


def release_notes(directory):
    directory = Path(directory)
    required = {"SHA256SUMS", "software-versions.json", "sources.lock.json", "config.full",
                "config.diff", "image-metadata.json", "validation.json"}
    if not all((directory / name).is_file() for name in required):
        raise ValueError("Release is missing build verification or version records")
    validation = json.loads((directory / "validation.json").read_text())
    if validation.get("bundled_files_verified_in_image") is not True:
        raise ValueError("Firmware contents have not passed image validation")
    metadata = json.loads((directory / "image-metadata.json").read_text())
    if BOARD not in validation["supported_devices"] or BOARD not in metadata["supported_devices"]:
        raise ValueError("Release does not target the expected AX6000 layout")
    images = list(directory.glob("*-sysupgrade.bin"))
    manifests = list(directory.glob("*.manifest"))
    if len(images) != 1 or len(manifests) != 1 or images[0].name != validation["image"]:
        raise ValueError("Expected one validated firmware image and package manifest")
    image = images[0]
    digest = sha256(image)
    if digest != validation["sha256"] or image.stat().st_size != validation["size_bytes"]:
        raise ValueError("Downloaded firmware differs from validated firmware")
    if (directory / "SHA256SUMS").read_text().strip() != f"{digest}  {image.name}":
        raise ValueError("Firmware checksum file does not match")
    software = json.loads((directory / "software-versions.json").read_text())
    packages = software["packages"]
    validate_themes(packages)
    bundled = software["bundled"]
    versions = validation["bundled_versions"]
    if any(versions[name] != bundled[name]["version"] for name in versions):
        raise ValueError("Component version records do not match")
    rows = "\n".join(f"| {name} | {versions[name]} |"
                     for name in ("mono", "aeris", "openclash", "mihomo", "zashboard", "tailcat"))
    return f"""Redmi AX6000（MT7986 / NMBM 布局）固件，UBI offset `0x600000`、size `0x6e00000`。

默认主题为 **Mono**，仅内置 **Mono / Aeris** 两个主题，均采用本次构建时的最新正式 Release。

| 组件 | 实际内置版本 |
| --- | --- |
{rows}

- 固件：`{image.name}`（{image.stat().st_size:,} 字节）
- SHA256：`{digest}`
- 配置提交：`{os.environ.get('GITHUB_SHA', 'local')}`
- 构建记录：[GitHub Actions](https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'wizcheu/ax6000-lede-build')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')})

已通过构建配置、设备元数据、软件包清单及 squashfs 内置文件哈希校验。此预发布版本尚未进行实机启动、无线、拨号或配置迁移测试。仅适用于 NMBM 布局，不适用于 stock 双 UBI 或 ubootmod 布局。

附件包含升级固件、`SHA256SUMS`、完整包清单、配置、源码锁文件、`software-versions.json` 和验证记录。此流程不刷写路由器。
"""


if __name__ == "__main__":
    print(release_notes(sys.argv[1]))
