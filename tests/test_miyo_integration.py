from __future__ import annotations

import base64
import hashlib
import hmac
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from miyo_integration import (
    BillingQuote,
    MiyoAccountService,
    MiyoIntegrationError,
    VerifiedAccount,
    seedance_pricing_candidates,
    usage_units,
    verify_signed_token,
)


SECRET = "miyo-test-secret-with-at-least-32-characters"
NOW_MS = 1_800_000_000_000


def signed_token(user_id: int = 7, role_id: int = 9, issued_at: int = NOW_MS) -> str:
    payload = base64.urlsafe_b64encode(f"{user_id}:{role_id}:{issued_at}".encode()).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{payload}=.{signature}"


class FakeCursor:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.current = None
        self.rowcount = 0
        self.lastrowid = 0
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> None:
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        self.rowcount = 0
        if "FROM users u JOIN user_roles" in compact:
            self.current = {
                "user_id": 7,
                "username": "测试用户",
                "balance": "12.34",
                "role_id": 9,
                "role": "staff",
                "product_id": 4,
                "product_code": "miyo_fashion",
                "plan_type": "monthly",
                "subscription_end": "2027-01-01 00:00:00",
                "days_remaining": 30,
            }
        elif "SELECT u.balance FROM users u" in compact:
            self.current = {"balance": "12.34"}
        elif "FROM model_pricing" in compact:
            self.current = {
                "model_type": params[0],
                "unit_price": "0.0420",
                "billing_type": "tokens",
                "status": 1,
            }
        elif compact.startswith("SELECT id, balance FROM users"):
            self.current = {"id": 7, "balance": "12.34"}
        elif compact.startswith("SELECT id, cost_amount, balance FROM token_usage"):
            self.current = (
                {"id": 88, "cost_amount": "0.0840", "balance": "12.26"}
                if self.duplicate
                else None
            )
        elif compact.startswith("SELECT 1 FROM products"):
            self.current = {"1": 1}
        elif compact.startswith("UPDATE users SET balance"):
            self.current = None
            self.rowcount = 1
        elif compact.startswith("SELECT balance FROM users"):
            self.current = {"balance": "12.26"}
        elif compact.startswith("INSERT INTO token_usage"):
            self.current = None
            self.rowcount = 1
            self.lastrowid = 99
        else:
            raise AssertionError(f"未覆盖 SQL: {compact}")

    def fetchone(self):
        return self.current

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.cursor_value = FakeCursor(duplicate=duplicate)
        self.committed = False
        self.rolled_back = False

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def begin(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        return None


class TokenTests(unittest.TestCase):
    def test_accepts_current_czmiyou_hmac_token(self) -> None:
        self.assertEqual(verify_signed_token(signed_token(), SECRET, now_ms=NOW_MS), (7, 9, NOW_MS))

    def test_rejects_tampered_and_unsigned_tokens(self) -> None:
        for token in (signed_token()[:-1] + "A", signed_token().split(".")[0]):
            with self.subTest(token=token):
                with self.assertRaises(MiyoIntegrationError) as raised:
                    verify_signed_token(token, SECRET, now_ms=NOW_MS)
                self.assertEqual(raised.exception.code, "INVALID_TOKEN")

    def test_rejects_expired_token(self) -> None:
        with self.assertRaises(MiyoIntegrationError) as raised:
            verify_signed_token(signed_token(issued_at=NOW_MS - 86_400_001), SECRET, now_ms=NOW_MS)
        self.assertEqual(raised.exception.code, "TOKEN_EXPIRED")

    def test_rejects_identifier_outside_javascript_safe_integer_range(self) -> None:
        with self.assertRaises(MiyoIntegrationError) as raised:
            verify_signed_token(
                signed_token(user_id=9_007_199_254_740_992),
                SECRET,
                now_ms=NOW_MS,
            )
        self.assertEqual(raised.exception.code, "INVALID_TOKEN")


class BillingTests(unittest.TestCase):
    def service(self, connection: FakeConnection) -> MiyoAccountService:
        return MiyoAccountService(
            environ={
                "MIYO_AUTH_ENABLED": "true",
                "AUTH_TOKEN_SECRET": SECRET,
                "MIYO_MINIMUM_BALANCE": "0.10",
            },
            connection_factory=lambda: connection,
            now_ms=lambda: NOW_MS,
        )

    def test_verifies_product_four_and_active_time_card(self) -> None:
        connection = FakeConnection()
        account = self.service(connection).verify_token(signed_token())
        self.assertEqual(account.user_id, 7)
        self.assertEqual(account.product_id, 4)
        self.assertEqual(account.product_code, "miyo_fashion")
        self.assertEqual(account.balance, Decimal("12.34"))
        self.assertEqual(connection.cursor_value.calls[0][1], (9, 4, "miyo_fashion", 7))

    def test_quotes_enabled_central_seedance_price(self) -> None:
        quote = self.service(FakeConnection()).quote_paid_stage(
            7,
            seedance_pricing_candidates(
                "doubao-seedance-2-5-260628", "720p", has_video_input=True
            ),
            expected_billing_type="tokens",
        )
        self.assertEqual(quote.model_type, "doubao-seedance-2-5-video")
        self.assertEqual(quote.unit_price, Decimal("0.0420"))

    def test_charges_once_and_writes_product_four_ledger(self) -> None:
        connection = FakeConnection()
        result = self.service(connection).charge_once(
            user_id=7,
            task_id="yzzh:v:provider-task-1",
            task_type="video",
            quote=BillingQuote("doubao-seedance-2-5-video", "tokens", Decimal("0.0420")),
            usage={"total_tokens": 2000, "output_tokens": 1800},
            description="衣装智换测试",
        )
        self.assertTrue(result.charged)
        self.assertEqual(result.cost, Decimal("0.0840"))
        insert = next(call for call in connection.cursor_value.calls if call[0].startswith("INSERT INTO token_usage"))
        self.assertEqual(insert[1][1], 4)
        self.assertEqual(insert[1][4], "yzzh:v:provider-task-1")
        self.assertTrue(connection.committed)

    def test_duplicate_task_is_not_charged_twice(self) -> None:
        connection = FakeConnection(duplicate=True)
        result = self.service(connection).charge_once(
            user_id=7,
            task_id="yzzh:v:provider-task-1",
            task_type="video",
            quote=BillingQuote("doubao-seedance-2-5-video", "tokens", Decimal("0.0420")),
            usage={"total_tokens": 2000},
            description="衣装智换测试",
        )
        self.assertTrue(result.duplicate)
        self.assertFalse(any(call[0].startswith("UPDATE users") for call in connection.cursor_value.calls))

    def test_requires_verifiable_usage(self) -> None:
        with self.assertRaises(MiyoIntegrationError) as raised:
            usage_units({}, "tokens")
        self.assertEqual(raised.exception.code, "GENERATION_RECONCILIATION_REQUIRED")


class FlaskEntryTests(unittest.TestCase):
    def setUp(self) -> None:
        import web_app

        self.web_app = web_app
        self.web_app.app.config.update(TESTING=True)
        self.account = VerifiedAccount(
            user_id=7,
            role_id=9,
            issued_at=NOW_MS,
            username="测试用户",
            role="staff",
            balance=Decimal("12.34"),
            product_id=4,
            product_code="miyo_fashion",
            plan_type="monthly",
            subscription_end="2027-01-01 00:00:00",
            days_remaining=30,
        )

    def test_api_rejects_missing_login(self) -> None:
        fake = mock.Mock(enabled=True, login_url="https://login.example.com")
        with mock.patch.object(self.web_app, "MIYO_SERVICE", fake):
            response = self.web_app.app.test_client().get("/api/account")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["code"], "AUTH_REQUIRED")

    def test_query_token_becomes_http_only_cookie_and_is_removed_from_url(self) -> None:
        fake = mock.Mock(enabled=True, login_url="https://login.example.com")
        fake.verify_token.return_value = self.account
        client = self.web_app.app.test_client()
        with mock.patch.object(self.web_app, "MIYO_SERVICE", fake):
            response = client.get("/?token=signed-value&mode=wardrobe")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/?mode=wardrobe")
        cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("miyo_account_token=signed-value", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_formal_mode_blocks_unmetered_browser_generation_channel(self) -> None:
        fake = mock.Mock(enabled=True, login_url="https://login.example.com")
        fake.verify_token.return_value = self.account
        with mock.patch.object(self.web_app, "MIYO_SERVICE", fake):
            response = self.web_app.app.test_client().get(
                "/api/seedance/browser?token=signed-value"
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "BILLING_CHANNEL_UNAVAILABLE")

    def test_paid_image_never_calls_provider_when_price_preflight_fails(self) -> None:
        service = mock.Mock(enabled=True)
        service.quote_paid_stage.side_effect = MiyoIntegrationError(
            "中央模型价格未配置或未启用，请联系管理员。",
            code="MODEL_PRICING_NOT_CONFIGURED",
            status_code=503,
        )
        provider = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            job = self.web_app.WebJob(
                id="price-gated-job",
                kind="wardrobe_prepare_scene",
                project="wardrobe_swap",
                run_dir=Path(temporary_directory),
                user_id=7,
            )
            with (
                mock.patch.object(self.web_app, "MIYO_SERVICE", service),
                mock.patch.object(self.web_app, "api_client", return_value=provider),
            ):
                with self.assertRaises(MiyoIntegrationError) as raised:
                    self.web_app.generate_paid_image(
                        job,
                        feature="场景提取",
                        prompt="test",
                        image_sources=["test.jpg"],
                        model="doubao-seedream-5-0-260128",
                        size="2K",
                    )
        self.assertEqual(raised.exception.code, "MODEL_PRICING_NOT_CONFIGURED")
        provider.generate_image.assert_not_called()

    def test_provider_result_is_preserved_when_central_charge_needs_reconciliation(self) -> None:
        service = mock.Mock(enabled=True)
        service.quote_paid_stage.return_value = BillingQuote(
            "doubao-seedream-5-0",
            "per_image",
            Decimal("0.2000"),
        )
        service.charge_once.side_effect = MiyoIntegrationError(
            "生成已完成，但中央计费写入失败，结果已转入人工对账。",
            code="GENERATION_RECONCILIATION_REQUIRED",
            status_code=503,
        )
        provider = mock.Mock()
        provider.generate_image.return_value = {
            "url": "https://result.example.com/image.png",
            "usage": {"generated_images": 1},
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            job = self.web_app.WebJob(
                id="reconciliation-job",
                kind="wardrobe_prepare_scene",
                project="wardrobe_swap",
                run_dir=Path(temporary_directory),
                user_id=7,
            )
            with (
                mock.patch.object(self.web_app, "MIYO_SERVICE", service),
                mock.patch.object(self.web_app, "api_client", return_value=provider),
            ):
                result = self.web_app.generate_paid_image(
                    job,
                    feature="场景提取",
                    prompt="test",
                    image_sources=["test.jpg"],
                    model="doubao-seedream-5-0-260128",
                    size="2K",
                )
        self.assertEqual(result["url"], "https://result.example.com/image.png")
        self.assertEqual(job.billing_status, "reconciliation_required")
        self.assertIn("人工对账", job.billing_error)

    def test_job_from_another_account_is_not_readable(self) -> None:
        run_dir = Path(tempfile.mkdtemp())
        foreign = self.web_app.WebJob(
            id="foreign-job",
            kind="generate",
            project="single_person_replication",
            run_dir=run_dir,
            user_id=99,
        )
        with self.web_app.JOBS_LOCK:
            self.web_app.JOBS[foreign.id] = foreign
        fake = mock.Mock(enabled=True, login_url="https://login.example.com")
        fake.verify_token.return_value = self.account
        try:
            with mock.patch.object(self.web_app, "MIYO_SERVICE", fake):
                response = self.web_app.app.test_client().get(
                    "/api/jobs/foreign-job",
                    headers={"Authorization": "Bearer signed-value"},
                )
            self.assertEqual(response.status_code, 404)
        finally:
            with self.web_app.JOBS_LOCK:
                self.web_app.JOBS.pop(foreign.id, None)


if __name__ == "__main__":
    unittest.main()
