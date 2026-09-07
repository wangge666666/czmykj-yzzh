"""Authenticated bridge to the unchanged original UI and per-account engine.

Historical BYOK calls stay in their worker. Platform workers receive public
capabilities only; the companion binds approved requests to the central account.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import Response, jsonify, request, send_from_directory

from hybrid_shared import HybridError
from yzzh_local.media import TEMP_DESTINATION, TEMP_UPLOAD_URL
from yzzh_local.workflow_approval import workflow_plan

SOURCE = Path(__file__).resolve().parents[1]
PAGES = {"/": "projects.html", "/single": "index.html", "/multi": "multi.html",
         "/person-only": "person.html", "/scene-only": "scene.html", "/clothing-only": "clothing.html",
         "/wardrobe-swap": "wardrobe.html", "/long-video": "long_video.html", "/real-long-video": "real_long_video.html"}
PAGES.update({"/projects/" + name: file for name, file in (
    ("single", "index.html"), ("multi", "multi.html"), ("person", "person.html"),
    ("scene", "scene.html"), ("clothing", "clothing.html"), ("wardrobe", "wardrobe.html"),
    ("long-video", "long_video.html"), ("real-long-video", "real_long_video.html"))})
RECOVERY_PATHS = {"/api/query", "/api/wardrobe-swap/recover-white-model",
                  "/api/person-only/recover-seedance", "/api/scene-only/recover-seedance", "/api/clothing-only/recover-seedance"}


def uncertain_temporary_upload(item):
    """Diagnose only an exact known upload; incomplete/paid records stay unknown."""
    summary = item.get("summary")
    return (item.get("state") == "uncertain" and item.get("task_id", "") == ""
            and isinstance(summary, dict) and summary.get("destination") == TEMP_DESTINATION
            and summary.get("method") == "POST"
            and summary.get("endpoint") == urlsplit(TEMP_UPLOAD_URL).path
            and summary.get("action") == "")


class OriginalBridge:
    def __init__(self, runtime, port):
        self.runtime, self.port = runtime, port
        self.workers, self.operations, self.pending = {}, {}, {}
        self.root = runtime.store.root / "original"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.http = requests.Session()
        self.http.trust_env = False
        atexit.register(self.close)

    def close(self):
        for worker in list(self.workers.values()):
            process = worker["process"]
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()
        self.workers.clear()

    def assert_settings_idle(self):
        if not self.runtime.account:
            return
        worker = self.workers.get(self.runtime.account["user_id"])
        if worker and worker["process"].poll() is None:
            status = self.http.get(worker["url"] + "/_engine/status", headers={"X-Engine-Key": worker["key"]}, timeout=3).json()
            if status.get("busy"):
                raise HybridError("ORIGINAL_TASK_RUNNING_KEEP_SETTINGS", 409)

    def context(self, owner, session, require_license=False):
        rt = self.runtime
        if not rt.account:
            raise HybridError("LOGIN_REQUIRED", 401)
        if str(rt.account["user_id"]) != str(owner) or session != rt.session_revision:
            raise HybridError("ACCOUNT_CHANGED_REFRESH", 409)
        if require_license:
            rt._verify()
        if getattr(rt, "mode", "byok") == "platform":
            capabilities = rt.provider_settings()
            values = {"__platform__": capabilities}
            revision = hashlib.sha256(json.dumps({"mode": "platform", "capabilities": capabilities.get("capabilities"),
                "models": capabilities.get("models"), "ready": capabilities.get("ready")}, sort_keys=True).encode()).hexdigest()
        else:
            values, revision = rt.settings.load(rt.account["user_id"])
        return rt.account["user_id"], values, revision

    def binding(self, operation, license=False):
        owner, _, revision = self.context(operation["owner"], operation["session"], license)
        if revision != operation["revision"]:
            raise HybridError("SETTINGS_CHANGED_REPLAN_REQUIRED", 409)
        return owner

    def _worker(self, owner, values, revision):
        worker = self.workers.get(owner)
        if worker and worker["process"].poll() is None:
            if worker["revision"] == revision:
                return worker
            status = self.http.get(worker["url"] + "/_engine/status", headers={"X-Engine-Key": worker["key"]}, timeout=3).json()
            if status.get("busy"):
                raise HybridError("ORIGINAL_TASK_RUNNING_KEEP_SETTINGS", 409)
            worker["process"].terminate()
            worker["process"].wait(timeout=5)
        account_root = self.root / str(owner)
        if "__platform__" in values:
            # Never resume old BYOK submissions under shared platform credentials.
            account_root = account_root / "platform"
            account_root.parent.mkdir(mode=0o700, exist_ok=True)
        account_root.mkdir(mode=0o700, exist_ok=True)
        if account_root.is_symlink():
            raise HybridError("UNSAFE_ACCOUNT_DIRECTORY")
        key = secrets.token_urlsafe(32)
        # Explicit environment: no inherited platform keys, proxy credentials,
        # PYTHONPATH hooks or application .env settings.
        env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL") if k in os.environ}
        env.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([sys.executable, "-m", "yzzh_local.original_worker"], cwd=str(SOURCE),
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        config = {"root": str(account_root), "values": values, "key": key,
                  "callback": f"http://127.0.0.1:{self.port}/_plugin/engine"}
        process.stdin.write(json.dumps(config) + "\n")
        process.stdin.close()
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
        try:
            result = json.loads(ready.get(timeout=20))
            worker_port = result["port"]
            if type(worker_port) is not int or not 1024 <= worker_port <= 65535:
                raise ValueError()
        except Exception:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
            raise HybridError("ORIGINAL_ENGINE_START_FAILED", 503) from None
        worker = {"process": process, "key": key, "revision": revision, "url": f"http://127.0.0.1:{worker_port}", "root": str(account_root)}
        self.workers[owner] = worker
        return worker

    def operation(self, owner, session, method, path):
        with self.runtime.lock:
            owner, values, revision = self.context(owner, session, method not in {"GET", "HEAD"} and path not in RECOVERY_PATHS)
            worker = self._worker(owner, values, revision)
            if len(self.operations) >= 10000:
                raise HybridError("LOCAL_SESSION_RESTART_REQUIRED", 503)
            key = uuid.uuid4().hex
            self.operations[key] = {"owner": owner, "session": session, "revision": revision,
                                    "path": path, "method": method, "worker_key": worker["key"],
                                    "workflow": workflow_plan(path, method, getattr(self.runtime, "mode", "byok"))}
            return key, worker

    def _record(self, item):
        root = self.root / str(item["owner"]) / "network"
        if item.get("service_mode") == "platform":
            root = root / "platform"
        root.mkdir(mode=0o700, exist_ok=True, parents=True)
        target = root / (item["id"] + ".json")
        temporary = root / (item["id"] + ".tmp")
        fd = os.open(str(temporary), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(item, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)

    def unresolved(self, owner):
        root = self.root / str(owner) / "network"
        if getattr(self.runtime, "mode", "byok") == "platform":
            root = root / "platform"
        for path in root.glob("*.json"):
            try:
                item = json.loads(path.read_text())
            except (OSError, ValueError):
                raise HybridError("NETWORK_JOURNAL_REQUIRES_REVIEW", 409) from None
            if item.get("state") in {"sending", "uncertain"}:
                yield item

    def check_unresolved(self, owner):
        items = [item for item in self.unresolved(owner) if self.needs_reconciliation(item)]
        if items:
            code = "PREVIOUS_UPLOAD_UNCERTAIN" if all(uncertain_temporary_upload(item) for item in items) else "PREVIOUS_REQUEST_UNCERTAIN_CHECK_PROVIDER"
            raise HybridError(code, 409)

    def needs_reconciliation(self, item):
        # An active dispatch is normal progress, not an unknown receipt. Durable
        # sending records from an earlier process still require reconciliation.
        if getattr(self.runtime, "mode", "byok") != "platform":
            return True
        active = self.pending.get(item.get("id"))
        return not (item.get("state") == "sending" and active and active.get("state") == "sending")

    def network(self, data, supplied):
        with self.runtime.lock:
            operation = self.operations.get(data.get("operation"))
            if not operation or not secrets.compare_digest(str(supplied), operation["worker_key"]):
                raise HybridError("INVALID_ENGINE_SESSION", 403)
            event = data.get("event")
            # Completion can arrive after logout; retain the result without
            # granting any additional execution rights.
            if event == "finish":
                item = self.pending.get(data.get("id"))
                if not item or item["operation"] != data["operation"] or item["state"] != "sending":
                    raise HybridError("NETWORK_APPROVAL_NOT_FOUND", 404)
                item["state"] = "completed" if data.get("ok") is True else "uncertain"
                task_id = data.get("task_id", "")
                if isinstance(task_id, str) and len(task_id) <= 128 and all(c.isalnum() or c in "_-" for c in task_id):
                    item["task_id"] = task_id
                self._record(item)
                return {"recorded": True}
            self.binding(operation)
            if event == "read":
                return {"allowed": True}
            if event == "request":
                self.binding(operation, license=True)
                self.check_unresolved(operation["owner"])
                summary = data.get("summary")
                if not isinstance(summary, dict) or len(json.dumps(summary)) > 16000:
                    raise HybridError("INVALID_NETWORK_SUMMARY")
                plan = operation.get("workflow")
                consent = operation.get("consent")
                if plan:
                    if summary.get("operation") not in plan["commands"]:
                        raise HybridError("WORKFLOW_OPERATION_OUTSIDE_SCOPE", 409)
                    if consent and (consent["state"] == "rejected" or time.time() - consent["created"] > 86400):
                        raise HybridError("WORKFLOW_CONSENT_ENDED", 409)
                key = uuid.uuid4().hex
                item = {"id": key, "operation": data["operation"], "owner": operation["owner"],
                        "source": operation["path"], "summary": summary, "state": "awaiting_approval", "created": time.time()}
                item["service_mode"] = getattr(self.runtime, "mode", "byok")
                if plan:
                    item["workflow"] = {**plan, "id": data["operation"]}
                    if consent and consent["state"] == "approved":
                        item.update(state="approved", approved_by=consent["id"])
                        if plan["asset_consent"]:
                            item["compliance_confirmed"] = True
                self.pending[key] = item
                self._record(item)
                return {"id": key}
            item = self.pending.get(data.get("id"))
            if not item or item["operation"] != data["operation"]:
                raise HybridError("NETWORK_APPROVAL_NOT_FOUND", 404)
            if time.time() - item["created"] > 600 and item["state"] == "awaiting_approval":
                item["state"] = "rejected"
                if item.get("workflow"):
                    operation["consent"] = {"state": "rejected", "id": item["id"], "created": time.time()}
                self._record(item)
            if event == "take" and item["state"] == "approved":
                self.binding(operation, license=True)
                self.check_unresolved(operation["owner"])
                item["state"] = "sending"
                self._record(item)
                return {"state": "send_once"}
            return {"state": item["state"]}

    def platform_request(self, data, supplied):
        from .platform_bridge import platform_request
        return platform_request(self, data, supplied)

    def approvals(self, owner, session):
        with self.runtime.lock:
            owner, _, _ = self.context(owner, session)
            result = [x for x in self.pending.values() if x["owner"] == owner and x["state"] == "awaiting_approval"
                      and self.operations[x["operation"]]["session"] == session]
            # Parallel requests within the same clicked action share one card.
            seen, grouped = set(), []
            for item in result:
                group = item["operation"] if item.get("workflow") else item["id"]
                if group not in seen:
                    grouped.append(item)
                    seen.add(group)
            # Derived display metadata only: preserve the durable journal and gate.
            unresolved = [{**item, "diagnosis": "temporary_upload_uncertain" if uncertain_temporary_upload(item) else "request_uncertain"}
                          for item in self.unresolved(owner) if self.needs_reconciliation(item)]
            return {"pending": grouped, "unresolved": unresolved}

    def agent_call(self, name, arguments):
        # Narrow tools, not an arbitrary HTTP proxy or an approval back door.
        with self.runtime.lock:
            if not self.runtime.account:
                raise HybridError("LOGIN_REQUIRED", 401)
            owner, session = self.runtime.account["user_id"], self.runtime.session_revision
            if name == "original_status":
                if set(arguments) != {"job_id"} or not re.fullmatch(r"[a-f0-9]{12}", str(arguments["job_id"])):
                    raise HybridError("INVALID_ORIGINAL_JOB")
                path, method = "/api/jobs/" + arguments["job_id"], "GET"
            elif name == "original_analyze":
                if set(arguments) - {"path", "project", "manual_cuts"} or arguments.get("project", "virtual") not in {"virtual", "real"}:
                    raise HybridError("INVALID_ORIGINAL_OPERATION")
                path = "/api/real-long-video/analyze" if arguments.get("project") == "real" else "/api/long-video/analyze"
                method = "POST"
            else:
                raise HybridError("UNKNOWN_TOOL")
            operation, worker = self.operation(owner, session, method, path)
        headers = {"X-Engine-Key": worker["key"], "X-Engine-Operation": operation}
        if method == "GET":
            response = self.http.get(worker["url"] + path, headers=headers, timeout=30, allow_redirects=False)
        else:
            source = Path(arguments.get("path", "")).expanduser().resolve()
            if not source.is_file() or source.suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"} or source.stat().st_size > 440 * 1024 * 1024:
                raise HybridError("VIDEO_FILE_REQUIRED_MAX_440_MIB")
            with source.open("rb") as stream:
                response = self.http.post(worker["url"] + path, headers=headers,
                    files={"reference_video": (source.name, stream)}, data={"manual_cuts": arguments.get("manual_cuts", "")}, timeout=60, allow_redirects=False)
        with self.runtime.lock:
            self.context(owner, session)
        if response.status_code not in {200, 202}:
            raise HybridError("ORIGINAL_OPERATION_REJECTED_CHECK_WORKBENCH", response.status_code)
        result = response.json()
        result["workbench"] = f"http://127.0.0.1:{self.port}/projects/" + ("real-long-video" if "real" in str(result.get("project", "")) else "long-video")
        return result

    def decide(self, data):
        with self.runtime.lock:
            self.context(data.get("owner"), data.get("session"))
            item = self.pending.get(data.get("id"))
            if not item or item["state"] != "awaiting_approval" or type(data.get("approved")) is not bool:
                raise HybridError("NETWORK_APPROVAL_NOT_FOUND", 404)
            self.binding(self.operations[item["operation"]], license=data["approved"])
            operation = self.operations[item["operation"]]
            scope = data.get("workflow_id")
            plan = operation.get("workflow") if scope else None
            if scope and (scope != item["operation"] or not plan or item.get("workflow", {}).get("id") != scope):
                raise HybridError("WORKFLOW_CONSENT_CHANGED", 409)
            if data["approved"] and (item["summary"].get("operation") == "assets.CreateAsset" or (plan and plan["asset_consent"])):
                if data.get("compliance_confirmed") is not True:
                    raise HybridError("ASSET_CONSENT_REQUIRED", 409)
                item["compliance_confirmed"] = True
            item["state"] = "approved" if data["approved"] else "rejected"
            if plan:
                operation["consent"] = {"state": item["state"], "id": item["id"], "created": time.time()}
                for sibling in self.pending.values():
                    if sibling["operation"] == item["operation"] and sibling["state"] == "awaiting_approval":
                        sibling.update(state=item["state"], approved_by=item["id"])
                        if data["approved"] and plan["asset_consent"]:
                            sibling["compliance_confirmed"] = True
                        self._record(sibling)
                item["approved_by"] = item["id"]
            self._record(item)
            return {"state": item["state"]}

    def proxy(self, path, media_session=None):
        owner = request.headers.get("X-Yzzh-Owner")
        session = media_session or request.headers.get("X-Yzzh-Context") or request.args.get("plugin_session")
        if not owner and request.method in {"GET", "HEAD"} and session == self.runtime.session_revision and self.runtime.account:
            owner = self.runtime.account["user_id"]
        operation, worker = self.operation(owner, session, request.method, path)
        headers = {"X-Engine-Key": worker["key"], "X-Engine-Operation": operation}
        for key in ("Content-Type", "Range", "If-Range"):
            if key in request.headers:
                headers[key] = request.headers[key]
        params = [(k, v) for k, v in request.args.items(multi=True) if k != "plugin_session"]
        # The incoming stream is bounded by Flask's 450 MiB content limit.
        response = self.http.request(request.method, worker["url"] + path, params=params,
            data=request.get_data(), headers=headers, timeout=(3, 660), allow_redirects=False, stream=True)
        if response.headers.get("Content-Type", "").startswith("application/json"):
            try:
                data = response.json()
            finally:
                response.close()
            def media_binding(value):
                if isinstance(value, dict):
                    return {k: media_binding(v) for k, v in value.items()}
                if isinstance(value, list):
                    return [media_binding(v) for v in value]
                if isinstance(value, str) and (value.startswith("/api/plugin-remote/") or (value.startswith("/api/jobs/") and ("/file/" in value or "/scene-groups/" in value))):
                    return "/_plugin/media/" + session + value
                return value
            # Never return A's completed response into a tab now bound to B.
            with self.runtime.lock:
                self.context(owner, session)
            return jsonify(media_binding(data)), response.status_code
        def chunks():
            try:
                for chunk in response.iter_content(128 * 1024):
                    with self.runtime.lock:
                        self.context(owner, session)
                    yield chunk
            finally:
                response.close()
        public_headers = {k: v for k, v in response.headers.items() if k.lower() in
                          {"content-type", "content-length", "content-range", "accept-ranges", "content-disposition"}}
        return Response(chunks(), status=response.status_code, headers=public_headers)


def install_original(app, runtime, port):
    bridge = OriginalBridge(runtime, port)
    app.extensions["original_bridge"] = bridge
    app.config["MAX_CONTENT_LENGTH"] = 450 * 1024 * 1024
    app.static_folder = str(SOURCE / "web")

    def page():
        html = (SOURCE / "web" / PAGES[request.path]).read_text(encoding="utf-8")
        # Synchronous bootstrap installs fetch authentication before old scripts.
        with runtime.lock:
            owner = runtime.account["user_id"] if runtime.account else ""
            binding = runtime.session_revision
        html = html.replace("</head>", f'<meta name="yzzh-owner" content="{owner}"><meta name="yzzh-context" content="{binding}"><meta name="yzzh-service-mode" content="{runtime.mode}"><script src="/_plugin/portal.js"></script></head>', 1)
        return Response(html, mimetype="text/html")
    app.view_functions["index"] = page
    for index, path in enumerate(PAGES):
        if path != "/":
            app.add_url_rule(path, "original_page_" + str(index), page)

    @app.get("/_plugin/portal.js")
    def portal():
        return send_from_directory(Path(__file__).parent / "web", "portal.js")

    @app.route("/_plugin/engine", methods=["POST"])
    def network():
        return jsonify(bridge.network(request.get_json(), request.headers.get("X-Engine-Key", "")))

    @app.post("/_plugin/platform")
    def platform():
        return jsonify({"result": bridge.platform_request(request.get_json(), request.headers.get("X-Engine-Key", ""))})

    @app.get("/_plugin/approvals")
    def approvals():
        return jsonify(bridge.approvals(request.headers.get("X-Yzzh-Owner"), request.headers.get("X-Yzzh-Context")))

    @app.post("/_plugin/reconcile")
    def reconcile():
        from .platform_bridge import reconcile_request
        return jsonify(reconcile_request(bridge, request.get_json()))

    @app.post("/_plugin/decision")
    def decision():
        return jsonify(bridge.decide(request.get_json()))

    @app.route("/api/<path:path>", methods=["GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"])
    def original_api(path):
        return bridge.proxy("/api/" + path)

    @app.get("/_plugin/media/<session>/api/<path:path>")
    def original_media(session, path):
        if not path.startswith(("jobs/", "plugin-remote/")):
            raise HybridError("INVALID_MEDIA", 404)
        return bridge.proxy("/api/" + path, media_session=session)

    @app.get("/agent-workbench")
    def agent_workbench():
        html = (Path(__file__).parent / "web" / "index.html").read_text()
        return Response(html.replace('/static/', '/_plugin/agent-assets/'), mimetype="text/html")

    @app.get("/_plugin/agent-assets/<path:path>")
    def agent_assets(path):
        return send_from_directory(Path(__file__).parent / "web", path)

    return bridge
