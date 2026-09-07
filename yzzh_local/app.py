from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from hybrid_shared import HybridError
from .runtime import Runtime
from .account import DEFAULT_LOGIN_URL
from .original import PAGES, install_original
from .launcher import PUBLIC as LAUNCH_ROUTES, install_launcher


def create_app(runtime, session_key, port):
    app = Flask(__name__, static_folder="web", static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
    origin = f"http://127.0.0.1:{port}"

    @app.before_request
    def protect():
        if request.host != f"127.0.0.1:{port}":
            raise HybridError("INVALID_HOST", 403)
        if request.headers.get("Origin") not in {None, origin}:
            raise HybridError("CROSS_ORIGIN_BLOCKED", 403)
        if request.path in LAUNCH_ROUTES:
            if (request.content_length or 0) > 16000:
                raise HybridError("REQUEST_TOO_LARGE", 413)
            return None  # Single-use nonce and same-origin headers checked by launcher.
        if request.path in PAGES or request.path == "/agent-workbench" or request.path == "/_plugin/portal.js" or request.path.startswith(("/static/", "/_plugin/agent-assets/")):
            return None
        if request.path == "/_plugin/engine":
            if not request.is_json or (request.content_length or 0) > 20000:
                raise HybridError("INVALID_ENGINE_REQUEST", 400)
            return None  # An independent worker credential is checked by the bridge.
        supplied = request.headers.get("X-Yzzh-Session") or request.cookies.get("yzzh_session", "")
        if not hmac.compare_digest(supplied, session_key):
            raise HybridError("LOCAL_SESSION_REQUIRED", 401)
        original_request = request.endpoint == "original_api"
        if not original_request and (request.content_length or 0) > 128 * 1024:
            raise HybridError("REQUEST_TOO_LARGE", 413)
        if request.method not in {"GET", "HEAD"} and not request.is_json and not original_request:
            raise HybridError("JSON_REQUIRED", 415)
        # Cookies authorize video playback; mutations require a non-simple header as well.
        if request.method not in {"GET", "HEAD"} and request.headers.get("X-Yzzh-Request") != "1":
            raise HybridError("CSRF_BLOCKED", 403)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        return response

    @app.errorhandler(HybridError)
    def expected(error):
        return jsonify({"error": error.code}), error.status

    @app.errorhandler(Exception)
    def failed(error):
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return jsonify({"error": "HTTP_REQUEST_REJECTED"}), error.code
        return jsonify({"error": "LOCAL_OPERATION_FAILED"}), 400

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.post("/session")
    def session():
        response = jsonify({"ready": True})
        response.set_cookie("yzzh_session", session_key, httponly=True, samesite="Strict")
        return response

    @app.get("/api/state")
    def state():
        with runtime.lock:
            return jsonify({"health": runtime.health(), "account": runtime.account,
                            "projects": runtime.list_projects(),
                            "settings": getattr(runtime, "provider_settings", lambda: None)()})

    @app.post("/api/settings")
    def settings():
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - {"values", "clear", "expected_owner", "expected_session"} or type(data.get("clear", False)) is not bool:
            raise HybridError("INVALID_SETTINGS")
        with runtime.lock:
            bridge = app.extensions.get("original_bridge")
            if bridge:
                bridge.assert_settings_idle()
            return jsonify(runtime.save_settings(data.get("values", {}), data.get("expected_owner"),
                                                 data.get("expected_session"), clear=data.get("clear", False)))

    @app.post("/api/login")
    def login():
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - {"username", "password", "role"}:
            raise HybridError("INVALID_LOGIN")
        if "role" in data:
            return jsonify(runtime.login(data.get("username"), data.get("password"), role=data["role"]))
        return jsonify(runtime.login(data.get("username"), data.get("password")))

    @app.post("/api/logout")
    def logout():
        return jsonify(runtime.logout())

    @app.post("/api/approve")
    def approve():
        data = request.get_json()
        return jsonify(runtime.approve(data["project_id"], data["plan_hash"], data["approved"]))

    @app.post("/api/call")
    def call():
        data = request.get_json()
        allowed = {"import_video", "process", "status", "plan", "submit", "poll", "export", "health", "list_projects"}
        name, args = data.get("name"), data.get("arguments", {})
        if name in {"original_analyze", "original_status"} and isinstance(args, dict) and "original_bridge" in app.extensions:
            return jsonify(app.extensions["original_bridge"].agent_call(name, args))
        if name not in allowed or not isinstance(args, dict):
            raise HybridError("UNKNOWN_TOOL")
        return jsonify(getattr(runtime, name)(**args))

    @app.get("/media/<project_id>/<artifact_id>")
    def media(project_id, artifact_id):
        path = runtime.artifact_path(project_id, artifact_id)
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "video/mp4"
        return send_file(path, mimetype=mime, conditional=True)

    if isinstance(runtime, Runtime):
        install_original(app, runtime, port)
        install_launcher(app, runtime, session_key, port)
    return app


def default_root():
    return Path(os.getenv("YZZH_DATA_DIR", str(Path.home() / ".czmiyou-yzzh"))).expanduser().resolve()


def main():
    from waitress import serve
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(default_root()))
    parser.add_argument("--port", type=int, default=int(os.getenv("YZZH_PORT", "7871")))
    parser.add_argument("--login-url", default=os.getenv("YZZH_LOGIN_URL", "").strip() or DEFAULT_LOGIN_URL)
    parser.add_argument("--development", action="store_true", help="Allow a loopback-only account fixture (tests only)")
    args = parser.parse_args()
    root = Path(args.data_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Take an OS lock BEFORE restart recovery. A second host must not mutate live state.
    process_lock = (root / "daemon.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        process_lock.seek(0); process_lock.write(b"0"); process_lock.flush(); process_lock.seek(0)
        msvcrt.locking(process_lock.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    runtime = Runtime(root, args.login_url, development=args.development)
    session_key = secrets.token_urlsafe(32)
    app = create_app(runtime, session_key, args.port)
    # Bind before publishing connection metadata so a failed start cannot replace a live server.
    from waitress import create_server
    server = create_server(app, host="127.0.0.1", port=args.port, threads=8)
    connection = runtime.store.root / "connection.json"
    fd = os.open(str(connection), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"port": args.port, "session": session_key, "pid": os.getpid()}, stream)
    import signal
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    try:
        server.run()
    finally:
        bridge = app.extensions.get("original_bridge")
        if bridge:
            bridge.close()


if __name__ == "__main__":
    main()
