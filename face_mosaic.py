from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np
import requests

from workflow_core import PROJECT_DIR, WorkflowError, inspect_video, resolve_ffmpeg


FACE_MODEL_PATH = PROJECT_DIR / "models" / "face-detector" / "face_detection_yunet_2023mar.onnx"
FACE_MODEL_DOWNLOAD_SOURCES = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    "face_detection_yunet_2023mar.onnx",
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx",
)
MIN_FACE_MODEL_BYTES = 200_000


Box = tuple[float, float, float, float]


def _valid_face_model(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= MIN_FACE_MODEL_BYTES
    except OSError:
        return False


def ensure_face_model(on_log: Callable[[str], None] | None = None) -> Path:
    """Download the small official OpenCV YuNet face detector when needed."""
    if _valid_face_model(FACE_MODEL_PATH):
        return FACE_MODEL_PATH
    FACE_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = FACE_MODEL_PATH.with_suffix(".part")
    last_error = ""
    for url in FACE_MODEL_DOWNLOAD_SOURCES:
        try:
            if on_log:
                on_log("首次使用：正在下载 OpenCV YuNet 人脸检测模型（约 230 KB）。")
            with requests.get(url, stream=True, timeout=(20, 120)) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            handle.write(chunk)
            if not _valid_face_model(temporary):
                raise WorkflowError("下载内容不是有效的 YuNet ONNX 模型。")
            os.replace(temporary, FACE_MODEL_PATH)
            return FACE_MODEL_PATH
        except Exception as exc:
            last_error = str(exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    raise WorkflowError(f"无法下载人脸检测模型，请检查网络后重试：{last_error}")


def _opencv_safe_model_path(path: Path) -> Path:
    """OpenCV DNN on Windows cannot always open models below a Unicode path."""
    try:
        str(path).encode("ascii")
        return path
    except UnicodeEncodeError:
        cache_dir = Path(tempfile.gettempdir()) / "depthflow-model-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / path.name
        if not cached.is_file() or cached.stat().st_size != path.stat().st_size:
            temporary = cached.with_suffix(".part")
            shutil.copyfile(path, temporary)
            os.replace(temporary, cached)
        return cached


def _iou(left: Box, right: Box) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    x1 = max(lx, rx)
    y1 = max(ly, ry)
    x2 = min(lx + lw, rx + rw)
    y2 = min(ly + lh, ry + rh)
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = max(1.0, lw * lh + rw * rh - intersection)
    return intersection / union


def _center_distance(left: Box, right: Box) -> float:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    distance = math.hypot((lx + lw / 2) - (rx + rw / 2), (ly + lh / 2) - (ry + rh / 2))
    scale = max(12.0, math.hypot(lw, lh), math.hypot(rw, rh))
    return distance / scale


def _clip_box(box: Box, width: int, height: int) -> Box:
    x, y, w, h = box
    x1 = max(0.0, min(float(width - 1), x))
    y1 = max(0.0, min(float(height - 1), y))
    x2 = max(x1 + 1.0, min(float(width), x + max(1.0, w)))
    y2 = max(y1 + 1.0, min(float(height), y + max(1.0, h)))
    return x1, y1, x2 - x1, y2 - y1


def expand_face_box(
    box: Box,
    frame_width: int,
    frame_height: int,
    *,
    width_scale: float = 1.55,
    height_scale: float = 1.85,
    upward_shift: float = 0.10,
) -> Box:
    """Expand a detected face to cover forehead, jaw and fast detector jitter."""
    x, y, width, height = box
    center_x = x + width / 2
    center_y = y + height / 2 - height * upward_shift
    expanded_width = width * max(1.0, width_scale)
    expanded_height = height * max(1.0, height_scale)
    return _clip_box(
        (
            center_x - expanded_width / 2,
            center_y - expanded_height / 2,
            expanded_width,
            expanded_height,
        ),
        frame_width,
        frame_height,
    )


@dataclass
class _Track:
    identifier: int
    box: np.ndarray
    velocity: np.ndarray = field(default_factory=lambda: np.zeros(4, dtype=np.float32))
    missed: int = 0


class FaceBoxTracker:
    """Small temporal tracker used only to keep privacy masks stable between detections."""

    def __init__(self, *, max_missed: int = 8) -> None:
        self.max_missed = max(0, int(max_missed))
        self._next_identifier = 1
        self._tracks: list[_Track] = []

    def reset(self) -> None:
        self._tracks.clear()

    def update(self, detections: Iterable[Box], *, frame_width: int, frame_height: int) -> list[Box]:
        detected = [np.asarray(_clip_box(box, frame_width, frame_height), dtype=np.float32) for box in detections]
        candidates: list[tuple[float, int, int]] = []
        for track_index, track in enumerate(self._tracks):
            predicted = tuple((track.box + track.velocity).tolist())
            for detection_index, detection in enumerate(detected):
                detection_box = tuple(detection.tolist())
                overlap = _iou(predicted, detection_box)
                distance = _center_distance(predicted, detection_box)
                if overlap >= 0.04 or distance <= 1.15:
                    candidates.append((overlap - 0.18 * distance, track_index, detection_index))
        candidates.sort(reverse=True)
        used_tracks: set[int] = set()
        used_detections: set[int] = set()
        for _score, track_index, detection_index in candidates:
            if track_index in used_tracks or detection_index in used_detections:
                continue
            track = self._tracks[track_index]
            detection = detected[detection_index]
            delta = detection - track.box
            track.velocity = 0.60 * track.velocity + 0.40 * delta
            track.box = 0.25 * (track.box + track.velocity) + 0.75 * detection
            track.box = np.asarray(_clip_box(tuple(track.box.tolist()), frame_width, frame_height), dtype=np.float32)
            track.missed = 0
            used_tracks.add(track_index)
            used_detections.add(detection_index)

        for track_index, track in enumerate(self._tracks):
            if track_index in used_tracks:
                continue
            track.box = np.asarray(
                _clip_box(tuple((track.box + track.velocity).tolist()), frame_width, frame_height),
                dtype=np.float32,
            )
            track.velocity *= 0.72
            track.missed += 1

        for detection_index, detection in enumerate(detected):
            if detection_index in used_detections:
                continue
            self._tracks.append(_Track(identifier=self._next_identifier, box=detection))
            self._next_identifier += 1

        self._tracks = [track for track in self._tracks if track.missed <= self.max_missed]
        return [tuple(track.box.tolist()) for track in self._tracks]


class YuNetFaceDetector:
    def __init__(
        self,
        model_path: str | Path,
        *,
        score_threshold: float = 0.55,
        nms_threshold: float = 0.30,
        max_side: int = 960,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self.opencv_model_path = _opencv_safe_model_path(self.model_path)
        self.score_threshold = float(score_threshold)
        self.nms_threshold = float(nms_threshold)
        self.max_side = max(320, int(max_side))
        self._detector: cv2.FaceDetectorYN | None = None
        self._input_size = (0, 0)

    def detect(self, frame: np.ndarray) -> list[Box]:
        height, width = frame.shape[:2]
        scale = min(1.0, self.max_side / max(width, height))
        if scale < 1.0:
            resized = cv2.resize(
                frame,
                (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            resized = frame
        input_height, input_width = resized.shape[:2]
        input_size = (input_width, input_height)
        if self._detector is None:
            self._detector = cv2.FaceDetectorYN.create(
                str(self.opencv_model_path),
                "",
                input_size,
                self.score_threshold,
                self.nms_threshold,
                5000,
            )
            self._input_size = input_size
        elif input_size != self._input_size:
            self._detector.setInputSize(input_size)
            self._input_size = input_size
        _status, faces = self._detector.detect(resized)
        if faces is None:
            return []
        inverse = 1.0 / scale
        return [
            (
                float(face[0]) * inverse,
                float(face[1]) * inverse,
                float(face[2]) * inverse,
                float(face[3]) * inverse,
            )
            for face in faces
            if len(face) >= 4
        ]


def apply_pixel_mosaic(frame: np.ndarray, boxes: Iterable[Box], *, block_size: int = 18) -> np.ndarray:
    """Apply an opaque nearest-neighbour pixel mosaic to every supplied box."""
    height, width = frame.shape[:2]
    block_size = max(6, int(block_size))
    for raw_box in boxes:
        x, y, box_width, box_height = _clip_box(raw_box, width, height)
        x1, y1 = int(math.floor(x)), int(math.floor(y))
        x2, y2 = int(math.ceil(x + box_width)), int(math.ceil(y + box_height))
        if x2 <= x1 or y2 <= y1:
            continue
        roi = frame[y1:y2, x1:x2]
        small_width = max(1, int(math.ceil(roi.shape[1] / block_size)))
        small_height = max(1, int(math.ceil(roi.shape[0] / block_size)))
        tiny = cv2.resize(roi, (small_width, small_height), interpolation=cv2.INTER_AREA)
        frame[y1:y2, x1:x2] = cv2.resize(
            tiny,
            (roi.shape[1], roi.shape[0]),
            interpolation=cv2.INTER_NEAREST,
        )
    return frame


def select_eye_privacy_faces(
    faces: list[Box],
    *,
    allow_three_view: bool = False,
    frame_width: int | None = None,
) -> list[Box]:
    """Choose the faces to obscure on a single-person reference board.

    Besides a normal portrait and a classic front/side/back board, the real-person
    workflow accepts a large facial portrait followed by front/side/back full-body
    views.  In that four-panel layout the large portrait and the frontal full-body
    view are both obscured; the side and back views remain unchanged.
    """
    if not faces:
        raise WorkflowError("人物参考图中未检测到清晰人脸，无法自动遮挡眼部。请换用无遮挡、清晰的正面或轻侧脸图片。")
    if len(faces) == 1:
        return [faces[0]]
    if not allow_three_view:
        raise WorkflowError("人物参考图中检测到多张人脸。请为每位人物上传只包含一个主体的图片。")
    if len(faces) > 3:
        raise WorkflowError("三视图参考板检测到超过 3 张人脸，请只保留同一人物的正面、侧面和背面视图。")
    ordered = sorted(faces, key=lambda item: item[0] + item[2] / 2)
    leftmost = ordered[0]
    second = ordered[1]
    board_width = float(frame_width or max(item[0] + item[2] for item in ordered))
    left_center = leftmost[0] + leftmost[2] / 2
    second_center = second[0] + second[2] / 2
    left_area = max(1.0, leftmost[2] * leftmost[3])
    second_area = max(1.0, second[2] * second[3])
    portrait_plus_turnaround = (
        left_center <= board_width * 0.43
        and board_width * 0.40 <= second_center <= board_width * 0.66
        and left_area >= second_area * 2.2
    )
    if portrait_plus_turnaround:
        return [leftmost, second]
    # A classic turnaround is front / side / back from left to right. A back
    # view normally has no detectable face, so only its leftmost front is masked.
    return [leftmost]


def select_eye_privacy_face(faces: list[Box], *, allow_three_view: bool = False) -> Box:
    """Backward-compatible single-face selector used by older callers/tests."""
    return select_eye_privacy_faces(faces, allow_three_view=allow_three_view)[0]


def render_eye_privacy_image(
    input_path: str | Path,
    output_path: str | Path,
    *,
    score_threshold: float = 0.55,
    bar_color_bgr: tuple[int, int, int] = (24, 24, 235),
    allow_three_view: bool = False,
) -> dict[str, int | str]:
    """Cover identity-bearing frontal eyes on a supported single-person board."""
    source = Path(input_path).expanduser().resolve()
    target = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到人物参考图：{source}")
    try:
        encoded = np.fromfile(source, dtype=np.uint8)
        frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    except (OSError, ValueError, cv2.error) as exc:
        raise WorkflowError("无法读取人物参考图，请换用清晰的 JPG、PNG、WEBP 或 BMP 图片。") from exc
    if frame is None or frame.size == 0:
        raise WorkflowError("无法读取人物参考图，请换用清晰的 JPG、PNG、WEBP 或 BMP 图片。")
    height, width = frame.shape[:2]
    if min(width, height) < 300:
        raise WorkflowError("人物参考图尺寸过小，宽和高都应至少为 300 像素。")
    detector = YuNetFaceDetector(ensure_face_model(), score_threshold=score_threshold)
    faces = detector.detect(frame)
    selected_faces = select_eye_privacy_faces(
        faces,
        allow_three_view=allow_three_view,
        frame_width=width,
    )
    for x, y, face_width, face_height in selected_faces:
        start = (
            max(0, int(round(x - face_width * 0.08))),
            max(0, int(round(y + face_height * 0.39))),
        )
        end = (
            min(width - 1, int(round(x + face_width * 1.08))),
            min(height - 1, int(round(y + face_height * 0.39))),
        )
        thickness = max(18, int(round(face_height * 0.22)))
        cv2.line(frame, start, end, bar_color_bgr, thickness=thickness, lineType=cv2.LINE_AA)
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = target.suffix.lower() if target.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} else ".jpg"
    params = [cv2.IMWRITE_JPEG_QUALITY, 96] if suffix in {".jpg", ".jpeg"} else []
    ok, output = cv2.imencode(suffix, frame, params)
    if not ok:
        raise WorkflowError("眼部遮挡图编码失败。")
    output.tofile(target)
    return {
        "output": str(target),
        "width": width,
        "height": height,
        "faces": len(faces),
        "masked_faces": len(selected_faces),
        "layout": (
            "portrait_three_view"
            if len(selected_faces) == 2
            else "three_view"
            if len(faces) > 1 and allow_three_view
            else "single"
        ),
    }


def render_face_mosaic_video(
    input_path: str | Path,
    output_path: str | Path,
    *,
    block_size: int = 18,
    score_threshold: float = 0.55,
    on_progress: Callable[[int, int, int], None] | None = None,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, int | str]:
    """Detect and track all visible faces, then render an H.264 MP4 with source audio."""
    source = Path(input_path).expanduser().resolve()
    target = Path(output_path).expanduser().resolve()
    if source == target:
        raise WorkflowError("打码视频不能覆盖原片。")
    info = inspect_video(source)
    model_path = ensure_face_model(on_log=on_log)
    detector = YuNetFaceDetector(model_path, score_threshold=score_threshold)
    tracker = FaceBoxTracker(max_missed=max(5, int(round(info.fps * 0.25))))
    target.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{info.width}x{info.height}",
        "-r", f"{info.fps:.8f}", "-i", "pipe:0", "-i", str(source),
        "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-shortest", str(target),
    ]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    encoder = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )
    capture = cv2.VideoCapture(str(source))
    processed = 0
    maximum_faces = 0
    frames_with_faces = 0
    try:
        if not capture.isOpened() or encoder.stdin is None:
            raise WorkflowError("无法打开原片或视频编码器。")
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            detections = detector.detect(frame)
            tracked = tracker.update(detections, frame_width=info.width, frame_height=info.height)
            expanded = [expand_face_box(box, info.width, info.height) for box in tracked]
            if expanded:
                frames_with_faces += 1
                maximum_faces = max(maximum_faces, len(expanded))
                apply_pixel_mosaic(frame, expanded, block_size=block_size)
            try:
                encoder.stdin.write(frame.tobytes())
            except BrokenPipeError as exc:
                raise WorkflowError("打码视频编码器提前退出。") from exc
            processed += 1
            if on_progress and (processed == 1 or processed % max(1, int(info.fps / 2)) == 0):
                on_progress(processed, info.frame_count, maximum_faces)
        if processed <= 0:
            raise WorkflowError("原片没有可读取的视频帧。")
        encoder.stdin.close()
        encoder.stdin = None
        _stdout, stderr = encoder.communicate()
        if encoder.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1200:]
            raise WorkflowError(f"打码视频编码失败：{detail or '未知错误'}")
    except Exception:
        if encoder.stdin is not None:
            try:
                encoder.stdin.close()
            except OSError:
                pass
        if encoder.poll() is None:
            encoder.kill()
        encoder.wait()
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        capture.release()

    if not target.is_file() or target.stat().st_size <= 0:
        raise WorkflowError("打码处理结束，但没有生成有效视频。")
    if on_progress:
        on_progress(processed, info.frame_count, maximum_faces)
    return {
        "output": str(target),
        "frames": processed,
        "frames_with_faces": frames_with_faces,
        "maximum_faces": maximum_faces,
    }
