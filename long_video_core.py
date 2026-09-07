from __future__ import annotations

import json
import hashlib
import math
import os
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from workflow_core import WorkflowError, atomic_write_text, inspect_video, resolve_ffmpeg


_FACE_CLASSIFIER: cv2.CascadeClassifier | None = None
_PEOPLE_HOG: cv2.HOGDescriptor | None = None
_PERSON_DETECTOR_NET: cv2.dnn.Net | None = None
_PERSON_DETECTOR_FAILED = False
_PERSON_DETECTOR_LOCK = threading.Lock()
_PERSON_DETECTOR_PATH = (
    Path(__file__).resolve().parent
    / "models"
    / "person-detector"
    / "object_detection_yolox_2022nov_int8bq.onnx"
)
_YOLOX_INPUT_SIZE = 640
_YOLOX_GRIDS: np.ndarray | None = None
_YOLOX_STRIDES: np.ndarray | None = None
AUDIO_STEM_MODEL = "htdemucs_ft"
AUDIO_STEM_VERSION = 1


@dataclass(frozen=True)
class ShotBoundary:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def public(self) -> dict[str, Any]:
        result = asdict(self)
        result["duration"] = round(self.duration, 3)
        result["start"] = round(self.start, 3)
        result["end"] = round(self.end, 3)
        return result


def _people_detectors() -> tuple[cv2.CascadeClassifier | None, cv2.HOGDescriptor | None]:
    """Create the lightweight local detectors lazily.

    These detections are suggestions only.  The web UI always asks the user to
    confirm the cast before a paid generation request is submitted.
    """
    global _FACE_CLASSIFIER, _PEOPLE_HOG
    if _FACE_CLASSIFIER is None:
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        classifier = cv2.CascadeClassifier(str(cascade_path))
        _FACE_CLASSIFIER = classifier if not classifier.empty() else None
    if _PEOPLE_HOG is None:
        hog = cv2.HOGDescriptor()
        hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        _PEOPLE_HOG = hog
    return _FACE_CLASSIFIER, _PEOPLE_HOG


def _remove_nested_people_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    kept: list[tuple[int, int, int, int]] = []
    for index, (x, y, width, height) in enumerate(boxes):
        area = width * height
        nested = False
        for other_index, (ox, oy, other_width, other_height) in enumerate(boxes):
            if index == other_index or other_width * other_height <= area:
                continue
            if x >= ox and y >= oy and x + width <= ox + other_width and y + height <= oy + other_height:
                nested = True
                break
        if not nested:
            kept.append((x, y, width, height))
    return kept


def _summarize_people_counts(counts: list[int], max_people: int = 4) -> dict[str, Any]:
    safe_counts = [max(0, min(int(value), max_people)) for value in counts]
    suggested = max(safe_counts, default=0)
    if not safe_counts or suggested == 0:
        confidence = "low"
    elif all(value == suggested for value in safe_counts):
        confidence = "high"
    elif safe_counts.count(suggested) >= 2:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "suggested_actor_count": suggested,
        "detection_confidence": confidence,
        "sample_actor_counts": safe_counts,
    }


def _person_detector() -> cv2.dnn.Net | None:
    global _PERSON_DETECTOR_NET, _PERSON_DETECTOR_FAILED
    if _PERSON_DETECTOR_NET is not None:
        return _PERSON_DETECTOR_NET
    if _PERSON_DETECTOR_FAILED or not _PERSON_DETECTOR_PATH.is_file():
        return None
    try:
        # OpenCV's Windows ONNX loader cannot open a path containing Chinese
        # characters. Loading the same file as a byte buffer avoids that bug.
        model_bytes = np.fromfile(_PERSON_DETECTOR_PATH, dtype=np.uint8)
        _PERSON_DETECTOR_NET = cv2.dnn.readNetFromONNX(model_bytes)
    except cv2.error:
        _PERSON_DETECTOR_FAILED = True
        return None
    return _PERSON_DETECTOR_NET


def _yolox_anchors() -> tuple[np.ndarray, np.ndarray]:
    global _YOLOX_GRIDS, _YOLOX_STRIDES
    if _YOLOX_GRIDS is None or _YOLOX_STRIDES is None:
        grids: list[np.ndarray] = []
        expanded_strides: list[np.ndarray] = []
        for stride in (8, 16, 32):
            size = _YOLOX_INPUT_SIZE // stride
            xv, yv = np.meshgrid(np.arange(size), np.arange(size))
            grid = np.stack((xv, yv), axis=2).reshape(1, -1, 2)
            grids.append(grid)
            expanded_strides.append(np.full((*grid.shape[:2], 1), stride))
        _YOLOX_GRIDS = np.concatenate(grids, axis=1).astype(np.float32)
        _YOLOX_STRIDES = np.concatenate(expanded_strides, axis=1).astype(np.float32)
    return _YOLOX_GRIDS, _YOLOX_STRIDES


def _xyxy_to_xywh_boxes(boxes: np.ndarray) -> list[list[float]]:
    """Convert decoded YOLOX boxes for OpenCV NMSBoxes.

    YOLOX decoding yields x1/y1/x2/y2, while OpenCV NMSBoxes expects
    left/top/width/height. Passing x2/y2 as width/height weakens overlap
    suppression and can turn one foreground person into several UI slots.
    """
    return [
        [
            float(box[0]),
            float(box[1]),
            max(0.0, float(box[2] - box[0])),
            max(0.0, float(box[3] - box[1])),
        ]
        for box in boxes
    ]


def _detect_people_yolox(
    frame: np.ndarray,
    *,
    confidence_threshold: float = 0.25,
    max_people: int = 4,
) -> list[tuple[tuple[int, int, int, int], float]]:
    net = _person_detector()
    if net is None:
        return []
    height, width = frame.shape[:2]
    ratio = min(_YOLOX_INPUT_SIZE / height, _YOLOX_INPUT_SIZE / width)
    resized = cv2.resize(
        cv2.cvtColor(frame, cv2.COLOR_BGR2RGB),
        (max(1, int(width * ratio)), max(1, int(height * ratio))),
        interpolation=cv2.INTER_LINEAR,
    ).astype(np.float32)
    padded = np.full((_YOLOX_INPUT_SIZE, _YOLOX_INPUT_SIZE, 3), 114.0, dtype=np.float32)
    padded[: resized.shape[0], : resized.shape[1]] = resized
    blob = np.transpose(padded, (2, 0, 1))[np.newaxis, :, :, :]
    with _PERSON_DETECTOR_LOCK:
        net.setInput(blob)
        output = net.forward(net.getUnconnectedOutLayersNames())[0][0].copy()

    grids, expanded_strides = _yolox_anchors()
    output[:, :2] = (output[:, :2] + grids[0]) * expanded_strides[0]
    output[:, 2:4] = np.exp(output[:, 2:4]) * expanded_strides[0]
    boxes = np.empty_like(output[:, :4])
    boxes[:, 0] = output[:, 0] - output[:, 2] / 2.0
    boxes[:, 1] = output[:, 1] - output[:, 3] / 2.0
    boxes[:, 2] = output[:, 0] + output[:, 2] / 2.0
    boxes[:, 3] = output[:, 1] + output[:, 3] / 2.0
    scores = output[:, 4:5] * output[:, 5:]
    class_ids = np.argmax(scores, axis=1)
    max_scores = np.max(scores, axis=1)
    person_indices = np.flatnonzero((class_ids == 0) & (max_scores >= confidence_threshold))
    if not len(person_indices):
        return []
    person_boxes = boxes[person_indices]
    person_scores = max_scores[person_indices]
    keep = cv2.dnn.NMSBoxes(
        _xyxy_to_xywh_boxes(person_boxes),
        person_scores.tolist(),
        confidence_threshold,
        0.5,
    )
    detections: list[tuple[tuple[int, int, int, int], float]] = []
    for relative_index in np.asarray(keep).reshape(-1):
        box = person_boxes[int(relative_index)] / ratio
        x1 = max(0, min(width - 1, int(round(float(box[0])))))
        y1 = max(0, min(height - 1, int(round(float(box[1])))))
        x2 = max(x1 + 1, min(width, int(round(float(box[2])))))
        y2 = max(y1 + 1, min(height, int(round(float(box[3])))))
        detections.append(((x1, y1, x2, y2), float(person_scores[int(relative_index)])))
    detections.sort(key=lambda item: (item[0][0] + item[0][2]) / 2.0)
    return detections[:max_people]


def detect_people_boxes(
    frame: np.ndarray,
    *,
    confidence_threshold: float = 0.25,
    max_people: int = 4,
) -> list[tuple[tuple[int, int, int, int], float]]:
    """Expose privacy-local YOLOX person boxes for final composition QA."""
    return _detect_people_yolox(
        frame,
        confidence_threshold=confidence_threshold,
        max_people=max_people,
    )


def _legacy_people_count(frame: np.ndarray) -> int:
    face_classifier, people_hog = _people_detectors()
    height, width = frame.shape[:2]
    scale = min(1.0, 480.0 / max(width, height))
    if scale < 1.0:
        frame = cv2.resize(
            frame,
            (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
            interpolation=cv2.INTER_AREA,
        )
    face_count = 0
    if face_classifier is not None:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_classifier.detectMultiScale(gray, scaleFactor=1.12, minNeighbors=5, minSize=(24, 24))
        face_count = len(faces)
    body_count = 0
    if people_hog is not None and frame.shape[0] >= 128 and frame.shape[1] >= 64:
        boxes, weights = people_hog.detectMultiScale(
            frame,
            winStride=(8, 8),
            padding=(8, 8),
            scale=1.08,
        )
        confident_boxes = [
            tuple(int(item) for item in box)
            for box, weight in zip(boxes, weights, strict=False)
            if float(weight) >= 0.15
        ]
        body_count = len(_remove_nested_people_boxes(confident_boxes))
    return max(face_count, body_count)


def _person_position_label(index: int, total: int) -> str:
    if total <= 1:
        return "主体人物"
    if total == 2:
        return ("左侧人物", "右侧人物")[index]
    if total == 3:
        return ("左侧人物", "中间人物", "右侧人物")[index]
    return f"从左第 {index + 1} 位人物"


def estimate_people_count(
    video_path: str | Path,
    *,
    max_people: int = 4,
    preview_path: str | Path | None = None,
) -> dict[str, Any]:
    """Detect people across five positions and expose explicit mapping slots.

    YOLOX detects frontal, profile, back-facing and partially visible people.
    The older face/HOG path remains as an offline fallback when the local ONNX
    model is unavailable. Results are suggestions and must be confirmed before
    a paid generation request is submitted.
    """
    source = Path(video_path).expanduser().resolve()
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return _summarize_people_counts([], max_people)
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    sample_positions = [0.12, 0.3, 0.5, 0.7, 0.88]
    counts: list[int] = []
    best_frame: np.ndarray | None = None
    best_detections: list[tuple[tuple[int, int, int, int], float]] = []
    detector_available = _person_detector() is not None
    try:
        for position in sample_positions:
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(frame_count - 1, int(frame_count * position)))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            detections = _detect_people_yolox(frame, max_people=max_people) if detector_available else []
            count = len(detections) if detector_available else _legacy_people_count(frame)
            counts.append(min(count, max_people))
            current_rank = (len(detections), sum(item[1] for item in detections))
            best_rank = (len(best_detections), sum(item[1] for item in best_detections))
            if detections and current_rank > best_rank:
                best_frame = frame.copy()
                best_detections = detections
    finally:
        capture.release()

    result = _summarize_people_counts(counts, max_people)
    result["detector_backend"] = "yolox" if detector_available else "legacy_face_hog"
    result["person_slots"] = []
    if best_frame is not None and best_detections:
        height, width = best_frame.shape[:2]
        annotated = best_frame.copy()
        for index, (box, confidence) in enumerate(best_detections):
            x1, y1, x2, y2 = box
            color = ((45, 210, 120), (235, 155, 35), (70, 140, 245), (210, 80, 200))[index]
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, max(3, round(min(width, height) / 300)))
            cv2.putText(
                annotated,
                f"P{index + 1}",
                (x1 + 8, max(36, y1 + 36)),
                cv2.FONT_HERSHEY_SIMPLEX,
                max(0.8, min(width, height) / 900),
                color,
                max(2, round(min(width, height) / 450)),
                cv2.LINE_AA,
            )
            result["person_slots"].append(
                {
                    "slot": index + 1,
                    "position": _person_position_label(index, len(best_detections)),
                    "confidence": round(confidence, 3),
                    "bbox": [
                        round(x1 / width, 4),
                        round(y1 / height, 4),
                        round(x2 / width, 4),
                        round(y2 / height, 4),
                    ],
                }
            )
        if preview_path is not None:
            target = Path(preview_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            suffix = target.suffix.lower() if target.suffix else ".jpg"
            ok, encoded = cv2.imencode(suffix, annotated)
            if ok:
                encoded.tofile(target)
                result["people_map_path"] = str(target)
    return result


def _visual_difference(previous: np.ndarray, current: np.ndarray) -> float:
    previous_gray = cv2.cvtColor(previous, cv2.COLOR_BGR2GRAY)
    current_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    pixel_score = float(np.mean(cv2.absdiff(previous_gray, current_gray))) / 255.0
    previous_hist = cv2.calcHist([previous], [0, 1], None, [24, 24], [0, 256, 0, 256])
    current_hist = cv2.calcHist([current], [0, 1], None, [24, 24], [0, 256, 0, 256])
    cv2.normalize(previous_hist, previous_hist)
    cv2.normalize(current_hist, current_hist)
    histogram_score = float(cv2.compareHist(previous_hist, current_hist, cv2.HISTCMP_BHATTACHARYYA))
    return 0.58 * histogram_score + 0.42 * pixel_score


def _scene_feature_distance(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator <= 1e-9:
        return 1.0
    return max(0.0, min(1.0, 1.0 - float(np.dot(left, right)) / denominator))


def cluster_scene_features(features: list[np.ndarray], *, threshold: float = 0.24) -> list[int]:
    """Group visually similar shot backgrounds; results are UI suggestions only."""
    prototypes: list[np.ndarray] = []
    counts: list[int] = []
    labels: list[int] = []
    for feature in features:
        vector = np.asarray(feature, dtype=np.float32).reshape(-1)
        distances = [_scene_feature_distance(vector, prototype) for prototype in prototypes]
        if distances and min(distances) <= threshold:
            group_index = int(np.argmin(distances))
            count = counts[group_index]
            prototypes[group_index] = (prototypes[group_index] * count + vector) / (count + 1)
            counts[group_index] += 1
        else:
            group_index = len(prototypes)
            prototypes.append(vector.copy())
            counts.append(1)
        labels.append(group_index + 1)
    return labels


def _shot_scene_feature(video_path: Path) -> np.ndarray:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise WorkflowError(f"无法读取分镜场景特征：{video_path.name}")
    frame_count = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1))
    capture.set(cv2.CAP_PROP_POS_FRAMES, max(0, frame_count // 2))
    ok, frame = capture.read()
    capture.release()
    if not ok or frame is None:
        raise WorkflowError(f"无法提取分镜中间帧：{video_path.name}")
    frame = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
    height, width = frame.shape[:2]
    background_mask = np.ones((height, width), dtype=np.uint8) * 255
    # Downweight the center where performers usually occupy the frame so scene
    # grouping follows environment continuity instead of clothing colors.
    cv2.rectangle(
        background_mask,
        (int(width * 0.22), int(height * 0.16)),
        (int(width * 0.78), int(height * 0.92)),
        0,
        thickness=-1,
    )
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    color_hist = cv2.calcHist([hsv], [0, 1], background_mask, [24, 8], [0, 180, 0, 256]).reshape(-1)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray_hist = cv2.calcHist([gray], [0], background_mask, [16], [0, 256]).reshape(-1)
    color_hist /= max(float(color_hist.sum()), 1e-9)
    gray_hist /= max(float(gray_hist.sum()), 1e-9)
    return np.concatenate((color_hist * 0.82, gray_hist * 0.18)).astype(np.float32)


def cluster_video_shot_scenes(video_paths: list[str | Path], *, threshold: float = 0.24) -> list[int]:
    features = [_shot_scene_feature(Path(value).expanduser().resolve()) for value in video_paths]
    return cluster_scene_features(features, threshold=threshold)


def _constrain_boundaries(
    raw_cuts: list[float],
    duration: float,
    *,
    min_shot_seconds: float,
    max_shot_seconds: float,
    max_shots: int,
) -> list[float]:
    """Preserve detected scene cuts and only subdivide overlong scenes.

    ``min_shot_seconds`` is intentionally retained in the signature for callers
    from older saved jobs, but it must never be used to discard a real scene
    boundary.  Seedance's minimum output duration is a generation concern, not
    a scene-detection rule.
    """
    del min_shot_seconds
    cuts: list[float] = []
    last = 0.0
    valid_cuts = sorted(
        float(cut)
        for cut in raw_cuts
        if math.isfinite(float(cut)) and 0.0 < float(cut) < duration
    )
    for cut in valid_cuts:
        # Drop exact/near-exact duplicate timestamps only. Short scenes are valid.
        if cut - last <= 1e-6:
            continue
        while max_shot_seconds > 0 and cut - last > max_shot_seconds + 1e-6:
            last = min(duration, last + max_shot_seconds)
            cuts.append(last)
        if cut - last > 1e-6:
            cuts.append(cut)
            last = cut

    while max_shot_seconds > 0 and duration - last > max_shot_seconds + 1e-6:
        last = min(duration, last + max_shot_seconds)
        cuts.append(last)

    if len(cuts) + 1 > max_shots:
        raise WorkflowError(
            f"检测到的分镜数量超过安全上限 {max_shots} 个。为避免漏掉真实镜头，"
            "程序已停止，而不是静默合并或删除切点。"
        )
    return cuts


def detect_video_shots(
    video_path: str | Path,
    *,
    sensitivity: float = 0.30,
    min_shot_seconds: float = 0.0,
    max_shot_seconds: float = 14.5,
    max_shots: int = 500,
    forced_cuts: list[float] | None = None,
) -> list[ShotBoundary]:
    source = Path(video_path).expanduser().resolve()
    info = inspect_video(source)
    threshold = max(0.08, min(float(sensitivity), 0.9))
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise WorkflowError(f"无法打开长视频：{source}")
    previous: np.ndarray | None = None
    raw_cuts: list[float] = []
    frame_index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            height, width = frame.shape[:2]
            scale = min(1.0, 320.0 / max(width, height))
            if scale < 1.0:
                frame = cv2.resize(
                    frame,
                    (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            if previous is not None and _visual_difference(previous, frame) >= threshold:
                raw_cuts.append(frame_index / info.fps)
            previous = frame
            frame_index += 1
    finally:
        capture.release()

    normalized_forced_cuts = sorted(
        float(cut)
        for cut in (forced_cuts or [])
        if math.isfinite(float(cut)) and 0.0 < float(cut) < info.duration
    )
    if normalized_forced_cuts:
        # A manually supplied timestamp takes precedence over an automatic cut
        # from the same frame neighborhood, avoiding accidental micro-segments.
        frame_tolerance = max(1.0 / max(info.fps, 1.0), 0.01)
        raw_cuts = [
            cut
            for cut in raw_cuts
            if all(abs(cut - forced) > frame_tolerance for forced in normalized_forced_cuts)
        ]
        raw_cuts.extend(normalized_forced_cuts)

    cuts = _constrain_boundaries(
        raw_cuts,
        info.duration,
        min_shot_seconds=min_shot_seconds,
        max_shot_seconds=max_shot_seconds,
        max_shots=max_shots,
    )
    points = [0.0, *cuts, info.duration]
    return [
        ShotBoundary(index=index, start=points[index - 1], end=points[index])
        for index in range(1, len(points))
    ]


def _run_ffmpeg(command: list[str], error_label: str) -> None:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()[-1200:]
        raise WorkflowError(f"{error_label}失败：{detail}")


def _audio_source_signature(source: Path, model_name: str) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    digest.update(f"|{model_name}|{AUDIO_STEM_VERSION}".encode("utf-8"))
    return digest.hexdigest()[:20]


def _create_silent_wav(output: Path, duration: float) -> Path:
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", f"{duration:.6f}", "-c:a", "pcm_s16le", str(output),
    ]
    _run_ffmpeg(command, "创建静音音频轨")
    return output


def separate_dialogue_background(
    source_path: str | Path,
    output_dir: str | Path,
    *,
    model_name: str = AUDIO_STEM_MODEL,
) -> tuple[Path, Path]:
    """Separate one full source video into cached dialogue and background stems."""
    source = Path(source_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到需要分离音轨的原片：{source}")
    target_dir.mkdir(parents=True, exist_ok=True)
    signature = _audio_source_signature(source, model_name)
    manifest_path = target_dir / "audio_stems.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            manifest = {}
        dialogue = target_dir / str(manifest.get("dialogue") or "")
        background = target_dir / str(manifest.get("background") or "")
        if (
            manifest.get("signature") == signature
            and dialogue.is_file()
            and background.is_file()
        ):
            return dialogue.resolve(), background.resolve()

    extracted = target_dir / "source_audio.wav"
    extract_command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100",
        "-c:a", "pcm_s16le", str(extracted),
    ]
    try:
        _run_ffmpeg(extract_command, "提取原片音轨")
    except WorkflowError as exc:
        # A video without any audio has no prosody to preserve. Keep the workflow
        # deterministic by caching two exact-duration silent stems.
        if "matches no streams" not in str(exc) and "does not contain any stream" not in str(exc):
            raise
        duration = inspect_video(source).duration
        dialogue = _create_silent_wav(target_dir / "dialogue_timing.wav", duration)
        background = _create_silent_wav(target_dir / "background.wav", duration)
        save_shot_manifest(
            manifest_path,
            {
                "signature": signature,
                "model": "silence",
                "dialogue": dialogue.name,
                "background": background.name,
            },
        )
        return dialogue.resolve(), background.resolve()

    separation_dir = target_dir / "demucs"
    model_cache = Path(__file__).resolve().parent / "models" / "demucs"
    model_cache.mkdir(parents=True, exist_ok=True)
    separator_script = Path(__file__).resolve().parent / "scripts" / "demucs_wav_separate.py"
    command = [
        sys.executable, str(separator_script),
        "--input", str(extracted),
        "--output", str(separation_dir),
        "--model", model_name,
        "--shifts", "1",
        "--overlap", "0.25",
    ]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    environment = dict(os.environ)
    environment["TORCH_HOME"] = str(model_cache)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=flags,
        env=environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()[-1800:]
        raise WorkflowError(
            "本地人声/BGM 分离失败，付费视频任务尚未提交。"
            f"请检查 Demucs 模型下载、本地音频解码或网络后重试。详情：{detail}"
        )
    candidates = list(separation_dir.glob(f"{model_name}/**/vocals.wav"))
    backgrounds = list(separation_dir.glob(f"{model_name}/**/no_vocals.wav"))
    if len(candidates) != 1 or len(backgrounds) != 1:
        raise WorkflowError("Demucs 已完成，但没有找到唯一的对白轨和背景轨。")
    dialogue = candidates[0].resolve()
    background = backgrounds[0].resolve()
    save_shot_manifest(
        manifest_path,
        {
            "signature": signature,
            "model": model_name,
            "dialogue": dialogue.relative_to(target_dir).as_posix(),
            "background": background.relative_to(target_dir).as_posix(),
        },
    )
    return dialogue, background


def slice_audio_track(
    source_path: str | Path,
    output_path: str | Path,
    *,
    start: float,
    duration: float,
) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    audio_filter = (
        f"atrim=start={max(0.0, start):.6f}:duration={duration:.6f},"
        f"asetpts=PTS-STARTPTS,apad=pad_dur={duration:.6f}"
    )
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-af", audio_filter, "-t", f"{duration:.6f}",
        "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(output),
    ]
    _run_ffmpeg(command, "切分对白韵律轨")
    return output


def mux_prosody_audio(
    video_path: str | Path,
    dialogue_path: str | Path,
    output_path: str | Path,
    *,
    target_duration: float,
) -> Path:
    video = Path(video_path).expanduser().resolve()
    dialogue = Path(dialogue_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    audio_filter = (
        f"[1:a]atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS,"
        f"apad=pad_dur={target_duration:.6f}[prosody]"
    )
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-i", str(dialogue),
        "-filter_complex", audio_filter,
        "-map", "0:v:0", "-map", "[prosody]",
        "-t", f"{target_duration:.6f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "写入对白语速与韵律参考")
    return output


def split_video_shots(
    video_path: str | Path,
    shots: list[ShotBoundary],
    output_dir: str | Path,
) -> list[Path]:
    source = Path(video_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg()
    outputs: list[Path] = []
    for shot in shots:
        shot_dir = target_dir / f"shot_{shot.index:02d}"
        shot_dir.mkdir(parents=True, exist_ok=True)
        output = shot_dir / "source.mp4"
        command = [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{shot.start:.6f}", "-i", str(source), "-t", f"{shot.duration:.6f}",
            "-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(output),
        ]
        _run_ffmpeg(command, f"拆分分镜 {shot.index:02d}")
        outputs.append(output)
    return outputs


def pad_video_with_trailing_black(
    source_path: str | Path,
    output_path: str | Path,
    *,
    minimum_duration: float = 2.0,
) -> Path:
    """Append pure black frames until a short reference reaches the minimum duration.

    This is intended for Seedance reference inputs only.  The generated shot is
    still conformed back to its original duration before the long video is joined.
    """
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到需要补黑的深度视频：{source}")
    if minimum_duration <= 0:
        raise WorkflowError("深度视频最小时长必须大于 0 秒。")
    info = inspect_video(source)
    if info.duration <= 0:
        raise WorkflowError("无法读取短分镜深度视频的时长。")
    if info.duration + 1e-6 >= minimum_duration:
        return source
    if output == source:
        raise WorkflowError("补黑输出不能覆盖原始深度视频。")

    output.parent.mkdir(parents=True, exist_ok=True)
    target_fps = 30
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0",
        "-vf",
        (
            f"setpts=PTS-STARTPTS,fps={target_fps},"
            f"tpad=stop_mode=add:stop_duration={minimum_duration:.6f}:color=black"
        ),
        "-t", f"{minimum_duration:.6f}", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(target_fps), "-fps_mode", "cfr",
        "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "短分镜深度视频片尾补黑")
    if not output.is_file() or output.stat().st_size == 0:
        raise WorkflowError("深度视频补黑已结束，但没有生成有效文件。")
    padded = inspect_video(output)
    if padded.duration + 1e-6 < minimum_duration:
        raise WorkflowError(
            f"深度视频补黑后仍不足 {minimum_duration:.1f} 秒：当前 {padded.duration:.3f} 秒。"
        )
    if not 24 <= padded.fps <= 60.1:
        raise WorkflowError(
            f"短分镜参考视频已补足时长，但恒定帧率校正失败：当前 {padded.fps:.2f} FPS。"
        )
    return output


def extend_video_with_trailing_hold(
    source_path: str | Path,
    output_path: str | Path,
    *,
    target_duration: float,
    with_audio: bool = False,
) -> Path:
    """Preserve source timing, hold its final frame, and optionally pad its audio."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到需要延长的参考视频：{source}")
    if target_duration <= 0:
        raise WorkflowError("参考视频目标时长必须大于 0 秒。")
    info = inspect_video(source)
    if info.duration <= 0:
        raise WorkflowError("无法读取需要延长的参考视频时长。")
    if info.duration > target_duration + 0.02:
        raise WorkflowError(
            f"参考视频目标时长不能短于原视频：原视频 {info.duration:.3f} 秒，目标 {target_duration:.3f} 秒。"
        )
    if output == source:
        raise WorkflowError("参考视频延长输出不能覆盖原文件。")

    output.parent.mkdir(parents=True, exist_ok=True)
    target_fps = 30
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0",
        "-vf",
        (
            f"setpts=PTS-STARTPTS,fps={target_fps},"
            f"tpad=stop_mode=clone:stop_duration={target_duration:.6f}"
        ),
    ]
    if with_audio:
        command += [
            "-map", "0:a:0?",
            "-af", f"apad=pad_dur={target_duration:.6f}",
        ]
    else:
        command += ["-an"]
    command += [
        "-t", f"{target_duration:.6f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(target_fps), "-fps_mode", "cfr",
    ]
    if with_audio:
        command += ["-c:a", "aac", "-b:a", "160k"]
    command += [
        "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "短分镜参考视频片尾定格延长")
    if not output.is_file() or output.stat().st_size == 0:
        raise WorkflowError("参考视频片尾定格处理已结束，但没有生成有效文件。")
    extended = inspect_video(output)
    if abs(extended.duration - target_duration) > max(1.0 / target_fps, 0.04):
        raise WorkflowError(
            f"参考视频片尾定格后的时长不正确：目标 {target_duration:.3f} 秒，当前 {extended.duration:.3f} 秒。"
        )
    if not 24 <= extended.fps <= 60.1:
        raise WorkflowError(
            f"参考视频片尾定格后的帧率不正确：当前 {extended.fps:.2f} FPS。"
        )
    return output


def conform_video_duration(
    source_path: str | Path,
    output_path: str | Path,
    target_duration: float,
    *,
    with_audio: bool = False,
) -> Path:
    """Trim without retiming; hold the last frame/silence only when input is short."""
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    actual = inspect_video(source).duration
    if actual <= 0 or target_duration <= 0:
        raise WorkflowError("无法校正分镜成片时长。")
    target_fps = 30
    command = [str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    video_filter = (
        f"trim=duration={target_duration:.6f},setpts=PTS-STARTPTS,fps={target_fps},"
        f"tpad=stop_mode=clone:stop_duration={target_duration:.6f}"
    )
    if with_audio:
        command += [
            "-filter_complex",
            (
                f"[0:v]{video_filter}[v];"
                f"[0:a]atrim=duration={target_duration:.6f},asetpts=PTS-STARTPTS,"
                f"apad=pad_dur={target_duration:.6f}[a]"
            ),
            "-map", "[v]", "-map", "[a]",
        ]
    else:
        command += ["-vf", video_filter, "-an"]
    command += [
        "-t", f"{target_duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(target_fps), "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "校正分镜成片时长")
    return output


def strip_video_audio(source_path: str | Path, output_path: str | Path) -> Path:
    source = Path(source_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到需要静音的视频：{source.name}")
    if source == output:
        raise WorkflowError("静音输出不能覆盖输入视频。")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-map", "0:v:0", "-c:v", "copy", "-an",
        "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "移除原人物音轨")
    if not output.is_file() or output.stat().st_size == 0:
        raise WorkflowError("原人物音轨已处理，但没有生成有效的静音参考视频。")
    return output


def compose_white_model_scene_control(
    white_model_video: str | Path,
    scene_image: str | Path,
    output_path: str | Path,
) -> Path:
    """Bake the target scene behind a green-screen white-model video.

    This silent control video keeps camera, framing, people silhouettes,
    occlusion, motion and the target environment in one temporal reference.
    It is intentionally used only by the real-person long-video workflow.
    """
    white_model = Path(white_model_video).expanduser().resolve()
    scene = Path(scene_image).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not white_model.is_file():
        raise WorkflowError(f"找不到白模控制视频：{white_model.name}")
    if not scene.is_file():
        raise WorkflowError(f"缺少真人分镜场景板：{scene.name}")
    if output in {white_model, scene}:
        raise WorkflowError("真人分镜控制视频不能覆盖输入素材。")
    info = inspect_video(white_model)
    output.parent.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"[1:v]scale={info.width}:{info.height}:force_original_aspect_ratio=increase,"
        f"crop={info.width}:{info.height}[bg];"
        "[0:v]format=rgba,colorkey=0x00B140:0.24:0.08[fg];"
        "[bg][fg]overlay=0:0:shortest=1:format=auto,format=yuv420p[v]"
    )
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(white_model), "-loop", "1", "-i", str(scene),
        "-filter_complex", filter_graph, "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16",
        "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "合成真人分镜时空控制视频")
    if not output.is_file() or output.stat().st_size == 0:
        raise WorkflowError("真人分镜时空控制视频合成失败。")
    return output


def concatenate_videos(video_paths: list[str | Path], output_path: str | Path) -> Path:
    if not video_paths:
        raise WorkflowError("没有可合并的分镜成片。")
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    concat_file = output.parent / "concat_inputs.txt"
    lines = []
    for value in video_paths:
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise WorkflowError(f"找不到待合并分镜：{path.name}")
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat",
        "-safe", "0", "-i", str(concat_file), "-c", "copy", "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "合并长视频")
    return output


def mux_original_audio(
    video_path: str | Path,
    original_path: str | Path,
    output_path: str | Path,
) -> Path:
    video = Path(video_path).expanduser().resolve()
    original = Path(original_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(resolve_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-i", str(original), "-map", "0:v:0", "-map", "1:a?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(output),
    ]
    _run_ffmpeg(command, "恢复原片音轨")
    return output


def save_shot_manifest(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))
