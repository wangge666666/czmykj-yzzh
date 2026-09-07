"""Bind worker platform requests to the current user and a reviewed request."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from hybrid_shared import HybridError, digest, file_hash

READ_OPERATIONS = frozenset({"video.get", "video.list", "assets.ListAssets", "assets.GetAsset",
                             "assets.ListAssetGroups", "assets.GetAssetGroup"})
WRITE_OPERATIONS = frozenset({"video.create", "image.generate", "analysis.create", "media.upload",
    "assets.CreateAsset", "assets.DeleteAsset", "assets.CreateAssetGroup", "assets.UpdateAssetGroup", "assets.DeleteAssetGroup"})

PUBLIC_ERRORS = {
    "PLATFORM_SERVICE_UNAVAILABLE": "平台服务暂时不可用，请管理员检查部署；无需填写个人 API Key。",
    "PLATFORM_CONNECTION_UNCERTAIN": "平台请求回执尚未确认，已保留记录；请先核对已有任务，避免重复提交。",
    "PLATFORM_SETTINGS_MANAGED_BY_ADMIN": "服务配置由管理员统一管理，无需填写个人 API Key。",
    "ASSET_CONSENT_REQUIRED": "请在本次人物入库确认中勾选素材使用权与审核授权。",
    "INSUFFICIENT_BALANCE": "米哟账户余额不足，请充值后继续。",
    "MODEL_PRICING_NOT_CONFIGURED": "当前模型的中央价格尚未配置，请联系管理员。",
    "PRODUCT_4_LICENSE_REQUIRED": "当前账号没有有效的衣装智换使用权限，请联系管理员开通。",
}


def project_for(source, stage):
    if "wardrobe" in stage or "wardrobe" in source:
        return "wardrobe"
    if "real-long" in source or "real-long" in stage:
        return "real"
    if "long-video" in source or "long-" in stage:
        return "virtual"
    return "wardrobe"


def platform_request(bridge, data, supplied):
    if not isinstance(data, dict):
        raise HybridError("INVALID_PLATFORM_REQUEST")
    with bridge.runtime.lock:
        operation = bridge.operations.get(data.get("operation"))
        if (not operation or not isinstance(supplied, str)
                or not secrets.compare_digest(supplied, operation["worker_key"])):
            raise HybridError("INVALID_ENGINE_SESSION", 403)
        if getattr(bridge.runtime, "mode", "byok") != "platform":
            raise HybridError("PLATFORM_MODE_REQUIRED", 409)
        command, payload = data.get("command"), data.get("payload")
        if command not in READ_OPERATIONS | WRITE_OPERATIONS or not isinstance(payload, dict):
            raise HybridError("INVALID_PLATFORM_OPERATION")
        writable = command in WRITE_OPERATIONS
        owner = bridge.binding(operation, license=writable)
        if command in {"assets.CreateVisualValidateSession", "assets.GetVisualValidateResult"}:
            raise HybridError("AIGC_ASSETS_ONLY")
        token, session = bridge.runtime.token, operation["session"]
        stage = data.get("stage", "")
        if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", stage):
            raise HybridError("INVALID_PLATFORM_STAGE")
        item = None
        if writable:
            item = bridge.pending.get(data.get("id"))
            if (not item or item["operation"] != data["operation"] or item["state"] != "sending"
                    or item.get("service_mode") != "platform"):
                raise HybridError("NETWORK_APPROVAL_REQUIRED", 409)
            summary = item["summary"]
            if (summary.get("operation") != command or summary.get("stage") != stage
                    or summary.get("payload_sha256") != digest(payload)):
                raise HybridError("APPROVED_REQUEST_CHANGED", 409)
        request_id = hashlib.sha256((item["id"] if item else data["operation"]).encode()).hexdigest()
        project = project_for(operation["path"], stage)
        worker_root = Path(bridge.workers[owner]["root"]).resolve()

    # No account lock is held across network I/O; another tab can inspect state.
    if command == "media.upload":
        try:
            path = Path(payload.get("path", "")).resolve(strict=True)
            path.relative_to(worker_root)
            if not path.is_file() or path.suffix.lower() not in {".mp4", ".mov", ".png", ".jpg", ".jpeg", ".webp"}:
                raise ValueError()
            maximum = 200 * 1024 * 1024 if path.suffix.lower() in {".mp4", ".mov"} else 30 * 1024 * 1024
            if not 0 < path.stat().st_size <= maximum:
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise HybridError("INVALID_PLATFORM_MEDIA", 400) from None
        with tempfile.TemporaryDirectory(prefix="yzzh-approved-") as directory:
            snapshot = Path(directory) / ("media" + path.suffix.lower())
            with path.open("rb") as source, snapshot.open("wb") as target:
                copied = 0
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    copied += len(block)
                    if copied > maximum:
                        raise HybridError("APPROVED_MEDIA_CHANGED", 409)
                    target.write(block)
            if snapshot.stat().st_size != item["summary"].get("bytes") or file_hash(snapshot) != item["summary"].get("sha256"):
                raise HybridError("APPROVED_MEDIA_CHANGED", 409)
            result = bridge.runtime.platform.upload(token, snapshot, request_id, project, stage)
    else:
        if command == "assets.CreateAsset":
            if item.get("compliance_confirmed") is not True:
                raise HybridError("ASSET_CONSENT_REQUIRED", 409)
            payload = {**payload, "compliance_confirmed": True}
        result = bridge.runtime.platform.operation(token, {"request_id": request_id, "project": project,
            "stage": stage, "operation": command, "payload": payload})
    with bridge.runtime.lock:
        bridge.context(owner, session)
    return result


class WorkerPlatformCallbacks:
    """Worker has only its private companion credential, never an account token."""
    def __init__(self, guard, current, register_result=lambda value: None, now=time.monotonic, sleep=time.sleep):
        self.guard, self.current = guard, current
        self.register_result, self.now, self.sleep = register_result, now, sleep

    def rpc(self, command, payload):
        return self._call(command, payload)

    def upload(self, path):
        return self._call("media.upload", {"path": str(path)}, media=Path(path))

    def _call(self, command, payload, media=None):
        from workflow_core import WorkflowError
        if command not in READ_OPERATIONS | WRITE_OPERATIONS:
            raise WorkflowError("平台仅提供 AIGC 素材和已支持的创作操作。")
        stage = re.sub(r"[^A-Za-z0-9_.:-]", "_", threading.current_thread().name.lower())[:64] or "main"
        network_id = None
        if command in WRITE_OPERATIONS:
            summary = {"destination": "米哟平台", "operation": command, "kind": "upload" if media else "generation",
                       "stage": stage, "payload_sha256": digest(payload),
                       "cost": "由米哟账户按管理员配置的通道和价格计费，本次请求仅执行一次。"}
            if media:
                summary.update(bytes=media.stat().st_size, sha256=file_hash(media),
                    purpose="上传本次素材供三项目创作使用", cost="素材存入平台；此步不创建付费生成任务。")
            else:
                summary["parameters"] = {key: value for key, value in payload.items()
                    if key in {"model", "duration", "resolution", "ratio", "generate_audio", "Name", "GroupType"}}
                if command.startswith("assets."):
                    summary["kind"] = "asset"
            network_id = self.guard.callback("request", summary=summary)["id"]
            deadline = self.now() + 610
            while self.now() < deadline:
                decision = self.guard.callback("take", id=network_id)["state"]
                if decision == "send_once":
                    break
                if decision not in {"awaiting_approval", "approved"}:
                    raise WorkflowError("本次操作未获批准，没有发送至平台。")
                self.sleep(1)
            else:
                raise WorkflowError("操作确认已超时，没有发送至平台。")
        else:
            self.guard.callback("read")
        ok, task_id = False, ""
        try:
            url = self.guard.config["callback"].removesuffix("/engine") + "/platform"
            response = self.guard.control.post(url, json={"operation": self.current.get(), "id": network_id,
                "command": command, "payload": payload, "stage": stage},
                headers={"X-Engine-Key": self.guard.config["key"]}, timeout=(3, 270), allow_redirects=False)
            if not response.ok:
                ok = 400 <= response.status_code < 500 and response.status_code not in {408, 429}
                code = response.json().get("error", "PLATFORM_REQUEST_FAILED")
                raise WorkflowError(PUBLIC_ERRORS.get(code if isinstance(code, str) else "", "平台未能完成本次操作，请保留任务记录并联系管理员检查服务状态。"))
            result = response.json().get("result")
            if not isinstance(result, dict):
                raise WorkflowError("平台返回格式异常，请保留任务记录核对。")
            ok = True
            if command == "video.create":
                task_id = result.get("id", "")
                ok = isinstance(task_id, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id))
                if not ok:
                    raise WorkflowError("平台任务编号尚未确认，请保留记录核对；不会自动重新生成。")
            self.register_result(result)
            return result
        finally:
            if network_id:
                self.guard.callback("finish", id=network_id, ok=ok, task_id=task_id)
