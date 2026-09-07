"""One-use browser handoff from the configured CZMIYOU login origin.

The portal exchanges messages with its own popup using exact origin/source/state.
No account token appears in a URL, disk state or the desktop wake-up protocol.
"""
import re
import secrets
import time
from urllib.parse import urlsplit
from pathlib import Path

from flask import jsonify, render_template_string, request, send_from_directory

from hybrid_shared import HybridError

PUBLIC = {"/_plugin/launch", "/_plugin/launch.js", "/_plugin/launch/accept"}


def install_launcher(app, runtime, session_key, port):
    origin = f"http://127.0.0.1:{port}"
    login = urlsplit(runtime.login_url)
    login_origin = f"{login.scheme}://{login.netloc}"
    nonces = {}

    @app.get("/_plugin/launch")
    def launch_page():
        with runtime.lock:
            now = time.monotonic()
            for nonce, expires in list(nonces.items()):
                if expires <= now:
                    nonces.pop(nonce, None)
            if len(nonces) >= 32:
                raise HybridError("LAUNCH_TOO_MANY_ATTEMPTS", 429)
            nonce = secrets.token_urlsafe(32)
            nonces[nonce] = now + 60
        return render_template_string('''<!doctype html><html lang="zh-CN"><head>
          <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
          <meta name="yzzh-launch-nonce" content="{{ nonce }}">
          <meta name="yzzh-login-origin" content="{{ login_origin }}">
          <title>正在打开衣装智换</title><script defer src="/_plugin/launch.js"></script>
          </head><body><main><h1>正在打开衣装智换</h1>
          <p id="launch-status" role="status">正在连接 CZMIYOU 统一登录，请勿关闭原账号页面。</p>
          <a href="{{ login_origin }}">返回 CZMIYOU 统一登录</a></main></body></html>''',
          nonce=nonce, login_origin=login_origin)

    @app.get("/_plugin/launch.js")
    def launch_script():
        return send_from_directory(Path(__file__).parent / "web", "launch.js")

    @app.post("/_plugin/launch/accept")
    def accept():
        if request.headers.get("Origin") != origin or request.headers.get("X-Yzzh-Request") != "1":
            raise HybridError("LAUNCH_ORIGIN_REJECTED", 403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"nonce", "token"}:
            raise HybridError("INVALID_LAUNCH")
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", nonce):
            raise HybridError("INVALID_LAUNCH", 403)
        with runtime.lock:
            expires = nonces.pop(nonce, 0)  # Consume even when upstream verification fails.
            if expires <= time.monotonic():
                raise HybridError("LAUNCH_EXPIRED_RETRY_FROM_PORTAL", 403)
            bridge = app.extensions.get("original_bridge")
            if bridge:
                bridge.assert_settings_idle()
            account = runtime.login_token(body.get("token"))
            response = jsonify({"logged_in": True, "licensed": bool(account["licensed"])})
            response.set_cookie("yzzh_session", session_key, httponly=True, samesite="Strict")
            return response
