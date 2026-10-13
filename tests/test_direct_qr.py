import hashlib
import io
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType
from PIL import Image
from aiogram.types import Chat, Message, User

from bot.handlers import buy_product, choose_product_payment_method
from bot.keyboards import CUSTOM_EMOJI_IDS, payment_method_keyboard
from bot.payments import (
    DirectQr,
    direct_qr_endpoint,
    direct_qr_form,
    download_direct_qr_image,
    khqr_merchant_name,
    parse_direct_qr_response,
    render_payment_card_png,
    render_qr_png,
)


class DirectQrTests(unittest.TestCase):
    def test_direct_qr_request_uses_documented_endpoint_and_signature(self) -> None:
        self.assertEqual(
            direct_qr_endpoint("profile-123"),
            "https://khqr.cc/api/profile-123/payment-gateway/v1/payments/qr-api-khqrcc",
        )
        form = direct_qr_form(
            secret_key="test-secret",
            transaction_id="ORDER_1",
            amount="36.00",
            success_url="https://example.com/success",
            remark="Order 1",
        )
        expected = hashlib.sha1(
            "test-secretORDER_136.00https://example.com/successOrder 1".encode("utf-8")
        ).hexdigest()
        self.assertEqual(form["hash"], expected)
        self.assertEqual(form["amount"], "36.00")

    def test_direct_qr_response_extracts_emv_content(self) -> None:
        result = parse_direct_qr_response(
            {"responseCode": "00", "data": {"qrContent": "000201010212"}}
        )
        self.assertEqual(result.content, "000201010212")

    def test_direct_qr_response_extracts_nested_qr_variants(self) -> None:
        result = parse_direct_qr_response(
            {"responseCode": "00", "data": {"qr": {"qr_code": "000201010212"}}}
        )
        self.assertEqual(result.content, "000201010212")

    def test_direct_qr_response_rejects_failure_or_missing_payload(self) -> None:
        with self.assertRaises(ValueError):
            parse_direct_qr_response({"responseCode": "1", "message": "Invalid hash"})
        with self.assertRaises(ValueError):
            parse_direct_qr_response({"responseCode": "00", "data": {"status": "success"}})

    def test_qr_payload_renders_valid_png(self) -> None:
        png = render_qr_png("000201010212")
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 0)
            self.assertGreater(image.height, 0)

    def test_payment_card_contains_real_khqr_and_order_details(self) -> None:
        payload = "0002010102125910VAN PHANNA"
        self.assertEqual(khqr_merchant_name(payload), "VAN PHANNA")
        png = render_payment_card_png(payload, 4500, "ROKIRA_12_a1b2c3d4")
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (900, 1320))

    def test_remote_qr_image_must_use_trusted_https_host(self) -> None:
        with self.assertRaises(ValueError):
            import asyncio

            asyncio.run(download_direct_qr_image("https://attacker.example/qr.png"))


class DirectQrTelegramFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_buy_sends_khqr_card_and_aba_mobile_button(self) -> None:
        class StoreDouble:
            create_calls = 0

            async def create_order(self, product_id, user_id):
                self.create_calls += 1
                return {
                    "id": 27,
                    "user_id": user_id,
                    "name": "Test account",
                    "game_name": "Free Fire",
                    "game_name_html": "",
                    "level_name": "67",
                    "level_name_html": "",
                    "price_cents": 3600,
                    "expires_at": "2026-10-03T07:03:00+00:00",
                }

            async def set_payment_transaction(self, order_id, transaction_id):
                return True

        message = Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=12345, type=ChatType.PRIVATE),
        )
        send_photo = AsyncMock()
        send_message = AsyncMock()
        object.__setattr__(message, "answer_photo", send_photo)
        object.__setattr__(message, "answer", send_message)
        query = SimpleNamespace(
            message=message,
            data="buy:9",
            from_user=User(id=12345, is_bot=False, first_name="test"),
            answer=AsyncMock(),
        )
        config = {
            "enabled": "true",
            "secret_key": "test-secret",
            "profile_id": "test-profile",
            "gateway_url": "https://khqr.cc/api/payment/requestv2",
            "success_url": "https://t.me/test_bot",
        }
        store = StoreDouble()

        with patch("bot.handlers.request_direct_qr", new=AsyncMock(return_value=DirectQr(content="000201010212"))):
            await buy_product(query, config)

            self.assertEqual(store.create_calls, 0)
            self.assertEqual(send_message.await_args.kwargs["reply_markup"], payment_method_keyboard(9))
            buttons = send_message.await_args.kwargs["reply_markup"].inline_keyboard[0]
            self.assertEqual(buttons[0].icon_custom_emoji_id, CUSTOM_EMOJI_IDS["pay_aba"])
            self.assertEqual(buttons[1].icon_custom_emoji_id, CUSTOM_EMOJI_IDS["wallet_topup"])
            self.assertEqual(buttons[1].callback_data, "pay:9:wallet")

            payment_query = SimpleNamespace(
                message=message,
                data="pay:9:aba",
                from_user=query.from_user,
                answer=AsyncMock(),
            )
            await choose_product_payment_method(payment_query, object(), store, frozenset(), config)

        send_photo.assert_awaited_once()
        self.assertEqual(store.create_calls, 1)
        sent = send_photo.await_args.kwargs
        self.assertIn("PNG", sent["photo"].filename.upper())
        self.assertIn("Order #27", sent["caption"])
        self.assertEqual(
            sent["reply_markup"].inline_keyboard[0][0].url,
            "https://aba.onelink.me/oRF8",
        )
        with Image.open(io.BytesIO(sent["photo"].data)) as image:
            self.assertEqual(image.size, (900, 1320))

    async def test_legacy_khqr_method_keeps_qr_without_aba_launch_button(self) -> None:
        class StoreDouble:
            async def create_order(self, product_id, user_id):
                return {
                    "id": 29,
                    "user_id": user_id,
                    "name": "Test account",
                    "game_name": "Free Fire",
                    "game_name_html": "",
                    "level_name": "67",
                    "level_name_html": "",
                    "price_cents": 3600,
                    "expires_at": "2026-10-03T07:03:00+00:00",
                }

            async def set_payment_transaction(self, order_id, transaction_id):
                return True

        message = Message(
            message_id=3,
            date=datetime.now(),
            chat=Chat(id=12345, type=ChatType.PRIVATE),
        )
        send_photo = AsyncMock()
        object.__setattr__(message, "answer_photo", send_photo)
        query = SimpleNamespace(
            message=message,
            data="pay:9:khqr",
            from_user=User(id=12345, is_bot=False, first_name="test"),
            answer=AsyncMock(),
        )
        config = {
            "enabled": "true",
            "secret_key": "test-secret",
            "profile_id": "test-profile",
            "gateway_url": "https://khqr.cc/api/payment/requestv2",
            "success_url": "https://t.me/test_bot",
        }

        with patch("bot.handlers.request_direct_qr", new=AsyncMock(return_value=DirectQr(content="000201010212"))):
            await choose_product_payment_method(query, object(), StoreDouble(), frozenset(), config)

        sent = send_photo.await_args.kwargs
        self.assertIn("KHQR", sent["caption"])
        self.assertNotIn("url", sent["reply_markup"].inline_keyboard[0][0].model_fields_set)

    async def test_wallet_choice_reports_shortfall_and_offers_topup(self) -> None:
        class StoreDouble:
            async def pay_product_with_wallet(self, product_id, user_id):
                return {"status": "insufficient", "balance_cents": 100, "price_cents": 3600}

        message = Message(
            message_id=4,
            date=datetime.now(),
            chat=Chat(id=12345, type=ChatType.PRIVATE),
        )
        send_message = AsyncMock()
        object.__setattr__(message, "answer", send_message)
        query = SimpleNamespace(
            message=message,
            data="pay:9:wallet",
            from_user=User(id=12345, is_bot=False, first_name="test"),
            answer=AsyncMock(),
        )

        await choose_product_payment_method(query, object(), StoreDouble(), frozenset(), {"enabled": "false"})

        sent_text = send_message.await_args.args[0]
        self.assertIn("សមតុល្យបច្ចុប្បន្ន: <b>$1.00</b>", sent_text)
        self.assertIn("នៅខ្វះ: <b>$35.00</b>", sent_text)
        self.assertEqual(
            send_message.await_args.kwargs["reply_markup"].inline_keyboard[-1][0].callback_data,
            "wallet:custom",
        )

    async def test_wallet_choice_pays_and_delivers_without_khqr_enabled(self) -> None:
        paid_order = {
            "status": "paid",
            "id": 37,
            "user_id": 12345,
            "price_cents": 3600,
            "balance_cents": 400,
        }
        store = SimpleNamespace(pay_product_with_wallet=AsyncMock(return_value=paid_order))
        query = SimpleNamespace(
            message=Message(
                message_id=5,
                date=datetime.now(),
                chat=Chat(id=12345, type=ChatType.PRIVATE),
            ),
            data="pay:9:wallet",
            from_user=User(id=12345, is_bot=False, first_name="test"),
            answer=AsyncMock(),
        )

        with patch("bot.handlers._deliver_paid_account", new=AsyncMock()) as deliver:
            await choose_product_payment_method(query, object(), store, frozenset(), {"enabled": "false"})

        store.pay_product_with_wallet.assert_awaited_once_with(9, 12345)
        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.kwargs["payment_method"], "កាបូបប្រាក់ក្នុង bot")

    async def test_failed_direct_qr_releases_product_without_sending_fake_qr(self) -> None:
        class StoreDouble:
            cancelled = False

            async def create_order(self, product_id, user_id):
                return {"id": 28, "user_id": user_id, "price_cents": 3600}

            async def cancel_order(self, order_id):
                self.cancelled = True

        message = Message(
            message_id=2,
            date=datetime.now(),
            chat=Chat(id=12345, type=ChatType.PRIVATE),
        )
        send_photo = AsyncMock()
        send_message = AsyncMock()
        object.__setattr__(message, "answer_photo", send_photo)
        object.__setattr__(message, "answer", send_message)
        query = SimpleNamespace(
            message=message,
            data="pay:9:aba",
            from_user=User(id=12345, is_bot=False, first_name="test"),
            answer=AsyncMock(),
        )
        store = StoreDouble()
        config = {
            "enabled": "true",
            "secret_key": "test-secret",
            "profile_id": "test-profile",
            "gateway_url": "https://khqr.cc/api/payment/requestv2",
            "success_url": "https://t.me/test_bot",
        }

        with patch("bot.handlers.request_direct_qr", new=AsyncMock(side_effect=ValueError("unavailable"))):
            await choose_product_payment_method(query, object(), store, frozenset(), config)

        self.assertTrue(store.cancelled)
        send_photo.assert_not_awaited()
        send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()