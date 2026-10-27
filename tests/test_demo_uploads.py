"""Private demo fixtures survive free-host restarts without entering Git."""

import base64
from io import BytesIO
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
from zipfile import ZipFile, ZipInfo

from deploy.restore_demo_uploads import restore_demo_uploads


class DemoUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="caspian-demo-uploads-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bundle = self.root / "bundle.b64"
        self.uploads = self.root / "uploads"
        self.key = "a" * 32

    def pack(self, entries):
        buffer = BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(buffer, "w") as archive:
                for name, content in entries:
                    archive.writestr(name, content)
        self.bundle.write_bytes(base64.b64encode(buffer.getvalue()) + b"\n")

    def test_absent_bundle_is_a_noop(self):
        self.assertEqual(restore_demo_uploads(self.bundle, None), 0)
        self.assertFalse(self.uploads.exists())

    def test_restores_and_can_run_again(self):
        self.pack([(self.key, b"demo document")])
        for _ in range(2):
            self.assertEqual(restore_demo_uploads(self.bundle, self.uploads), 1)
        self.assertEqual((self.uploads / self.key).read_bytes(), b"demo document")

    def test_does_not_replace_existing_content(self):
        self.pack([(self.key, b"demo")])
        self.uploads.mkdir()
        existing = self.uploads / self.key
        existing.write_bytes(b"other content")
        with self.assertRaises(ValueError):
            restore_demo_uploads(self.bundle, self.uploads)
        self.assertEqual(existing.read_bytes(), b"other content")

    def test_rejects_path_traversal_before_any_write(self):
        self.pack([(self.key, b"valid"), ("../outside", b"bad")])
        with self.assertRaises(ValueError):
            restore_demo_uploads(self.bundle, self.uploads)
        self.assertFalse(self.uploads.exists())

    def test_rejects_duplicate_keys(self):
        self.pack([(self.key, b"first"), (self.key, b"second")])
        with self.assertRaises(ValueError):
            restore_demo_uploads(self.bundle, self.uploads)

    def test_rejects_archive_symlink(self):
        entry = ZipInfo(self.key)
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.pack([(entry, b"../outside")])
        with self.assertRaises(ValueError):
            restore_demo_uploads(self.bundle, self.uploads)

    def test_rejects_invalid_base64(self):
        self.bundle.write_bytes(b"not a base64 archive")
        with self.assertRaises(ValueError):
            restore_demo_uploads(self.bundle, self.uploads)

    def test_requires_private_upload_directory(self):
        self.pack([(self.key, b"demo")])
        with self.assertRaises(ValueError):
            restore_demo_uploads(self.bundle, None)


if __name__ == "__main__":
    unittest.main()
