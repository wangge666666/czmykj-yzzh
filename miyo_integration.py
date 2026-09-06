from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Sequence


PRODUCT_ID = 4
PRODUCT_CODE = "miyo_fashion"
DEFAULT_TOKEN_TTL_MS = 24 * 60 * 60 * 1000
DEFAULT_TOKEN_FUTURE_TOLERANCE_MS = 5 * 60 * 1000
CENT = Decimal("0.01")
FOUR_DECIMALS = Decimal("0.0001")
MAX_SAFE_INTEGER = 9_007_199_254_740_991


class MiyoIntegrationError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class VerifiedAccount:
    user_id: int
    role_id: int
    issued_at: int
    username: str
    role: str
    balance: Decimal
    product_id: int
    product_code: str
    plan_type: str
    subscription_end: str
    days_remaining: int

    def public(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "role": self.role,
            "balance": float(self.balance),
            "product_id": self.product_id,
            "product_code": self.product_code,
            "plan_type": self.plan_type,
            "subscription_end": self.subscription_end,
            "days_remaining": self.days_remaining,
        }


@dataclass(frozen=True)
class BillingQuote:
    model_type: str
    billing_type: str
    unit_price: Decimal

    def public(self) -> dict[str, str]:
        return {
            "model_type": self.model_type,
            "billing_type": self.billing_type,
            "unit_price": format(self.unit_price, "f"),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BillingQuote":
        model_type = str(value.get("model_type") or "").strip()
        billing_type = str(value.get("billing_type") or "").strip()
        unit_price = _decimal_price(value.get("unit_price"))
        if not model_type or billing_type not in {"tokens", "per_image"} or unit_price is None:
            raise MiyoIntegrationError(
                "本地保存的中央价格快照无效，请联系管理员处理对账。",
                code="BILLING_QUOTE_INVALID",
                status_code=503,
            )
        return cls(model_type=model_type, billing_type=billing_type, unit_price=unit_price)


@dataclass(frozen=True)
class BillingResult:
    charged: bool
    duplicate: bool
    usage_id: int
    cost: Decimal
    balance: Decimal
    total_tokens: int
    generated_count: int

    def public(self) -> dict[str, Any]:
        return {
            "charged": self.charged,
            "duplicate": self.duplicate,
            "usage_id": self.usage_id,
            "cost": float(self.cost),
            "balance": float(self.balance),
            "total_tokens": self.total_tokens,
            "generated_count": self.generated_count,
        }


def _strict_bool(value: str | None, fallback: bool) -> bool:
    if value is None or not value.strip():
        return fallback
    normalized = value.strip().lower()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    return fallback


def _strict_positive_int(value: str | None, fallback: int, *, maximum: int | None = None) -> int:
    text = (value or "").strip()
    if not text:
        return fallback
    if not re.fullmatch(r"\d+", text):
        return fallback
    parsed = int(text, 10)
    if parsed <= 0 or (maximum is not None and parsed > maximum):
        return fallback
    return parsed


def _strict_nonnegative_int(value: str | None, fallback: int, *, maximum: int | None = None) -> int:
    text = (value or "").strip()
    if not text:
        return fallback
    if not re.fullmatch(r"\d+", text):
        return fallback
    parsed = int(text, 10)
    if maximum is not None and parsed > maximum:
        return fallback
    return parsed


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_SAFE_INTEGER else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        parsed = int(value.strip(), 10)
        return parsed if parsed <= MAX_SAFE_INTEGER else None
    return None


def _decimal_price(value: Any) -> Decimal | None:
    text = str(value if value is not None else "").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d{0,5})(?:\.\d{1,4})?", text):
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    return parsed if parsed >= 0 else None


def _nonnegative_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value if value is not None else "").strip())
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _base64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(value + padding)


def verify_signed_token(
    token: str,
    secret: str,
    *,
    now_ms: int,
    ttl_ms: int = DEFAULT_TOKEN_TTL_MS,
    future_tolerance_ms: int = DEFAULT_TOKEN_FUTURE_TOLERANCE_MS,
) -> tuple[int, int, int]:
    clean_secret = secret.strip()
    if len(clean_secret) < 32:
        raise MiyoIntegrationError(
            "登录安全密钥未配置或长度不足 32 位。",
            code="AUTH_TOKEN_SECRET_NOT_CONFIGURED",
            status_code=503,
        )
    parts = token.strip().split(".")
    if len(parts) != 2 or not parts[0].endswith("="):
        raise MiyoIntegrationError("登录信息无效，请重新登录。", code="INVALID_TOKEN", status_code=401)
    encoded = parts[0][:-1]
    signature = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", signature):
        raise MiyoIntegrationError("登录信息无效，请重新登录。", code="INVALID_TOKEN", status_code=401)
    expected = base64.urlsafe_b64encode(
        hmac.new(clean_secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(signature, expected):
        raise MiyoIntegrationError("登录信息无效，请重新登录。", code="INVALID_TOKEN", status_code=401)
    try:
        decoded = _base64url_decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise MiyoIntegrationError("登录信息无效，请重新登录。", code="INVALID_TOKEN", status_code=401) from exc
    values = decoded.split(":")
    if len(values) != 3 or not all(re.fullmatch(r"\d+", value or "") for value in values):
        raise MiyoIntegrationError("登录信息无效，请重新登录。", code="INVALID_TOKEN", status_code=401)
    user_id, role_id, issued_at = (int(value, 10) for value in values)
    if (
        user_id <= 0
        or role_id <= 0
        or issued_at <= 0
        or user_id > MAX_SAFE_INTEGER
        or role_id > MAX_SAFE_INTEGER
        or issued_at > MAX_SAFE_INTEGER
    ):
        raise MiyoIntegrationError("登录信息无效，请重新登录。", code="INVALID_TOKEN", status_code=401)
    if now_ms - issued_at > ttl_ms or issued_at - now_ms > future_tolerance_ms:
        raise MiyoIntegrationError("登录已过期，请重新登录。", code="TOKEN_EXPIRED", status_code=401)
    return user_id, role_id, issued_at


def seedance_pricing_candidates(model: str, resolution: str, *, has_video_input: bool) -> list[str]:
    value = model.strip().lower()
    if value.startswith("doubao-seedance-2-5"):
        base = "doubao-seedance-2-5"
    elif value.startswith("doubao-seedance-2-0-fast"):
        base = "doubao-seedance-2-0-fast"
    elif value.startswith("doubao-seedance-2-0-mini"):
        base = "doubao-seedance-2-0-mini"
    elif value.startswith("doubao-seedance-2-0"):
        base = "doubao-seedance-2-0"
    else:
        base = re.sub(r"-\d{6}$", "", value)
    suffix = "video" if has_video_input else "no-video"
    clean_resolution = resolution.strip().lower()
    candidates: list[str] = []
    if base in {"doubao-seedance-2-0", "doubao-seedance-2-5"} and clean_resolution in {"1080p", "4k"}:
        candidates.append(f"{base}-{clean_resolution}-{suffix}")
    elif base.startswith("doubao-seedance-"):
        candidates.append(f"{base}-{suffix}")
    candidates.append(base)
    return list(dict.fromkeys(item for item in candidates if item))


def generic_pricing_candidates(model: str) -> list[str]:
    value = model.strip().lower()
    base = re.sub(r"-\d{6}$", "", value)
    return list(dict.fromkeys(item for item in (value, base) if item))


def usage_units(usage: Mapping[str, Any] | None, billing_type: str) -> tuple[int, int, Decimal]:
    usage = usage or {}
    total_tokens = _positive_int(usage.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _positive_int(usage.get("completion_tokens"))
    generated_count = _positive_int(usage.get("generated_images"))
    if generated_count is None:
        generated_count = _positive_int(usage.get("generated_count"))
    if billing_type == "tokens":
        if total_tokens is None:
            raise MiyoIntegrationError(
                "供应商成功响应缺少可核验的 Token 用量，已转入人工对账。",
                code="GENERATION_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        return total_tokens, generated_count or 0, Decimal(total_tokens) / Decimal(1000)
    if billing_type == "per_image":
        count = generated_count if generated_count is not None else 1
        if count <= 0:
            raise MiyoIntegrationError(
                "供应商成功响应中的生图数量无效，已转入人工对账。",
                code="GENERATION_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        return total_tokens or 0, count, Decimal(count)
    raise MiyoIntegrationError(
        "中央模型计费方式无效，请联系管理员。",
        code="MODEL_PRICING_NOT_CONFIGURED",
        status_code=503,
    )


class MiyoAccountService:
    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        connection_factory: Callable[[], Any] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.environ = environ if environ is not None else os.environ
        self.connection_factory = connection_factory
        self.now_ms = now_ms or (lambda: __import__("time").time_ns() // 1_000_000)

    @property
    def enabled(self) -> bool:
        raw = self.environ.get("MIYO_AUTH_ENABLED")
        if raw is None or not raw.strip():
            return False
        return _strict_bool(raw, True)

    @property
    def login_url(self) -> str:
        return (self.environ.get("MIYO_LOGIN_URL") or "").strip()

    @property
    def minimum_balance(self) -> Decimal:
        value = _decimal_price(self.environ.get("MIYO_MINIMUM_BALANCE", "0.10"))
        return value if value is not None else Decimal("0.10")

    def _connect(self) -> Any:
        if self.connection_factory is not None:
            return self.connection_factory()
        required = {
            "host": (self.environ.get("MIYO_MYSQL_HOST") or self.environ.get("DB_HOST") or "").strip(),
            "user": (self.environ.get("MIYO_MYSQL_USER") or self.environ.get("DB_USER") or "").strip(),
            "password": self.environ.get("MIYO_MYSQL_PASSWORD") or self.environ.get("DB_PASSWORD") or "",
            "database": (self.environ.get("MIYO_MYSQL_DATABASE") or self.environ.get("DB_NAME") or "").strip(),
        }
        if any(not value for value in required.values()):
            raise MiyoIntegrationError(
                "账号中心数据库未配置，请补齐 MIYO_MYSQL_* 环境变量。",
                code="MIYO_MYSQL_NOT_CONFIGURED",
                status_code=503,
            )
        raw_port = (self.environ.get("MIYO_MYSQL_PORT") or self.environ.get("DB_PORT") or "3306").strip()
        if not re.fullmatch(r"\d+", raw_port) or not 1 <= int(raw_port, 10) <= 65535:
            raise MiyoIntegrationError(
                "账号中心数据库端口必须是 1-65535 的十进制整数。",
                code="MIYO_MYSQL_PORT_INVALID",
                status_code=503,
            )
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:  # pragma: no cover - guarded by deployment dependencies
            raise MiyoIntegrationError(
                "中央计费数据库驱动未安装。",
                code="MIYO_MYSQL_DRIVER_MISSING",
                status_code=503,
            ) from exc
        try:
            return pymysql.connect(
                host=required["host"],
                port=int(raw_port, 10),
                user=required["user"],
                password=required["password"],
                database=required["database"],
                charset="utf8mb4",
                cursorclass=DictCursor,
                connect_timeout=_strict_positive_int(
                    self.environ.get("MIYO_MYSQL_CONNECT_TIMEOUT_SECONDS"), 10, maximum=60
                ),
                read_timeout=15,
                write_timeout=15,
                autocommit=False,
            )
        except MiyoIntegrationError:
            raise
        except Exception as exc:
            raise MiyoIntegrationError(
                "暂时无法连接账号中心，请稍后重试。",
                code="MIYO_MYSQL_UNAVAILABLE",
                status_code=503,
            ) from exc

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.close()
        except Exception:
            pass

    @staticmethod
    def _cursor(connection: Any) -> Any:
        return connection.cursor()

    def verify_token(self, token: str) -> VerifiedAccount:
        user_id, role_id, issued_at = verify_signed_token(
            token,
            self.environ.get("AUTH_TOKEN_SECRET", ""),
            now_ms=self.now_ms(),
            ttl_ms=_strict_positive_int(
                self.environ.get("MIYO_AUTH_TOKEN_TTL_MS"), DEFAULT_TOKEN_TTL_MS
            ),
            future_tolerance_ms=_strict_nonnegative_int(
                self.environ.get("MIYO_AUTH_TOKEN_FUTURE_TOLERANCE_MS"),
                DEFAULT_TOKEN_FUTURE_TOLERANCE_MS,
            ),
        )
        connection = self._connect()
        cursor = self._cursor(connection)
        try:
            cursor.execute(
                """
                SELECT u.id AS user_id, u.username, u.balance,
                       ur.id AS role_id, ur.role,
                       p.id AS product_id, p.code AS product_code,
                       s.plan_type, s.end_time AS subscription_end,
                       GREATEST(0, CEIL(TIMESTAMPDIFF(SECOND, NOW(), s.end_time) / 86400)) AS days_remaining
                  FROM users u
                  JOIN user_roles ur
                    ON ur.id = %s AND ur.user_id = u.id AND ur.status = 'approved'
                  JOIN products p
                    ON p.id = %s AND p.code = %s AND p.status = 1
                  JOIN user_subscriptions s
                    ON s.user_id = u.id AND s.product_id = p.id
                   AND s.status = 'active'
                   AND s.start_time <= NOW() AND s.end_time > NOW()
                 WHERE u.id = %s
                 ORDER BY s.end_time DESC
                 LIMIT 1
                """,
                (role_id, PRODUCT_ID, PRODUCT_CODE, user_id),
            )
            row = cursor.fetchone()
        except Exception as exc:
            raise MiyoIntegrationError(
                "账号、产品 4 或时间卡校验失败，请稍后重试。",
                code="MIYO_MYSQL_UNAVAILABLE",
                status_code=503,
            ) from exc
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            self._close(connection)
        if not row:
            raise MiyoIntegrationError(
                "当前账号没有有效的衣装智换时间卡，或产品 4 尚未启用。",
                code="PRODUCT_SUBSCRIPTION_REQUIRED",
                status_code=403,
            )
        balance = _nonnegative_decimal(row.get("balance"))
        if balance is None:
            raise MiyoIntegrationError(
                "账号余额数据异常，请联系管理员。",
                code="ACCOUNT_BALANCE_INVALID",
                status_code=503,
            )
        return VerifiedAccount(
            user_id=int(row["user_id"]),
            role_id=int(row["role_id"]),
            issued_at=issued_at,
            username=str(row.get("username") or "用户"),
            role=str(row.get("role") or ""),
            balance=balance,
            product_id=int(row["product_id"]),
            product_code=str(row.get("product_code") or ""),
            plan_type=str(row.get("plan_type") or ""),
            subscription_end=str(row.get("subscription_end") or ""),
            days_remaining=max(0, int(row.get("days_remaining") or 0)),
        )

    def quote_paid_stage(
        self,
        user_id: int,
        model_candidates: Sequence[str],
        *,
        expected_billing_type: str | None = None,
    ) -> BillingQuote:
        connection = self._connect()
        cursor = self._cursor(connection)
        try:
            cursor.execute(
                """
                SELECT u.balance
                  FROM users u
                  JOIN products p ON p.id = %s AND p.code = %s AND p.status = 1
                  JOIN user_subscriptions s
                    ON s.user_id = u.id AND s.product_id = p.id
                   AND s.status = 'active'
                   AND s.start_time <= NOW() AND s.end_time > NOW()
                 WHERE u.id = %s
                 ORDER BY s.end_time DESC
                 LIMIT 1
                """,
                (PRODUCT_ID, PRODUCT_CODE, user_id),
            )
            access = cursor.fetchone()
            if not access:
                raise MiyoIntegrationError(
                    "衣装智换时间卡已失效，不能提交付费任务。",
                    code="PRODUCT_SUBSCRIPTION_REQUIRED",
                    status_code=403,
                )
            balance = _nonnegative_decimal(access.get("balance"))
            if balance is None:
                raise MiyoIntegrationError(
                    "账号余额数据异常，不能提交付费任务。",
                    code="ACCOUNT_BALANCE_INVALID",
                    status_code=503,
                )
            if balance < self.minimum_balance:
                raise MiyoIntegrationError(
                    "账户余额低于 ¥0.10，请充值后再使用。",
                    code="INSUFFICIENT_BALANCE",
                    status_code=402,
                )
            for candidate in list(dict.fromkeys(str(item).strip().lower() for item in model_candidates if str(item).strip())):
                cursor.execute(
                    "SELECT model_type, unit_price, billing_type, status FROM model_pricing WHERE model_type = %s LIMIT 1",
                    (candidate,),
                )
                row = cursor.fetchone()
                if not row:
                    continue
                if int(row.get("status") or 0) != 1:
                    raise MiyoIntegrationError(
                        "中央模型价格已停用，请联系管理员。",
                        code="MODEL_PRICING_NOT_CONFIGURED",
                        status_code=503,
                    )
                billing_type = str(row.get("billing_type") or "tokens")
                if expected_billing_type and billing_type != expected_billing_type:
                    raise MiyoIntegrationError(
                        "中央模型计费方式配置错误，请联系管理员。",
                        code="MODEL_PRICING_NOT_CONFIGURED",
                        status_code=503,
                    )
                unit_price = _decimal_price(row.get("unit_price"))
                if unit_price is None:
                    raise MiyoIntegrationError(
                        "中央模型价格格式错误，请联系管理员。",
                        code="MODEL_PRICING_NOT_CONFIGURED",
                        status_code=503,
                    )
                return BillingQuote(str(row["model_type"]), billing_type, unit_price)
        except MiyoIntegrationError:
            raise
        except Exception as exc:
            raise MiyoIntegrationError(
                "读取中央模型价格失败，请稍后重试。",
                code="MIYO_MYSQL_UNAVAILABLE",
                status_code=503,
            ) from exc
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            self._close(connection)
        raise MiyoIntegrationError(
            "中央模型价格未配置或未启用，请联系管理员。",
            code="MODEL_PRICING_NOT_CONFIGURED",
            status_code=503,
        )

    def charge_once(
        self,
        *,
        user_id: int,
        task_id: str,
        task_type: str,
        quote: BillingQuote,
        usage: Mapping[str, Any] | None,
        description: str,
    ) -> BillingResult:
        clean_task_id = task_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,50}", clean_task_id):
            raise MiyoIntegrationError(
                "计费任务 ID 无效，已转入人工对账。",
                code="GENERATION_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        total_tokens, generated_count, units = usage_units(usage, quote.billing_type)
        cost = (units * quote.unit_price).quantize(FOUR_DECIMALS, rounding=ROUND_HALF_UP)
        if cost < 0:
            raise MiyoIntegrationError(
                "计费金额无效，已转入人工对账。",
                code="GENERATION_RECONCILIATION_REQUIRED",
                status_code=503,
            )
        connection = self._connect()
        cursor = self._cursor(connection)
        try:
            connection.begin()
            cursor.execute("SELECT id, balance FROM users WHERE id = %s LIMIT 1 FOR UPDATE", (user_id,))
            locked_user = cursor.fetchone()
            if not locked_user:
                raise MiyoIntegrationError(
                    "账号不存在，生成结果已转入人工对账。",
                    code="GENERATION_RECONCILIATION_REQUIRED",
                    status_code=503,
                )
            cursor.execute(
                "SELECT id, cost_amount, balance FROM token_usage WHERE user_id = %s AND product_id = %s AND task_id = %s LIMIT 1",
                (user_id, PRODUCT_ID, clean_task_id),
            )
            existing = cursor.fetchone()
            if existing:
                existing_cost = _nonnegative_decimal(existing.get("cost_amount"))
                existing_balance = _nonnegative_decimal(existing.get("balance"))
                locked_balance = _nonnegative_decimal(locked_user.get("balance"))
                if existing_cost is None or (existing_balance is None and locked_balance is None):
                    raise MiyoIntegrationError(
                        "历史账单数据异常，结果已转入人工对账。",
                        code="GENERATION_RECONCILIATION_REQUIRED",
                        status_code=503,
                    )
                connection.rollback()
                return BillingResult(
                    charged=False,
                    duplicate=True,
                    usage_id=int(existing["id"]),
                    cost=existing_cost,
                    balance=existing_balance if existing_balance is not None else locked_balance,
                    total_tokens=total_tokens,
                    generated_count=generated_count,
                )
            cursor.execute(
                """
                SELECT 1
                  FROM products p
                  JOIN user_subscriptions s ON s.product_id = p.id
                 WHERE p.id = %s AND p.code = %s AND p.status = 1
                   AND s.user_id = %s AND s.status = 'active'
                   AND s.start_time <= NOW() AND s.end_time > NOW()
                 LIMIT 1
                """,
                (PRODUCT_ID, PRODUCT_CODE, user_id),
            )
            if not cursor.fetchone():
                raise MiyoIntegrationError(
                    "生成完成时衣装智换时间卡已失效，结果已转入人工对账。",
                    code="GENERATION_RECONCILIATION_REQUIRED",
                    status_code=503,
                )
            if cost > 0:
                cursor.execute(
                    "UPDATE users SET balance = balance - %s WHERE id = %s AND balance >= %s",
                    (cost, user_id, cost),
                )
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    raise MiyoIntegrationError(
                        "生成已完成，但余额不足以记账，结果已转入人工对账。",
                        code="GENERATION_RECONCILIATION_REQUIRED",
                        status_code=503,
                    )
            cursor.execute("SELECT balance FROM users WHERE id = %s LIMIT 1", (user_id,))
            balance_row = cursor.fetchone() or {}
            balance = _nonnegative_decimal(balance_row.get("balance"))
            if balance is None:
                raise MiyoIntegrationError(
                    "生成已完成，但扣费后余额无法核验，结果已转入人工对账。",
                    code="GENERATION_RECONCILIATION_REQUIRED",
                    status_code=503,
                )
            cursor.execute(
                """
                INSERT INTO token_usage (
                  user_id, product_id, model_type, task_type, task_id,
                  total_tokens, output_tokens, generated_count,
                  cost_amount, amount, balance, description, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (
                    user_id,
                    PRODUCT_ID,
                    quote.model_type[:30],
                    task_type.strip()[:20] or "unknown",
                    clean_task_id,
                    total_tokens,
                    _positive_int((usage or {}).get("output_tokens")) or 0,
                    generated_count,
                    cost,
                    cost,
                    balance,
                    " ".join(description.split())[:200],
                ),
            )
            usage_id = int(getattr(cursor, "lastrowid", 0) or 0)
            connection.commit()
            return BillingResult(
                charged=True,
                duplicate=False,
                usage_id=usage_id,
                cost=cost,
                balance=balance,
                total_tokens=total_tokens,
                generated_count=generated_count,
            )
        except MiyoIntegrationError:
            connection.rollback()
            raise
        except Exception as exc:
            connection.rollback()
            raise MiyoIntegrationError(
                "生成已完成，但中央计费写入失败，结果已转入人工对账。",
                code="GENERATION_RECONCILIATION_REQUIRED",
                status_code=503,
            ) from exc
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            self._close(connection)
