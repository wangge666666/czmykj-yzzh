from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import cv2
import numpy as np

from depth_video import YoloPersonPreserver, limited_frame_count, render_video


class DepthDurationLimitTests(unittest.TestCase):
    def test_person_preserver_keeps_near_and_far_people_as_separate_layers(self) -> None:
        preserver = YoloPersonPreserver.__new__(YoloPersonPreserver)
        preserver.last_score = 0.0
        preserver.last_candidates = []
        preserver.last_layers = []
        preserver.last_people = 0
        far_mask = np.zeros((518, 518), dtype=np.float32)
        far_mask[80:260, 60:220] = 1.0
        near_mask = np.zeros((518, 518), dtype=np.float32)
        near_mask[90:500, 280:500] = 1.0
        detections = [
            ((10, 10, 45, 48), 0.82),
            ((52, 5, 99, 94), 0.91),
        ]
        with patch(
            "long_video_core._detect_people_yolox", return_value=detections
        ), patch.object(
            preserver, "_grabcut_person", side_effect=[far_mask, near_mask]
        ):
            floor = preserver.segment(np.zeros((100, 100, 3), dtype=np.uint8))
        self.assertIsNotNone(floor)
        self.assertEqual(preserver.last_people, 2)
        self.assertGreater(float(floor[200, 400]), float(floor[150, 100]))
        self.assertGreater(float(floor[150, 100]), 0.60)

    def test_24_fps_is_limited_to_348_frames(self) -> None:
        frames = limited_frame_count(361, 24.0, 14.5)
        self.assertEqual(frames, 348)
        self.assertLessEqual(frames / 24.0, 14.5)

    def test_short_video_keeps_all_frames(self) -> None:
        self.assertEqual(limited_frame_count(240, 24.0, 14.5), 240)

    def test_fractional_fps_never_exceeds_limit(self) -> None:
        frames = limited_frame_count(1000, 29.97, 14.5)
        self.assertLessEqual(frames / 29.97, 14.5)

    def test_invalid_metadata_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            limited_frame_count(0, 24.0, 14.5)
        with self.assertRaises(ValueError):
            limited_frame_count(100, 0.0, 14.5)

    def test_depth_encoder_does_not_truncate_video_to_short_audio(self) -> None:
        capture = Mock()
        capture.get.side_effect = lambda prop: {
            cv2.CAP_PROP_FPS: 30.0,
            cv2.CAP_PROP_FRAME_WIDTH: 16,
            cv2.CAP_PROP_FRAME_HEIGHT: 16,
            cv2.CAP_PROP_FRAME_COUNT: 14,
        }.get(prop, 0)
        capture.read.side_effect = [
            (True, np.zeros((16, 16, 3), dtype=np.uint8)) for _ in range(14)
        ]
        renderer = Mock()
        renderer.render.return_value = np.zeros((16, 16), dtype=np.uint8)
        process = Mock()
        process.stdin = Mock()
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as tmp, patch(
            "depth_video.open_video", return_value=capture
        ), patch("depth_video.subprocess.Popen", return_value=process) as popen:
            render_video(
                renderer,
                Path(tmp) / "source.mp4",
                Path(tmp) / "depth.mp4",
                Path(tmp) / "ffmpeg.exe",
            )
        command = popen.call_args.args[0]
        self.assertNotIn("-shortest", command)
        self.assertEqual(command[command.index("-t") + 1], f"{14 / 30:.9f}")
        self.assertEqual(process.stdin.write.call_count, 14)


if __name__ == "__main__":
    unittest.main()
