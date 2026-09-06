from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageFilter


MODEL_HEIGHT = 924
MODEL_WIDTH = 518
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def open_session(model_path: Path) -> ort.InferenceSession:
    providers = ort.get_available_providers()
    preferred = ["DmlExecutionProvider", "CPUExecutionProvider"]
    active = [provider for provider in preferred if provider in providers]
    return ort.InferenceSession(str(model_path), providers=active)


def estimate_depth_raw(image: Image.Image, session: ort.InferenceSession) -> np.ndarray:
    resized = image.convert("RGB").resize((MODEL_WIDTH, MODEL_HEIGHT), Image.Resampling.BICUBIC)
    rgb = np.asarray(resized, dtype=np.float32) / 255.0
    tensor = ((rgb - MEAN) / STD).transpose(2, 0, 1)[None]
    depth = session.run(None, {session.get_inputs()[0].name: tensor})[0][0]
    return depth.astype(np.float32)


def normalize_depth(depth: np.ndarray, low: float | None = None, high: float | None = None) -> tuple[np.ndarray, float, float]:
    measured_low, measured_high = np.percentile(depth, (2.0, 98.0))
    if low is None or high is None:
        low, high = float(measured_low), float(measured_high)
    normalized = np.clip((depth - low) / max(high - low, 1e-6), 0.0, 1.0)
    return normalized.astype(np.float32), float(measured_low), float(measured_high)


def blur_float(field: np.ndarray, sigma: float) -> np.ndarray:
    radius = max(1, int(np.ceil(sigma * 3.0)))
    coordinates = np.arange(-radius, radius + 1, dtype=np.float32)
    kernel = np.exp(-(coordinates * coordinates) / (2.0 * sigma * sigma))
    kernel /= kernel.sum()

    height, width = field.shape
    vertical_pad = np.pad(field, ((radius, radius), (0, 0)), mode="edge")
    vertical = np.zeros_like(field, dtype=np.float32)
    for offset, weight in enumerate(kernel):
        vertical += vertical_pad[offset : offset + height] * weight

    horizontal_pad = np.pad(vertical, ((0, 0), (radius, radius)), mode="edge")
    output = np.zeros_like(field, dtype=np.float32)
    for offset, weight in enumerate(kernel):
        output += horizontal_pad[:, offset : offset + width] * weight
    return output


def make_white_clay(image: Image.Image, depth: np.ndarray) -> Image.Image:
    smooth = blur_float(depth, 0.72)
    dy, dx = np.gradient(smooth)

    relief = 19.0
    nx = -dx * relief
    ny = -dy * relief
    nz = np.ones_like(smooth)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / norm, ny / norm, nz / norm

    light = np.array([-0.42, -0.56, 0.71], dtype=np.float32)
    light /= np.linalg.norm(light)
    diffuse = np.clip(nx * light[0] + ny * light[1] + nz * light[2], 0.0, 1.0)

    broad = blur_float(smooth, 11.0)
    occlusion = np.clip((broad - smooth) * 2.35, 0.0, 0.19)
    rim = np.clip(np.hypot(dx, dy) * 14.0, 0.0, 0.13)

    small = image.convert("L").resize((MODEL_WIDTH, MODEL_HEIGHT), Image.Resampling.LANCZOS)
    luma = np.asarray(small, dtype=np.float32) / 255.0
    luma_soft = blur_float(luma, 1.45)
    luma_broad = blur_float(luma, 10.0)
    soft_luma = 0.98 + (luma_broad - 0.5) * 0.18
    material_detail = (luma - luma_soft) * 0.46 + (luma_soft - luma_broad) * 0.24

    height_light = np.linspace(1.04, 0.94, MODEL_HEIGHT, dtype=np.float32)[:, None]
    shade = (0.40 + diffuse * 0.48) * soft_luma * height_light
    shade *= 1.0 - occlusion
    shade -= rim * 0.19
    shade += np.clip(material_detail, -0.12, 0.12)
    shade += (smooth - 0.5) * 0.05
    shade += np.power(diffuse, 14.0) * 0.035
    shade = np.clip(shade, 0.27, 0.92)

    target_size = image.size
    shade_image = Image.fromarray(np.uint8(shade * 255), mode="L")
    shade_image = shade_image.resize(target_size, Image.Resampling.LANCZOS)
    shade_image = shade_image.filter(ImageFilter.UnsharpMask(radius=1.1, percent=85, threshold=2))
    gray = np.asarray(shade_image, dtype=np.uint8)

    # Neutral, slightly cool gray gives the matte viewport/clay material seen in white-model previews.
    result = np.stack(
        [
            np.clip(gray.astype(np.int16) + 2, 0, 255),
            np.clip(gray.astype(np.int16) + 3, 0, 255),
            np.clip(gray.astype(np.int16) + 4, 0, 255),
        ],
        axis=-1,
    ).astype(np.uint8)
    return Image.fromarray(result, mode="RGB")


def process_images(input_dir: Path, output_dir: Path, model_path: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    session = open_session(model_path)
    images = sorted(
        path for path in input_dir.iterdir() if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
    )
    if not images:
        raise SystemExit(f"No images found in {input_dir}")
    for index, path in enumerate(images, start=1):
        with Image.open(path) as source:
            rgb = source.convert("RGB")
            raw_depth = estimate_depth_raw(rgb, session)
            depth, _, _ = normalize_depth(raw_depth)
            output = make_white_clay(rgb, depth)
            output.save(output_dir / f"{path.stem}_whiteclay.jpg", quality=94, subsampling=0)
        print(f"[{index}/{len(images)}] {path.name}", flush=True)


def process_video(
    input_video: Path,
    output_video: Path,
    model_path: Path,
    ffmpeg_path: Path,
    width: int,
    height: int,
    fps: float,
) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    session = open_session(model_path)
    frame_bytes = width * height * 3

    decoder = subprocess.Popen(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_video),
            "-map",
            "0:v:0",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    encoder = subprocess.Popen(
        [
            str(ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-s",
            f"{width}x{height}",
            "-r",
            f"{fps:g}",
            "-i",
            "-",
            "-i",
            str(input_video),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "16",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(output_video),
        ],
        stdin=subprocess.PIPE,
    )

    frame_index = 0
    running_low: float | None = None
    running_high: float | None = None
    try:
        assert decoder.stdout is not None
        assert encoder.stdin is not None
        while True:
            data = decoder.stdout.read(frame_bytes)
            if not data:
                break
            if len(data) != frame_bytes:
                raise RuntimeError(f"Incomplete decoded frame: {len(data)} of {frame_bytes} bytes")

            array = np.frombuffer(data, dtype=np.uint8).reshape(height, width, 3)
            source = Image.fromarray(array, mode="RGB")
            raw_depth = estimate_depth_raw(source, session)
            _, measured_low, measured_high = normalize_depth(raw_depth)

            if running_low is None or running_high is None:
                running_low, running_high = measured_low, measured_high
            else:
                current_span = max(measured_high - measured_low, 1e-6)
                running_span = max(running_high - running_low, 1e-6)
                scale_jump = max(current_span / running_span, running_span / current_span)
                center_jump = abs((measured_low + measured_high) - (running_low + running_high)) / (2.0 * running_span)
                if scale_jump > 1.65 or center_jump > 0.42:
                    running_low, running_high = measured_low, measured_high
                else:
                    alpha = 0.22
                    running_low = running_low * (1.0 - alpha) + measured_low * alpha
                    running_high = running_high * (1.0 - alpha) + measured_high * alpha

            depth, _, _ = normalize_depth(raw_depth, running_low, running_high)
            rendered = make_white_clay(source, depth)
            encoder.stdin.write(np.asarray(rendered, dtype=np.uint8).tobytes())
            frame_index += 1
            if frame_index == 1 or frame_index % 30 == 0:
                print(f"Rendered {frame_index} frames", flush=True)
    finally:
        if decoder.stdout is not None:
            decoder.stdout.close()
        if encoder.stdin is not None:
            encoder.stdin.close()

    decoder_code = decoder.wait()
    encoder_code = encoder.wait()
    if decoder_code != 0 or encoder_code != 0:
        raise RuntimeError(f"FFmpeg failed: decoder={decoder_code}, encoder={encoder_code}")
    print(f"Finished {frame_index} frames -> {output_video}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render frames as matte gray-white clay using monocular depth.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-dir", type=Path)
    source.add_argument("--input-video", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--output-video", type=Path)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--width", type=int, default=1080)
    parser.add_argument("--height", type=int, default=1920)
    parser.add_argument("--fps", type=float, default=30.0)
    args = parser.parse_args()
    if args.input_dir is not None:
        if args.output_dir is None:
            parser.error("--output-dir is required with --input-dir")
        process_images(args.input_dir, args.output_dir, args.model)
    else:
        if args.output_video is None or args.ffmpeg is None:
            parser.error("--output-video and --ffmpeg are required with --input-video")
        process_video(
            args.input_video,
            args.output_video,
            args.model,
            args.ffmpeg,
            args.width,
            args.height,
            args.fps,
        )


if __name__ == "__main__":
    main()
