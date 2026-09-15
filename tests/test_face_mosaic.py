from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

import face_mosaic
from face_mosaic import (
    FaceBoxTracker,
    apply_pixel_mosaic,
    detect_faces_all_orientations,
    expand_face_box,
    select_eye_privacy_face,
    select_eye_privacy_faces,
)
from workflow_core import WorkflowError


class FaceModelIntegrityTests(unittest.TestCase):
    """Synthetic weights exercise integrity and repair without downloading."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "models" / "face_detection_yunet_2023mar.onnx"
        self.target.parent.mkdir()
        self.valid = b"synthetic-yunet-fixture" * 64
        for name, value in (
            ("FACE_MODEL_PATH", self.target),
            ("FACE_MODEL_SIZE_BYTES", len(self.valid)),
            ("FACE_MODEL_SHA256", hashlib.sha256(self.valid).hexdigest()),
        ):
            setting = patch.object(face_mosaic, name, value)
            setting.start()
            self.addCleanup(setting.stop)
        transport = patch.object(face_mosaic.requests, "get")
        self.get = transport.start()
        self.addCleanup(transport.stop)

    def download(self, content):
        result = Mock()
        result.__enter__ = Mock(return_value=result)
        result.__exit__ = Mock(return_value=False)
        result.iter_content.return_value = [content[:32], content[32:]]
        return result

    def test_exact_size_and_digest_are_both_required(self):
        self.assertFalse(face_mosaic._valid_face_model(self.target))
        for invalid in (self.valid[:-1], self.valid + b"x", b"x" * len(self.valid)):
            self.target.write_bytes(invalid)
            self.assertFalse(face_mosaic._valid_face_model(self.target))
        self.target.write_bytes(self.valid)
        self.assertTrue(face_mosaic._valid_face_model(self.target))
        self.get.assert_not_called()

    def test_valid_existing_file_is_reused_without_a_download(self):
        self.target.write_bytes(self.valid)
        self.assertEqual(face_mosaic.ensure_face_model(), self.target)
        self.get.assert_not_called()

    def test_corrupted_file_is_replaced_only_after_validated_download(self):
        damaged = b"x" * len(self.valid)
        self.target.write_bytes(damaged)
        self.get.return_value = self.download(self.valid)
        replace = face_mosaic.os.replace

        def validated_replace(source, destination):
            self.assertEqual(self.target.read_bytes(), damaged)
            self.assertTrue(face_mosaic._valid_face_model(source))
            replace(source, destination)

        with patch.object(face_mosaic.os, "replace", side_effect=validated_replace) as moved:
            self.assertEqual(face_mosaic.ensure_face_model(), self.target)
        self.assertEqual(self.target.read_bytes(), self.valid)
        self.assertEqual(self.get.call_count, 1)
        moved.assert_called_once()
        self.assertFalse(self.target.with_suffix(".part").exists())

    def test_invalid_downloads_preserve_original_and_remove_temporary(self):
        damaged = b"x" * len(self.valid)
        self.target.write_bytes(damaged)
        self.get.side_effect = [self.download(damaged) for _ in face_mosaic.FACE_MODEL_DOWNLOAD_SOURCES]
        with self.assertRaisesRegex(WorkflowError, "SHA-256"):
            face_mosaic.ensure_face_model()
        self.assertEqual(self.target.read_bytes(), damaged)
        self.assertEqual(self.get.call_count, len(face_mosaic.FACE_MODEL_DOWNLOAD_SOURCES))
        self.assertFalse(self.target.with_suffix(".part").exists())

    def test_second_source_can_repair_after_a_bad_first_download(self):
        self.get.side_effect = [self.download(b"bad"), self.download(self.valid)]
        self.assertEqual(face_mosaic.ensure_face_model(), self.target)
        self.assertTrue(face_mosaic._valid_face_model(self.target))
        self.assertEqual(self.get.call_count, 2)

    def test_unicode_path_cache_does_not_reuse_same_size_corruption(self):
        source = self.root / "中文模型.onnx"
        source.write_bytes(self.valid)
        cache_root = self.root / "cache"
        cached = cache_root / "depthflow-model-cache" / source.name
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"x" * len(self.valid))
        with patch.object(face_mosaic.tempfile, "gettempdir", return_value=str(cache_root)):
            self.assertEqual(face_mosaic._opencv_safe_model_path(source), cached)
        self.assertTrue(face_mosaic._valid_face_model(cached))
        self.get.assert_not_called()


class FaceMosaicTests(unittest.TestCase):
    def test_rotation_detection_maps_all_directions_and_keeps_multiple_people(self):
        detector = Mock()
        # Original frame width=200, height=100. The upright face appears twice;
        # two additional faces are detected only in the 180 and 270 degree views.
        detector.detect.side_effect = [
            [(10,20,20,30)], [(50,10,30,20)], [(110,50,20,30)], [(10,40,20,20)],
        ]
        faces = detect_faces_all_orientations(detector, np.zeros((100,200,3),np.uint8))
        self.assertEqual(detector.detect.call_count,4)
        self.assertEqual(faces,[(10,20,20,30),(70,20,20,30),(140,10,20,20)])

    def test_expanded_box_covers_more_than_detected_face_and_stays_in_frame(self) -> None:
        expanded = expand_face_box((4, 6, 30, 40), 100, 80)
        x, y, width, height = expanded
        self.assertGreater(width, 30)
        self.assertGreater(height, 40)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 100)
        self.assertLessEqual(y + height, 80)

    def test_tracker_holds_privacy_mask_through_short_detector_miss(self) -> None:
        tracker = FaceBoxTracker(max_missed=2)
        first = tracker.update([(10, 10, 20, 20)], frame_width=100, frame_height=100)
        second = tracker.update([], frame_width=100, frame_height=100)
        third = tracker.update([], frame_width=100, frame_height=100)
        fourth = tracker.update([], frame_width=100, frame_height=100)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(len(third), 1)
        self.assertEqual(fourth, [])

    def test_tracker_keeps_two_nearby_people_as_two_masks(self) -> None:
        tracker = FaceBoxTracker()
        tracker.update([(8, 10, 20, 20), (60, 12, 20, 20)], frame_width=100, frame_height=100)
        updated = tracker.update([(11, 10, 20, 20), (57, 13, 20, 20)], frame_width=100, frame_height=100)
        self.assertEqual(len(updated), 2)

    def test_mosaic_is_opaque_and_only_changes_target_region(self) -> None:
        image = np.arange(64 * 64 * 3, dtype=np.uint8).reshape((64, 64, 3))
        original = image.copy()
        apply_pixel_mosaic(image, [(16, 16, 24, 24)], block_size=12)
        self.assertTrue(np.array_equal(image[:10, :10], original[:10, :10]))
        self.assertFalse(np.array_equal(image[16:40, 16:40], original[16:40, 16:40]))
        self.assertLess(len(np.unique(image[16:40, 16:40].reshape((-1, 3)), axis=0)), 12)

    def test_three_view_privacy_selects_only_leftmost_front_face(self) -> None:
        front = (80.0, 30.0, 60.0, 80.0)
        side = (310.0, 32.0, 55.0, 78.0)
        self.assertEqual(
            select_eye_privacy_face([side, front], allow_three_view=True),
            front,
        )

    def test_portrait_plus_three_view_masks_portrait_and_frontal_full_body(self) -> None:
        portrait = (253.5, 156.8, 260.0, 361.8)
        frontal_full_body = (894.6, 77.9, 66.7, 92.1)
        side_full_body = (1168.1, 77.5, 56.9, 81.6)
        self.assertEqual(
            select_eye_privacy_faces(
                [side_full_body, portrait, frontal_full_body],
                allow_three_view=True,
                frame_width=1672,
            ),
            [portrait, frontal_full_body],
        )

    def test_multiple_faces_still_require_three_view_mode(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "检测到多张人脸"):
            select_eye_privacy_face(
                [(80.0, 30.0, 60.0, 80.0), (310.0, 32.0, 55.0, 78.0)],
                allow_three_view=False,
            )

    def test_three_view_rejects_more_than_three_detected_faces(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "超过 3 张人脸"):
            select_eye_privacy_face(
                [(float(index * 100), 30.0, 60.0, 80.0) for index in range(4)],
                allow_three_view=True,
            )


if __name__ == "__main__":
    unittest.main()
