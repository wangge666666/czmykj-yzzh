"""Per-account, local-only BYOK settings. No ambient provider keys are read."""
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

from hybrid_shared import HybridError
from yzzh_local.media import TOS_FIELDS, upload_destination, upload_mode

FIELDS = ("ARK_API_KEY", "ARK_MODEL", "TOS_ACCESS_KEY", "TOS_SECRET_KEY", "TOS_BUCKET", "ARK_IMAGE_MODEL", "ARK_PERFORMANCE_MODEL", "MEDIA_UPLOAD_MODE")
REQUIRED = ("ARK_API_KEY", "ARK_MODEL")
SECRETS = {"ARK_API_KEY", "TOS_ACCESS_KEY", "TOS_SECRET_KEY"}


class Settings:
    def __init__(self, root):
        self.root = Path(root) / "credentials"
        self.cache = {}

    def path(self, owner):
        if type(owner) is not int or owner <= 0:
            raise HybridError("LOGIN_REQUIRED", 401)
        return self.root / str(owner) / ".env"

    def load(self, owner):
        path = self.path(owner)
        values = {k: "" for k in FIELDS}
        try:
            if path.is_symlink() or path.parent.is_symlink() or self.root.is_symlink():
                raise ValueError()
            if path.exists():
                if path.stat().st_size > 64 * 1024:
                    raise ValueError()
                seen = set()
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    name, separator, value = line.partition("=")
                    name, value = name.strip(), value.strip()
                    if separator != "=" or name not in FIELDS or name in seen:
                        raise ValueError()
                    seen.add(name)
                    values[name] = json.loads(value) if value.startswith('"') else value
            self._validate(values)
        except (OSError, ValueError, TypeError):
            raise HybridError("LOCAL_SETTINGS_INVALID", 400) from None
        # Opaque revision, not a digest of any secret. External edits and daemon
        # restarts invalidate unsent approvals without persisting secrets twice.
        cached = self.cache.get(owner)
        if cached is None or cached[0] != values:
            cached = (dict(values), uuid.uuid4().hex)
            self.cache[owner] = cached
        return dict(values), cached[1]

    @staticmethod
    def _validate(values):
        for value in values.values():
            if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
                raise ValueError()
        for key in ("ARK_MODEL", "ARK_IMAGE_MODEL", "ARK_PERFORMANCE_MODEL"):
            if values[key] and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", values[key]):
                raise ValueError()
        if values["TOS_BUCKET"] and not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", values["TOS_BUCKET"]):
            raise ValueError()
        if values.get("MEDIA_UPLOAD_MODE", "") not in {"", "temporary", "tos"}:
            raise ValueError()

    def save(self, owner, changes):
        if not isinstance(changes, dict) or set(changes) - set(FIELDS):
            raise HybridError("INVALID_SETTINGS")
        values, _ = self.load(owner)
        try:
            for key, value in changes.items():
                if not isinstance(value, str):
                    raise ValueError()
                # Empty secret fields mean keep existing; clearing has its own action.
                if value.strip() or key not in SECRETS:
                    values[key] = value.strip()
            self._validate(values)
        except (ValueError, TypeError):
            raise HybridError("INVALID_SETTINGS") from None
        self._write(owner, values)
        return self.public(owner)

    def clear(self, owner):
        self._write(owner, {k: "" for k in FIELDS})
        return self.public(owner)

    def _write(self, owner, values):
        path = self.path(owner)
        # Validate the existing path before creating/replacing files.
        self.load(owner)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.parent.chmod(0o700)
        fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                for key in FIELDS:
                    stream.write(key + "=" + json.dumps(values[key], ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.cache.pop(owner, None)

    def public(self, owner):
        values, revision = self.load(owner)
        required = REQUIRED + (TOS_FIELDS if upload_mode(values) == "tos" else ())
        return {"provider": "volcengine-ark", "configured": all(values[k] for k in required),
                "missing": [k for k in required if not values[k]], "upload_mode": upload_mode(values),
                "upload": upload_destination(values),
                "fields": {k: bool(values[k]) for k in FIELDS}, "model": values["ARK_MODEL"],
                "image_model": values["ARK_IMAGE_MODEL"], "performance_model": values["ARK_PERFORMANCE_MODEL"],
                "bucket": values["TOS_BUCKET"], "revision": revision,
                "config_path": str(self.path(owner)), "billing": "customer_provider_account",
                "storage_region": "cn-beijing", "checked": "local_format_only"}
