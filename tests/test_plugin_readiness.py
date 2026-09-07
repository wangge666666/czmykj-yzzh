"""Offline readiness regressions: synthetic model adapters, no account or media."""
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from yzzh_local.original_worker import local_readiness


class LocalReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.face_path = self.root / "models/face-detector/face_detection_yunet_2023mar.onnx"
        self.depth_path = self.root / "models/depth-anything-v2-small/model_fp16.onnx"
        for path in (self.face_path, self.depth_path):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"fixture-model")
        self.ffmpeg = self.root / "ffmpeg"
        self.ffmpeg.write_bytes(b"fixture-executable")
        self.core = types.SimpleNamespace(resolve_ffmpeg=Mock(return_value=self.ffmpeg),
            _valid_depth_model=Mock(side_effect=lambda path: path.is_file()),
            PROJECT_DIR=self.root / "account-data", ensure_depth_model=Mock(side_effect=AssertionError("No download")))
        self.face = types.SimpleNamespace(_valid_face_model=Mock(side_effect=lambda path: path.is_file()),
            YuNetFaceDetector=Mock(return_value=types.SimpleNamespace(detect=Mock(return_value=[]))),
            ensure_face_model=Mock(side_effect=AssertionError("No download")))
        self.renderer = Mock(return_value=types.SimpleNamespace(render=Mock(return_value=np.zeros((240, 320)))))
        self.cache = {}
        patches = [patch("yzzh_local.original_worker.SOURCE", self.root),
                   patch.dict("sys.modules", {"depth_video": types.SimpleNamespace(DepthRenderer=self.renderer)}),
                   patch("requests.sessions.Session.request", side_effect=AssertionError("No network"))]
        for patcher in patches:
            patcher.start(); self.addCleanup(patcher.stop)
        run = patch("subprocess.run", return_value=types.SimpleNamespace(stdout=b"ffmpeg version fixture\n"))
        self.run = run.start(); self.addCleanup(run.stop)

    def check(self, **kwargs):
        return local_readiness(self.core, self.face, self.cache, **kwargs)

    def test_fixed_install_paths_actual_model_calls_and_ffmpeg_execution(self):
        result = self.check()
        self.assertTrue(result["ready"])
        self.assertEqual(len(result["checks"]), 3)
        self.face.YuNetFaceDetector.assert_called_once_with(self.face_path)
        self.renderer.assert_called_once_with(self.depth_path, faithful_depth=True)
        self.run.assert_called_once_with([str(self.ffmpeg), "-version"], capture_output=True, timeout=5, check=True)
        self.face.ensure_face_model.assert_not_called()
        self.core.ensure_depth_model.assert_not_called()

    def test_cache_reuses_checks_but_file_changes_and_manual_recheck_invalidate(self):
        self.assertFalse(self.check()["cached"])
        self.assertTrue(self.check()["cached"])
        self.assertEqual(self.run.call_count, 1)
        self.depth_path.write_bytes(b"changed-model-file")
        self.assertFalse(self.check()["cached"])
        self.assertEqual(self.run.call_count, 2)
        self.assertFalse(self.check(force=True)["cached"])
        self.assertEqual(self.run.call_count, 3)

    def test_missing_or_corrupt_models_fail_before_loading_and_repair_is_detected(self):
        self.face_path.unlink()
        self.core._valid_depth_model.return_value = False
        self.core._valid_depth_model.side_effect = None
        result = self.check()
        self.assertFalse(result["ready"])
        self.assertTrue(all("缺失或损坏" in item["message"] for item in result["checks"][:2]))
        self.face.YuNetFaceDetector.assert_not_called(); self.renderer.assert_not_called()
        self.face_path.write_bytes(b"repaired")
        self.core._valid_depth_model.return_value = True
        self.assertTrue(self.check()["ready"])

    def test_runtime_errors_and_bad_output_are_safe_and_never_ready(self):
        self.face.YuNetFaceDetector.side_effect = RuntimeError("fixture-secret-never-display")
        self.renderer.return_value.render.return_value = np.full((240, 320), np.nan)
        self.run.side_effect = PermissionError("fixture-private-path-never-display")
        result = self.check()
        self.assertFalse(result["ready"])
        self.assertTrue(all(not item["ready"] for item in result["checks"]))
        self.assertNotIn("never-display", str(result))

    def test_replacement_during_inference_cannot_be_marked_ready(self):
        def replaced(frame, **kwargs):
            self.depth_path.write_bytes(b"model-replaced-mid-check")
            return np.zeros((240, 320))
        self.renderer.return_value.render.side_effect = replaced
        result = self.check()
        self.assertFalse(result["ready"])
        self.assertIn("发生变化", result["message"])
        self.assertNotIn("signature", self.cache)

    def test_missing_ffmpeg_and_non_ffmpeg_executable_are_not_ready(self):
        self.core.resolve_ffmpeg.side_effect = RuntimeError("fixture-private-path")
        self.assertFalse(self.check()["checks"][-1]["ready"])
        self.run.assert_not_called()
        self.core.resolve_ffmpeg.side_effect = None
        self.run.return_value.stdout = b"not ffmpeg"
        self.assertFalse(self.check()["checks"][-1]["ready"])

    def test_busy_task_and_concurrent_check_defer_without_inference(self):
        self.assertEqual(self.check(busy=True)["status"], "busy")
        self.cache["lock"].acquire()
        try:
            self.assertEqual(self.check()["status"], "busy")
        finally:
            self.cache["lock"].release()
        self.core.resolve_ffmpeg.assert_not_called()
        self.face.YuNetFaceDetector.assert_not_called()
        self.renderer.assert_not_called(); self.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
