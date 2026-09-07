from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from flask import Flask, g, jsonify, request, send_file

from hybrid_shared import HybridError, Store, digest, file_hash, validate_manifest
from .account import BillingQuote, MiyoAccountService, MiyoIntegrationError, seedance_pricing_candidates
from .provider import ArkProvider


def create_app(root, *, accounts=None, provider=None, models=None, now=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 232 * 1024 * 1024
    store = Store(root)
    accounts = accounts or MiyoAccountService()
    provider = provider or ArkProvider()
    models = models if models is not None else [x.strip() for x in os.getenv("YZZH_MODELS", "").split(",") if x.strip()]
    now = now or time.time
    app.extensions["hybrid_store"] = store

    def public(record):
        return {k: record[k] for k in ("id", "state", "quote", "expires_at", "manifest_hash", "billing", "error") if k in record}

    @app.errorhandler(HybridError)
    def expected(error):
        return jsonify({"error": error.code}), error.status

    @app.errorhandler(MiyoIntegrationError)
    def account_error(error):
        return jsonify({"error": error.code}), error.status_code

    @app.errorhandler(500)
    def unexpected(_error):
        return jsonify({"error": "INTERNAL_ERROR"}), 500

    @app.before_request
    def authenticate():
        if request.path == "/health" and request.method == "GET":
            return None
        value = request.headers.get("Authorization", "")
        if not value.startswith("Bearer ") or len(value) > 4103:
            raise HybridError("AUTH_REQUIRED", 401)
        # Never honor MIYO_AUTH_ENABLED=false in the commercial gateway.
        g.account = accounts.verify_token(value[7:])

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health():
        return jsonify({"service": "yzzh-cloud", "version": "0.1.0", "product_id": 4})

    @app.get("/v1/account")
    def account():
        result = g.account.public()
        result["models"] = models
        return jsonify(result)

    @app.post("/v1/quotes")
    def quote():
        manifest = validate_manifest(request.get_json(), models)
        price = accounts.quote_paid_stage(g.account.user_id,
            seedance_pricing_candidates(manifest["model"], manifest["resolution"], has_video_input=True),
            expected_billing_type="tokens")
        record = {"id": uuid.uuid4().hex, "owner": g.account.user_id, "state": "quoted",
                  "manifest": manifest, "manifest_hash": digest(manifest), "quote": price.public(),
                  "expires_at": int(now()) + 900}
        store.put(record, "QUOTED")
        return jsonify(public(record))

    @app.post("/v1/tasks/<task_id>/submit")
    def submit(task_id):
        owner = g.account.user_id
        # Claim before reading/writing uploads; other processes see the same durable claim.
        with store.transaction() as db:
            record = store.get(task_id, owner, db)
            if record["state"] != "quoted":
                return jsonify(public(record))
            if record["expires_at"] < now():
                raise HybridError("QUOTE_EXPIRED", 409)
            current = accounts.quote_paid_stage(owner,
                seedance_pricing_candidates(record["manifest"]["model"], record["manifest"]["resolution"], has_video_input=True),
                expected_billing_type="tokens")
            if current.public() != record["quote"]:
                raise HybridError("PRICE_CHANGED_REQUOTE_REQUIRED", 409)
            record["state"] = "receiving"
            store.put(record, "UPLOAD_STARTED", db)
        root = store.root / task_id
        root.mkdir(exist_ok=True, mode=0o700)
        try:
            files = {}
            if set(request.files) != {x["slot"] for x in record["manifest"]["assets"]}:
                raise HybridError("ASSET_SET_CHANGED")
            for item in record["manifest"]["assets"]:
                target = root / item["slot"]
                upload = request.files[item["slot"]]
                size = 0
                with target.open("wb") as output:
                    while True:
                        block = upload.stream.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > item["size"]:
                            raise HybridError("ASSET_SIZE_CHANGED")
                        output.write(block)
                if size != item["size"] or file_hash(target) != item["sha256"]:
                    raise HybridError("ASSET_HASH_CHANGED")
                with target.open("rb") as stream:
                    header = stream.read(32)
                valid = b"ftyp" in header if item["kind"] == "video" else header.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
                if not valid:
                    raise HybridError("INVALID_MEDIA")
                files[item["slot"]] = target
        except Exception:
            record.update(state="failed", error="UPLOAD_VALIDATION_FAILED")
            store.put(record, "UPLOAD_REJECTED")
            return jsonify(public(record))
        record["state"] = "submitting"
        store.put(record, "PROVIDER_SUBMIT_STARTED")
        try:
            record["provider_id"] = provider.submit(record["manifest"], files)
            record["state"] = "running"
        except Exception:
            record.update(state="submit_uncertain", error="QUERY_ONLY_CONTACT_SUPPORT")
        store.put(record, "PROVIDER_SUBMIT_RETURNED")
        return jsonify(public(record))

    @app.get("/v1/tasks/<task_id>")
    def status(task_id):
        owner = g.account.user_id
        # Serialize finalization/charge; explicit single-host SQLite deployment.
        # No provider POST is ever made by this recovery path.
        with store.transaction() as db:
            record = store.get(task_id, owner, db)
            if record["state"] not in {"running", "reconciliation_required"}:
                return jsonify(public(record))
            try:
                if "provider_result" not in record:
                    task = provider.query(record["provider_id"])
                    if task.get("status") in {"failed", "expired", "cancelled"}:
                        record.update(state="failed", error="PROVIDER_TASK_FAILED")
                        store.put(record, "PROVIDER_TASK_FAILED", db)
                        return jsonify(public(record))
                    if task.get("status") != "succeeded":
                        return jsonify(public(record))
                    record["provider_result"] = task
                output = store.root / task_id / "output.mp4"
                task = record["provider_result"]
                if not record.get("output_saved"):
                    provider.download(task, output)
                    record["output_saved"] = True
                # Output and remote task remain recoverable even if the central write fails.
                bill = accounts.charge_once(user_id=owner, task_id="hybrid:" + task_id,
                    task_type="video", quote=BillingQuote.from_mapping(record["quote"]),
                    usage=task.get("usage"), description="衣装智换插件视频生成")
                record.update(state="succeeded", billing=bill.public(), error="")
            except MiyoIntegrationError:
                record.update(state="reconciliation_required", error="BILLING_RECONCILIATION_REQUIRED")
            except Exception:
                record["error"] = "QUERY_OR_DOWNLOAD_FAILED_RETRY_QUERY"
            store.put(record, "PROVIDER_STATUS_CHECKED", db)
        return jsonify(public(record))

    @app.get("/v1/tasks/<task_id>/output")
    def output(task_id):
        record = store.get(task_id, g.account.user_id)
        if record["state"] != "succeeded" or not record.get("billing"):
            raise HybridError("OUTPUT_NOT_RELEASED", 409)
        return send_file(store.root / task_id / "output.mp4", mimetype="video/mp4", as_attachment=True,
                         download_name="yzzh-output.mp4")

    return app


def main():
    import argparse
    from waitress import serve
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7872)
    args = parser.parse_args()
    serve(create_app(args.data_dir), host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
