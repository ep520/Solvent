import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from callguard import storage


class AtomicStorageTest(unittest.TestCase):
    def test_json_is_published_and_has_no_temporary_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.json"
            storage.atomic_write_json(target, {"status": "complete"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"status": "complete"})
            self.assertEqual(list(target.parent.glob(".result.json.*.tmp")), [])

    def test_failed_replace_preserves_previous_complete_file_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "result.json"
            target.write_text('{"status":"old"}\n', encoding="utf-8")
            with patch("callguard.storage.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    storage.atomic_write_json(target, {"status": "new"})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"status": "old"})
            self.assertEqual(list(target.parent.glob(".result.json.*.tmp")), [])
