"""Run the shipped shell helpers in an isolated root, without network or real keys."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
INIT = (ROOT / "files/etc/init.d/tailcat").read_text()
MIGRATION = (ROOT / "files/etc/uci-defaults/98-tailcat-bundle").read_text()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "etc/tailcat/router.private.json"
        self.address = self.root / "etc/tailcat/address"
        self.log = self.root / "commands"
        self.init = self.root / "source-init"
        self.init.write_text(self.reroot(INIT))
        self.put("etc/init.d/tailcat", '#!/bin/sh\nprintf "%s\\n" "$1" >> "$TEST_LOG"\n', 0o755)
        self.put("usr/bin/tailcat", '''#!/bin/sh
printf '%s\n' genkey >> "$TEST_LOG"
[ "$1" = genkey ] && [ "$3" = --fixed-region ] || exit 9
key=${2#--key=}
printf '%s' 'test-only-identity' > "$key"
[ "${FAIL_GEN:-0}" = 0 ] || exit 1
printf '%s\n' 'tcTEST_only_not_a_real_address'
''', 0o755)

    def reroot(self, text):
        return re.sub(r"/(?:rom/)?(?:etc|usr|lib|var)(?=/)", lambda m: str(self.root) + m[0], text)

    def put(self, name, content, mode=0o600):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)
        return path

    def run_init(self, action, **env):
        harness = '''extra_command() { :; }
procd_open_instance() { :; }
procd_set_param() { printf '%s\n' "$*" >> "$TEST_LOG"; }
procd_close_instance() { :; }
. "$1"
"$2"
'''
        return subprocess.run(["sh", "-c", harness, "test", str(self.init), action],
                              env={**os.environ, "IPKG_INSTROOT": "", "TEST_LOG": str(self.log), **env},
                              text=True, capture_output=True)

    def test_setup_is_idempotent_and_private(self):
        for _ in range(2):
            result = self.run_init("setup")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("tcTEST", result.stdout + result.stderr)
        self.assertEqual(self.log.read_text().splitlines().count("genkey"), 1)
        self.assertEqual(self.key.read_text(), "test-only-identity")
        for path, mode in ((self.key, 0o600), (self.address, 0o600), (self.key.parent, 0o700)):
            self.assertEqual(path.stat().st_mode & 0o777, mode)
        self.assertEqual(self.run_init("address").stdout, self.address.read_text())
        self.assertFalse((self.root / "var/lock/tailcat-setup").exists())

    def test_failed_generation_leaves_no_partial_identity_and_can_retry(self):
        result = self.run_init("setup", FAIL_GEN="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.key.exists())
        self.assertFalse(self.address.exists())
        self.assertEqual(list(self.key.parent.glob(".setup.*")), [])
        self.assertEqual(self.run_init("setup").returncode, 0)

    def test_existing_identity_is_never_regenerated(self):
        self.put("etc/tailcat/router.private.json", "existing-private-key")
        self.assertEqual(self.run_init("setup").returncode, 0)
        self.assertEqual(self.key.read_text(), "existing-private-key")
        self.assertNotIn("genkey", self.log.read_text())
        self.key.write_text("")
        self.assertNotEqual(self.run_init("setup").returncode, 0)
        self.assertEqual(self.key.read_text(), "")

    def test_concurrent_setup_cannot_replace_identity(self):
        (self.root / "var/lock/tailcat-setup").mkdir(parents=True)
        self.assertNotEqual(self.run_init("setup").returncode, 0)
        self.assertFalse(self.key.exists())
        self.assertFalse(self.log.exists())

    def test_start_requires_key_and_limits_ports_without_logging_token(self):
        self.assertNotEqual(self.run_init("start_service").returncode, 0)
        self.assertFalse(self.log.exists())
        self.put("etc/tailcat/router.private.json", "existing-private-key")
        result = self.run_init("start_service")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text()
        self.assertIn(" 22,80,9090\n", commands)
        self.assertIn("TAILCAT_ADDR_FILE=" + str(self.address), commands)
        self.assertIn("stdout 0\nstderr 0\n", commands)
        self.assertNotIn("existing-private-key", commands)

    def test_build_time_enable_is_a_noop(self):
        result = self.run_init("enable", IPKG_INSTROOT=str(self.root))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "etc/rc.d").exists())
        self.assertFalse(self.key.exists())

    def migration(self, known=True):
        old_init, old_binary = "legacy-init", "legacy-binary"
        self.put("etc/init.d/tailcat", old_init if known else "custom-init")
        self.put("usr/bin/tailcat", old_binary)
        self.put("rom/etc/init.d/tailcat", "new-firmware-init")
        self.put("rom/usr/bin/tailcat", "new-firmware-binary")
        self.put("etc/tailcat/router.private.json", "existing-private-key")
        self.put("etc/tailcat/address", "tcTEST_preserved")
        config = self.put("etc/sysupgrade.conf", self.reroot(
            "/usr/bin/tailcat\n/etc/init.d/tailcat\n/etc/tailcat/\n/etc/config/firewall\n"))
        link = self.root / "etc/rc.d/S95tailcat"
        link.parent.mkdir(parents=True)
        link.symlink_to("../init.d/tailcat")
        migration = re.sub(r"(?m)^legacy_init=.*$", "legacy_init=" + hashlib.sha256(old_init.encode()).hexdigest(), MIGRATION)
        migration = re.sub(r"(?m)^legacy_binary=.*$", "legacy_binary=" + hashlib.sha256(old_binary.encode()).hexdigest(), migration)
        result = subprocess.run(["sh", "-c", self.reroot(migration)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.key.read_text(), "existing-private-key")
        self.assertEqual(self.address.read_text(), "tcTEST_preserved")
        self.assertTrue(link.is_symlink())
        return config

    def test_known_legacy_migration_updates_program_but_retains_identity(self):
        config = self.migration()
        self.assertEqual((self.root / "usr/bin/tailcat").read_text(), "new-firmware-binary")
        self.assertEqual((self.root / "etc/init.d/tailcat").read_text(), "new-firmware-init")
        self.assertEqual(config.read_text(), self.reroot("/etc/tailcat/\n/etc/config/firewall\n"))

    def test_custom_service_is_not_replaced(self):
        config = self.migration(known=False)
        self.assertEqual((self.root / "etc/init.d/tailcat").read_text(), "custom-init")
        self.assertEqual((self.root / "usr/bin/tailcat").read_text(), "legacy-binary")
        self.assertIn(self.reroot("/usr/bin/tailcat\n"), config.read_text())


if __name__ == "__main__":
    unittest.main()
