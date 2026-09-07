"""Server-side Ark/TOS adapter; no local ML dependencies, no paid POST retry."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests

from hybrid_shared import HybridError


class ArkProvider:
    def __init__(self, environ=None):
        self.env = environ if environ is not None else os.environ

    def _request(self, method, path, payload=None):
        key = self.env.get("ARK_API_KEY", "").strip()
        if not key:
            raise HybridError("PROVIDER_NOT_CONFIGURED", 503)
        response = requests.request(method, "https://ark.cn-beijing.volces.com/api/v3" + path,
                                    headers={"Authorization": "Bearer " + key}, json=payload,
                                    timeout=(15, 180), allow_redirects=False)
        if response.status_code != 200:
            raise HybridError("PROVIDER_REQUEST_REJECTED", 503)
        data = response.json()
        if not isinstance(data, dict):
            raise HybridError("PROVIDER_INVALID_RESPONSE", 503)
        return data

    def submit(self, manifest, files):
        import tos
        required = ["TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_BUCKET", "ARK_API_KEY"]
        if any(not self.env.get(k, "").strip() for k in required):
            raise HybridError("PROVIDER_NOT_CONFIGURED", 503)
        client = tos.TosClientV2(self.env["TOS_ACCESS_KEY"], self.env["TOS_SECRET_KEY"],
                                 self.env.get("TOS_ENDPOINT", "tos-cn-beijing.volces.com"),
                                 self.env.get("TOS_REGION", "cn-beijing"))
        bucket = self.env["TOS_BUCKET"]
        content = [{"type": "text", "text": manifest["prompt"]}]
        for asset in manifest["assets"]:
            with Path(files[asset["slot"]]).open("rb") as stream:
                png = stream.read(8) == b"\x89PNG\r\n\x1a\n"
            extension = ".mp4" if asset["kind"] == "video" else ".png" if png else ".jpg"
            key = "yzzh-plugin/" + uuid.uuid4().hex + extension
            client.put_object_from_file(bucket, key, str(files[asset["slot"]]))
            signed = client.pre_signed_url(tos.HttpMethodType.Http_Method_Get, bucket=bucket, key=key, expires=86400)
            kind = "video_url" if asset["kind"] == "video" else "image_url"
            content.append({"type": kind, kind: {"url": signed.signed_url}, "role": "reference_" + asset["kind"]})
        payload = {k: manifest[k] for k in ("model", "resolution", "ratio", "duration")}
        payload.update(content=content, generate_audio=True, watermark=False)
        data = self._request("POST", "/contents/generations/tasks", payload)
        task_id = data.get("id")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise HybridError("PROVIDER_SUBMIT_UNCERTAIN", 503)
        return task_id

    def query(self, task_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise HybridError("INVALID_PROVIDER_TASK")
        return self._request("GET", "/contents/generations/tasks/" + task_id)

    def download(self, task, target):
        url = (task.get("content") or {}).get("video_url", "")
        parts = urlsplit(url)
        # Server operator must explicitly permit the real provider output hosts.
        hosts = {h.strip() for h in self.env.get("YZZH_OUTPUT_HOSTS", "").split(",") if h.strip()}
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in {None, 443} or parts.hostname not in hosts:
            raise HybridError("OUTPUT_HOST_NOT_ALLOWED", 503)
        with requests.get(url, stream=True, timeout=(15, 180), allow_redirects=False) as response:
            if response.status_code != 200:
                raise HybridError("OUTPUT_DOWNLOAD_FAILED", 503)
            with Path(target).open("wb") as output:
                size = 0
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 1024 ** 3:
                        raise HybridError("OUTPUT_TOO_LARGE", 503)
                    output.write(chunk)
        with Path(target).open("rb") as stream:
            header = stream.read(32)
        if b"ftyp" not in header:
            raise HybridError("OUTPUT_INVALID_MP4", 503)
