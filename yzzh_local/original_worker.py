"""Private, account-scoped process for the original Flask workflows.

Never run web_app's public CLI. All HTTP ingress needs the worker secret;
all provider mutations wait for a separate human decision in the companion.
"""
from __future__ import annotations

import contextvars
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests
from hybrid_shared import HybridError
from yzzh_local.media import TEMP_UPLOAD_URL, TEMP_DESTINATION, TEMP_NOTICE, upload_mode, validate_temporary_url, upload_response_url

CURRENT = contextvars.ContextVar("original_operation", default="")
SOURCE = Path(__file__).resolve().parents[1]
READ_ASSET_ACTIONS = {"ListAssets", "GetAsset", "ListAssetGroups", "GetAssetGroup"}


def destination(url, bucket):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("插件只允许已支持的 HTTPS 素材与生成服务。")
    if url == TEMP_UPLOAD_URL:
        return TEMP_DESTINATION
    if host == "litter.catbox.moe":
        validate_temporary_url(url)
        return "Litterbox 素材读取"
    if host == "ark.cn-beijing.volces.com":
        return "火山方舟"
    if host == "open.volcengineapi.com":
        return "火山人物库"
    if host.endswith(".tos-cn-beijing.volces.com"):
        return "客户北京 TOS" if host == bucket + ".tos-cn-beijing.volces.com" else "供应商结果 TOS"
    raise ValueError("此网络目的地尚未获得插件支持；不会自动改用其他存储或公网隧道。")


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: redact(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[凭据已隐藏]")
        value = re.sub(r"https?://[^\s<>\"']+", lambda m: "[临时素材链接已隐藏]" if urlsplit(m.group(0)).hostname == "litter.catbox.moe" else m.group(0).split("?", 1)[0] + ("?[签名已隐藏]" if "?" in m.group(0) else ""), value)
    return value


class RemoteImageError(ValueError):
    """Only fixed, public messages may cross the private image proxy."""
    def __init__(self, code, message, status):
        super().__init__(message)
        self.code, self.status = code, status

    def public(self):
        return {"error": str(self), "code": self.code, "action": "refresh_character_library"}


class RemoteImages:
    """Private URL registry: signed character thumbnails never reach the UI."""
    def __init__(self, bucket, platform_urls=None):
        self.bucket, self.urls, self.ids = bucket, {}, {}
        self.platform_urls = platform_urls if platform_urls is not None else set()

    def register(self, value):
        if isinstance(value, list):
            return [self.register(v) for v in value]
        if not isinstance(value, dict):
            return value
        result = {k: self.register(v) for k, v in value.items()}
        if str(result.get("uri", "")).startswith("asset://") and result.get("url"):
            url = result["url"]
            try:
                if url not in self.platform_urls and "TOS" not in destination(url, self.bucket):
                    raise ValueError()
                if url not in self.ids:
                    if len(self.urls) >= 5000:
                        raise ValueError()
                    key = uuid.uuid4().hex
                    self.ids[url], self.urls[key] = key, url
                result["url"] = "/api/plugin-remote/" + self.ids[url]
            except (ValueError, TypeError, HybridError):
                result["url"] = ""
                result["preview_error"] = "此角色图片域名尚未支持安全预览。"
        return result

    def read(self, key):
        url = self.urls.get(key)
        if not url:
            raise RemoteImageError("PREVIEW_EXPIRED", "角色预览链接已失效，请刷新角色库。", 410)
        try:
            with requests.get(url, timeout=(5, 30), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise RemoteImageError("PREVIEW_UPSTREAM_ERROR", "角色图片服务暂时无法提供预览，请刷新角色库后重试。", 502)
                content = bytearray()
                for chunk in response.iter_content(128 * 1024):
                    content.extend(chunk)
                    if len(content) > 20 * 1024 * 1024:
                        raise RemoteImageError("PREVIEW_TOO_LARGE", "角色图片超过安全预览大小，暂时无法显示。", 413)
        except requests.Timeout:
            raise RemoteImageError("PREVIEW_TIMEOUT", "角色图片读取超时，请稍后刷新角色库。", 504) from None
        except requests.RequestException:
            raise RemoteImageError("PREVIEW_NETWORK_ERROR", "角色图片连接失败，请检查网络后刷新角色库。", 502) from None
        import cv2
        import numpy as np
        try:
            pixels = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
            if pixels is None or pixels.shape[0] * pixels.shape[1] > 25_000_000:
                raise RemoteImageError("PREVIEW_DECODE_ERROR", "角色图片格式无法安全预览，请刷新角色库或选择其他角色。", 422)
            ok, image = cv2.imencode(".png", pixels)
            if not ok:
                raise RemoteImageError("PREVIEW_DECODE_ERROR", "角色图片格式无法安全预览，请刷新角色库或选择其他角色。", 422)
        except cv2.error:
            raise RemoteImageError("PREVIEW_DECODE_ERROR", "角色图片格式无法安全预览，请刷新角色库或选择其他角色。", 422) from None
        return image.tobytes()


class NetworkGuard:
    def __init__(self, config, raw_send=None):
        self.config = config
        self.platform_urls = set()
        self.raw_send = raw_send or requests.Session.send
        self.control = requests.Session()
        self.control.trust_env = False
        self.control.send = types.MethodType(self.raw_send, self.control)

    def callback(self, event, **values):
        operation = CURRENT.get()
        if not operation:
            raise RuntimeError("此后台请求没有授权上下文，已阻止外发。请从原页面重新操作。")
        response = self.control.post(self.config["callback"], json={"operation": operation, "event": event, **values},
            headers={"X-Engine-Key": self.config["key"]}, timeout=(3, 35), allow_redirects=False)
        if response.status_code != 200:
            # Deliberately don't forward request/response text, signed URLs or keys.
            try:
                code = response.json().get("error", "AUTHORIZATION_UNAVAILABLE")
            except ValueError:
                code = "AUTHORIZATION_UNAVAILABLE"
            if code == "PREVIOUS_UPLOAD_UNCERTAIN":
                raise RuntimeError("上一次临时素材上传的回执未确认，素材可能已上传。新的云端请求已暂停；请查看确认面板中的上传记录，不会自动重传。")
            raise RuntimeError("插件授权暂停：" + str(code))
        return response.json()

    def send(self, session, prepared, **kwargs):
        bucket = self.config["values"].get("TOS_BUCKET", "")
        is_platform = "__platform__" in self.config["values"]
        if is_platform and prepared.method not in {"GET", "HEAD"}:
            raise RuntimeError("平台模式的云端操作必须通过已确认的平台接口，不能直连供应商。")
        if is_platform and prepared.url in self.platform_urls:
            if any(key.lower() in {"authorization", "cookie", "x-api-key"} for key in prepared.headers):
                raise RuntimeError("素材读取不能携带账号或模型凭据。")
            target = "平台已登记素材"
        else:
            target = destination(prepared.url, bucket)
        if target.startswith("Litterbox"):
            if upload_mode(self.config["values"]) != "temporary":
                raise RuntimeError("当前没有选择临时素材托管，已阻止上传。")
            if any(k.lower() in {"authorization", "cookie", "x-api-key"} for k in prepared.headers):
                raise RuntimeError("临时素材服务不能接收账号或模型凭据。")
            if (target == TEMP_DESTINATION and prepared.method != "POST") or (target != TEMP_DESTINATION and prepared.method not in {"GET", "HEAD"}):
                raise RuntimeError("不允许此临时素材请求。")
        action = parse_qs(urlsplit(prepared.url).query).get("Action", [""])[0]
        mutating = prepared.method not in {"GET", "HEAD"} and not (target == "火山人物库" and action in READ_ASSET_ACTIONS)
        if target == "供应商结果 TOS" and mutating:
            raise RuntimeError("禁止向其他账号的 TOS 桶写入。")
        # Follow no redirects: a trusted provider may return an untrusted URL.
        kwargs["allow_redirects"] = False
        if not mutating:
            self.callback("read")
            return self.raw_send(session, prepared, **kwargs)
        # Freeze the exact outgoing bytes before asking the user. This also
        # handles TOS's streaming reader without keeping whole videos in RAM.
        frozen = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
        digest, size = hashlib.sha256(), 0
        body = prepared.body
        reader = body if hasattr(body, "read") else io.BytesIO(body.encode() if isinstance(body, str) else bytes(body or b""))
        while True:
            chunk = reader.read(128 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, bytes):
                frozen.close()
                raise RuntimeError("不支持的上传正文格式。")
            size += len(chunk)
            if size > 450 * 1024 * 1024:
                frozen.close()
                raise RuntimeError("上传素材超过插件大小限制。")
            digest.update(chunk)
            frozen.write(chunk)
        frozen.seek(0)
        summary = {"destination": target, "method": prepared.method,
                   "endpoint": urlsplit(prepared.url).path, "action": action, "bytes": size,
                   "sha256": digest.hexdigest(), "stage": threading.current_thread().name,
                   "cost": "由你的供应商账号支付。无可靠价格估算；不扣 CZMIYOU 余额。此确认只允许本次请求。"}
        if target == TEMP_DESTINATION:
            summary.update(purpose="自动上传所选图片或参考视频，供后续审核或生成读取", privacy=TEMP_NOTICE,
                           cost="此步只上传素材，不创建付费生成任务；稍后的模型调用单独确认。")
        if target == "火山方舟" and size < 16 * 1024 * 1024:
            try:
                payload = json.load(frozen)
                summary["parameters"] = {k: payload[k] for k in ("model", "duration", "resolution", "ratio", "prompt", "generate_audio") if k in payload}
                content = payload.get("content", [])
                if isinstance(content, list):
                    summary["references"] = [x.get("type") for x in content if isinstance(x, dict)]
                    summary["text"] = "\n".join(str(x.get("text", "")) for x in content if isinstance(x, dict))[:4000]
            except (ValueError, TypeError):
                pass
        frozen.seek(0)
        summary = redact(summary, [self.config["values"].get(k, "") for k in ("ARK_API_KEY", "TOS_ACCESS_KEY", "TOS_SECRET_KEY")])
        try:
            network_id = self.callback("request", summary=summary)["id"]
            deadline = time.monotonic() + 610
            while time.monotonic() < deadline:
                state = self.callback("take", id=network_id)["state"]
                if state == "send_once":
                    break
                if state not in {"awaiting_approval", "approved"}:
                    raise RuntimeError("用户未批准此请求；没有发送。")
                time.sleep(1)
            else:
                raise RuntimeError("网络确认已超时；没有发送。")
            # The PreparedRequest has stable URL/headers; callers have no reference
            # to this private frozen stream while the approval is pending.
            prepared.body = frozen
            prepared.headers["Content-Length"] = str(size)
            prepared.headers.pop("Transfer-Encoding", None)
            result, ok, task_id = None, False, ""
            try:
                result = self.raw_send(session, prepared, **kwargs)
                ok = 400 <= result.status_code < 500 and result.status_code not in {408, 429}
                if 200 <= result.status_code < 300 and target == "火山方舟":
                    parsed = result.json()
                    if isinstance(parsed, dict) and urlsplit(prepared.url).path.endswith("/contents/generations/tasks"):
                        task_id = parsed.get("id", "")
                        ok = isinstance(task_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id))
                    elif isinstance(parsed, dict):
                        endpoint = urlsplit(prepared.url).path
                        if endpoint.endswith("/images/generations"):
                            data = parsed.get("data")
                            ok = (isinstance(data, list) and bool(data) and isinstance(data[0], dict)
                                  and isinstance(data[0].get("url"), str) and data[0]["url"].startswith("https://"))
                        elif endpoint.endswith("/responses"):
                            from performance_analysis import _response_text
                            ok = bool(_response_text(parsed).strip())
                elif 200 <= result.status_code < 300 and target == "火山人物库":
                    parsed = result.json()
                    if isinstance(parsed, dict):
                        metadata = parsed.get("ResponseMetadata")
                        payload = parsed.get("Result")
                        if isinstance(metadata, dict) and isinstance(metadata.get("Error"), dict) and metadata["Error"]:
                            ok = True  # Explicit provider rejection, not an unknown acceptance.
                        elif action in {"CreateAsset", "CreateAssetGroup"}:
                            payload = payload if isinstance(payload, dict) else parsed
                            prefix = "asset" if action == "CreateAsset" else "group"
                            reference = payload.get("Id") or payload.get("GroupId") or payload.get("GroupID") or ""
                            ok = isinstance(reference, str) and bool(re.fullmatch(prefix + r"-[A-Za-z0-9_-]{6,120}", reference))
                            task_id = reference if ok else ""
                        else:
                            ok = isinstance(metadata, dict) and isinstance(metadata.get("RequestId"), str) and bool(metadata["RequestId"])
                elif 200 <= result.status_code < 300 and "TOS" in target:
                    ok = True
                elif 200 <= result.status_code < 300 and target == TEMP_DESTINATION:
                    upload_response_url(result)
                    ok = True
            finally:
                self.callback("finish", id=network_id, ok=ok, task_id=task_id)
            return result
        finally:
            frozen.close()


def isolated_session_init(session_init):
    def private_session(session, *args, **kwargs):
        session_init(session, *args, **kwargs)
        # Disable ambient proxy/netrc authentication BEFORE prepare_request.
        session.trust_env = False
    return private_session


def local_readiness(core, face, cache, *, force=False, busy=False):
    """Check installed basics only; never download models or inspect user media."""
    deferred = {"ready": False, "status": "busy", "checks": [],
                "message": "本地任务正在运行或自检中，请完成后点击重新检查。"}
    lock = cache.setdefault("lock", threading.Lock())
    if busy or not lock.acquire(blocking=False):
        return deferred
    try:
        face_path = SOURCE / "models" / "face-detector" / "face_detection_yunet_2023mar.onnx"
        depth_path = SOURCE / "models" / "depth-anything-v2-small" / "model_fp16.onnx"
        try:
            ffmpeg = core.resolve_ffmpeg()
        except Exception:
            ffmpeg = None

        def fingerprint(path):
            try:
                stat = path.stat()
                return (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            except (OSError, AttributeError):
                return (str(path), None)

        signature = tuple(fingerprint(path) for path in (face_path, depth_path, ffmpeg))
        if not force and cache.get("signature") == signature:
            return {**cache["result"], "cached": True}
        checks = []
        for name, label, model, validate in (
            ("face", "人脸打码", face_path, face._valid_face_model),
            ("depth", "深度处理", depth_path, core._valid_depth_model),
        ):
            check = {"id": name, "label": label, "ready": False}
            try:
                if not validate(model):
                    check["message"] = "基础模型缺失或损坏，请让 Codex 修复基础模型后重新检查。"
                else:
                    import numpy as np
                    frame = np.zeros((240, 320, 3), dtype=np.uint8)
                    frame[:, :, 0] = np.arange(320, dtype=np.uint16).astype(np.uint8)
                    if name == "face":
                        result = face.YuNetFaceDetector(model).detect(frame)
                        if not isinstance(result, list) or not np.isfinite(result).all():
                            raise ValueError("invalid local inference")
                    else:
                        from depth_video import DepthRenderer
                        result = DepthRenderer(model, faithful_depth=True).render(frame, use_temporal=False)
                        if result.shape != frame.shape[:2] or not np.isfinite(result).all():
                            raise ValueError("invalid local inference")
                    check.update(ready=True, message="模型校验、加载与合成画面处理通过。")
            except Exception:
                check["message"] = "模型暂时无法运行，请让 Codex 修复本地运行环境后重新检查。"
            checks.append(check)
        video = {"id": "ffmpeg", "label": "视频工具", "ready": False}
        try:
            import subprocess
            if ffmpeg is None:
                raise ValueError("missing local executable")
            result = subprocess.run([str(ffmpeg), "-version"], capture_output=True, timeout=5, check=True)
            if not result.stdout.startswith(b"ffmpeg version "):
                raise ValueError("invalid local executable")
            video.update(ready=True, message="FFmpeg 启动检查通过。")
        except Exception:
            video["message"] = "视频工具缺失或无法启动，请让 Codex 修复插件运行环境。"
        checks.append(video)
        passed = all(check["ready"] for check in checks)
        result = {"ready": passed, "status": "ready" if passed else "incomplete", "checks": checks,
                  "cached": False, "message": "人脸打码、深度处理、视频工具已就绪。" if passed else "本地基础功能尚未就绪，请按下方提示修复。"}
        # Do not mark a concurrently replaced file ready using its old result.
        if signature != tuple(fingerprint(path) for path in (face_path, depth_path, ffmpeg)):
            return {"ready": False, "status": "incomplete", "checks": [], "cached": False,
                    "message": "本地模型或视频工具在检查期间发生变化，请重新检查。"}
        cache.update(signature=signature, result=result)
        return result
    finally:
        lock.release()


def configure(config):
    os.umask(0o077)
    requests.Session.__init__ = isolated_session_init(requests.Session.__init__)
    root = Path(config["root"]).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key in list(os.environ):
        if key.startswith(("ARK_", "TOS_", "VOLCENGINE_", "DEPTHFLOW_", "SEEDANCE_")):
            os.environ.pop(key)
    values = config["values"]
    for key in ("ARK_API_KEY", "TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_BUCKET", "ARK_IMAGE_MODEL", "ARK_PERFORMANCE_MODEL"):
        if values.get(key):
            os.environ[key] = values[key]
    if values.get("ARK_MODEL"):
        os.environ["ARK_VIDEO_MODEL"] = values["ARK_MODEL"]
    if isinstance(values.get("__platform__"), dict):
        models = values["__platform__"].get("models", {})
        for field, variable in (("white", "ARK_VIDEO_MODEL"), ("image", "ARK_IMAGE_MODEL"), ("analysis", "ARK_PERFORMANCE_MODEL")):
            model = models.get(field)
            if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", model):
                os.environ[variable] = model
    # Imports are ordered intentionally: account data roots are bound before
    # the original app computes its workspace/cache/profile paths.
    import workflow_core as core
    core.PROJECT_DIR, core.RUNS_DIR = root, root / "runs"
    core.load_env_file = lambda *args, **kwargs: {}
    import web_app as web
    web.WEB_DIR = SOURCE / "web"
    web.app.static_folder = str(web.WEB_DIR)
    web.MAX_AUTOMATIC_COMPOSITION_RETRIES = 0
    web.MAX_REAL_AUTOMATIC_COMPOSITION_RETRIES = 0
    web.MAX_REAL_AUTOMATIC_WHITE_MODEL_RETRIES = 0

    def unsupported(*args, **kwargs):
        raise core.WorkflowError("插件使用已选择的自动上传与 API 通道；不自动开启公网隧道或浏览器代操作。")
    core.TemporaryPublicTunnel.start = unsupported
    from yzzh_local.original_media import install_media, upload_log
    platform_capabilities = values.get("__platform__")
    media_mode = "platform" if isinstance(platform_capabilities, dict) else install_media(web, core, values)
    web.SEEDANCE_WEB.launch = unsupported
    web.SEEDANCE_WEB.generate = unsupported
    # Prevent implicit model downloads. Source installs with explicit existing
    # weights continue to use those files; the light customer ZIP has none.
    import face_mosaic
    face_mosaic.FACE_MODEL_PATH = SOURCE / "models" / "face-detector" / "face_detection_yunet_2023mar.onnx"
    original_face = face_mosaic.ensure_face_model
    def existing_face(*args, **kwargs):
        if not face_mosaic._valid_face_model(face_mosaic.FACE_MODEL_PATH):
            raise core.WorkflowError("尚未安装人脸模型，请通过插件安装向导明确安装后再打码；没有下载模型。")
        return original_face(*args, **kwargs)
    face_mosaic.ensure_face_model = existing_face
    web.ensure_face_model = existing_face
    original_depth = core.ensure_depth_model
    def existing_depth(*args, **kwargs):
        if not core._valid_depth_model(core.DEPTH_MODEL_PATH):
            raise core.WorkflowError("尚未安装深度模型，请先明确安装；没有下载模型或执行深度推理。")
        return original_depth(*args, **kwargs)
    core.ensure_depth_model = existing_depth
    if hasattr(web, "ensure_depth_model"):
        web.ensure_depth_model = existing_depth
    import long_video_core as long_core
    import subprocess
    original_run = subprocess.run
    def offline_audio_run(command, *args, **kwargs):
        if isinstance(command, list) and len(command) > 1 and Path(str(command[1])).name == "demucs_wav_separate.py":
            command = [command[0], str(Path(__file__).with_name("offline_audio.py")), *command[2:]]
        return original_run(command, *args, **kwargs)
    long_core.subprocess = types.SimpleNamespace(**{name: getattr(subprocess, name) for name in dir(subprocess) if not name.startswith("__")})
    long_core.subprocess.run = offline_audio_run

    secrets_to_hide = [values.get(k, "") for k in ("ARK_API_KEY", "TOS_ACCESS_KEY", "TOS_SECRET_KEY")]
    original_log, original_update = web.WebJob.log, web.WebJob.update
    web.WebJob.log = lambda job, message: original_log(job, redact(upload_log(message, media_mode), secrets_to_hide))
    web.WebJob.update = lambda job, **changes: original_update(job, **{k: redact(upload_log(v, media_mode) if k == "stage" else v, secrets_to_hide) if k in {"error", "stage"} else v for k, v in changes.items()})

    active, active_lock = set(), threading.Lock()
    class BoundThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            self.bound = contextvars.copy_context()
            super().__init__(*args, **kwargs)
        def start(self):
            with active_lock:
                active.add(self)
            try:
                return super().start()
            except Exception:
                with active_lock:
                    active.discard(self)
                raise
        def run(self):
            try:
                self.bound.run(super().run)
            finally:
                with active_lock:
                    active.discard(self)
    web.threading = types.SimpleNamespace(**{name: getattr(threading, name) for name in dir(threading) if not name.startswith("__")})
    web.threading.Thread = BoundThread

    guard = NetworkGuard(config)
    if media_mode == "platform":
        from yzzh_local.platform import PlatformTransport, install_platform
        from yzzh_local.platform_bridge import WorkerPlatformCallbacks
        def register_platform_media(result):
            if isinstance(result, list):
                for item in result:
                    register_platform_media(item)
            elif isinstance(result, dict):
                for name, value in result.items():
                    if name in {"url", "signed_url", "video_url", "URL", "Url", "ImageUrl", "CoverUrl", "ThumbnailUrl", "MediaUrl"} and isinstance(value, str):
                        try:
                            parsed = urlsplit(value)
                            if (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                                    and parsed.port in {None, 443} and len(guard.platform_urls) < 5000):
                                guard.platform_urls.add(value)
                        except ValueError:
                            pass
                    else:
                        register_platform_media(value)
        callbacks = WorkerPlatformCallbacks(guard, CURRENT, register_platform_media)
        install_platform(web, core, PlatformTransport(callbacks.rpc, callbacks.upload), platform_capabilities)
    requests.Session.send = lambda session, prepared, **kwargs: guard.send(session, prepared, **kwargs)
    from flask import request, jsonify, Response
    remote_images = RemoteImages(values.get("TOS_BUCKET", ""), guard.platform_urls)
    @web.app.before_request
    def private_ingress():
        import hmac
        if not hmac.compare_digest(request.headers.get("X-Engine-Key", ""), config["key"]):
            return jsonify({"error": "PRIVATE_ENGINE"}), 403
        if not request.path.startswith(("/api/", "/_engine/")):
            return jsonify({"error": "PRIVATE_ENGINE"}), 404
        CURRENT.set(request.headers.get("X-Engine-Operation", ""))
        if request.path.startswith("/api/real-long-video/character-library/validation-sessions"):
            return jsonify({"error": "当前插件仅提供 AIGC 虚拟人像流程，不提供真人活体授权。请使用虚拟人像库；此请求没有发往供应商。"}), 409

    @web.app.get("/_engine/status")
    def engine_status():
        with active_lock:
            return jsonify({"busy": bool(active)})

    readiness_cache = {"lock": threading.Lock()}

    @web.app.get("/api/plugin-readiness")
    def plugin_readiness():
        with active_lock:
            busy = bool(active)
        return jsonify(local_readiness(core, face_mosaic, readiness_cache,
                                       force=request.args.get("recheck") == "1", busy=busy))

    @web.app.get("/api/plugin-remote/<key>")
    def remote_image(key):
        try:
            return Response(remote_images.read(key), mimetype="image/png")
        except RemoteImageError as error:
            response = jsonify(error.public())
            response.status_code = error.status
        except Exception:
            response = jsonify({"error": "角色预览暂时不可用，请刷新角色库。", "code": "PREVIEW_UNAVAILABLE", "action": "refresh_character_library"})
            response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response

    @web.app.after_request
    def safe_result(response):
        if response.is_json:
            data = response.get_json(silent=True)
            if isinstance(data, (dict, list)):
                if request.path == "/api/config" and isinstance(data, dict):
                    data.update(temporary_upload_ready=media_mode in {"temporary", "platform"}, temporary_tunnel_ready=False,
                                ark_assets_upload_ready=web.ark_assets_configured() and (media_mode in {"temporary", "platform"} or web.TosMediaStore.configured()),
                                plugin_upload_mode=media_mode, plugin_upload_notice=("米哟平台素材服务" if media_mode == "platform" else TEMP_NOTICE if media_mode == "temporary" else "自己的北京 TOS"),
                                plugin_interface="original", plugin_audio="optional_offline_extension")
                    if media_mode == "platform":
                        data.update(ark_ready=platform_capabilities.get("ready") is True,
                                    temporary_upload_ready=(platform_capabilities.get("ready") is True and platform_capabilities.get("capabilities", {}).get("media") is True),
                                    plugin_service_mode="platform")
                if "character-library" in request.path and isinstance(data, dict):
                    if "groups" in data and "assets" in data:
                        from yzzh_local.original_media import library_status
                        if media_mode == "platform":
                            from yzzh_local.platform import platform_library_status
                            data = platform_library_status(data)
                        else:
                            data = library_status(data, media_mode)
                    elif data.get("asset_id") and data.get("status") == "Processing" and media_mode == "temporary":
                        data["message"] = "人物素材已提交审核，状态变为 Active 后使用对应人物 ID；Litterbox 源文件申请保存 72 小时，插件不能延长或提前删除。"
                response.set_data(json.dumps(redact(remote_images.register(data), secrets_to_hide), ensure_ascii=False))
        return response
    return web.app


def main():
    config = json.loads(sys.stdin.readline(128 * 1024))
    app = configure(config)
    from waitress import create_server
    server = create_server(app, host="127.0.0.1", port=0, threads=8)
    print(json.dumps({"port": int(server.effective_port)}), flush=True)
    server.run()


if __name__ == "__main__":
    main()
