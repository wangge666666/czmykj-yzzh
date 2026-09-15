from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlparse

import cv2
import requests


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_SEEDANCE_MODEL = "doubao-seedance-2-0-260128"
DEFAULT_SEEDANCE_25_MODEL = "doubao-seedance-2-5-260628"
DEFAULT_SEEDREAM_MODEL = "doubao-seedream-5-0-260128"
MAX_DEPTH_VIDEO_SECONDS = 14.5
MAX_DEPTH_VIDEO_BYTES = 200 * 1024 * 1024
MAX_FREE_UPLOAD_BYTES = 95 * 1024 * 1024
TEMPFILE_UPLOAD_URL = "https://tempfile.org/api/upload/local"
TEMPFILE_DELETE_URL = "https://tempfile.org/api/file/{file_id}"
TEMPFILE_DIRECT_URL = "https://tempfile.org/{file_id}/download"
DEPTH_MODEL_PATH = PROJECT_DIR / "models" / "depth-anything-v2-small" / "model_fp16.onnx"
DEPTH_MODEL_SHA256 = "2df6223f206b5164e21f664ace61dabeb9bb6a49b8b5a3e00510b4807d0f5b04"
DEPTH_MODEL_SIZE_BYTES = 49_642_442
DEPTH_MODEL_DOWNLOAD_SOURCES = (
    (
        "ModelScope 国内镜像",
        "https://modelscope.cn/models/onnx-community/depth-anything-v2-small/"
        "resolve/master/onnx/model_fp16.onnx",
    ),
    (
        "Hugging Face",
        "https://huggingface.co/onnx-community/depth-anything-v2-small/"
        "resolve/main/onnx/model_fp16.onnx?download=true",
    ),
)
DEPTH_MODEL_DOWNLOAD_LOCK = threading.Lock()
DEPTH_SCRIPT_PATH = PROJECT_DIR / "depth_video.py"
RUNS_DIR = PROJECT_DIR / "runs"
TOOLS_DIR = PROJECT_DIR / "tools"
CLOUDFLARED_PATH = TOOLS_DIR / "cloudflared.exe"
CLOUDFLARED_VERSION = "2026.6.1"
CLOUDFLARED_SHA256 = "5253e66f1f493c4e13539749f1aa86fd0c61e3072900fec29a44ba046a6d97e2"
CLOUDFLARED_DOWNLOAD_SOURCES = (
    (
        "校验镜像",
        "https://sourceforge.net/projects/cloudflare-tunnel.mirror/files/"
        f"{CLOUDFLARED_VERSION}/cloudflared-windows-amd64.exe/download",
    ),
    (
        "GitHub 官方发布页",
        "https://github.com/cloudflare/cloudflared/releases/download/"
        f"{CLOUDFLARED_VERSION}/cloudflared-windows-amd64.exe",
    ),
)
CLOUDFLARED_DOWNLOAD_LOCK = threading.Lock()

DEFAULT_PROMPT = """将@图片 1中的人物定义为主角。主角的服装严格参考@图片 2中的服装，背景与空间环境严格参考@图片 3中的场景。
参考@视频 1的完整动作、表演节奏、身体姿态、空间位置变化、镜头运镜、取景、透视和构图，完全复刻原视频主角的脚步、抬手、摆臂、躯干倾斜、弹跳重心变化、转向、停顿与定格姿势；动作的起始时间、落点、强拍定格和节奏变化尽量逐帧对齐，镜头轨迹与构图同步对齐。
@视频 1是单目灰度深度参考，灰度仅表示距离与遮挡关系，不代表最终色彩或画面风格。最终视频必须使用@图片 1、@图片 2、@图片 3的正常彩色视觉信息，不要输出黑白画面、深度图、白模、轮廓光或灰度材质。保持人物身份、服装和场景稳定，动作自然连续，边缘清晰，无闪烁、无重影、无字幕、无Logo、无水印。"""


class WorkflowError(RuntimeError):
    """Expected, user-facing workflow error."""


def _valid_depth_model(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != DEPTH_MODEL_SIZE_BYTES:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == DEPTH_MODEL_SHA256
    except OSError:
        return False


def ensure_depth_model(on_log: Callable[[str], None] | None = None) -> Path:
    """Download and verify the local depth model when it is absent or corrupt."""
    if _valid_depth_model(DEPTH_MODEL_PATH):
        return DEPTH_MODEL_PATH
    with DEPTH_MODEL_DOWNLOAD_LOCK:
        if _valid_depth_model(DEPTH_MODEL_PATH):
            return DEPTH_MODEL_PATH
        DEPTH_MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = DEPTH_MODEL_PATH.with_suffix(".onnx.download")
        temporary.unlink(missing_ok=True)
        last_error = ""
        for source_name, source_url in DEPTH_MODEL_DOWNLOAD_SOURCES:
            try:
                if on_log:
                    on_log(f"首次使用：正在从{source_name}下载深度模型（约 49.6 MB）。")
                with requests.get(
                    source_url,
                    stream=True,
                    allow_redirects=True,
                    timeout=(20, 180),
                ) as response:
                    response.raise_for_status()
                    total = int(response.headers.get("content-length") or 0)
                    downloaded = 0
                    reported = -10
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if not chunk:
                                continue
                            handle.write(chunk)
                            downloaded += len(chunk)
                            if on_log and total > 0:
                                percent = min(100, int(downloaded * 100 / total))
                                bucket = percent // 10 * 10
                                if bucket >= reported + 10:
                                    reported = bucket
                                    on_log(f"深度模型下载进度：{bucket}%")
                if not _valid_depth_model(temporary):
                    raise WorkflowError("下载内容的大小或 SHA-256 校验不一致。")
                temporary.replace(DEPTH_MODEL_PATH)
                if on_log:
                    on_log("深度模型下载并校验完成。")
                return DEPTH_MODEL_PATH
            except Exception as exc:
                last_error = f"{source_name}：{exc}"
                temporary.unlink(missing_ok=True)
        raise WorkflowError(f"深度模型自动下载失败。{last_error}")


def _valid_cloudflared_binary(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size <= 5 * 1024 * 1024:
            return False
        with path.open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


def ensure_cloudflared(on_log: Callable[[str], None] | None = None) -> Path:
    """Download the official Windows connector once for zero-config temporary URLs."""
    if _valid_cloudflared_binary(CLOUDFLARED_PATH):
        return CLOUDFLARED_PATH
    with CLOUDFLARED_DOWNLOAD_LOCK:
        if _valid_cloudflared_binary(CLOUDFLARED_PATH):
            return CLOUDFLARED_PATH
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        for stale_download in TOOLS_DIR.glob("cloudflared-*.download"):
            try:
                stale_download.unlink(missing_ok=True)
            except OSError:
                pass
        if on_log:
            on_log("首次使用：正在下载官方临时通道组件（只需一次）。")
        last_error = ""
        for source_name, source_url in CLOUDFLARED_DOWNLOAD_SOURCES:
            temporary = TOOLS_DIR / f"cloudflared-{uuid.uuid4().hex}.download"
            try:
                if os.name == "nt":
                    result = subprocess.run(
                        [
                            "curl.exe",
                            "-L",
                            "--fail",
                            "--silent",
                            "--show-error",
                            "--connect-timeout",
                            "12",
                            "--max-time",
                            "180",
                            "--output",
                            str(temporary),
                            source_url,
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=195,
                    )
                    if result.returncode != 0:
                        raise WorkflowError(result.stderr.strip() or f"{source_name}下载失败。")
                else:
                    with requests.get(
                        source_url,
                        stream=True,
                        timeout=(12, 120),
                        headers={"User-Agent": "DepthFlow/1.0"},
                    ) as response:
                        response.raise_for_status()
                        with temporary.open("wb") as handle:
                            for chunk in response.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    handle.write(chunk)
                digest = hashlib.sha256()
                with temporary.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest().lower() != CLOUDFLARED_SHA256:
                    raise WorkflowError(f"{source_name}下载文件的安全校验不匹配。")
                if not _valid_cloudflared_binary(temporary):
                    raise WorkflowError(f"{source_name}下载不完整。")
                os.replace(temporary, CLOUDFLARED_PATH)
                if on_log:
                    on_log("临时通道组件准备完成，并已通过官方 SHA-256 校验。")
                return CLOUDFLARED_PATH
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                last_error = str(exc)
                if on_log:
                    on_log(f"{source_name}不可用，正在尝试备用下载地址。")
        raise WorkflowError(f"无法下载临时通道组件：{last_error or '所有下载地址均不可用'}")


class TemporaryPublicTunnel:
    """Task-scoped TryCloudflare tunnel used when TOS is not configured."""

    URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

    def __init__(self, local_origin: str, on_log: Callable[[str], None] | None = None) -> None:
        self.local_origin = local_origin.rstrip("/")
        self.on_log = on_log
        self.process: subprocess.Popen[str] | None = None
        self.public_origin = ""

    def start(self, timeout: float = 45.0) -> str:
        executable = ensure_cloudflared(self.on_log)
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [
                str(executable),
                "tunnel",
                "--no-autoupdate",
                "--protocol",
                "http2",
                "--url",
                self.local_origin,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
        messages: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                messages.put(line.strip())
            messages.put(None)

        threading.Thread(target=read_output, daemon=True, name="cloudflared-output").start()
        deadline = time.monotonic() + timeout
        recent: list[str] = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None and messages.empty():
                break
            try:
                line = messages.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                break
            recent.append(line)
            recent = recent[-12:]
            match = self.URL_PATTERN.search(line)
            if match:
                self.public_origin = match.group(0)
                if self.on_log:
                    self.on_log("一次性加密视频通道已建立。")
                return self.public_origin
        self.close()
        detail = next((line for line in reversed(recent) if "ERR" in line or "error" in line.lower()), "")
        suffix = f"（{detail}）" if detail else ""
        raise WorkflowError(f"临时视频通道建立失败{suffix}")

    def wait_until_reachable(self, public_url: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise WorkflowError("临时视频通道意外停止。")
            try:
                response = requests.head(public_url, timeout=(5, 10), allow_redirects=True)
                if response.status_code in {200, 206}:
                    return
                last_error = f"HTTP {response.status_code}"
            except requests.RequestException as exc:
                last_error = str(exc)
            time.sleep(1)
        raise WorkflowError(f"临时公网地址无法访问：{last_error or '连接超时'}")

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class TemporaryVideoServer:
    """Serve exactly one token-protected MP4 on a random localhost port."""

    def __init__(self, video_path: Path, token: str) -> None:
        self.video_path = video_path.resolve()
        self.route_path = f"/media/{token}/depth.mp4"
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.local_origin = ""

    def start(self) -> str:
        video_path = self.video_path
        route_path = self.route_path
        if not video_path.is_file():
            raise WorkflowError("准备临时视频服务时找不到深度视频。")

        class VideoHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
                self._serve_video(send_body=False)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                self._serve_video(send_body=True)

            def _serve_video(self, *, send_body: bool) -> None:
                request_path = self.path.split("?", 1)[0]
                if request_path != route_path:
                    self.send_error(404)
                    return
                size = video_path.stat().st_size
                start, end = 0, max(0, size - 1)
                status = 200
                range_header = self.headers.get("Range", "").strip()
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                    if not match or (not match.group(1) and not match.group(2)):
                        self._range_error(size)
                        return
                    if match.group(1):
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else size - 1
                    else:
                        suffix_length = int(match.group(2))
                        start = max(0, size - suffix_length)
                        end = size - 1
                    if start >= size or start < 0 or end < start:
                        self._range_error(size)
                        return
                    end = min(end, size - 1)
                    status = 206
                content_length = max(0, end - start + 1)
                self.send_response(status)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(content_length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if not send_body:
                    return
                remaining = content_length
                try:
                    with video_path.open("rb") as handle:
                        handle.seek(start)
                        while remaining > 0:
                            chunk = handle.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _range_error(self, size: int) -> None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), VideoHandler)
        self.server.daemon_threads = True
        port = int(self.server.server_address[1])
        self.local_origin = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
            name=f"temporary-video-{port}",
        )
        self.thread.start()
        return f"{self.local_origin}{self.route_path}"

    def close(self) -> None:
        server = self.server
        self.server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.thread = None


class TemporaryFileServer:
    """Serve exactly one token-protected file on a random localhost port."""

    def __init__(self, file_path: Path, token: str, *, public_name: str | None = None) -> None:
        self.file_path = file_path.resolve()
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", public_name or self.file_path.name).strip("-.")
        safe_name = safe_name or "asset.bin"
        self.route_path = f"/media/{token}/{quote(safe_name)}"
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.local_origin = ""

    def start(self) -> str:
        file_path = self.file_path
        route_path = self.route_path
        if not file_path.is_file():
            raise WorkflowError("准备临时素材服务时找不到人物图片。")
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

        class FileHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
                self._serve_file(send_body=False)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
                self._serve_file(send_body=True)

            def _serve_file(self, *, send_body: bool) -> None:
                request_path = self.path.split("?", 1)[0]
                if request_path != route_path:
                    self.send_error(404)
                    return
                size = file_path.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not send_body:
                    return
                try:
                    with file_path.open("rb") as handle:
                        while True:
                            chunk = handle.read(1024 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FileHandler)
        self.server.daemon_threads = True
        port = int(self.server.server_address[1])
        self.local_origin = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
            name=f"temporary-file-{port}",
        )
        self.thread.start()
        return f"{self.local_origin}{self.route_path}"

    def close(self) -> None:
        server = self.server
        self.server = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        self.thread = None


class ArkAPIError(WorkflowError):
    def __init__(self, status_code: int, message: str, code: str = "") -> None:
        raw_message = message
        combined = f"{code} {message}".lower()
        if "inputimagesensitivecontentdetected" in combined or "privacyinformation" in combined:
            message = (
                "输入参考图被火山方舟的真人/隐私安全审核拒绝，本次生成没有开始。"
                "相同图片重复提交仍会失败；真人形象请先在方舟可信素材库完成本人授权并使用 asset:// 素材 ID，"
                "或更换为不含可识别真人的参考图。"
            )
        elif "inputtextsensitivecontentdetected" in combined:
            message = "提示词被火山方舟安全审核拒绝，本次生成没有开始；请修改提示词后再提交。"
        elif "outputvideosensitivecontentdetected" in combined:
            message = "生成结果未通过火山方舟安全审核，因此没有可下载成片；请调整参考素材或提示词后重试。"
        prefix = f"方舟 API 请求失败（HTTP {status_code}）"
        if code:
            prefix += f"[{code}]"
        super().__init__(f"{prefix}：{message}")
        self.status_code = status_code
        self.code = code
        self.raw_message = raw_message


class ArkConnectionError(WorkflowError):
    """A transport failure where an API response was never received."""

    def __init__(self, method: str, message: str, attempts: int) -> None:
        method = method.upper()
        if method == "POST":
            text = (
                "提交到火山方舟时网络连接中断，创建结果可能未知。"
                "为避免重复计费，程序不会自动重复提交付费请求。"
            )
        else:
            text = f"无法连接火山方舟（{method}，已尝试 {attempts} 次）。"
        super().__init__(f"{text}详情：{message}")
        self.method = method
        self.attempts = attempts


@dataclass(frozen=True)
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    frame_count: int
    duration: float
    size_bytes: int


@dataclass(frozen=True)
class UploadedObject:
    object_key: str
    signed_url: str


def load_env_file(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    env_path = path or PROJECT_DIR / ".env"
    loaded: dict[str, str] = {}
    if not env_path.is_file():
        return loaded
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def timestamped_run_dir(prefix: str = "job") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = RUNS_DIR / f"{stamp}_{prefix}"
    suffix = 1
    while candidate.exists():
        candidate = RUNS_DIR / f"{stamp}_{prefix}_{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def inspect_video(path: str | Path) -> VideoInfo:
    video_path = Path(path).expanduser().resolve()
    if not video_path.is_file():
        raise WorkflowError(f"找不到视频文件：{video_path}")
    capture = cv2.VideoCapture(str(video_path))
    try:
        if not capture.isOpened():
            raise WorkflowError(f"无法读取视频：{video_path}")
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        capture.release()
    if width <= 0 or height <= 0 or fps <= 0 or frame_count <= 0:
        raise WorkflowError("视频元数据不完整，无法确认尺寸、帧率或时长。")
    duration = frame_count / fps
    return VideoInfo(
        path=str(video_path),
        width=width,
        height=height,
        fps=fps,
        frame_count=frame_count,
        duration=duration,
        size_bytes=video_path.stat().st_size,
    )


def extract_scene_reference_frames(
    video_path: str | Path,
    output_dir: str | Path,
    *,
    count: int = 3,
    max_duration_seconds: float = MAX_DEPTH_VIDEO_SECONDS,
    max_side: int = 1536,
) -> list[Path]:
    """Extract evenly spaced, compressed frames for Seedream scene recovery."""
    if not 1 <= int(count) <= 6:
        raise WorkflowError("场景参考帧数量必须为 1–6 张。")
    source = Path(video_path).expanduser().resolve()
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise WorkflowError(f"无法打开参考视频：{source}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS)) or 0.0
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or total_frames <= 0:
            raise WorkflowError("无法读取参考视频帧率或帧数。")
        effective_frames = min(total_frames, max(1, int(max_duration_seconds * fps)))
        fractions = [0.5] if count == 1 else [0.18 + 0.64 * index / (count - 1) for index in range(count)]
        results: list[Path] = []
        for ordinal, fraction in enumerate(fractions, start=1):
            frame_index = min(effective_frames - 1, max(0, int(round((effective_frames - 1) * fraction))))
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                raise WorkflowError(f"读取场景参考帧失败：第 {frame_index} 帧。")
            height, width = frame.shape[:2]
            scale = min(1.0, float(max_side) / max(width, height))
            if scale < 1.0:
                frame = cv2.resize(
                    frame,
                    (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
                    interpolation=cv2.INTER_AREA,
                )
            target = target_dir / f"scene_source_{ordinal:02d}.jpg"
            encoded_ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, 88],
            )
            if not encoded_ok or encoded is None:
                raise WorkflowError(f"保存场景参考帧失败：{target.name}")
            try:
                # cv2.imwrite may fail silently on Windows paths containing Chinese
                # characters. Python's pathlib writes Unicode paths correctly.
                target.write_bytes(encoded.tobytes())
            except OSError as exc:
                raise WorkflowError(f"保存场景参考帧失败：{target.name}（{exc}）") from exc
            if not target.is_file() or target.stat().st_size <= 0:
                raise WorkflowError(f"保存场景参考帧失败：{target.name}")
            results.append(target)
        return results
    finally:
        capture.release()


def validate_seedance_reference_video(path: str | Path) -> VideoInfo:
    info = inspect_video(path)
    errors: list[str] = []
    if Path(info.path).suffix.lower() not in {".mp4", ".mov"}:
        errors.append("格式必须为 MP4 或 MOV")
    if not 2 <= info.duration <= 15.15:
        errors.append(f"时长必须为 2–15 秒（当前约 {info.duration:.2f} 秒）")
    if not 24 <= info.fps <= 60.1:
        errors.append(f"帧率必须为 24–60 FPS（当前 {info.fps:.2f}）")
    ratio = info.width / info.height
    if not 0.4 <= ratio <= 2.5:
        errors.append(f"宽高比必须在 0.4–2.5（当前 {ratio:.3f}）")
    if not (300 <= info.width <= 6000 and 300 <= info.height <= 6000):
        errors.append(f"宽高必须各在 300–6000 px（当前 {info.width}×{info.height}）")
    pixels = info.width * info.height
    if not 409_600 <= pixels <= 8_295_044:
        errors.append(f"总像素必须在 409600–8295044（当前 {pixels}）")
    if info.size_bytes > 200 * 1024 * 1024:
        errors.append("文件必须不超过 200 MB")
    if errors:
        raise WorkflowError("参考视频不符合 Seedance 2.0 要求：\n- " + "\n- ".join(errors))
    return info


def resolve_ffmpeg() -> Path:
    configured = os.getenv("FFMPEG_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if path.is_file():
            return path
        raise WorkflowError(f"FFMPEG_PATH 指向的文件不存在：{path}")
    try:
        import imageio_ffmpeg

        path = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise WorkflowError("未找到 FFmpeg，请先运行安装脚本。") from exc
    if not path.is_file():
        raise WorkflowError(f"未找到 FFmpeg：{path}")
    return path


def build_depth_command(
    input_path: str | Path,
    output_path: str | Path,
    *,
    blur_range: str = "",
) -> list[str]:
    source = Path(input_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise WorkflowError(f"找不到参考视频：{source}")
    if not DEPTH_MODEL_PATH.is_file():
        raise WorkflowError(f"找不到深度模型：{DEPTH_MODEL_PATH}")
    if not DEPTH_SCRIPT_PATH.is_file():
        raise WorkflowError(f"找不到深度处理脚本：{DEPTH_SCRIPT_PATH}")
    if output.exists():
        raise WorkflowError(f"输出文件已存在，请换一个名称：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(DEPTH_SCRIPT_PATH),
        "--input",
        str(source),
        "--model",
        str(DEPTH_MODEL_PATH),
        "--output",
        str(output),
        "--ffmpeg",
        str(resolve_ffmpeg()),
        "--faithful-depth",
        "--max-duration",
        str(MAX_DEPTH_VIDEO_SECONDS),
    ]
    value = blur_range.strip()
    if value:
        if not re.fullmatch(r"\d+:\d+", value):
            raise WorkflowError("运动模糊补偿范围须使用“起始帧:结束帧”，例如 291:305。")
        start, end = (int(part) for part in value.split(":", 1))
        if start > end:
            raise WorkflowError("运动模糊补偿的起始帧不能大于结束帧。")
        command.extend(["--blur-range", value])
    return command


def run_depth_generation(
    input_path: str | Path,
    output_path: str | Path,
    *,
    blur_range: str = "",
    on_log: Callable[[str], None] | None = None,
) -> Path:
    ensure_depth_model(on_log=on_log)
    command = build_depth_command(input_path, output_path, blur_range=blur_range)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(
        command,
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert process.stdout is not None
    for line in process.stdout:
        text = line.rstrip()
        if on_log and text:
            try:
                on_log(text)
            except Exception:
                # A console code-page or UI logging problem must never abort
                # an otherwise healthy multi-minute render.
                pass
    return_code = process.wait()
    if return_code != 0:
        raise WorkflowError(f"深度视频生成失败，退出码：{return_code}")
    result = Path(output_path).resolve()
    if not result.is_file() or result.stat().st_size == 0:
        raise WorkflowError("深度处理已结束，但没有生成有效的视频文件。")
    info = inspect_video(result)
    if info.duration > MAX_DEPTH_VIDEO_SECONDS + 1e-6:
        raise WorkflowError(
            "深度视频输出超过 14.5 秒安全上限："
            f"当前 {info.duration:.3f} 秒。"
        )
    if info.size_bytes > MAX_DEPTH_VIDEO_BYTES:
        raise WorkflowError(
            "深度视频输出超过 200 MB 安全上限："
            f"当前 {info.size_bytes / (1024 * 1024):.1f} MB。"
        )
    return result


def _is_remote_or_asset(value: str) -> bool:
    return value.startswith(("https://", "http://", "asset://", "data:image/"))


def image_to_data_url(source: str | Path) -> str:
    value = str(source).strip()
    if _is_remote_or_asset(value):
        return value
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise WorkflowError(f"找不到图片：{path}")
    supported = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".gif": "image/gif",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }
    mime = supported.get(path.suffix.lower())
    if not mime:
        mime = mimetypes.guess_type(path.name)[0]
    if not mime or not mime.startswith("image/"):
        raise WorkflowError(f"不支持的图片格式：{path.suffix or '无扩展名'}")
    if path.stat().st_size >= 30 * 1024 * 1024:
        raise WorkflowError(f"图片必须小于 30 MB：{path.name}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def validate_image_payload_size(image_sources: Iterable[str]) -> None:
    local_total = 0
    for source in image_sources:
        value = source.strip()
        if not value or _is_remote_or_asset(value):
            continue
        path = Path(value).expanduser().resolve()
        if path.is_file():
            local_total += path.stat().st_size
    # Base64 adds about 33%; leave room for JSON and prompt below the 64 MB limit.
    if local_total > 45 * 1024 * 1024:
        raise WorkflowError("三张本地参考图合计过大。请压缩到约 45 MB 以下再提交。")


def validate_video_reference(value: str) -> str:
    reference = value.strip()
    if reference.startswith("asset://"):
        return reference
    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise WorkflowError("深度视频须填写公网 http(s) URL 或 asset:// 素材 ID。")
    return reference


def validate_seedance_output_parameters(*, model: str, ratio: str, duration: int) -> None:
    """Validate both ordinary generation and Seedance 2.5 video-editing controls."""
    if ratio not in {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}:
        raise WorkflowError(f"不支持的宽高比：{ratio}")
    numeric_duration = int(duration)
    if numeric_duration == -1:
        if str(model or "").strip() != DEFAULT_SEEDANCE_25_MODEL:
            raise WorkflowError("只有 Seedance 2.5 视频编辑任务允许使用自动时长 -1。")
        if ratio != "adaptive":
            raise WorkflowError("Seedance 2.5 视频编辑任务必须使用 adaptive 比例。")
        return
    if not 4 <= numeric_duration <= 15:
        raise WorkflowError("Seedance 输出时长必须为 4–15 秒。")


def build_seedance_payload(
    *,
    prompt: str,
    person_source: str,
    clothing_source: str,
    scene_source: str,
    depth_video_reference: str,
    model: str = DEFAULT_SEEDANCE_MODEL,
    resolution: str = "720p",
    ratio: str = "adaptive",
    duration: int = 5,
    generate_audio: bool = False,
    watermark: bool = False,
) -> dict[str, Any]:
    prompt = prompt.strip()
    if not prompt:
        raise WorkflowError("提示词不能为空。")
    if len(prompt) > 2000:
        raise WorkflowError("提示词过长。官方建议中文提示词不超过 500 字。")
    image_sources = [person_source, clothing_source, scene_source]
    if any(not item.strip() for item in image_sources):
        raise WorkflowError("人物、服装和场景三项参考素材都必须填写。")
    validate_image_payload_size(image_sources)
    if resolution not in {"480p", "720p", "1080p", "4k"}:
        raise WorkflowError(f"不支持的输出分辨率：{resolution}")
    validate_seedance_output_parameters(model=model, ratio=ratio, duration=duration)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for source in image_sources:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_to_data_url(source)},
                "role": "reference_image",
            }
        )
    content.append(
        {
            "type": "video_url",
            "video_url": {"url": validate_video_reference(depth_video_reference)},
            "role": "reference_video",
        }
    )
    return {
        "model": model.strip() or DEFAULT_SEEDANCE_MODEL,
        "content": content,
        "resolution": resolution,
        "ratio": ratio,
        "duration": int(duration),
        "generate_audio": bool(generate_audio),
        "watermark": bool(watermark),
    }


def build_video_reference_seedance_payload(
    *,
    prompt: str,
    video_reference: str,
    model: str = DEFAULT_SEEDANCE_MODEL,
    resolution: str = "720p",
    ratio: str = "adaptive",
    duration: int = 5,
    generate_audio: bool = False,
    watermark: bool = False,
) -> dict[str, Any]:
    """Build a Seedance request constrained by one reference video only."""
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise WorkflowError("提示词不能为空。")
    if len(clean_prompt) > 2000:
        raise WorkflowError("提示词过长；请控制在 2000 个字符以内。")
    if resolution not in {"480p", "720p", "1080p", "4k"}:
        raise WorkflowError(f"不支持的输出分辨率：{resolution}")
    validate_seedance_output_parameters(model=model, ratio=ratio, duration=duration)
    return {
        "model": model.strip() or DEFAULT_SEEDANCE_MODEL,
        "content": [
            {"type": "text", "text": clean_prompt},
            {
                "type": "video_url",
                "video_url": {"url": validate_video_reference(video_reference)},
                "role": "reference_video",
            },
        ],
        "resolution": resolution,
        "ratio": ratio,
        "duration": int(duration),
        "generate_audio": bool(generate_audio),
        "watermark": bool(watermark),
    }


def build_scene_seedance_payload(
    *,
    prompt: str,
    scene_source: str,
    depth_video_reference: str,
    model: str = DEFAULT_SEEDANCE_MODEL,
    resolution: str = "720p",
    ratio: str = "adaptive",
    duration: int = 5,
    generate_audio: bool = False,
    watermark: bool = False,
) -> dict[str, Any]:
    """Build a scene-only redraw payload for a shot with no people."""
    clean_prompt = prompt.strip()
    if not clean_prompt:
        raise WorkflowError("提示词不能为空。")
    if len(clean_prompt) > 2000:
        raise WorkflowError("提示词过长；请控制在 2000 个字符以内。")
    if not scene_source.strip():
        raise WorkflowError("无人分镜也必须指定新场景参考图。")
    validate_image_payload_size([scene_source])
    if resolution not in {"480p", "720p", "1080p", "4k"}:
        raise WorkflowError(f"不支持的输出分辨率：{resolution}")
    validate_seedance_output_parameters(model=model, ratio=ratio, duration=duration)
    return {
        "model": model.strip() or DEFAULT_SEEDANCE_MODEL,
        "content": [
            {"type": "text", "text": clean_prompt},
            {
                "type": "image_url",
                "image_url": {"url": image_to_data_url(scene_source)},
                "role": "reference_image",
            },
            {
                "type": "video_url",
                "video_url": {"url": validate_video_reference(depth_video_reference)},
                "role": "reference_video",
            },
        ],
        "resolution": resolution,
        "ratio": ratio,
        "duration": int(duration),
        "generate_audio": bool(generate_audio),
        "watermark": bool(watermark),
    }


def build_multi_seedance_payload(
    *,
    prompt: str,
    character_sources: list[tuple[str, ...]],
    scene_source: str,
    depth_video_reference: str,
    model: str = DEFAULT_SEEDANCE_MODEL,
    resolution: str = "720p",
    ratio: str = "adaptive",
    duration: int = 5,
    generate_audio: bool = False,
    watermark: bool = False,
    include_scene_reference: bool = True,
) -> dict[str, Any]:
    """Build an isolated 1-4 character Seedance reference payload.

    Virtual actors use two images (appearance/clothing). Real-person character
    library mode uses an authorized asset plus one clothing image.
    The shared scene is normally the final image. Real-person long-video
    generation may bake the shot scene into the temporal reference instead,
    preventing a static scene image from pulling the camera away from it.
    """
    prompt = prompt.strip()
    if not prompt:
        raise WorkflowError("提示词不能为空。")
    if len(prompt) > 2000:
        raise WorkflowError("提示词过长；请控制在 2000 个字符以内。")
    if not 1 <= len(character_sources) <= 4:
        raise WorkflowError("人物复刻当前支持 1–4 位人物。")
    image_sources: list[str] = []
    for index, pair in enumerate(character_sources, start=1):
        if len(pair) not in {2, 3} or any(not str(item).strip() for item in pair):
            raise WorkflowError(f"人物 {index} 必须填写完整的人物身份参考与服装图。")
        image_sources.extend(str(item) for item in pair)
    if include_scene_reference:
        if not scene_source.strip():
            raise WorkflowError("场景参考图不能为空。")
        image_sources.append(scene_source)
    if len(image_sources) > 9:
        raise WorkflowError("Seedance 2.0 最多接收 9 张参考图片。")
    validate_image_payload_size(image_sources)
    if resolution not in {"480p", "720p", "1080p", "4k"}:
        raise WorkflowError(f"不支持的输出分辨率：{resolution}")
    validate_seedance_output_parameters(model=model, ratio=ratio, duration=duration)

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for source in image_sources:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": image_to_data_url(source)},
                "role": "reference_image",
            }
        )
    content.append(
        {
            "type": "video_url",
            "video_url": {"url": validate_video_reference(depth_video_reference)},
            "role": "reference_video",
        }
    )
    return {
        "model": model.strip() or DEFAULT_SEEDANCE_MODEL,
        "content": content,
        "resolution": resolution,
        "ratio": ratio,
        "duration": int(duration),
        "generate_audio": bool(generate_audio),
        "watermark": bool(watermark),
    }


def build_motion_reference_payload(
    *, prompt: str, image_sources: list[str], video_reference: str,
    model: str = DEFAULT_SEEDANCE_25_MODEL, resolution: str = "720p",
    ratio: str = "adaptive", duration: int = -1,
    generate_audio: bool = False, watermark: bool = False,
) -> dict[str, Any]:
    """Ordered image references plus motion video; frame guides use reference images.

    This deliberately does not mix native first/last-frame roles with video
    reference mode. Frame composition is requested through the prompt.
    """
    if not prompt.strip() or len(prompt) > 2000:
        raise WorkflowError("提示词不能为空，且不能超过 2000 字。")
    if not 1 <= len(image_sources) <= 9 or any(not str(s).strip() for s in image_sources):
        raise WorkflowError("人物动作迁移需要 1–9 张有效参考图片。")
    if resolution not in {"480p", "720p"}:
        raise WorkflowError("人物动作迁移支持 480p 或 720p。")
    validate_seedance_output_parameters(model=model, ratio=ratio, duration=duration)
    validate_image_payload_size(image_sources)
    return {
        "model": model, "resolution": resolution, "ratio": ratio, "duration": duration,
        "generate_audio": bool(generate_audio), "watermark": bool(watermark),
        "content": [{"type": "text", "text": prompt.strip()}] + [
            {"type": "image_url", "image_url": {"url": image_to_data_url(s)}, "role": "reference_image"}
            for s in image_sources
        ] + [{"type": "video_url", "video_url": {"url": validate_video_reference(video_reference)}, "role": "reference_video"}],
    }


class ArkVideoClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_ARK_BASE_URL,
        session: requests.Session | None = None,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise WorkflowError(".env 中缺少 ARK_API_KEY。")
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: tuple[int, int] = (15, 120),
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        method = method.upper()
        # Read-only requests are safe to retry after transient TLS/network failures.
        # POST task creation is deliberately attempted once because a lost response
        # can still mean that the paid task was accepted by Ark.
        attempts = 3 if method in {"GET", "HEAD"} else 1
        last_error: requests.RequestException | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=json_body,
                    params=params,
                    timeout=timeout,
                )
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt < attempts:
                    time.sleep(attempt)
                    continue
                raise ArkConnectionError(method, str(exc), attempts) from exc
        else:  # pragma: no cover - the loop either breaks or raises
            raise ArkConnectionError(method, str(last_error or "未知网络错误"), attempts)
        if response.ok:
            try:
                return response.json()
            except ValueError as exc:
                raise WorkflowError("火山方舟返回了无法解析的响应。") from exc
        code = ""
        message = response.reason or "未知错误"
        try:
            data = response.json()
            error = data.get("error") or data.get("Error") or data
            if isinstance(error, dict):
                code = str(error.get("code") or error.get("Code") or "")
                message = str(error.get("message") or error.get("Message") or message)
        except ValueError:
            pass
        raise ArkAPIError(response.status_code, message[:800], code[:200])

    def check_credentials(self) -> str:
        try:
            self.list_tasks(page_size=1)
            return "API Key 有效，任务查询权限正常。"
        except ArkAPIError as exc:
            if exc.status_code in {400, 404, 405, 422}:
                # These errors can occur when a regional deployment does not expose list queries,
                # but authentication has already succeeded.
                return "API Key 已通过鉴权；当前接口未开放任务列表查询。"
            raise


    def generate_image(
        self,
        *,
        prompt: str,
        image_sources: list[str],
        model: str = DEFAULT_SEEDREAM_MODEL,
        size: str = "2K",
        watermark: bool = False,
    ) -> dict[str, Any]:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise WorkflowError("Seedream 场景提取提示词不能为空。")
        if not 1 <= len(image_sources) <= 10:
            raise WorkflowError("Seedream 参考图数量必须为 1–10 张。")
        images = [image_to_data_url(source) for source in image_sources]
        payload = {
            "model": model.strip() or DEFAULT_SEEDREAM_MODEL,
            "prompt": clean_prompt,
            "image": images,
            "size": size,
            "sequential_image_generation": "disabled",
            "stream": False,
            "response_format": "url",
            "watermark": bool(watermark),
        }
        response = self._request(
            "POST",
            "/images/generations",
            json_body=payload,
            timeout=(30, 1200),
        )
        data = response.get("data") or []
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise WorkflowError("Seedream 返回的图片数据为空或格式异常。")
        url = str(data[0].get("url") or "").strip()
        if not url:
            raise WorkflowError("Seedream 任务成功，但响应中没有图片下载 URL。")
        return {
            "url": url,
            "size": str(data[0].get("size") or ""),
            "model": str(response.get("model") or model),
            "usage": response.get("usage"),
        }

    def list_tasks(self, *, page_size: int = 20) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/contents/generations/tasks",
            params={"page_size": max(1, min(int(page_size), 100))},
        )
        items = data.get("items") or []
        if not isinstance(items, list):
            raise WorkflowError("方舟任务列表响应格式异常。")
        return [item for item in items if isinstance(item, dict)]

    def recover_created_task(
        self,
        known_task_ids: set[str],
        *,
        model: str,
        created_after: float,
        created_before: float | None = None,
        resolution: str = "",
        ratio: str = "",
        duration: int = 0,
        generate_audio: bool | None = None,
        attempts: int = 4,
        poll_interval: float = 2.0,
    ) -> str:
        """Recover a task ID after an ambiguous POST connection failure.

        Recovery only succeeds when exactly one new task matches the submission
        fingerprint. Ambiguous results are intentionally left unresolved so the
        caller never risks attaching to another task on the same account.
        """
        for attempt in range(max(1, attempts)):
            candidates: list[str] = []
            try:
                for item in self.list_tasks(page_size=50):
                    task_id = str(item.get("id") or "").strip()
                    item_model = str(item.get("model") or "").strip()
                    try:
                        created_at = float(item.get("created_at") or 0)
                    except (TypeError, ValueError):
                        created_at = 0
                    item_resolution = str(item.get("resolution") or "").strip()
                    item_ratio = str(item.get("ratio") or "").strip()
                    try:
                        item_duration = int(float(item.get("duration") or 0))
                    except (TypeError, ValueError):
                        item_duration = 0
                    item_audio = item.get("generate_audio")
                    if (
                        task_id
                        and task_id not in known_task_ids
                        and item_model == model
                        and created_at >= created_after - 5
                        and (created_before is None or created_at <= created_before + 5)
                        and (not resolution or not item_resolution or item_resolution == resolution)
                        and (not ratio or not item_ratio or item_ratio == ratio)
                        and (not duration or not item_duration or item_duration == int(duration))
                        and (
                            generate_audio is None
                            or item_audio is None
                            or bool(item_audio) is bool(generate_audio)
                        )
                    ):
                        candidates.append(task_id)
            except (ArkConnectionError, ArkAPIError):
                candidates = []
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                return ""
            if attempt + 1 < max(1, attempts):
                time.sleep(max(0.1, poll_interval))
        return ""

    def create_task(self, payload: dict[str, Any]) -> str:
        # Do not automatically retry POST: a network timeout can still mean the paid task exists.
        data = self._request(
            "POST",
            "/contents/generations/tasks",
            json_body=payload,
            timeout=(30, 180),
        )
        task_id = str(data.get("id") or "").strip()
        if not task_id:
            raise WorkflowError("方舟已响应，但没有返回视频任务 ID。")
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any]:
        task_id = task_id.strip()
        if not task_id:
            raise WorkflowError("任务 ID 不能为空。")
        return self._request("GET", f"/contents/generations/tasks/{task_id}")

    def wait_for_task(
        self,
        task_id: str,
        *,
        poll_interval: int = 15,
        timeout_seconds: int = 7200,
        on_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        last_status = ""
        while True:
            task = self.get_task(task_id)
            status = str(task.get("status") or "unknown")
            if on_status and status != last_status:
                on_status(task)
            last_status = status
            if status == "succeeded":
                return task
            if status in {"failed", "expired", "cancelled"}:
                error = task.get("error") or {}
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise WorkflowError(f"Seedance 任务状态为 {status}：{message or '未提供原因'}")
            if time.monotonic() - started > timeout_seconds:
                raise WorkflowError(
                    f"等待已超过 {timeout_seconds // 60} 分钟。任务仍在云端运行，可稍后用任务 ID 查询。"
                )
            time.sleep(max(5, poll_interval))


class ArkAssetsClient:
    """AK/SK-signed client for Ark's private character Assets API."""

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        *,
        host: str = "open.volcengineapi.com",
        region: str = "cn-beijing",
        service: str = "ark",
        version: str = "2024-01-01",
        project_name: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        if not self.access_key or not self.secret_key:
            raise WorkflowError("火山人物库需要配置素材管理 AK/SK。")
        self.host = host.strip().lower()
        self.region = region.strip() or "cn-beijing"
        self.service = service.strip() or "ark"
        self.version = version.strip() or "2024-01-01"
        self.project_name = str(project_name or "default").strip() or "default"
        self.session = session or requests.Session()

    @staticmethod
    def _hmac_sha256(key: bytes, value: str) -> bytes:
        import hmac

        return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()

    def _authorization(self, action: str, body: bytes, timestamp: datetime) -> dict[str, str]:
        import hmac

        x_date = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        short_date = x_date[:8]
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_query = (
            f"Action={quote(action, safe='-_.~')}&Version={quote(self.version, safe='-_.~')}"
        )
        canonical_headers = (
            "content-type:application/json\n"
            f"host:{self.host}\n"
            f"x-content-sha256:{payload_hash}\n"
            f"x-date:{x_date}\n"
        )
        signed_headers = "content-type;host;x-content-sha256;x-date"
        canonical_request = "\n".join(
            ("POST", "/", canonical_query, canonical_headers, signed_headers, payload_hash)
        )
        scope = f"{short_date}/{self.region}/{self.service}/request"
        string_to_sign = "\n".join(
            (
                "HMAC-SHA256",
                x_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            )
        )
        date_key = self._hmac_sha256(self.secret_key.encode("utf-8"), short_date)
        region_key = self._hmac_sha256(date_key, self.region)
        service_key = self._hmac_sha256(region_key, self.service)
        signing_key = self._hmac_sha256(service_key, "request")
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "Content-Type": "application/json",
            "Host": self.host,
            "X-Content-Sha256": payload_hash,
            "X-Date": x_date,
            "Authorization": (
                f"HMAC-SHA256 Credential={self.access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def call(self, action: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        clean_action = str(action or "").strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,63}", clean_action):
            raise WorkflowError("火山人物库操作名称无效。")
        payload = json.dumps(body or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = self._authorization(clean_action, payload, datetime.now(timezone.utc))
        url = (
            f"https://{self.host}/?Action={quote(clean_action, safe='-_.~')}"
            f"&Version={quote(self.version, safe='-_.~')}"
        )
        try:
            response = self.session.post(url, data=payload, headers=headers, timeout=(20, 180))
        except requests.RequestException as exc:
            raise ArkConnectionError("POST", str(exc), 1) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise WorkflowError("火山人物库返回了无法解析的响应。") from exc
        metadata = data.get("ResponseMetadata") if isinstance(data, dict) else None
        metadata_error = metadata.get("Error") if isinstance(metadata, dict) else None
        if not response.ok or isinstance(metadata_error, dict):
            error = metadata_error if isinstance(metadata_error, dict) else data.get("error", {})
            code = str(error.get("Code") or error.get("code") or "") if isinstance(error, dict) else ""
            message = (
                str(error.get("Message") or error.get("message") or response.reason)
                if isinstance(error, dict)
                else str(error or response.reason)
            )
            raise ArkAPIError(response.status_code, message[:800], code[:200])
        return data

    def list_asset_groups(self, *, group_type: str, page_size: int = 100) -> list[dict[str, Any]]:
        data = self.call(
            "ListAssetGroups",
            {
                "Filter": {"GroupType": group_type},
                "PageNumber": 1,
                "PageSize": max(1, min(int(page_size), 100)),
                "SortBy": "CreateTime",
                "SortOrder": "Desc",
                "ProjectName": self.project_name,
            },
        )
        return [
            item
            for item in ((data.get("Result") or {}).get("Items") or [])
            if isinstance(item, dict)
        ]

    def create_asset_group(
        self,
        *,
        name: str,
        group_type: str = "AIGC",
        description: str = "",
    ) -> str:
        clean_name = " ".join(str(name or "").split())[:64]
        clean_description = " ".join(str(description or "").split())[:300]
        clean_group_type = str(group_type or "").strip()
        if not clean_name:
            raise WorkflowError("角色组名称不能为空。")
        if clean_group_type not in {"AIGC", "LivenessFace"}:
            raise WorkflowError("火山人物角色组类型无效。")
        data = self.call(
            "CreateAssetGroup",
            {
                "Name": clean_name,
                "Description": clean_description,
                "GroupType": clean_group_type,
                "ProjectName": self.project_name,
            },
        )
        result = data.get("Result") if isinstance(data.get("Result"), dict) else data
        group_id = str(
            result.get("Id")
            or result.get("GroupId")
            or result.get("GroupID")
            or ""
        ).strip()
        if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", group_id):
            raise WorkflowError("火山人物库已接收建组请求，但没有返回有效的 Group ID。")
        return group_id

    def create_visual_validate_session(
        self,
        *,
        callback_url: str,
        project_name: str | None = None,
    ) -> dict[str, str]:
        clean_callback = str(callback_url or "").strip()
        if not re.fullmatch(r"https?://[^\s]{3,2000}", clean_callback):
            raise WorkflowError("真人认证回调地址无效。")
        clean_project = str(project_name or self.project_name).strip() or self.project_name
        data = self.call(
            "CreateVisualValidateSession",
            {"CallbackURL": clean_callback, "ProjectName": clean_project},
        )
        result = data.get("Result") if isinstance(data.get("Result"), dict) else data
        token = str(result.get("BytedToken") or result.get("byted_token") or "").strip()
        h5_link = str(
            result.get("H5Link") or result.get("H5Url") or result.get("h5_link") or ""
        ).strip()
        returned_callback = str(result.get("CallbackURL") or clean_callback).strip()
        if not token or not re.fullmatch(r"https?://[^\s]{3,12000}", h5_link):
            raise WorkflowError("火山已接收真人认证请求，但没有返回有效的认证链接或 BytedToken。")
        return {
            "byted_token": token,
            "h5_link": h5_link,
            "callback_url": returned_callback,
            "project_name": clean_project,
        }

    def get_visual_validate_result(
        self,
        *,
        byted_token: str,
        project_name: str | None = None,
    ) -> dict[str, Any]:
        token = str(byted_token or "").strip()
        if not token or len(token) > 4096:
            raise WorkflowError("真人认证 BytedToken 无效。")
        clean_project = str(project_name or self.project_name).strip() or self.project_name
        data = self.call(
            "GetVisualValidateResult",
            {"BytedToken": token, "ProjectName": clean_project},
        )
        result = data.get("Result") if isinstance(data.get("Result"), dict) else data
        group_id = str(
            result.get("GroupId")
            or result.get("GroupID")
            or result.get("group_id")
            or result.get("Id")
            or ""
        ).strip()
        if group_id and not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", group_id):
            raise WorkflowError("火山真人认证已返回结果，但 Group ID 格式无效。")
        return {
            "group_id": group_id,
            "status": str(result.get("Status") or result.get("ValidateStatus") or "").strip(),
        }

    def update_asset_group(
        self,
        group_id: str,
        *,
        name: str,
        description: str = "",
        project_name: str | None = None,
    ) -> None:
        if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", str(group_id or "")):
            raise WorkflowError("火山人物角色组 ID 无效。")
        clean_name = " ".join(str(name or "").split())[:64]
        clean_description = " ".join(str(description or "").split())[:300]
        if not clean_name:
            raise WorkflowError("角色组名称不能为空。")
        self.call(
            "UpdateAssetGroup",
            {
                "Id": group_id,
                "Name": clean_name,
                "Description": clean_description,
                "ProjectName": str(project_name or self.project_name).strip() or self.project_name,
            },
        )

    def delete_asset_group(self, group_id: str, *, project_name: str | None = None) -> None:
        if not re.fullmatch(r"group-[A-Za-z0-9_-]{6,120}", str(group_id or "")):
            raise WorkflowError("要删除的火山人物角色组 ID 无效。")
        self.call(
            "DeleteAssetGroup",
            {"Id": group_id, "ProjectName": str(project_name or self.project_name).strip() or self.project_name},
        )

    def list_assets(
        self,
        *,
        group_type: str,
        group_ids: list[str] | None = None,
        statuses: list[str] | None = None,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"GroupType": group_type}
        if group_ids:
            filters["GroupIds"] = list(group_ids)
        if statuses:
            filters["Statuses"] = list(statuses)
        data = self.call(
            "ListAssets",
            {
                "Filter": filters,
                "PageNumber": 1,
                "PageSize": max(1, min(int(page_size), 100)),
                "SortBy": "CreateTime",
                "SortOrder": "Desc",
                "ProjectName": self.project_name,
            },
        )
        return [
            item
            for item in ((data.get("Result") or {}).get("Items") or [])
            if isinstance(item, dict)
        ]

    def create_asset(self, *, group_id: str, url: str, name: str, asset_type: str = "Image") -> str:
        data = self.call(
            "CreateAsset",
            {
                "GroupId": group_id,
                "URL": url,
                "Name": name,
                "AssetType": asset_type,
                "ProjectName": self.project_name,
            },
        )
        asset_id = str((data.get("Result") or {}).get("Id") or "").strip()
        if not re.fullmatch(r"asset-[A-Za-z0-9_-]{6,120}", asset_id):
            raise WorkflowError("火山人物库已接收上传，但没有返回有效的 Asset ID。")
        return asset_id

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"asset-[A-Za-z0-9_-]{6,120}", asset_id):
            raise WorkflowError("火山人物素材 ID 无效。")
        data = self.call("GetAsset", {"Id": asset_id, "ProjectName": self.project_name})
        result = data.get("Result") or {}
        return result if isinstance(result, dict) else {}

    def delete_asset(self, asset_id: str) -> None:
        if not re.fullmatch(r"asset-[A-Za-z0-9_-]{6,120}", asset_id):
            raise WorkflowError("要删除的火山人物素材 ID 无效。")
        self.call("DeleteAsset", {"Id": asset_id, "ProjectName": self.project_name})


class TosMediaStore:
    def __init__(self, *, bucket: str | None = None) -> None:
        required = {
            "TOS_ACCESS_KEY": os.getenv("TOS_ACCESS_KEY", "").strip(),
            "TOS_SECRET_KEY": os.getenv("TOS_SECRET_KEY", "").strip(),
            "TOS_BUCKET": (bucket if bucket is not None else os.getenv("TOS_BUCKET", "")).strip(),
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise WorkflowError(
                "自动上传深度视频还缺少 TOS 配置：" + "、".join(missing)
                + "。也可以直接粘贴深度视频的公网 URL。"
            )
        try:
            import tos
        except ImportError as exc:  # pragma: no cover - installation issue
            raise WorkflowError("未安装火山 TOS Python SDK，请重新运行安装脚本。") from exc
        self.tos = tos
        self.bucket = required["TOS_BUCKET"]
        self.region = os.getenv("TOS_REGION", "cn-beijing").strip() or "cn-beijing"
        self.endpoint = (
            os.getenv("TOS_ENDPOINT", "tos-cn-beijing.volces.com").strip()
            or "tos-cn-beijing.volces.com"
        )
        self.prefix = os.getenv("TOS_PREFIX", "seedance-inputs").strip().strip("/")
        self.client = tos.TosClientV2(
            required["TOS_ACCESS_KEY"],
            required["TOS_SECRET_KEY"],
            self.endpoint,
            self.region,
        )

    @staticmethod
    def configured() -> bool:
        return all(
            os.getenv(key, "").strip()
            for key in ("TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_BUCKET")
        )

    def upload_file(self, path: str | Path, *, expires: int = 259_200) -> UploadedObject:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise WorkflowError(f"找不到待上传素材：{source}")
        now = datetime.now().strftime("%Y/%m/%d")
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-.") or "depth"
        key = f"{self.prefix}/{now}/{safe_stem}-{uuid.uuid4().hex[:12]}{source.suffix.lower()}"
        try:
            self.client.put_object_from_file(self.bucket, key, str(source))
            output = self.client.pre_signed_url(
                self.tos.HttpMethodType.Http_Method_Get,
                bucket=self.bucket,
                key=key,
                expires=expires,
            )
        except Exception as exc:
            raise WorkflowError(f"上传到 TOS 失败：{exc}") from exc
        return UploadedObject(object_key=key, signed_url=output.signed_url)

    def upload_video(self, path: str | Path, *, expires: int = 259_200) -> UploadedObject:
        return self.upload_file(path, expires=expires)

    def delete(self, object_key: str) -> None:
        try:
            self.client.delete_object(self.bucket, object_key)
        except Exception as exc:
            raise WorkflowError(f"删除 TOS 临时素材失败：{exc}") from exc


class TempFileMediaStore:
    """Anonymous short-lived public storage used for Seedance API video input."""

    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()

    @staticmethod
    def available() -> bool:
        return True

    def upload_video(
        self,
        path: str | Path,
        *,
        expires_hours: int = 1,
        attempts: int = 3,
    ) -> UploadedObject:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise WorkflowError(f"找不到待上传视频：{source}")
        size = source.stat().st_size
        if size <= 0:
            raise WorkflowError("待上传的深度视频为空。")
        if size > MAX_FREE_UPLOAD_BYTES:
            raise WorkflowError(
                "免费临时通道要求深度视频不超过 95 MB："
                f"当前 {size / (1024 * 1024):.1f} MB。"
            )
        if expires_hours not in {1, 6, 24, 48}:
            raise WorkflowError("免费临时视频有效期只支持 1、6、24 或 48 小时。")

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.")
        safe_name = safe_name or "depth.mp4"
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                with source.open("rb") as handle:
                    response = self.session.post(
                        TEMPFILE_UPLOAD_URL,
                        files={"files": (safe_name, handle, "video/mp4")},
                        data={"expiryHours": str(expires_hours)},
                        timeout=(30, 600),
                    )
                response.raise_for_status()
                payload = response.json()
                files = payload.get("files") if isinstance(payload, dict) else None
                item = files[0] if isinstance(files, list) and files else {}
                file_id = str(item.get("id") or "").strip()
                remote_size = int(item.get("size") or 0)
                if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", file_id):
                    raise WorkflowError("免费临时通道没有返回有效的文件 ID。")
                if remote_size and remote_size != size:
                    raise WorkflowError("免费临时通道返回的文件大小与本地不一致。")
                direct_url = TEMPFILE_DIRECT_URL.format(file_id=file_id)
                self._verify_public_video(direct_url, expected_size=size)
                return UploadedObject(object_key=file_id, signed_url=direct_url)
            except Exception as exc:
                last_error = str(exc)
                if attempt < max(1, attempts):
                    time.sleep(attempt * 2)
        raise WorkflowError(f"免费临时视频上传失败（已重试）：{last_error}")

    def upload_file(
        self,
        path: str | Path,
        *,
        expires_hours: int = 6,
        attempts: int = 5,
        maximum_bytes: int = 30 * 1024 * 1024,
    ) -> UploadedObject:
        """Upload a short-lived public image/file for Ark asset ingestion."""

        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise WorkflowError(f"找不到待上传素材：{source}")
        size = source.stat().st_size
        if size <= 0:
            raise WorkflowError("待上传素材为空。")
        if size > maximum_bytes:
            raise WorkflowError(
                f"临时素材不能超过 {maximum_bytes / (1024 * 1024):.0f} MB："
                f"当前 {size / (1024 * 1024):.1f} MB。"
            )
        if expires_hours not in {1, 6, 24, 48}:
            raise WorkflowError("临时素材有效期只支持 1、6、24 或 48 小时。")

        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.") or "character.png"
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        last_error = ""
        for attempt in range(1, max(1, attempts) + 1):
            try:
                with source.open("rb") as handle:
                    response = self.session.post(
                        TEMPFILE_UPLOAD_URL,
                        files={"files": (safe_name, handle, content_type)},
                        data={"expiryHours": str(expires_hours)},
                        timeout=(30, 180),
                    )
                response.raise_for_status()
                payload = response.json()
                files = payload.get("files") if isinstance(payload, dict) else None
                item = files[0] if isinstance(files, list) and files else {}
                file_id = str(item.get("id") or "").strip()
                remote_size = int(item.get("size") or 0)
                if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", file_id):
                    raise WorkflowError("限时临时图片通道没有返回有效文件 ID。")
                if remote_size and remote_size != size:
                    raise WorkflowError("限时临时图片通道返回的文件大小与本地不一致。")
                direct_url = TEMPFILE_DIRECT_URL.format(file_id=file_id)
                self._verify_public_file(
                    direct_url,
                    expected_size=size,
                    expected_content_type=content_type,
                )
                return UploadedObject(object_key=file_id, signed_url=direct_url)
            except Exception as exc:
                last_error = str(exc)
                if attempt < max(1, attempts):
                    time.sleep(attempt * 2)
        raise WorkflowError(f"限时临时图片上传失败（已重试）：{last_error}")

    def _verify_public_file(
        self,
        url: str,
        *,
        expected_size: int,
        expected_content_type: str,
    ) -> None:
        response = self.session.head(url, allow_redirects=True, timeout=(15, 45))
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        expected_family = expected_content_type.split("/", 1)[0].lower()
        if (
            expected_family
            and f"{expected_family}/" not in content_type
            and "application/octet-stream" not in content_type
        ):
            raise WorkflowError(f"临时图片直链返回了错误的内容类型：{content_type or '未知'}")
        remote_size = int(response.headers.get("Content-Length") or 0)
        if remote_size and remote_size != expected_size:
            raise WorkflowError("临时图片直链文件长度与本地不一致。")

    def _verify_public_video(self, url: str, *, expected_size: int) -> None:
        response = self.session.head(url, allow_redirects=True, timeout=(15, 45))
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "video/" not in content_type and "application/octet-stream" not in content_type:
            raise WorkflowError(f"临时直链返回了错误的内容类型：{content_type or '未知'}")
        remote_size = int(response.headers.get("Content-Length") or 0)
        if remote_size and remote_size != expected_size:
            raise WorkflowError("临时直链文件长度与本地视频不一致。")

    def delete(self, file_id: str) -> None:
        identifier = file_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,80}", identifier):
            raise WorkflowError("临时视频文件 ID 无效，无法安全删除。")
        try:
            response = self.session.delete(
                TEMPFILE_DELETE_URL.format(file_id=identifier),
                timeout=(15, 45),
            )
            if response.status_code not in {200, 404}:
                response.raise_for_status()
        except Exception as exc:
            raise WorkflowError(f"删除免费临时视频失败：{exc}") from exc


def download_file(
    url: str,
    destination: str | Path,
    *,
    on_progress: Callable[[int, int], None] | None = None,
    on_retry: Callable[[int, int, str], None] | None = None,
    attempts: int = 5,
) -> Path:
    """Download a remote artifact with safe GET retries and byte-range resume.

    Some Ark TOS result links occasionally close a TLS stream early on Windows.
    Requests keeps the partial file for ordinary retries; when all of those fail,
    curl gets one independent HTTP/1.1 resume attempt before the download is
    reported as failed.  This is still a read-only GET and never resubmits the
    paid generation task.
    """
    target = Path(destination).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    maximum_attempts = max(1, int(attempts))
    last_error: Exception | None = None
    for attempt in range(1, maximum_attempts + 1):
        existing = partial.stat().st_size if partial.is_file() else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        try:
            with requests.get(
                url,
                stream=True,
                timeout=(30, 180),
                headers=headers or None,
            ) as response:
                if response.status_code == 416 and existing:
                    content_range = str(response.headers.get("Content-Range") or "")
                    complete_match = re.fullmatch(r"bytes \*/(\d+)", content_range)
                    if complete_match and int(complete_match.group(1)) == existing:
                        partial.replace(target)
                        return target
                response.raise_for_status()
                resume = existing > 0 and response.status_code == 206
                content_length = int(response.headers.get("Content-Length", "0") or 0)
                if resume:
                    content_range = str(response.headers.get("Content-Range") or "")
                    range_match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range)
                    if range_match and int(range_match.group(1)) != existing:
                        partial.unlink(missing_ok=True)
                        raise requests.ConnectionError("远端返回的断点位置与本地临时文件不一致。")
                    total = (
                        int(range_match.group(3))
                        if range_match and range_match.group(3) != "*"
                        else existing + content_length
                    )
                    mode = "ab"
                    downloaded = existing
                else:
                    total = content_length
                    mode = "wb"
                    downloaded = 0
                with partial.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if on_progress:
                            on_progress(downloaded, total)
                final_size = partial.stat().st_size
                if total > 0 and final_size < total:
                    raise requests.ConnectionError(
                        f"下载提前结束：已收到 {final_size} 字节，应为 {total} 字节。"
                    )
            partial.replace(target)
            return target
        except Exception as exc:
            last_error = exc
            if attempt >= maximum_attempts:
                break
            if on_retry:
                on_retry(attempt + 1, maximum_attempts, str(exc))
            time.sleep(min(8.0, float(2 ** (attempt - 1))))
    curl_error = ""
    try:
        curl_command = [
            "curl.exe",
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--http1.1",
            "--retry",
            "8",
            "--retry-all-errors",
            "--retry-delay",
            "2",
            "--retry-max-time",
            "420",
            "--connect-timeout",
            "30",
            "--max-time",
            "1200",
        ]
        if partial.is_file() and partial.stat().st_size > 0:
            curl_command.extend(["--continue-at", "-"])
        curl_command.extend(["--output", str(partial), url])
        result = subprocess.run(
            curl_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=1260,
        )
        if result.returncode == 0 and partial.is_file() and partial.stat().st_size > 0:
            final_size = partial.stat().st_size
            partial.replace(target)
            if on_progress:
                on_progress(final_size, final_size)
            return target
        curl_error = (result.stderr or result.stdout or f"curl exit {result.returncode}").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        curl_error = str(exc)
    detail = str(last_error or "未知网络错误")
    if curl_error:
        detail = f"{detail}；备用下载通道：{curl_error}"
    raise WorkflowError(
        f"远端文件下载失败，已自动重试 {maximum_attempts} 次并尝试备用断点通道；"
        f"本地断点文件已保留。详情：{detail}"
    ) from last_error


def atomic_write_text(path: Path, text: str) -> Path:
    """Publish one complete file without sharing staging files across writers."""
    temporary: Path | None = None
    try:
        # Keep staging on the target filesystem so replacement remains atomic.
        # Each caller owns its temporary file, including across processes.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(10):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                # Windows can briefly lock the destination during concurrent reads/replacements.
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 9:
                    raise
                time.sleep(0.01 * (attempt + 1))
        return path
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_job_record(run_dir: Path, record: dict[str, Any]) -> Path:
    safe_record = dict(record)
    safe_record.pop("api_key", None)
    safe_record.pop("signed_url", None)
    path = run_dir / "job.json"
    return atomic_write_text(
        path,
        json.dumps(safe_record, ensure_ascii=False, indent=2, default=str),
    )


def video_info_dict(info: VideoInfo) -> dict[str, Any]:
    return asdict(info)
