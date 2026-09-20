import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import check_plugin_sync


class PluginSyncTests(unittest.TestCase):
    def test_installed_file_must_match_current_source_even_with_same_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {"runtime/example.py": hashlib.sha256(b"new").hexdigest()}
            (root / "SHA256SUMS.json").write_text(json.dumps(expected), encoding="utf-8")
            file = root / "runtime/example.py"
            file.parent.mkdir()
            file.write_bytes(b"new")
            self.assertEqual(check_plugin_sync.check_directory("install", root, expected), [])
            file.write_bytes(b"old")
            self.assertIn("install: stale or missing runtime/example.py",
                          check_plugin_sync.check_directory("install", root, expected))

    def test_archive_checks_payload_not_just_version(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "client.zip"
            expected = {"runtime/example.py": hashlib.sha256(b"new").hexdigest()}
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("czmiyou-yzzh/SHA256SUMS.json", json.dumps(expected))
                archive.writestr("czmiyou-yzzh/runtime/example.py", b"old")
            self.assertIn("archive: stale runtime/example.py",
                          check_plugin_sync.check_archive(archive_path, expected))


if __name__ == "__main__":
    unittest.main()
