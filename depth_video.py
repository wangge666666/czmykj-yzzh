from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort


MODEL_SIZE = 518
DEFAULT_MAX_DURATION_SECONDS = 14.5
MEAN = np.asarray([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.asarray([0.229, 0.224, 0.225], dtype=np.float32)


class PersonSegmenter:
    INPUT_SIZE = 640
    REG_BINS = 16

    def __init__(self, model_path: Path) -> None:
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = False
        available = ort.get_available_providers()
        providers = [
            p
            for p in ("DmlExecutionProvider", "CPUExecutionProvider")
            if p in available
        ]
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=providers
        )
        self.input_name = self.session.get_inputs()[0].name
        self.last_score = 0.0
        self.last_candidates: list[tuple[float, float, float, float, float]] = []

    @staticmethod
    def _sigmoid(value: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(value, -30.0, 30.0)))

    @staticmethod
    def _nms(boxes: np.ndarray, scores: np.ndarray, threshold: float = 0.5) -> list[int]:
        order = scores.argsort()[::-1]
        keep: list[int] = []
        while order.size:
            current = int(order[0])
            keep.append(current)
            if order.size == 1:
                break
            others = order[1:]
            xx1 = np.maximum(boxes[current, 0], boxes[others, 0])
            yy1 = np.maximum(boxes[current, 1], boxes[others, 1])
            xx2 = np.minimum(boxes[current, 2], boxes[others, 2])
            yy2 = np.minimum(boxes[current, 3], boxes[others, 3])
            intersection = np.maximum(xx2 - xx1, 0.0) * np.maximum(yy2 - yy1, 0.0)
            area_current = max(
                (boxes[current, 2] - boxes[current, 0])
                * (boxes[current, 3] - boxes[current, 1]),
                1e-6,
            )
            area_others = np.maximum(
                (boxes[others, 2] - boxes[others, 0])
                * (boxes[others, 3] - boxes[others, 1]),
                1e-6,
            )
            overlap = intersection / (area_current + area_others - intersection + 1e-6)
            order = others[overlap <= threshold]
        return keep

    def segment(self, frame: np.ndarray) -> np.ndarray | None:
        height, width = frame.shape[:2]
        scale = min(self.INPUT_SIZE / width, self.INPUT_SIZE / height)
        resized_width = max(int(round(width * scale)), 1)
        resized_height = max(int(round(height * scale)), 1)
        pad_x = (self.INPUT_SIZE - resized_width) // 2
        pad_y = (self.INPUT_SIZE - resized_height) // 2
        resized = cv2.resize(
            frame, (resized_width, resized_height), interpolation=cv2.INTER_AREA
        )
        canvas = np.full(
            (self.INPUT_SIZE, self.INPUT_SIZE, 3), 114, dtype=np.uint8
        )
        canvas[pad_y : pad_y + resized_height, pad_x : pad_x + resized_width] = resized
        tensor = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        tensor = np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])
        outputs = self.session.run(None, {self.input_name: tensor})

        proto = None
        regressions: dict[int, np.ndarray] = {}
        classes: dict[int, np.ndarray] = {}
        coefficients: dict[int, np.ndarray] = {}
        for output in outputs:
            if output.ndim != 4:
                continue
            if output.shape[1] in (32, 144) and output.shape[2] == output.shape[3]:
                channels, grid_height, grid_width = output.shape[1:]
                if channels == 32 and grid_height == 160:
                    proto = output[0]
                elif channels == 144:
                    regressions[grid_height] = output[0, :64]
                    classes[grid_height] = output[0, 64:]
                elif channels == 32:
                    coefficients[grid_height] = output[0]
            elif output.shape[1] == output.shape[2] and output.shape[3] in (32, 64, 80):
                grid_height = output.shape[1]
                channels = output.shape[3]
                channel_first = output[0].transpose(2, 0, 1)
                if channels == 64:
                    regressions[grid_height] = channel_first
                elif channels == 80:
                    classes[grid_height] = channel_first
                elif channels == 32:
                    coefficients[grid_height] = channel_first
        if proto is None:
            return None

        box_parts: list[np.ndarray] = []
        score_parts: list[np.ndarray] = []
        coefficient_parts: list[np.ndarray] = []
        bins = np.arange(self.REG_BINS, dtype=np.float32)
        for grid_size in (80, 40, 20):
            if (
                grid_size not in regressions
                or grid_size not in classes
                or grid_size not in coefficients
            ):
                continue
            raw_regression = regressions[grid_size]
            raw_classes = classes[grid_size]
            stride = self.INPUT_SIZE / grid_size
            distribution = raw_regression.reshape(
                4, self.REG_BINS, grid_size, grid_size
            )
            distribution = distribution.transpose(2, 3, 0, 1).reshape(-1, 4, self.REG_BINS)
            distribution -= distribution.max(axis=2, keepdims=True)
            probabilities = np.exp(distribution)
            probabilities /= probabilities.sum(axis=2, keepdims=True)
            distances = (probabilities * bins).sum(axis=2) * stride
            grid_y, grid_x = np.meshgrid(
                np.arange(grid_size, dtype=np.float32),
                np.arange(grid_size, dtype=np.float32),
                indexing="ij",
            )
            center_x = (grid_x.reshape(-1) + 0.5) * stride
            center_y = (grid_y.reshape(-1) + 0.5) * stride
            boxes = np.stack(
                [
                    center_x - distances[:, 0],
                    center_y - distances[:, 1],
                    center_x + distances[:, 2],
                    center_y + distances[:, 3],
                ],
                axis=1,
            )
            person_scores = self._sigmoid(raw_classes[0].reshape(-1))
            mask_coefficients = coefficients[grid_size].transpose(1, 2, 0).reshape(-1, 32)
            selected = person_scores >= 0.01
            box_parts.append(boxes[selected])
            score_parts.append(person_scores[selected])
            coefficient_parts.append(mask_coefficients[selected])

        if not box_parts or sum(part.shape[0] for part in box_parts) == 0:
            self.last_score = 0.0
            return None
        boxes = np.concatenate(box_parts, axis=0)
        scores = np.concatenate(score_parts, axis=0)
        coeffs = np.concatenate(coefficient_parts, axis=0)
        boxes = np.clip(boxes, 0.0, float(self.INPUT_SIZE))
        keep = self._nms(boxes, scores)

        best_index = None
        best_rank = -1.0
        self.last_candidates = []
        for index in keep:
            x1, y1, x2, y2 = boxes[index]
            content_x1 = np.clip(x1 - pad_x, 0.0, float(resized_width))
            content_y1 = np.clip(y1 - pad_y, 0.0, float(resized_height))
            content_x2 = np.clip(x2 - pad_x, 0.0, float(resized_width))
            content_y2 = np.clip(y2 - pad_y, 0.0, float(resized_height))
            box_width = max(content_x2 - content_x1, 0.0)
            box_height = max(content_y2 - content_y1, 0.0)
            area_ratio = box_width * box_height / max(
                resized_width * resized_height, 1
            )
            height_ratio = box_height / max(resized_height, 1)
            center_ratio = ((content_x1 + content_x2) * 0.5) / max(
                resized_width, 1
            )
            vertical_center = float(
                (content_y1 + content_y2) * 0.5 / max(resized_height, 1)
            )
            self.last_candidates.append(
                (
                    float(scores[index]),
                    float(area_ratio),
                    float(height_ratio),
                    float(center_ratio),
                    vertical_center,
                )
            )
            if (
                area_ratio < 0.008
                or area_ratio > 0.72
                or height_ratio < 0.18
                or center_ratio < 0.22
                or center_ratio > 0.78
                or vertical_center < 0.18
                or vertical_center > 0.76
            ):
                continue
            center_preference = np.exp(-((center_ratio - 0.5) / 0.42) ** 2)
            vertical_preference = np.exp(-((vertical_center - 0.50) / 0.34) ** 2)
            size_preference = np.exp(-((area_ratio - 0.11) / 0.15) ** 2)
            rank = (
                0.50 * center_preference
                + 0.20 * vertical_preference
                + 0.15 * size_preference
                + 0.15 * min(float(scores[index]) / 0.20, 1.0)
            )
            if rank > best_rank:
                best_rank = rank
                best_index = index
        if best_index is None:
            self.last_score = 0.0
            return None

        self.last_score = float(scores[best_index])
        raw_mask = coeffs[best_index] @ proto.reshape(32, -1)
        mask = self._sigmoid(raw_mask).reshape(160, 160)
        x1, y1, x2, y2 = boxes[best_index] / 4.0
        crop = np.zeros_like(mask)
        crop[
            max(int(np.floor(y1)), 0) : min(int(np.ceil(y2)), 160),
            max(int(np.floor(x1)), 0) : min(int(np.ceil(x2)), 160),
        ] = 1.0
        mask *= crop
        mask = cv2.resize(
            mask, (self.INPUT_SIZE, self.INPUT_SIZE), interpolation=cv2.INTER_LINEAR
        )
        mask = mask[
            pad_y : pad_y + resized_height,
            pad_x : pad_x + resized_width,
        ]
        mask = cv2.resize(mask, (MODEL_SIZE, MODEL_SIZE), interpolation=cv2.INTER_LINEAR)
        probable = mask >= 0.30
        subject_mask = probable.astype(np.float32)
        subject_mask = cv2.morphologyEx(
            subject_mask, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8)
        )
        subject_mask = cv2.morphologyEx(
            subject_mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8)
        )
        return cv2.GaussianBlur(subject_mask, (0, 0), 0.42)


class YoloPersonPreserver:
    """Preserve every detected actor as a separate, softly matted depth layer."""

    def __init__(self, model_path: Path) -> None:
        if not model_path.is_file():
            raise FileNotFoundError(f"Person detector does not exist: {model_path}")
        self.last_score = 0.0
        self.last_candidates: list[tuple[float, float, float, float, float]] = []
        self.last_layers: list[tuple[np.ndarray, float]] = []
        self.last_people = 0

    @staticmethod
    def _grabcut_person(
        small_frame: np.ndarray,
        rect: tuple[int, int, int, int],
        score: float,
    ) -> np.ndarray | None:
        x, y, width, height = rect
        grab_mask = np.zeros(small_frame.shape[:2], dtype=np.uint8)
        background_model = np.zeros((1, 65), dtype=np.float64)
        foreground_model = np.zeros((1, 65), dtype=np.float64)
        try:
            cv2.grabCut(
                small_frame,
                grab_mask,
                rect,
                background_model,
                foreground_model,
                1,
                cv2.GC_INIT_WITH_RECT,
            )
            mask = np.isin(
                grab_mask, (cv2.GC_FGD, cv2.GC_PR_FGD)
            ).astype(np.uint8)
        except cv2.error:
            mask = np.zeros(small_frame.shape[:2], dtype=np.uint8)
        roi = mask[y : y + height, x : x + width]
        coverage = float(roi.mean()) if roi.size else 0.0
        if coverage < 0.05 or coverage > 0.94:
            # Avoid turning a weak false detection into a bright geometric blob.
            if score < 0.48:
                return None
            mask.fill(0)
            center = (x + width // 2, y + height // 2)
            axes = (max(1, int(width * 0.38)), max(1, int(height * 0.48)))
            cv2.ellipse(mask, center, axes, 0, 0, 360, 1, thickness=-1)
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8)
        )
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype=np.uint8)
        )
        return cv2.GaussianBlur(mask.astype(np.float32), (0, 0), 0.65)

    def segment(self, frame: np.ndarray) -> np.ndarray | None:
        # Import lazily to keep the standalone depth tool's startup lightweight.
        # The shared detector loads this exact bundled model through OpenCV DNN;
        # ONNX Runtime 1.27 cannot initialize its INT8 block-quantized nodes.
        from long_video_core import _detect_people_yolox

        height, width = frame.shape[:2]
        small = cv2.resize(
            frame, (MODEL_SIZE, MODEL_SIZE), interpolation=cv2.INTER_AREA
        )
        detections = _detect_people_yolox(
            frame, confidence_threshold=0.30, max_people=8
        )
        self.last_candidates = []
        self.last_layers = []
        self.last_score = max((score for _, score in detections), default=0.0)
        for box, score in detections:
            x1, y1, x2, y2 = box
            box_width = x2 - x1
            box_height = y2 - y1
            area_ratio = box_width * box_height / max(width * height, 1)
            height_ratio = box_height / max(height, 1)
            center_ratio = ((x1 + x2) * 0.5) / max(width, 1)
            vertical_center = ((y1 + y2) * 0.5) / max(height, 1)
            self.last_candidates.append(
                (score, area_ratio, height_ratio, center_ratio, vertical_center)
            )
            if area_ratio < 0.006 or height_ratio < 0.16:
                continue
            sx1 = max(0, min(MODEL_SIZE - 2, int(x1 * MODEL_SIZE / width)))
            sy1 = max(0, min(MODEL_SIZE - 2, int(y1 * MODEL_SIZE / height)))
            sx2 = max(
                sx1 + 2,
                min(MODEL_SIZE, int(np.ceil(x2 * MODEL_SIZE / width))),
            )
            sy2 = max(
                sy1 + 2,
                min(MODEL_SIZE, int(np.ceil(y2 * MODEL_SIZE / height))),
            )
            mask = self._grabcut_person(
                small, (sx1, sy1, sx2 - sx1, sy2 - sy1), score
            )
            if mask is None or int(np.count_nonzero(mask >= 0.35)) < 180:
                continue
            # Large/tall actors are near; small actors remain a distinct mid-gray
            # layer instead of being crushed into the far-background black level.
            target = float(
                np.clip(0.48 + 0.50 * np.sqrt(height_ratio), 0.60, 0.96)
            )
            self.last_layers.append((mask, target))
        self.last_people = len(self.last_layers)
        if not self.last_layers:
            return None
        floor = np.zeros((MODEL_SIZE, MODEL_SIZE), dtype=np.float32)
        for mask, target in self.last_layers:
            floor = np.maximum(floor, mask * target)
        return floor

    def enhance_depth(self, depth: np.ndarray) -> np.ndarray:
        enhanced = depth.astype(np.float32).copy()
        # Apply far actors first and near actors last so overlap ordering survives.
        for mask, target in sorted(self.last_layers, key=lambda layer: layer[1]):
            core = mask >= 0.55
            if int(core.sum()) < 120:
                continue
            values = enhanced[core]
            low, high = np.percentile(values, (8.0, 92.0))
            if float(high - low) < 1e-4:
                local = np.full_like(enhanced, 0.5)
            else:
                local = np.clip((enhanced - low) / (high - low), 0.0, 1.0)
            desired = np.clip(target - 0.10 + 0.14 * local, 0.0, 0.985)
            lifted = np.maximum(enhanced, desired)
            alpha = 0.88 * np.clip((mask - 0.04) / 0.72, 0.0, 1.0)
            enhanced = (1.0 - alpha) * enhanced + alpha * lifted
        return enhanced


class DepthRenderer:
    def __init__(
        self,
        model_path: Path,
        segment_model_path: Path | None = None,
        faithful_depth: bool = False,
        blur_range: tuple[int, int] | None = None,
    ) -> None:
        options = ort.SessionOptions()
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # ORT 1.27's extended transformer fusion can corrupt this exported FP16
        # Depth Anything graph during initialization. Basic optimizations are safe.
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        options.enable_mem_pattern = False
        available = ort.get_available_providers()
        providers = [
            p
            for p in ("DmlExecutionProvider", "CPUExecutionProvider")
            if p in available
        ]
        self.session = ort.InferenceSession(
            str(model_path), sess_options=options, providers=providers
        )
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        self.low: float | None = None
        self.high: float | None = None
        self.prev_gray: np.ndarray | None = None
        self.prev_depth: np.ndarray | None = None
        self.prev_subject: np.ndarray | None = None
        self.faithful_depth = faithful_depth
        self.blur_range = blur_range
        self.segmenter = (
            YoloPersonPreserver(segment_model_path)
            if segment_model_path is not None
            else None
        )
        print("providers=" + ",".join(self.session.get_providers()), flush=True)

    @staticmethod
    def _guided_filter(
        guidance: np.ndarray,
        depth: np.ndarray,
        radius: int = 6,
        epsilon: float = 0.009,
    ) -> np.ndarray:
        size = (radius * 2 + 1, radius * 2 + 1)
        mean_i = cv2.boxFilter(guidance, -1, size, borderType=cv2.BORDER_REFLECT)
        mean_p = cv2.boxFilter(depth, -1, size, borderType=cv2.BORDER_REFLECT)
        corr_i = cv2.boxFilter(guidance * guidance, -1, size, borderType=cv2.BORDER_REFLECT)
        corr_ip = cv2.boxFilter(guidance * depth, -1, size, borderType=cv2.BORDER_REFLECT)
        variance_i = corr_i - mean_i * mean_i
        covariance_ip = corr_ip - mean_i * mean_p
        a = covariance_ip / (variance_i + epsilon)
        b = mean_p - a * mean_i
        mean_a = cv2.boxFilter(a, -1, size, borderType=cv2.BORDER_REFLECT)
        mean_b = cv2.boxFilter(b, -1, size, borderType=cv2.BORDER_REFLECT)
        return mean_a * guidance + mean_b

    @staticmethod
    def _independent_motion(
        current_gray: np.ndarray,
        previous_gray: np.ndarray,
        backward_flow: np.ndarray,
    ) -> np.ndarray:
        points = cv2.goodFeaturesToTrack(
            current_gray,
            maxCorners=350,
            qualityLevel=0.012,
            minDistance=7,
            blockSize=7,
        )
        affine = None
        if points is not None and len(points) >= 12:
            previous_points, status, _ = cv2.calcOpticalFlowPyrLK(
                current_gray,
                previous_gray,
                points,
                None,
                winSize=(21, 21),
                maxLevel=3,
            )
            if previous_points is not None and status is not None:
                valid = status.reshape(-1).astype(bool)
                if int(valid.sum()) >= 10:
                    affine, _ = cv2.estimateAffinePartial2D(
                        points.reshape(-1, 2)[valid],
                        previous_points.reshape(-1, 2)[valid],
                        method=cv2.RANSAC,
                        ransacReprojThreshold=2.5,
                        maxIters=1500,
                        confidence=0.98,
                    )
        grid_x, grid_y = np.meshgrid(
            np.arange(MODEL_SIZE, dtype=np.float32),
            np.arange(MODEL_SIZE, dtype=np.float32),
        )
        if affine is not None:
            expected_x = (
                affine[0, 0] * grid_x + affine[0, 1] * grid_y + affine[0, 2]
            )
            expected_y = (
                affine[1, 0] * grid_x + affine[1, 1] * grid_y + affine[1, 2]
            )
            expected_flow = np.stack(
                (expected_x - grid_x, expected_y - grid_y), axis=2
            )
        else:
            median_flow = np.median(backward_flow.reshape(-1, 2), axis=0)
            expected_flow = np.empty_like(backward_flow)
            expected_flow[..., 0] = median_flow[0]
            expected_flow[..., 1] = median_flow[1]
        residual = backward_flow - expected_flow
        magnitude = np.sqrt(np.sum(residual * residual, axis=2))
        motion = np.clip((magnitude - 0.32) / 2.1, 0.0, 1.0).astype(np.float32)
        motion = cv2.dilate(
            motion, np.ones((9, 9), dtype=np.uint8), iterations=1
        )
        return cv2.GaussianBlur(motion, (0, 0), 2.0)

    @staticmethod
    def _refine_subject_with_motion(
        frame: np.ndarray,
        coarse_subject: np.ndarray,
        independent_motion: np.ndarray,
    ) -> np.ndarray:
        probable = coarse_subject >= 0.18
        if int(probable.sum()) < 250:
            return coarse_subject
        allowed = cv2.dilate(
            probable.astype(np.uint8),
            np.ones((13, 13), dtype=np.uint8),
            iterations=1,
        ).astype(bool)
        sure_foreground = probable & (independent_motion >= 0.42)
        probable_foreground = probable & (independent_motion >= 0.10)
        if int(sure_foreground.sum()) < 60:
            subject = coarse_subject * np.clip(
                0.18 + 1.25 * independent_motion, 0.0, 1.0
            )
        else:
            grab_mask = np.full(
                (MODEL_SIZE, MODEL_SIZE), cv2.GC_BGD, dtype=np.uint8
            )
            grab_mask[allowed] = cv2.GC_PR_BGD
            grab_mask[probable_foreground] = cv2.GC_PR_FGD
            grab_mask[sure_foreground] = cv2.GC_FGD
            small_frame = cv2.resize(
                frame, (MODEL_SIZE, MODEL_SIZE), interpolation=cv2.INTER_AREA
            )
            background_model = np.zeros((1, 65), dtype=np.float64)
            foreground_model = np.zeros((1, 65), dtype=np.float64)
            try:
                cv2.grabCut(
                    small_frame,
                    grab_mask,
                    None,
                    background_model,
                    foreground_model,
                    2,
                    cv2.GC_INIT_WITH_MASK,
                )
                subject = np.isin(
                    grab_mask, (cv2.GC_FGD, cv2.GC_PR_FGD)
                ).astype(np.float32)
                subject *= allowed.astype(np.float32)
            except cv2.error:
                subject = coarse_subject * np.clip(
                    0.18 + 1.25 * independent_motion, 0.0, 1.0
                )
        subject = cv2.morphologyEx(
            subject, cv2.MORPH_OPEN, np.ones((3, 3), dtype=np.uint8)
        )
        subject = cv2.morphologyEx(
            subject, cv2.MORPH_CLOSE, np.ones((15, 15), dtype=np.uint8)
        )
        return DepthRenderer._solidify_subject(subject, threshold=0.36)

    @staticmethod
    def _solidify_subject(subject: np.ndarray, threshold: float = 0.28) -> np.ndarray:
        binary = (subject >= threshold).astype(np.uint8)
        binary = cv2.morphologyEx(
            binary, cv2.MORPH_CLOSE, np.ones((15, 15), dtype=np.uint8)
        )
        component_count, labels, statistics, _ = cv2.connectedComponentsWithStats(
            binary, connectivity=8
        )
        if component_count > 1:
            areas = statistics[1:, cv2.CC_STAT_AREA]
            largest_label = int(np.argmax(areas)) + 1
            binary = (labels == largest_label).astype(np.uint8)
        # Fill only small enclosed defects. Large negative spaces between limbs
        # carry the pose and must remain visible in the depth silhouette.
        filled = binary.copy()
        inverse = (1 - binary).astype(np.uint8)
        hole_count, hole_labels, hole_stats, _ = cv2.connectedComponentsWithStats(
            inverse, connectivity=8
        )
        mask_height, mask_width = binary.shape
        for hole_label in range(1, hole_count):
            x = int(hole_stats[hole_label, cv2.CC_STAT_LEFT])
            y = int(hole_stats[hole_label, cv2.CC_STAT_TOP])
            hole_width = int(hole_stats[hole_label, cv2.CC_STAT_WIDTH])
            hole_height = int(hole_stats[hole_label, cv2.CC_STAT_HEIGHT])
            hole_area = int(hole_stats[hole_label, cv2.CC_STAT_AREA])
            touches_border = (
                x == 0
                or y == 0
                or x + hole_width >= mask_width
                or y + hole_height >= mask_height
            )
            if not touches_border and hole_area <= 1800:
                filled[hole_labels == hole_label] = 1
        filled = cv2.morphologyEx(
            filled, cv2.MORPH_CLOSE, np.ones((7, 7), dtype=np.uint8)
        )
        return cv2.GaussianBlur(filled.astype(np.float32), (0, 0), 0.40)

    @staticmethod
    def _input_tensor(frame: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        rgb = cv2.resize(
            rgb, (MODEL_SIZE, MODEL_SIZE), interpolation=cv2.INTER_AREA
        ).astype(np.float32) / 255.0
        tensor = ((rgb - MEAN) / STD).transpose(2, 0, 1)[None]
        gray = cv2.cvtColor((rgb * 255.0).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        return np.ascontiguousarray(tensor, dtype=np.float32), gray

    def render(
        self,
        frame: np.ndarray,
        use_temporal: bool = True,
        frame_index: int | None = None,
    ) -> np.ndarray:
        # Refresh the expensive detector/matte on alternating frames. Optical
        # flow below carries every actor mask through the intervening frame.
        # Frame zero and preview calls are always analyzed directly.
        refresh_subject = bool(
            self.segmenter is not None
            and (
                frame_index is None
                or frame_index % 2 == 0
                or self.prev_subject is None
            )
        )
        subject = self.segmenter.segment(frame) if refresh_subject else None
        subject_refreshed = subject is not None
        tensor, gray = self._input_tensor(frame)
        raw = self.session.run([self.output_name], {self.input_name: tensor})[0]
        depth = np.asarray(raw, dtype=np.float32).squeeze()

        # Robust per-frame range, slowly adapted to avoid exposure-like flicker.
        percentile_range = (0.5, 99.5) if self.faithful_depth else (1.5, 98.5)
        low_now, high_now = np.percentile(depth, percentile_range)
        scene_cut = False
        if self.prev_gray is not None:
            scene_cut = float(
                np.mean(np.abs(gray.astype(np.float32) - self.prev_gray.astype(np.float32)))
            ) > 48.0
        if self.low is None or self.high is None or scene_cut:
            self.low, self.high = float(low_now), float(high_now)
        else:
            self.low = 0.92 * self.low + 0.08 * float(low_now)
            self.high = 0.92 * self.high + 0.08 * float(high_now)

        normalized = (depth - self.low) / max(self.high - self.low, 1e-6)
        if self.faithful_depth:
            # Preserve continuous metric-like separation and curved-surface
            # gradients. Only the extreme tails reach pure black or white.
            normalized = np.clip((normalized - 0.01) / 0.98, 0.0, 1.0)
            normalized = np.power(normalized, 0.96).astype(np.float32)
        else:
            normalized = np.clip((normalized - 0.015) / 0.84, 0.0, 1.0)
            # Separate close and far surfaces more strongly while retaining gradients.
            sigmoid = 1.0 / (1.0 + np.exp(-9.0 * (normalized - 0.33)))
            sigmoid_min = 1.0 / (1.0 + np.exp(9.0 * 0.33))
            sigmoid_max = 1.0 / (1.0 + np.exp(-9.0 * (1.0 - 0.33)))
            sigmoid = (sigmoid - sigmoid_min) / (sigmoid_max - sigmoid_min)
            normalized = 0.26 * normalized + 0.74 * sigmoid
        # Smooth flat surfaces without softening depth discontinuities.
        normalized = cv2.bilateralFilter(
            normalized.astype(np.float32), 7, 0.075, 5.0
        )

        # Motion-compensated temporal blend reduces flicker without freezing motion.
        if use_temporal and self.prev_gray is not None and self.prev_depth is not None and not scene_cut:
            backward = cv2.calcOpticalFlowFarneback(
                gray,
                self.prev_gray,
                None,
                0.5,
                3,
                19,
                3,
                5,
                1.1,
                0,
            )
            grid_x, grid_y = np.meshgrid(
                np.arange(MODEL_SIZE, dtype=np.float32),
                np.arange(MODEL_SIZE, dtype=np.float32),
            )
            warped_previous = cv2.remap(
                self.prev_depth,
                grid_x + backward[..., 0],
                grid_y + backward[..., 1],
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            temporal_weight = 0.03
            blur_compensation_enabled = (
                self.faithful_depth
                and self.blur_range is not None
                and frame_index is not None
                and self.blur_range[0] <= frame_index <= self.blur_range[1]
            )
            if blur_compensation_enabled:
                current_sharpness = float(
                    cv2.Laplacian(gray, cv2.CV_32F).var()
                )
                blur_weight = float(
                    np.clip((55.0 - current_sharpness) / 55.0, 0.0, 0.88)
                )
                temporal_weight = max(temporal_weight, blur_weight)
            normalized = (
                (1.0 - temporal_weight) * normalized
                + temporal_weight * warped_previous
            )

            if self.prev_subject is not None:
                warped_subject = cv2.remap(
                    self.prev_subject,
                    grid_x + backward[..., 0],
                    grid_y + backward[..., 1],
                    cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                )
                if subject is None:
                    subject = 0.96 * warped_subject
                else:
                    subject = np.maximum(subject, 0.38 * warped_subject)
        else:
            if scene_cut:
                self.prev_subject = None

        if subject is not None:
            subject = np.clip(subject, 0.0, 1.0).astype(np.float32)
            if (
                subject_refreshed
                and self.segmenter is not None
                and self.segmenter.last_layers
            ):
                normalized = self.segmenter.enhance_depth(normalized)
                # Carry a weak floor through an occasional one-frame detection
                # miss without flattening an actor to a single gray value.
                normalized = np.maximum(normalized, 0.72 * subject)
            elif self.segmenter is not None:
                normalized = np.maximum(normalized, 0.90 * subject)
            else:
                normalized = np.maximum(normalized, 0.82 * subject)
            self.prev_subject = subject
        else:
            self.prev_subject = None

        self.prev_gray = gray
        self.prev_depth = normalized.astype(np.float32)

        height, width = frame.shape[:2]
        refine_width = max(width // 2, 1)
        refine_height = max(height // 2, 1)
        guidance = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        guidance = cv2.resize(
            guidance, (refine_width, refine_height), interpolation=cv2.INTER_AREA
        ).astype(np.float32) / 255.0
        depth_half = cv2.resize(
            normalized, (refine_width, refine_height), interpolation=cv2.INTER_CUBIC
        ).astype(np.float32)
        if self.faithful_depth:
            refined = self._guided_filter(
                guidance, depth_half, radius=4, epsilon=0.014
            )
            soft = cv2.GaussianBlur(refined, (0, 0), 0.92)
            refined = np.clip(refined + 0.38 * (refined - soft), 0.0, 1.0)
        else:
            refined = self._guided_filter(guidance, depth_half)
            soft = cv2.GaussianBlur(refined, (0, 0), 1.15)
            refined = np.clip(refined + 0.82 * (refined - soft), 0.0, 1.0)
        if subject is not None:
            subject_half = cv2.resize(
                subject, (refine_width, refine_height), interpolation=cv2.INTER_LINEAR
            ).astype(np.float32)
            subject_half = cv2.GaussianBlur(subject_half, (0, 0), 0.46)
            refined = np.maximum(refined, 0.72 * subject_half)
        output_interpolation = (
            cv2.INTER_CUBIC if self.faithful_depth else cv2.INTER_LANCZOS4
        )
        output = cv2.resize(
            refined, (width, height), interpolation=output_interpolation
        )
        return np.clip(output * 255.0, 0, 255).astype(np.uint8)


def open_video(path: Path) -> cv2.VideoCapture:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频：{path}")
    return capture


def limited_frame_count(total_frames: int, fps: float, max_duration_seconds: float) -> int:
    if total_frames <= 0:
        raise ValueError("视频总帧数必须大于 0")
    if fps <= 0:
        raise ValueError("视频帧率必须大于 0")
    if max_duration_seconds <= 0:
        raise ValueError("最大输出时长必须大于 0")
    duration_frames = max(1, int(max_duration_seconds * fps + 1e-9))
    return min(total_frames, duration_frames)


def write_png(path: Path, image: np.ndarray) -> None:
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError(f"无法编码预览图：{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data.tofile(str(path))


def render_preview(
    renderer: DepthRenderer, input_path: Path, output_path: Path, frame_index: int
) -> None:
    capture = open_video(input_path)
    warmup_start = max(frame_index - 20, 0)
    capture.set(cv2.CAP_PROP_POS_FRAMES, warmup_start)
    ok = False
    frame = None
    depth = None
    for current in range(warmup_start, frame_index + 1):
        ok, frame = capture.read()
        if not ok:
            break
        depth = renderer.render(
            frame,
            use_temporal=current > warmup_start,
            frame_index=current,
        )
    capture.release()
    if not ok or frame is None or depth is None:
        raise RuntimeError(f"无法读取第 {frame_index} 帧")
    write_png(output_path, depth)
    if renderer.segmenter is not None:
        print(f"subject_score={renderer.segmenter.last_score:.4f}", flush=True)
        print(f"subject_candidates={renderer.segmenter.last_candidates}", flush=True)
    print(f"preview={output_path}", flush=True)


def render_video(
    renderer: DepthRenderer,
    input_path: Path,
    output_path: Path,
    ffmpeg_path: Path,
    max_duration_seconds: float = DEFAULT_MAX_DURATION_SECONDS,
) -> None:
    capture = open_video(input_path)
    fps = float(capture.get(cv2.CAP_PROP_FPS)) or 30.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    render_total = limited_frame_count(total, fps, max_duration_seconds)
    render_duration = render_total / fps
    if output_path.exists():
        raise FileExistsError(f"输出文件已存在：{output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-s:v",
        f"{width}x{height}",
        "-r",
        f"{fps:.8f}",
        "-i",
        "pipe:0",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "17",
        "-maxrate",
        "45M",
        "-bufsize",
        "9M",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-t",
        f"{render_duration:.9f}",
        str(output_path),
    ]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE, creationflags=flags)
    if encoder.stdin is None:
        raise RuntimeError("无法连接视频编码器")

    started = time.perf_counter()
    frame_index = 0
    if render_total < total:
        print(
            "depth_duration_limit="
            f"{max_duration_seconds:.3f}s source_frames={total} output_frames={render_total}",
            flush=True,
        )
    try:
        while frame_index < render_total:
            ok, frame = capture.read()
            if not ok:
                break
            depth = renderer.render(
                frame,
                use_temporal=True,
                frame_index=frame_index,
            )
            encoder.stdin.write(depth.tobytes())
            frame_index += 1
            if frame_index == 1 or frame_index % 15 == 0 or frame_index == render_total:
                elapsed = max(time.perf_counter() - started, 1e-6)
                rate = frame_index / elapsed
                print(
                    f"depth_progress={frame_index}/{render_total} fps={rate:.2f}",
                    flush=True,
                )
    finally:
        capture.release()
        encoder.stdin.close()

    return_code = encoder.wait()
    if return_code != 0:
        raise RuntimeError(f"视频编码失败，退出码：{return_code}")
    print(f"output={output_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="将普通视频转换为近白远黑的深度图视频")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--segment-model", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--preview-frame", type=int)
    parser.add_argument("--faithful-depth", action="store_true")
    parser.add_argument(
        "--max-duration",
        type=float,
        default=DEFAULT_MAX_DURATION_SECONDS,
        help="Maximum output duration in seconds; defaults to 14.5",
    )
    parser.add_argument(
        "--blur-range",
        help="Inclusive zero-based frame range for targeted blur compensation, e.g. 291:305",
    )
    args = parser.parse_args()

    blur_range = None
    if args.blur_range:
        try:
            start_text, end_text = args.blur_range.split(":", 1)
            blur_range = (int(start_text), int(end_text))
        except (ValueError, TypeError):
            parser.error("--blur-range must use START:END frame numbers")
        if blur_range[0] < 0 or blur_range[1] < blur_range[0]:
            parser.error("--blur-range must satisfy 0 <= START <= END")

    renderer = DepthRenderer(
        args.model.resolve(),
        args.segment_model.resolve() if args.segment_model is not None else None,
        faithful_depth=args.faithful_depth,
        blur_range=blur_range,
    )
    if args.preview_frame is not None:
        render_preview(renderer, args.input.resolve(), args.output.resolve(), args.preview_frame)
    else:
        if args.ffmpeg is None:
            parser.error("完整视频处理需要 --ffmpeg")
        render_video(
            renderer,
            args.input.resolve(),
            args.output.resolve(),
            args.ffmpeg.resolve(),
            max_duration_seconds=args.max_duration,
        )


if __name__ == "__main__":
    main()
