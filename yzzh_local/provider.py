"""Customer-owned Ark/TOS adapter. Fixed destinations, no generation POST retry."""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

from hybrid_shared import HybridError
from yzzh_local.media import TemporaryMediaStore, TOS_FIELDS, upload_mode

ARK_BASE = "https://ark.cn-beijing.volces.com/api/v3"
TOS_ENDPOINT = "tos-cn-beijing.volces.com"


class ArkProvider:
    def __init__(self, values):
        self.values = dict(values)

    def preflight(self, manifest, files):
        required = ("ARK_API_KEY", "ARK_MODEL") + (TOS_FIELDS if upload_mode(self.values) == "tos" else ())
        if any(not self.values.get(k) for k in required):
            raise HybridError("PROVIDER_SETTINGS_REQUIRED", 409)
        if manifest["model"] != self.values["ARK_MODEL"]:
            raise HybridError("MODEL_SETTINGS_CHANGED_REPLAN", 409)
        from workflow_core import validate_seedance_reference_video
        for asset in manifest["assets"]:
            path = Path(files[asset["slot"]])
            if asset["kind"] == "video":
                try:
                    validate_seedance_reference_video(path)
                except Exception:
                    raise HybridError("REFERENCE_VIDEO_FORMAT_UNSUPPORTED", 400) from None
        if upload_mode(self.values) == "tos":
            try:
                import tos  # noqa: F401
            except ImportError:
                raise HybridError("TOS_DEPENDENCY_REQUIRED", 503) from None

    def _request(self, method, path, payload=None):
        key = self.values.get("ARK_API_KEY", "")
        if not key:
            raise HybridError("PROVIDER_SETTINGS_REQUIRED", 409)
        try:
            with requests.request(method, ARK_BASE + path, json=payload, headers={"Authorization": "Bearer " + key},
                                  timeout=(15, 180), allow_redirects=False, stream=True) as response:
                if response.status_code not in {200, 201}:
                    # Non-2xx POST is conservatively uncertain: do not assume a
                    # gateway error proves the vendor did not accept this task.
                    raise HybridError("PROVIDER_REQUEST_FAILED", 503)
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 2 * 1024 * 1024:
                        raise HybridError("PROVIDER_RESPONSE_TOO_LARGE", 503)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (requests.RequestException, ValueError, TypeError):
            raise HybridError("PROVIDER_CONNECTION_UNCERTAIN", 503) from None

    def submit(self, manifest, files, event):
        self.preflight(manifest, files)
        mode = upload_mode(self.values)
        if mode == "tos":
            import tos
            client = tos.TosClientV2(self.values["TOS_ACCESS_KEY"], self.values["TOS_SECRET_KEY"],
                                     "https://" + TOS_ENDPOINT, "cn-beijing")
            bucket = self.values["TOS_BUCKET"]
        else:
            temporary = TemporaryMediaStore()
        content = [{"type": "text", "text": manifest["prompt"]}]
        for asset in manifest["assets"]:
            path = Path(files[asset["slot"]])
            if mode == "tos":
                key = "yzzh-plugin/" + uuid.uuid4().hex + path.suffix.lower()
                event("UPLOAD_PLANNED", {"bucket": bucket, "key": key})
                client.put_object_from_file(bucket, key, str(path))
                signed = client.pre_signed_url(tos.HttpMethodType.Http_Method_Get, bucket=bucket, key=key, expires=86400)
                url = signed.signed_url
            elif asset["kind"] == "image":
                from workflow_core import image_to_data_url
                url = image_to_data_url(path)
            else:
                event("UPLOAD_PLANNED", {"service": "Litterbox", "slot": asset["slot"]})
                url = temporary.upload_video(path).signed_url
            kind = "video_url" if asset["kind"] == "video" else "image_url"
            content.append({"type": kind, kind: {"url": url}, "role": "reference_" + asset["kind"]})
        payload = {k: manifest[k] for k in ("model", "resolution", "ratio", "duration")}
        payload.update(content=content, generate_audio=True, watermark=False)
        event("PROVIDER_POST_STARTED", {})  # durable boundary BEFORE the paid request
        result = self._request("POST", "/contents/generations/tasks", payload)
        task_id = result.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise HybridError("PROVIDER_SUBMIT_UNCERTAIN", 503)
        return task_id

    def query(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise HybridError("INVALID_PROVIDER_TASK")
        return self._request("GET", "/contents/generations/tasks/" + task_id)

    def download(self, task, target):
        try:
            content = task.get("content")
            url = content.get("video_url") if isinstance(content, dict) else None
            if not isinstance(url, str):
                raise ValueError()
            parts = urlsplit(url)
            if (parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443}
                    or not parts.hostname or not parts.hostname.endswith("." + TOS_ENDPOINT)):
                raise ValueError()
        except (ValueError, TypeError):
            raise HybridError("OUTPUT_HOST_NOT_ALLOWED", 503) from None
        try:
            with requests.get(url, stream=True, timeout=(15, 180), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise HybridError("OUTPUT_DOWNLOAD_FAILED", 503)
                with Path(target).open("xb") as output:
                    size = 0
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > 1024 ** 3:
                            raise HybridError("OUTPUT_TOO_LARGE", 503)
                        output.write(chunk)
        except requests.RequestException:
            raise HybridError("OUTPUT_DOWNLOAD_FAILED", 503) from None
