"""Installer contract tests: temporary bundles and simulated model inference only."""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.build_hybrid_plugin import build


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins/czmiyou-yzzh/scripts/setup.py"
REAL_RUN = subprocess.run
spec = importlib.util.spec_from_file_location("plugin_setup_under_test", SCRIPT)
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = (Path(self.temporary.name) / "client bundle").resolve()
        runtime = self.root / "runtime"
        runtime.mkdir(parents=True)
        (self.root / "requirements-local.txt").write_text("# offline fixture\n")
        for name in ("workflow_core.py", "face_mosaic.py", "depth_video.py"):
            # Dry-run parses only download constants; no runtime import is needed.
            (runtime / name).write_bytes((ROOT / name).read_bytes())
        self.python = self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.addCleanup(patch.stopall)
        patch("sys.version_info", (3, 12, 0)).start()
        self.builder = patch.object(setup.venv, "EnvBuilder").start()
        self.run = patch.object(setup.subprocess, "run").start()
        patch("sys.stdout", self.output).start()
        patch("sys.stderr", self.errors).start()

    def main(self, *args):
        return setup.main(list(args), root=self.root)

    def existing_python(self):
        self.python.parent.mkdir(parents=True)
        self.python.touch()

    def test_default_full_install_and_legacy_models_install_both_models(self):
        for args in ((), ("--models",)):
            with self.subTest(args=args):
                self.run.reset_mock()
                self.assertEqual(self.main(*args), 0)
                self.assertEqual(self.run.call_count, 2)
                pip, models = self.run.call_args_list
                self.assertEqual(pip.args[0][:4], [str(self.python), "-m", "pip", "install"])
                self.assertEqual(models.args[0][0:3], [str(self.python), "-I", "-c"])
                self.assertEqual(models.args[0][-2:], [str(SCRIPT), str(self.root.resolve())])
                self.assertEqual(models.kwargs, {"cwd": str(self.root.resolve()), "check": True})

    def test_dry_run_names_models_sources_size_and_writes_nothing(self):
        self.assertEqual(self.main("--dry-run"), 0)
        text = self.output.getvalue()
        for expected in ("YuNet", "2023mar", "230 KB", "49642442", "49.9 MB", "100 MB",
                         "ModelScope", "modelscope.cn", "huggingface.co", "opencv_zoo", "inference"):
            self.assertIn(expected, text)
        self.builder.assert_not_called()
        self.run.assert_not_called()
        self.assertFalse((self.root / ".venv").exists())
        self.assertFalse((self.root / "runtime/models").exists())

    def test_models_only_reuses_private_python_even_with_older_launcher_python(self):
        self.existing_python()
        with patch("sys.version_info", (3, 9, 0)):
            self.assertEqual(self.main("--models-only"), 0)
        self.builder.assert_not_called()
        self.assertEqual(self.run.call_count, 1)
        self.assertEqual(self.run.call_args.args[0][0], str(self.python))
        self.assertNotIn("pip", self.run.call_args.args[0])

    def test_models_only_missing_environment_fails_without_installing(self):
        self.assertEqual(self.main("--models-only"), 1)
        self.builder.assert_not_called()
        self.run.assert_not_called()
        self.assertIn("Private Python is missing", self.errors.getvalue())

    def test_skip_models_explicitly_reports_incomplete_local_readiness(self):
        self.assertEqual(self.main("--skip-models"), 0)
        self.assertEqual(self.run.call_count, 1)
        self.assertIn("NOT READY", self.output.getvalue())
        self.assertNotIn("both model checks completed", self.output.getvalue())
        with self.assertRaises(SystemExit):
            self.main("--skip-models", "--models-only")

    def test_model_stage_failure_never_reports_complete_installation(self):
        self.run.side_effect = [None, subprocess.CalledProcessError(1, "fixture-model-stage")]
        self.assertEqual(self.main(), 1)
        self.assertIn("Installation incomplete", self.errors.getvalue())
        self.assertNotIn("both model checks completed", self.output.getvalue())

    def test_incomplete_source_skeleton_fails_before_environment_changes(self):
        (self.root / "runtime/depth_video.py").unlink()
        self.assertEqual(self.main(), 1)
        self.builder.assert_not_called()
        self.run.assert_not_called()

    def test_packaged_dry_run_works_from_an_unrelated_working_directory(self):
        archive = build(self.root / "customer.zip")
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(self.root / "extracted")
        plugin = self.root / "extracted/czmiyou-yzzh"
        result = REAL_RUN([sys.executable, str(plugin / "scripts/setup.py"), "--dry-run"],
                          cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(plugin / "runtime/models/face-detector"), result.stdout)
        self.assertIn("49642442", result.stdout)
        self.assertFalse((plugin / ".venv").exists())
        self.assertFalse((plugin / "runtime/models").exists())

    def fake_models(self, *, existing=True):
        frame = object()
        prediction = types.SimpleNamespace(shape=(518, 518))
        np = types.ModuleType("numpy")
        np.uint8 = object()
        np.zeros = Mock(return_value=frame)
        np.asarray = Mock(return_value=Mock(squeeze=Mock(return_value=prediction)))
        np.isfinite = Mock(return_value=Mock(all=Mock(return_value=True)))
        core = types.ModuleType("workflow_core")
        core._valid_depth_model = Mock(return_value=existing)
        core.ensure_depth_model = Mock()
        face = types.ModuleType("face_mosaic")
        face._valid_face_model = Mock(return_value=existing)
        face.ensure_face_model = Mock()
        face.YuNetFaceDetector = Mock(return_value=Mock(detect=Mock(return_value=[])))
        depth = types.ModuleType("depth_video")
        depth.MODEL_SIZE = 518
        renderer = Mock(input_name="pixel_values", output_name="depth")
        renderer._input_tensor.return_value = ("synthetic-tensor", None)
        renderer.session.run.return_value = ["synthetic-depth"]
        depth.DepthRenderer = Mock(return_value=renderer)
        patch.dict(sys.modules, {"numpy": np, "workflow_core": core, "face_mosaic": face, "depth_video": depth}).start()
        # install_models normally runs in an isolated child; restore its path here.
        patch.object(sys, "path", list(sys.path)).start()
        return np, core, face, depth, frame

    def test_existing_valid_models_reused_but_both_still_infer_on_synthetic_frame(self):
        np, core, face, depth, frame = self.fake_models()
        setup.install_models(self.root)
        face_path, depth_path = setup.model_paths(self.root.resolve())
        self.assertEqual(face.FACE_MODEL_PATH, face_path)
        self.assertEqual(core.DEPTH_MODEL_PATH, depth_path)
        self.assertEqual(core.PROJECT_DIR, self.root.resolve() / "runtime")
        face.ensure_face_model.assert_not_called()
        core.ensure_depth_model.assert_not_called()
        np.zeros.assert_called_once_with((320, 320, 3), dtype=np.uint8)
        face.YuNetFaceDetector.assert_called_once_with(face_path)
        face.YuNetFaceDetector.return_value.detect.assert_called_once_with(frame)
        depth.DepthRenderer.assert_called_once_with(depth_path, faithful_depth=True)
        depth.DepthRenderer.return_value.session.run.assert_called_once_with(
            ["depth"], {"pixel_values": "synthetic-tensor"})

    def test_missing_models_use_existing_downloaders_after_binding_bundle_paths(self):
        _, core, face, _, _ = self.fake_models(existing=False)
        face.ensure_face_model.side_effect = lambda **_: self.assertEqual(
            face.FACE_MODEL_PATH, setup.model_paths(self.root.resolve())[0])
        core.ensure_depth_model.side_effect = lambda **_: self.assertEqual(
            core.DEPTH_MODEL_PATH, setup.model_paths(self.root.resolve())[1])
        setup.install_models(self.root)
        face.ensure_face_model.assert_called_once()
        core.ensure_depth_model.assert_called_once()

    def test_unloadable_face_or_invalid_depth_prediction_is_not_ready(self):
        _, _, face, depth, _ = self.fake_models()
        face.YuNetFaceDetector.return_value.detect.side_effect = RuntimeError("fixture-load-failure")
        with self.assertRaisesRegex(RuntimeError, "fixture-load-failure"):
            setup.install_models(self.root)
        depth.DepthRenderer.assert_not_called()
        face.YuNetFaceDetector.return_value.detect.side_effect = None
        face.YuNetFaceDetector.return_value.detect.return_value = []
        numpy = sys.modules["numpy"]
        numpy.asarray.return_value.squeeze.return_value.shape = (1, 1)
        with self.assertRaisesRegex(RuntimeError, "invalid depth"):
            setup.install_models(self.root)


if __name__ == "__main__":
    unittest.main()
