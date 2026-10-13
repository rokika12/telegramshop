import tempfile
import unittest
from datetime import datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User
from cryptography.fernet import Fernet

from bot.database import Store
from bot.handlers import _deliver_paid_account, _parse_wallet_amount, buy_product
from bot.payments import _response_check, create_checkout_url


class PaymentResponseTests(unittest.TestCase):
    def test_custom_wallet_amount_accepts_usd_values_with_two_decimals(self) -> None:
        self.assertEqual(_parse_wallet_amount("$7.50"), 750)
        self.assertEqual(_parse_wallet_amount("7.5 USD"), 750)

    def test_custom_wallet_amount_rejects_invalid_precision_and_range(self) -> None:
        for value in ("abc", "0.99", "1000.01", "2.345", "NaN"):
            with self.subTest(value=value):
                self.assertIsNone(_parse_wallet_amount(value))

    def test_pending_response_is_not_success(self) -> None:
        result = _response_check({"responseCode": 0, "data": {"status": "pending", "amount": "34.00"}})
        self.assertEqual(result.status, "pending")

    def test_success_requires_integer_zero_response_code(self) -> None:
        result = _response_check({"responseCode": False, "data": {"status": "success", "amount": "34.00"}})
        self.assertEqual(result.status, "error")

    def test_success_requires_whole_cent_amount(self) -> None:
        result = _response_check({"responseCode": 0, "data": {"status": "success", "amount": "34.001"}})
        self.assertEqual(result.status, "error")

    def test_verified_success_returns_amount_in_cents(self) -> None:
        result = _response_check({"responseCode": 0, "data": {"status": "success", "amount": "34.00"}})
        self.assertEqual((result.status, result.amount_cents), ("success", 3400))

    def test_checkout_url_matches_khqrcc_managed_checkout_docs(self) -> None:
        url = create_checkout_url(
            profile_id="profile-123",
            secret_key="test-secret",
            transaction_id="ORDER_10001",
            amount_cents=3600,
            success_url="https://example.com/payment/success",
            remark="Order 10001",
        )
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/api/payment/requestv2/profile-123")
        self.assertEqual(params["transaction_id"], ["ORDER_10001"])
        self.assertEqual(params["amount"], ["36"])
        expected_hash = hashlib.sha1(
            "test-secretORDER_1000136https://example.com/payment/successOrder 10001".encode("utf-8")
        ).hexdigest()
        self.assertEqual(params["hash"], [expected_hash])

    def test_decimal_checkout_amount_uses_php_float_text(self) -> None:
        url = create_checkout_url(
            profile_id="profile-123",
            secret_key="test-secret",
            transaction_id="ORDER_10002",
            amount_cents=1250,
            success_url="https://example.com/payment/success",
            remark="Order 10002",
        )
        self.assertEqual(parse_qs(urlsplit(url).query)["amount"], ["12.5"])


class PaidOrderGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "orders.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.register_user(12345, "test", "test_user")
        self.product_id = await self.store.create_product(
            game="Free Fire",
            level="67",
            name="Test account",
            price_cents=3400,
            photo_file_ids=["photo-id"],
            account_email="test@example.com",
            account_password="test-password",
            license_key="RBX-SECRET-123",
            created_by=1,
        )
        self.order = await self.store.create_order(self.product_id, 12345)
        assert self.order is not None

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_amount_mismatch_does_not_complete_or_release_account(self) -> None:
        result = await self.store.complete_paid_order(self.order["id"], verified_amount_cents=3399)
        self.assertIsNone(result)
        pending = await self.store.pending_orders()
        self.assertEqual(len(pending), 1)

    async def test_expired_order_cannot_complete(self) -> None:
        import aiosqlite

        async with aiosqlite.connect(self.store.path) as db:
            await db.execute("UPDATE orders SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (self.order["id"],))
            await db.commit()
        result = await self.store.complete_paid_order(self.order["id"], verified_amount_cents=3400)
        self.assertIsNone(result)

    async def test_key_is_encrypted_and_delivered_only_after_exact_payment(self) -> None:
        import aiosqlite

        self.assertNotIn("license_key", self.order)
        async with aiosqlite.connect(self.store.path) as db:
            cursor = await db.execute(
                "SELECT license_key_encrypted FROM products WHERE id = ?",
                (self.product_id,),
            )
            encrypted_key = (await cursor.fetchone())[0]
        self.assertNotIn("RBX-SECRET-123", encrypted_key)

        self.assertIsNone(await self.store.complete_paid_order(self.order["id"], 3399))
        delivered = await self.store.complete_paid_order(self.order["id"], 3400)

        self.assertIsNotNone(delivered)
        self.assertEqual(delivered["license_key"], "RBX-SECRET-123")


class WalletPurchaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "wallet-purchase.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.register_user(12345, "customer", "customer_name")
        self.product_id = await self.store.create_product(
            game="Software",
            level="License",
            name="Canva Pro",
            price_cents=3400,
            photo_file_ids=["product-photo"],
            account_email="",
            account_password="",
            license_key="WALLET-KEY-456",
            created_by=1,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _fund_wallet(self, amount_cents: int) -> None:
        topup = await self.store.create_wallet_topup(12345, amount_cents, amount_cents)
        self.assertIsNotNone(await self.store.complete_wallet_topup(topup["id"], amount_cents))

    async def test_insufficient_wallet_does_not_deduct_or_reserve_product(self) -> None:
        await self._fund_wallet(2000)

        result = await self.store.pay_product_with_wallet(self.product_id, 12345)

        self.assertEqual(result, {"status": "insufficient", "balance_cents": 2000, "price_cents": 3400})
        self.assertEqual(await self.store.wallet_balance_cents(12345), 2000)
        self.assertEqual((await self.store.available_product(self.product_id))["status"], "available")
        self.assertEqual(await self.store.pending_orders(), [])

    async def test_sufficient_wallet_deducts_exactly_and_marks_order_paid(self) -> None:
        await self._fund_wallet(5000)

        result = await self.store.pay_product_with_wallet(self.product_id, 12345)

        self.assertEqual(result["status"], "paid")
        self.assertEqual(result["license_key"], "WALLET-KEY-456")
        self.assertEqual(result["balance_cents"], 1600)
        self.assertEqual(await self.store.wallet_balance_cents(12345), 1600)
        self.assertEqual((await self.store.available_product(self.product_id))["status"], "sold")
        history = await self.store.order_history(12345)
        self.assertEqual([(order["status"], order["price_cents"]) for order in history], [("paid", 3400)])

    async def test_concurrent_wallet_purchase_cannot_charge_twice(self) -> None:
        await self._fund_wallet(5000)

        first, second = await asyncio.gather(
            self.store.pay_product_with_wallet(self.product_id, 12345),
            self.store.pay_product_with_wallet(self.product_id, 12345),
        )

        self.assertEqual([first["status"], second["status"]].count("paid"), 1)
        self.assertEqual([first["status"], second["status"]].count("unavailable"), 1)
        self.assertEqual(await self.store.wallet_balance_cents(12345), 1600)


class PaidDigitalProductTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "digital.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.register_user(12345, "customer", "customer_name")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_verified_product_can_have_delivery_without_credentials(self) -> None:
        product_id = await self.store.create_product(
            game="Software",
            level="License",
            name="Canva Pro",
            price_cents=500,
            photo_file_ids=["cover-photo"],
            account_email="",
            account_password="",
            created_by=1,
            delivery_url="https://example.com/download?key=value",
        )
        order = await self.store.create_order(product_id, 12345)
        self.assertIsNotNone(order)

        delivered = await self.store.complete_paid_order(order["id"], verified_amount_cents=500)

        self.assertIsNotNone(delivered)
        self.assertEqual(delivered["account_email"], "")
        self.assertEqual(delivered["account_password"], "")
        self.assertEqual(delivered["delivery_url"], "https://example.com/download?key=value")


class KeyCatalogInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "key-catalog.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.register_user(12345, "customer", "customer_name")

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_option_stock_counts_and_exact_payment_delivery(self) -> None:
        item_id = await self.store.create_key_catalog_item(
            name="Roblox Access",
            name_html="Roblox Access",
            description="One day access",
            description_html="One day access",
            media=[{"type": "photo", "file_id": "promo-photo"}],
            options=[
                {
                    "label": "1 day",
                    "label_html": "1 day",
                    "price_cents": 500,
                    "stock": [
                        {"license_key": "KEY-SECRET-1"},
                        {"license_key": "KEY-SECRET-2"},
                    ],
                },
                {
                    "label": "1 month",
                    "label_html": "1 month",
                    "price_cents": 1200,
                    "stock": [{"delivery_file_id": "monthly-file-id"}],
                },
            ],
            created_by=1,
        )

        item = (await self.store.key_catalog_items())[0]
        option = (await self.store.key_catalog_options(item_id))[0]
        self.assertEqual(item["name"], "Roblox Access")
        self.assertEqual(item["media"], [{"type": "photo", "file_id": "promo-photo"}])
        self.assertEqual(option["stock_count"], 2)

        stock_unit = await self.store.available_key_stock_product(option["id"])
        order = await self.store.create_order(stock_unit["id"], 12345)
        self.assertIsNotNone(order)
        pending = (await self.store.pending_orders())[0]
        self.assertEqual(pending["game_name"], "Roblox Access")
        self.assertEqual(pending["level_name"], "1 day")
        self.assertIsNone(await self.store.complete_paid_order(order["id"], 499))
        self.assertEqual((await self.store.key_catalog_options(item_id))[0]["stock_count"], 1)

        paid_order = await self.store.complete_paid_order(order["id"], 500)
        self.assertIn(paid_order["license_key"], {"KEY-SECRET-1", "KEY-SECRET-2"})
        self.assertEqual((await self.store.key_catalog_options(item_id))[0]["stock_count"], 1)

        await self._fund_key_wallet(1200)
        file_option = (await self.store.key_catalog_options(item_id))[1]
        file_stock = await self.store.available_key_stock_product(file_option["id"])
        wallet_order = await self.store.pay_product_with_wallet(file_stock["id"], 12345)
        self.assertEqual(wallet_order["status"], "paid")
        self.assertEqual(wallet_order["level_name"], "1 month")
        self.assertEqual(wallet_order["delivery_file_id"], "monthly-file-id")
        self.assertEqual(wallet_order["price_cents"], 1200)
        self.assertEqual((await self.store.key_catalog_options(item_id))[1]["stock_count"], 0)

    async def _fund_key_wallet(self, amount_cents: int) -> None:
        topup = await self.store.create_wallet_topup(12345, amount_cents, amount_cents)
        self.assertIsNotNone(await self.store.complete_wallet_topup(topup["id"], amount_cents))


class PaidDeliveryMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_file_is_sent_after_paid_order_message(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        order = {
            "id": 71,
            "user_id": 12345,
            "name": "Canva Pro",
            "game_name": "Software",
            "level_name": "License",
            "price_cents": 500,
            "account_email": "",
            "account_password": "",
            "license_key": "<RBX-KEY-789>",
            "delivery_file_id": "telegram-document-id",
            "delivery_url": "",
        }

        await _deliver_paid_account(bot, order, frozenset())

        sent_message = bot.send_message.await_args.args[1]
        self.assertIn("ការបង់ប្រាក់ជោគជ័យ", sent_message)
        self.assertNotIn("Gmail:", sent_message)
        self.assertNotIn("Password:", sent_message)
        self.assertIn("&lt;RBX-KEY-789&gt;", sent_message)
        bot.send_document.assert_awaited_once()
        self.assertEqual(bot.send_document.await_args.args[:2], (12345, "telegram-document-id"))

    async def test_download_link_is_clickable_in_verified_order_message(self) -> None:
        bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
        order = {
            "id": 72,
            "user_id": 12345,
            "name": "Canva Pro",
            "game_name": "Software",
            "level_name": "License",
            "price_cents": 500,
            "account_email": "",
            "account_password": "",
            "delivery_file_id": "",
            "delivery_url": "https://example.com/download?a=1&b=2",
        }

        await _deliver_paid_account(bot, order, frozenset())

        sent_message = bot.send_message.await_args.args[1]
        self.assertIn('<a href="https://example.com/download?a=1&amp;b=2">ចុចទីនេះ</a>', sent_message)
        bot.send_document.assert_not_awaited()


class WalletTopupGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "wallet.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.register_user(12345, "test", "test_user")
        self.topup = await self.store.create_wallet_topup(12345, 500, 525)
        self.assertTrue(await self.store.set_wallet_topup_transaction(self.topup["id"], "TOPUP_TEST_1"))

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_exact_verified_amount_credits_once(self) -> None:
        credited = await self.store.complete_wallet_topup(self.topup["id"], verified_amount_cents=500)

        self.assertIsNotNone(credited)
        self.assertEqual(credited["balance_cents"], 525)
        self.assertEqual(credited["first_name"], "test")
        self.assertEqual(credited["username"], "test_user")
        self.assertIsNone(await self.store.complete_wallet_topup(self.topup["id"], verified_amount_cents=500))
        self.assertEqual(await self.store.wallet_balance_cents(12345), 525)

    async def test_amount_mismatch_does_not_credit_wallet(self) -> None:
        result = await self.store.complete_wallet_topup(self.topup["id"], verified_amount_cents=499)

        self.assertIsNone(result)
        self.assertEqual(await self.store.wallet_balance_cents(12345), 0)
        self.assertEqual(len(await self.store.pending_wallet_topups()), 1)

    async def test_expired_topup_cannot_credit_wallet(self) -> None:
        import aiosqlite

        async with aiosqlite.connect(self.store.path) as db:
            await db.execute(
                "UPDATE wallet_topups SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (self.topup["id"],),
            )
            await db.commit()

        result = await self.store.complete_wallet_topup(self.topup["id"], verified_amount_cents=500)
        self.assertIsNone(result)
        self.assertEqual(await self.store.wallet_balance_cents(12345), 0)


class AdminCredentialEditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "accounts.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.register_user(12345, "test", None)
        self.product_id = await self.store.create_product(
            game="Free Fire",
            level="67",
            name="Test account",
            price_cents=3400,
            photo_file_ids=["photo-id"],
            account_email="old@example.com",
            account_password="old-password",
            created_by=1,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_credential_replacements_are_encrypted(self) -> None:
        self.assertTrue(await self.store.update_product_email(self.product_id, "new@example.com"))
        self.assertTrue(await self.store.update_product_password(self.product_id, "new-password"))
        import aiosqlite

        async with aiosqlite.connect(self.store.path) as db:
            row = await (await db.execute(
                "SELECT account_email_encrypted, account_password_encrypted FROM products WHERE id = ?",
                (self.product_id,),
            )).fetchone()
        self.assertEqual(self.store._cipher.decrypt(row[0].encode("ascii")).decode("utf-8"), "new@example.com")
        self.assertEqual(self.store._cipher.decrypt(row[1].encode("ascii")).decode("utf-8"), "new-password")

    async def test_email_preview_is_masked_for_admin_identification(self) -> None:
        self.assertEqual(await self.store.product_email_preview(self.product_id), "ol***@example.com")

    async def test_admin_can_retrieve_credentials_for_available_product(self) -> None:
        self.assertEqual(
            await self.store.admin_product_credentials(self.product_id),
            {"email": "old@example.com", "password": "old-password"},
        )

    async def test_reserved_account_rejects_credential_changes(self) -> None:
        order = await self.store.create_order(self.product_id, 12345)
        self.assertIsNotNone(order)
        self.assertIsNone(await self.store.admin_product_credentials(self.product_id))
        self.assertFalse(await self.store.update_product_email(self.product_id, "new@example.com"))
        self.assertFalse(await self.store.update_product_password(self.product_id, "new-password"))


class DisabledCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_checkout_does_not_reserve_or_create_order(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "disabled.sqlite3", Fernet.generate_key().decode("ascii"))
            await store.initialize()
            product_id = await store.create_product(
                game="Free Fire",
                level="67",
                name="Test account",
                price_cents=3400,
                photo_file_ids=["photo-id"],
                account_email="test@example.com",
                account_password="test-password",
                created_by=1,
            )
            answers = []

            async def answer(text: str, **kwargs) -> None:
                answers.append(text)

            query = SimpleNamespace(
                message=Message(
                    message_id=1,
                    date=datetime.now(),
                    chat=Chat(id=12345, type=ChatType.PRIVATE),
                ),
                data=f"buy:{product_id}",
                from_user=User(id=12345, is_bot=False, first_name="test"),
                answer=answer,
            )
            await buy_product(query)
            self.assertTrue(answers)
            self.assertEqual(await store.pending_orders(), [])
            product = await store.available_product(product_id)
            self.assertEqual(product["status"], "available")


if __name__ == "__main__":
    unittest.main()