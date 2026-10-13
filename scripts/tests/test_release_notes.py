"""Do not publish incomplete, stale or unvalidated firmware artifacts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_notes import release_notes
from source_utils import sha256, write_json
from validate import BOARD


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.image = self.root / "openwrt-ax6000-squashfs-sysupgrade.bin"
        self.image.write_bytes(b"validated firmware fixture")
        digest = sha256(self.image)
        versions = {name: "1.0.2" for name in ("mono", "aeris", "openclash", "mihomo", "zashboard", "tailcat")}
        write_json(self.root / "validation.json", {
            "image": self.image.name, "sha256": digest, "size_bytes": self.image.stat().st_size,
            "bundled_files_verified_in_image": True, "supported_devices": [BOARD],
            "bundled_versions": versions,
        })
        write_json(self.root / "image-metadata.json", {"supported_devices": [BOARD]})
        write_json(self.root / "software-versions.json", {
            "packages": {"luci-theme-mono": "1.0.2-r1", "luci-theme-aeris": "1.0.2-r1"},
            "bundled": {name: {"version": version} for name, version in versions.items()},
        })
        (self.root / "SHA256SUMS").write_text(f"{digest}  {self.image.name}\n")
        for name in ("sources.lock.json", "config.full", "config.diff", "openwrt.manifest"):
            (self.root / name).write_text("fixture\n")

    def test_notes_describe_actual_versions_and_validation_boundary(self):
        notes = release_notes(self.root)
        self.assertIn("| mono | 1.0.2 |", notes)
        self.assertIn("| aeris | 1.0.2 |", notes)
        self.assertIn(sha256(self.image), notes)
        self.assertIn("默认主题为 **Mono**", notes)
        self.assertIn("尚未进行实机启动", notes)

    def test_modified_image_is_rejected(self):
        self.image.write_bytes(b"different firmware")
        with self.assertRaisesRegex(ValueError, "differs from validated"):
            release_notes(self.root)

    def test_missing_record_is_rejected(self):
        (self.root / "sources.lock.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing build verification"):
            release_notes(self.root)

    def test_unvalidated_image_is_rejected(self):
        path = self.root / "validation.json"
        record = json.loads(path.read_text())
        record["bundled_files_verified_in_image"] = False
        write_json(path, record)
        with self.assertRaisesRegex(ValueError, "not passed image validation"):
            release_notes(self.root)


if __name__ == "__main__":
    unittest.main()
