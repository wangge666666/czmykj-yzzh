"""Install the private environment and verify the two required local models."""
import argparse
import ast
import os
import subprocess
import sys
import venv
from pathlib import Path


def literal_constant(path, name):
    """Read download metadata without importing uninstalled runtime dependencies."""
    for statement in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise RuntimeError("Missing model metadata in complete bundle: " + name)


def model_paths(root):
    runtime = root / "runtime"
    return (
        runtime / "models" / "face-detector" / "face_detection_yunet_2023mar.onnx",
        runtime / "models" / "depth-anything-v2-small" / "model_fp16.onnx",
    )


def describe_models(root):
    runtime = root / "runtime"
    face, depth = model_paths(root)
    print("Required local models (included by default):")
    print("  OpenCV YuNet 2023mar: approximately 230 KB (estimate);", face)
    print("    Exact bytes:", literal_constant(runtime / "face_mosaic.py", "FACE_MODEL_SIZE_BYTES"))
    print("    SHA-256:", literal_constant(runtime / "face_mosaic.py", "FACE_MODEL_SHA256"))
    for url in literal_constant(runtime / "face_mosaic.py", "FACE_MODEL_DOWNLOAD_SOURCES"):
        print("    Source:", url)
    size = literal_constant(runtime / "workflow_core.py", "DEPTH_MODEL_SIZE_BYTES")
    print("  Depth Anything V2 Small FP16:", size, "bytes;", depth)
    print("    SHA-256:", literal_constant(runtime / "workflow_core.py", "DEPTH_MODEL_SHA256"))
    for name, url in literal_constant(runtime / "workflow_core.py", "DEPTH_MODEL_DOWNLOAD_SOURCES"):
        print("    Source:", name, url)
    print("  Model files: about 49.9 MB total; allow about 100 MB for files and download temporaries (estimate).")
    print("  Private Python/dependency disk usage is additional and varies by platform.")
    print("  Reuse valid files; loading and synthetic-frame inference must pass for both models.")


def install_models(root):
    """Run only under the bundle's private Python; never inspect customer media."""
    if sys.version_info < (3, 10):
        raise RuntimeError("The private Python must be 3.10+; run the full setup with Python 3.12.")
    root = Path(root).resolve()
    runtime = root / "runtime"
    sys.path.insert(0, str(runtime))
    import numpy as np
    import workflow_core as core

    # Bind before importing face_mosaic: it imports PROJECT_DIR from core.
    # Neither the caller's working directory nor a user account directory owns models.
    core.PROJECT_DIR = runtime
    face_path, depth_path = model_paths(root)
    core.DEPTH_MODEL_PATH = depth_path
    import face_mosaic as face
    from depth_video import DepthRenderer, MODEL_SIZE
    face.FACE_MODEL_PATH = face_path
    frame = np.zeros((320, 320, 3), dtype=np.uint8)

    face_reused = face._valid_face_model(face_path)
    if not face_reused:
        face.ensure_face_model(on_log=print)
    boxes = face.YuNetFaceDetector(face_path).detect(frame)
    if not isinstance(boxes, list) or not np.isfinite(boxes).all():
        raise RuntimeError("YuNet synthetic-frame inference returned invalid detections")
    print("YuNet: loading and synthetic-frame inference passed" + ("; existing file reused." if face_reused else "."))

    depth_reused = core._valid_depth_model(depth_path)
    if not depth_reused:
        core.ensure_depth_model(on_log=print)
    # Reuse the runtime renderer's ORT BASIC optimization and preprocessing,
    # rather than a different session configuration that may fail in production.
    renderer = DepthRenderer(depth_path, faithful_depth=True)
    tensor, _ = renderer._input_tensor(frame)
    prediction = np.asarray(renderer.session.run(
        [renderer.output_name], {renderer.input_name: tensor}
    )[0]).squeeze()
    if prediction.shape != (MODEL_SIZE, MODEL_SIZE) or not np.isfinite(prediction).all():
        raise RuntimeError("Depth model synthetic-frame inference returned invalid depth")
    print("Depth Anything V2 Small: hash, loading and synthetic-frame inference passed" +
          ("; existing file reused." if depth_reused else "."))


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--models", action="store_true", help="Compatibility option: required models are installed by default")
    mode.add_argument("--skip-models", action="store_true", help="Install only the environment; local model workflows remain unready")
    mode.add_argument("--models-only", action="store_true", help="Install/verify required models with the existing private Python, without reinstalling dependencies")
    parser.add_argument("--dry-run", action="store_true", help="Describe changes and model sources without installing or downloading")
    args = parser.parse_args(argv)
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        if not all((root / name).is_file() for name in (
            "requirements-local.txt", "runtime/workflow_core.py", "runtime/face_mosaic.py", "runtime/depth_video.py"
        )):
            raise RuntimeError("Use the complete extracted customer bundle; the source plugin skeleton is not installable.")
        if args.models_only:
            print("Use existing private Python:", python, "(no venv creation or pip install)")
        else:
            print("Create/update private venv:", root / ".venv")
            print("Install local requirements:", root / "requirements-local.txt")
        if args.skip_models:
            print("Models skipped explicitly: face masking and depth processing remain NOT READY.")
            print("Repair later with: python scripts/setup.py --models-only")
        else:
            describe_models(root)
        print("No global Codex/WorkBuddy configuration is modified; no customer media or provider generation is used.")
        if args.dry_run:
            return 0
        if not args.models_only and sys.version_info < (3, 10):
            raise RuntimeError("Use Python 3.10+ for customer installation (Python 3.12 recommended).")
        if args.models_only:
            if not python.is_file():
                raise RuntimeError("Private Python is missing; run setup.py without --models-only first.")
        else:
            # Relocatable POSIX Python distributions need executable symlinks.
            venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(root / ".venv")
            subprocess.run([str(python), "-m", "pip", "install", "-r", str(root / "requirements-local.txt")], check=True)
        if args.skip_models:
            print("Environment installation completed; required local models were skipped. Full installation is NOT READY.")
            return 0
        sys.stdout.flush()
        subprocess.run([
            str(python), "-I", "-c",
            "import runpy,sys; from pathlib import Path; runpy.run_path(sys.argv[1])['install_models'](Path(sys.argv[2]))",
            str(Path(__file__).resolve()), str(root),
        ], cwd=str(root), check=True)
        print("Required local environment and both model checks completed. Paid provider/account validation is separate.")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("Installation incomplete:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
