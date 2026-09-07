"""Customer licensing over the existing account service; no signing/DB secrets."""
from urllib.parse import urlsplit

import requests

from hybrid_shared import HybridError, trusted_url

DEFAULT_LOGIN_URL = "https://mch39t7vkw.coze.site/api/auth/login"
LOGIN_ROLES = frozenset({"customer", "staff", "agent", "partner", "admin"})


def login_role(value):
    # The choice requests a role; only the account service can grant it.
    if not isinstance(value, str) or value not in LOGIN_ROLES:
        raise HybridError("INVALID_LOGIN_ROLE")
    return value


def login_rejection(data):
    # Never forward arbitrary account-service text (it may contain secrets).
    known = {"账号或密码错误": ("LOGIN_CREDENTIALS_INVALID", 401),
             "身份无效或未审批": ("LOGIN_ROLE_NOT_APPROVED", 403)}
    error = data.get("error") if isinstance(data, dict) else None
    code, status = known.get(error, ("LOGIN_REJECTED", 401)) if isinstance(error, str) else ("LOGIN_REJECTED", 401)
    return HybridError(code, status)


class AccountClient:
    def __init__(self, login_url=DEFAULT_LOGIN_URL, *, development=False):
        self.login_url = trusted_url(login_url, local=development)
        parts = urlsplit(self.login_url)
        if parts.path != "/api/auth/login":
            raise HybridError("INVALID_ACCOUNT_ENDPOINT")
        self.verify_url = self.login_url.removesuffix("/login") + "/verify-token"

    def _post(self, url, payload):
        try:
            with requests.post(url, json=payload, timeout=(10, 30), allow_redirects=False, stream=True) as response:
                status = response.status_code
                if status not in {200, 401, 403}:
                    raise HybridError("LICENSE_SERVICE_UNAVAILABLE", 503)
                body = bytearray()
                for chunk in response.iter_content(16384):
                    body.extend(chunk)
                    if len(body) > 128 * 1024:
                        raise HybridError("LICENSE_INVALID_RESPONSE", 503)
            import json
            try:
                result = json.loads(body)
            except (ValueError, UnicodeError):
                if status in {401, 403}:
                    raise HybridError("LOGIN_REJECTED", 401) from None
                raise HybridError("LICENSE_INVALID_RESPONSE", 503) from None
            if status in {401, 403}:
                raise login_rejection(result) if url == self.login_url else HybridError("LOGIN_REJECTED", 401)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (requests.RequestException, ValueError, TypeError):
            raise HybridError("LICENSE_SERVICE_UNAVAILABLE", 503) from None

    def login(self, username, password, role="customer"):
        role = login_role(role)
        data = self._post(self.login_url, {"username": username, "password": password, "role": role})
        token = data.get("token")
        if data.get("success") is not True:
            raise login_rejection(data)
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise HybridError("LICENSE_INVALID_RESPONSE", 503)
        return token

    def verify(self, token):
        data = self._post(self.verify_url, {"token": token})
        owner = data.get("userId")
        if data.get("valid") is not True or type(owner) is not int or owner <= 0:
            raise HybridError("LOGIN_REJECTED", 401)
        subscriptions = data.get("subscriptions")
        if not isinstance(subscriptions, list):
            raise HybridError("LICENSE_INVALID_RESPONSE", 503)
        # The server filters enabled products and current coverage, including
        # stacked time cards whose active tail starts in the future. Do not
        # duplicate that calculation using the customer's clock or balance.
        valid = [s for s in subscriptions if isinstance(s, dict) and type(s.get("productId")) is int
                 and s["productId"] == 4 and s.get("status") == "active"
                 and isinstance(s.get("endTime"), str) and s["endTime"]]
        name = data.get("username")
        return {"user_id": owner, "username": name[:100] if isinstance(name, str) else "CZMIYOU 客户",
                "product_id": 4, "licensed": bool(valid),
                "subscription_end": max(s["endTime"] for s in valid) if valid else None,
                "license_status": "active" if valid else "inactive"}
