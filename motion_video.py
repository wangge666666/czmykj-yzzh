"""Small, verified TOS inputs for motion transfer; original media stays intact."""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import time
from pathlib import Path

import requests

from workflow_core import WorkflowError


def compact_motion_video(source: Path, host, *, on_log=None) -> Path:
    source = Path(source)
    info = host.validate_seedance_reference_video(source)
    fingerprint = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    output = source.parent / f"motion_upload_v1_{fingerprint}.mp4"
    if output.is_file():
        try:
            cached = host.validate_seedance_reference_video(output)
            if abs(cached.duration - info.duration) < .08 and cached.frame_count == info.frame_count:
                return output
        except WorkflowError:
            pass
    # Stay above Ark's minimum pixel count without changing timing or cropping.
    scale = min(1.0, math.sqrt(409_600 / (info.width * info.height)))
    width = int(math.ceil(info.width * scale / 2)) * 2
    height = int(math.ceil(info.height * scale / 2)) * 2
    temporary = output.with_suffix(".part.mp4")
    if on_log:
        on_log(f"正在制作动作传输副本：{info.width}×{info.height} → {width}×{height}，保留全部 {info.frame_count} 帧与原时长。")
    try:
        result = subprocess.run([
            str(host.resolve_ffmpeg()), "-y", "-i", str(source), "-map", "0:v:0",
            "-vf", f"scale={width}:{height}:flags=lanczos,setsar=1", "-fps_mode", "passthrough",
            "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-maxrate", "1600k", "-bufsize", "3200k",
            "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(temporary),
        ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            raise WorkflowError("动作传输副本转码失败：" + result.stderr[-500:])
        checked = host.validate_seedance_reference_video(temporary)
        if abs(checked.duration - info.duration) >= .08 or checked.frame_count != info.frame_count:
            raise WorkflowError("传输副本的帧数或时长与原视频不一致，已停止提交。")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    if on_log:
        on_log(f"传输副本已就绪：{info.size_bytes / 1048576:.2f} MB → {output.stat().st_size / 1048576:.2f} MB；原始视频保持不变。")
    return output


def verify_motion_video_download(url: str, source: Path) -> None:
    """Read the actual GET-signed object, not HEAD; validate complete file bytes."""
    expected_size = source.stat().st_size
    expected_hash = hashlib.sha256(source.read_bytes()).digest()
    last_error = ""
    for attempt in range(2):
        try:
            digest = hashlib.sha256()
            received = 0
            started = time.monotonic()
            with requests.get(url, stream=True, timeout=(10, 20), headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise WorkflowError(f"TOS 完整下载检查返回 HTTP {response.status_code}。")
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() - started > 45:
                        raise WorkflowError("TOS 完整下载检查超过 45 秒。")
                    received += len(chunk)
                    if received > expected_size:
                        raise WorkflowError("TOS 下载内容超出本地视频长度。")
                    digest.update(chunk)
            if received != expected_size or digest.digest() != expected_hash:
                raise WorkflowError("TOS 下载不完整或文件校验不一致。")
            return
        except requests.RequestException as exc:
            # Do not put the signed URL or its credentials into logs/errors.
            last_error = f"TOS 下载检查发生网络错误（{type(exc).__name__}）。"
        except WorkflowError as exc:
            last_error = str(exc)
    raise WorkflowError(last_error)


def prepare_motion_video_reference(source: Path, host, *, on_log=None):
    bucket = os.getenv("MOTION_TRANSFER_TOS_BUCKET", "").strip() or os.getenv("TOS_BUCKET", "").strip()
    if not bucket:
        raise WorkflowError("人物动作迁移缺少 TOS 存储桶配置：请设置 MOTION_TRANSFER_TOS_BUCKET 或 TOS_BUCKET。尚未提交生成任务。")
    compact = compact_motion_video(source, host, on_log=on_log)
    tos = host.TosMediaStore(bucket=bucket)
    uploaded = None
    try:
        if on_log:
            on_log("正在把动作传输副本上传到 TOS，并检查完整文件是否可下载。")
        uploaded = tos.upload_video(compact, expires=3600)
        verify_motion_video_download(uploaded.signed_url, compact)
        if on_log:
            on_log("TOS 完整下载检查通过，文件长度与 SHA-256 校验一致；即将提交生成任务。")
        return host.SeedanceVideoReferenceSource(url=uploaded.signed_url, channel="tos", tos=tos, object_key=uploaded.object_key)
    except Exception as exc:
        if uploaded is not None:
            try:
                tos.delete(uploaded.object_key)
            except Exception:
                pass
        detail = str(exc) if isinstance(exc, WorkflowError) else type(exc).__name__
        raise WorkflowError(f"人物动作迁移参考视频上传或下载检查失败，尚未提交生成任务：{detail}") from exc
