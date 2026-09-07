from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from long_video_core import save_shot_manifest
from workflow_core import save_job_record


class AtomicJobRecordTests(unittest.TestCase):
    @staticmethod
    def writers(root: Path):
        return (
            ("job", root / "job.json", lambda value: save_job_record(root, value)),
            ("manifest", root / "shots.json", lambda value: save_shot_manifest(root / "shots.json", value)),
        )

    def test_concurrent_writers_publish_complete_records_with_separate_temporary_files(self):
        for name in ("job", "manifest"):
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, target, save = next(item for item in self.writers(root) if item[0] == name)
                previous = {"stage": "already saved", "values": [0]}
                save(previous)
                records = [{"stage": f"writer {index}", "values": [index] * 4096,
                            "text": "合成任务记录" * 1024} for index in (1, 2)]
                barrier = threading.Barrier(2)
                original_replace = os.replace
                temporary_paths = []
                observed_records = []

                def synchronized_replace(source, destination):
                    temporary_paths.append(Path(source))
                    observed_records.append(json.loads(target.read_text(encoding="utf-8")))
                    # Both writers finish their temporary file before either
                    # rename runs. A shared .tmp deterministically loses here.
                    barrier.wait(timeout=5)
                    return original_replace(source, destination)

                with patch("os.replace", side_effect=synchronized_replace):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(save, record) for record in records]
                        for future in futures:
                            self.assertEqual(future.result(timeout=10), target)
                self.assertEqual(observed_records, [previous, previous])
                self.assertEqual(len(set(temporary_paths)), 2)
                self.assertTrue(all(path.parent == root for path in temporary_paths))
                self.assertIn(json.loads(target.read_text(encoding="utf-8")), records)
                self.assertEqual(list(root.iterdir()), [target])

    def test_failed_publication_preserves_the_previous_record_and_cleans_its_temporary_file(self):
        for name in ("job", "manifest"):
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, target, save = next(item for item in self.writers(root) if item[0] == name)
                save({"stage": "already saved"})
                original_bytes = target.read_bytes()
                with patch("os.replace", side_effect=OSError("synthetic replace failure")):
                    with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                        save({"stage": "new state"})
                self.assertEqual(target.read_bytes(), original_bytes)
                self.assertEqual(list(root.iterdir()), [target])

    def test_failed_file_sync_preserves_the_previous_record_and_cleans_its_temporary_file(self):
        for name in ("job", "manifest"):
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, target, save = next(item for item in self.writers(root) if item[0] == name)
                save({"stage": "already saved"})
                original_bytes = target.read_bytes()
                with patch("os.fsync", side_effect=OSError("synthetic sync failure")):
                    with self.assertRaisesRegex(OSError, "synthetic sync failure"):
                        save({"stage": "new state"})
                self.assertEqual(target.read_bytes(), original_bytes)
                self.assertEqual(list(root.iterdir()), [target])

    def test_serialization_failure_leaves_no_partial_file(self):
        for name in ("job", "manifest"):
            with self.subTest(writer=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                _, target, save = next(item for item in self.writers(root) if item[0] == name)
                save({"stage": "already saved"})
                original_bytes = target.read_bytes()
                circular = {}
                circular["cycle"] = circular
                with self.assertRaises(ValueError):
                    save(circular)
                self.assertEqual(target.read_bytes(), original_bytes)
                self.assertEqual(list(root.iterdir()), [target])

    def test_job_record_keeps_its_redaction_and_serialization_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            record = {"api_key": "synthetic-key", "signed_url": "synthetic-signed-url",
                      "stage": "已保存", "output": Path("synthetic.mp4")}
            result = save_job_record(root, record)
            self.assertEqual(json.loads(result.read_text(encoding="utf-8")),
                             {"stage": "已保存", "output": "synthetic.mp4"})
            self.assertIn("api_key", record)
            self.assertIn("signed_url", record)
            self.assertEqual(list(root.iterdir()), [result])


if __name__ == "__main__":
    unittest.main()
