from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
from PIL import Image

from white_clay_render import estimate_depth_raw, normalize_depth, open_session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    session = open_session(args.model)
    images = sorted(path for path in args.input_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg"})
    for path in images:
        with Image.open(path) as source:
            raw = estimate_depth_raw(source.convert("RGB"), session)
        depth, _, _ = normalize_depth(raw)
        depth_image = Image.fromarray(np.uint8(np.clip(depth, 0.0, 1.0) * 255), mode="L")
        depth_image.save(args.output_dir / f"{path.stem}_depth.png")
        print(path.name, flush=True)


if __name__ == "__main__":
    main()
