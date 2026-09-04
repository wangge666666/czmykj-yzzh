from __future__ import annotations

import unittest

import numpy as np

from face_mosaic import (
    FaceBoxTracker,
    apply_pixel_mosaic,
    expand_face_box,
    select_eye_privacy_face,
    select_eye_privacy_faces,
)
from workflow_core import WorkflowError


class FaceMosaicTests(unittest.TestCase):
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
