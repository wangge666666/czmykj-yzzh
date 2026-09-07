"""Small, credential-free contracts shared by the local client and cloud gateway."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


class HybridError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HybridError("INVALID_ID")
    return value


def trusted_url(value, *, local=False):
    parts = urlsplit(value)
    if (parts.username or parts.password or parts.query or parts.fragment or not parts.hostname
            or (parts.scheme != "https" and not (
                local and parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}))):
        raise HybridError("HTTPS_ENDPOINT_REQUIRED")
    return value.rstrip("/")


def validate_manifest(value, models=None):
    if not isinstance(value, dict) or set(value) != {"prompt", "model", "resolution", "ratio", "duration", "assets"}:
        raise HybridError("INVALID_MANIFEST")
    if not isinstance(value["prompt"], str) or not 1 <= len(value["prompt"].strip()) <= 2000:
        raise HybridError("INVALID_PROMPT")
    if not isinstance(value["model"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value["model"]):
        raise HybridError("INVALID_MODEL")
    if models is not None and value["model"] not in models:
        raise HybridError("MODEL_NOT_ALLOWED")
    if value["resolution"] not in {"480p", "720p"} or value["ratio"] not in {"16:9", "9:16", "1:1", "adaptive"}:
        raise HybridError("INVALID_OUTPUT_FORMAT")
    if type(value["duration"]) is not int or not 4 <= value["duration"] <= 15:
        raise HybridError("INVALID_DURATION")
    assets = value["assets"]
    if not isinstance(assets, list) or not 2 <= len(assets) <= 4:
        raise HybridError("REFERENCE_VIDEO_AND_IMAGES_REQUIRED")
    if [x.get("kind") for x in assets if isinstance(x, dict)].count("video") != 1:
        raise HybridError("ONE_REFERENCE_VIDEO_REQUIRED")
    for index, item in enumerate(assets):
        if not isinstance(item, dict) or set(item) != {"slot", "kind", "sha256", "size"}:
            raise HybridError("INVALID_ASSET")
        if item["slot"] != f"asset_{index}" or item["kind"] not in {"video", "image"}:
            raise HybridError("INVALID_ASSET")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise HybridError("INVALID_ASSET_HASH")
        limit = 200 * 1024 * 1024 if item["kind"] == "video" else 10 * 1024 * 1024
        if type(item["size"]) is not int or not 0 < item["size"] <= limit:
            raise HybridError("INVALID_ASSET_SIZE")
    return value


class Store:
    """SQLite transactions serialize state transitions across processes."""
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.sqlite3"
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, id TEXT, code TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP)")
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(str(self.path), timeout=15)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, key, owner=None, db=None):
        identifier(key)
        if db is None:
            with self.transaction() as conn:
                return self.get(key, owner, conn)
        row = db.execute("SELECT owner,body FROM records WHERE id=?", (key,)).fetchone()
        if not row or (owner is not None and row[0] != owner):
            raise HybridError("NOT_FOUND", 404)
        return json.loads(row[1])

    def put(self, record, event, db=None):
        if db is None:
            with self.transaction() as conn:
                return self.put(record, event, conn)
        db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,owner=excluded.owner",
                   (record["id"], record.get("owner", 0), json.dumps(record, ensure_ascii=False, allow_nan=False)))
        db.execute("INSERT INTO events(id,code) VALUES (?,?)", (record["id"], event))

    def list(self, owner=None):
        with self.transaction() as db:
            rows = db.execute("SELECT body FROM records" if owner is None else
                              "SELECT body FROM records WHERE owner=?", () if owner is None else (owner,)).fetchall()
        return [json.loads(x[0]) for x in rows]
