"""Authenticated platform client used only by the companion, never the worker."""
from __future__ import annotations

import copy
import json
import threading
import time
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import requests

from hybrid_shared import HybridError, trusted_url

# The existing creation-center entry in the CZMIYOU account portal.
DEFAULT_PLATFORM_URL = "https://7dqznbpct4.coze.site"
FEATURES = ("video", "image", "analysis", "assets", "media")
PROJECTS = ("wardrobe", "virtual", "real")


class _NoPlatformCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def empty_capabilities(code="PLATFORM_SERVICE_UNAVAILABLE"):
    return {"mode": "platform", "product_id": 4, "ready": False,
            "projects": list(PROJECTS), "capabilities": {key: False for key in FEATURES},
            "models": {}, "fields": {}, "configured": False,
            "message": "平台服务尚未就绪，请管理员检查服务部署和配置；用户无需填写 API Key。",
            "code": code}


class PlatformService:
    def __init__(self, url=DEFAULT_PLATFORM_URL, *, development=False, http=None, now=time.monotonic):
        self.url = trusted_url(url, local=development).rstrip("/")
        parts = urlsplit(self.url)
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise HybridError("INVALID_PLATFORM_ENDPOINT")
        self.http = http or requests.Session()
        self.http.trust_env = False
        if hasattr(self.http, "cookies"):
            self.http.cookies.clear()
            self.http.cookies.set_policy(_NoPlatformCookies())
        self.now = now
        self.cache = {}
        self.lock = threading.Lock()

    def _request(self, token, method, path, *, payload=None, files=None, data=None):
        if not token:
            raise HybridError("LOGIN_REQUIRED", 401)
        try:
            response = self.http.request(method, self.url + "/api/yzzh" + path,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                json=payload, files=files, data=data, timeout=(10, 240 if method == "POST" else 20),
                allow_redirects=False, stream=True)
            with response:
                body = bytearray()
                for block in response.iter_content(64 * 1024):
                    body.extend(block)
                    if len(body) > 24 * 1024 * 1024:
                        raise HybridError("PLATFORM_INVALID_RESPONSE", 502)
                try:
                    result = json.loads(body)
                except (ValueError, UnicodeError):
                    raise HybridError("PLATFORM_SERVICE_UNAVAILABLE", 503) from None
                if not isinstance(result, dict):
                    raise HybridError("PLATFORM_INVALID_RESPONSE", 502)
                if not 200 <= response.status_code < 300:
                    # The response may contain private provider errors: expose codes only.
                    code = result.get("code")
                    import re
                    if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", code):
                        code = "PLATFORM_REQUEST_REJECTED"
                    raise HybridError(code, response.status_code)
                return result
        except requests.RequestException:
            # No POST retry: the durable request ID is needed for reconciliation.
            raise HybridError("PLATFORM_CONNECTION_UNCERTAIN" if method == "POST" else "PLATFORM_SERVICE_UNAVAILABLE", 503) from None

    def capabilities(self, token, owner, session, *, force=False):
        key = (owner, session)
        with self.lock:
            saved = self.cache.get(key)
            if not force and saved and self.now() - saved[0] < 20:
                return copy.deepcopy(saved[1])
        try:
            response = self._request(token, "GET", "/capabilities")
            data = response.get("result", response)
            if not isinstance(data, dict) or data.get("mode") != "platform" or data.get("product_id") != 4:
                raise HybridError("PLATFORM_INVALID_RESPONSE", 502)
            result = empty_capabilities("")
            flags = data.get("capabilities", {})
            result["capabilities"] = {name: isinstance(flags, dict) and flags.get(name) is True for name in FEATURES}
            models = data.get("models", {})
            import re
            result["models"] = {name: value for name, value in models.items()
                if name in {"video", "white", "image", "analysis"} and isinstance(value, str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value)} if isinstance(models, dict) else {}
            projects = data.get("projects", [])
            result["ready"] = (data.get("ready") is True and all(result["capabilities"].values())
                               and isinstance(projects, list) and all(name in projects for name in PROJECTS))
            result["configured"] = result["ready"]
            result["licensed"] = data.get("licensed") is True
            balance = data.get("balance")
            if type(balance) in {int, float}:
                import math
                if math.isfinite(balance):
                    result["balance"] = balance
            if result["ready"]:
                result["message"] = "三个项目共用的平台服务已配置，费用按管理员设置从米哟账户结算。"
        except HybridError as error:
            result = empty_capabilities(error.code)
        with self.lock:
            # Old sessions must not accumulate tokens or public account metadata.
            self.cache = {key: (self.now(), copy.deepcopy(result))}
        return result

    def operation(self, token, envelope):
        data = self._request(token, "POST", "/operations", payload=envelope)
        if "result" not in data:
            raise HybridError("PLATFORM_INVALID_RESPONSE", 502)
        return data["result"]

    def upload(self, token, path, request_id, project, stage):
        mime = {".mp4": "video/mp4", ".mov": "video/quicktime", ".png": "image/png",
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower())
        if not mime:
            raise HybridError("INVALID_PLATFORM_MEDIA")
        with path.open("rb") as stream:
            data = self._request(token, "POST", "/media", files={"file": (path.name, stream, mime)},
                data={"request_id": request_id, "project": project, "stage": stage})
        if not isinstance(data.get("result"), dict):
            raise HybridError("PLATFORM_INVALID_RESPONSE", 502)
        return data["result"]

    def receipt(self, token, request_id):
        import re
        if not re.fullmatch(r"[a-f0-9]{32,64}", request_id):
            raise HybridError("INVALID_PLATFORM_REQUEST")
        return self._request(token, "GET", "/operations/" + request_id)
