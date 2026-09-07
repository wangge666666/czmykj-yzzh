"""Opt-in live upload smoke check using only freshly generated, neutral fixtures.

Never accepts a user's file, credentials, or a generation prompt. No paid API.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-synthetic", action="store_true")
    args = parser.parse_args()
    if not args.live_synthetic:
        raise SystemExit("No upload. Use --live-synthetic to upload neutral generated test media to Litterbox.")
    from yzzh_local.media import TemporaryMediaStore
    from hybrid_shared import HybridError
    from workflow_core import resolve_ffmpeg
    results = []
    with tempfile.TemporaryDirectory(prefix="yzzh-public-fixture-") as directory:
        root = Path(directory)
        for kind, name, options in (
            ("image", "synthetic.png", ["-frames:v", "1"]),
            ("video", "synthetic.mp4", ["-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p"]),
        ):
            path = root / name
            subprocess.run([str(resolve_ffmpeg()), "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x320:r=30", *options, str(path)], check=True)
            store = TemporaryMediaStore()
            try:
                (store.upload_video if kind == "video" else store.upload_file)(path)
                results.append({"kind": kind, "bytes": path.stat().st_size, "upload_and_readback": "passed", "host": "Litterbox"})
            except HybridError as error:
                results.append({"kind": kind, "upload_and_readback": "failed", "code": error.code})
                print(json.dumps({"results": results, "paid_generation": False}))
                return 1
        print(json.dumps({"results": results, "paid_generation": False, "retention": "host states three days; not observed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
