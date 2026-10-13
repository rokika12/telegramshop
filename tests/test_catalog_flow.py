import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.enums import ChatType
from aiogram.types import Chat, Message
from cryptography.fernet import Fernet

from bot.database import Store
from bot.handlers import (
    AdminProductForm,
    KeyCatalogForm,
    choose_key_option,
    finish_key_option_stock,
    receive_key_catalog_name,
    receive_key_stock_text,
    receive_product_key,
    show_key_catalog_item,
    show_levels,
)


class CatalogGameSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "catalog.sqlite3", Fernet.generate_key().decode("ascii"))
        await self.store.initialize()
        await self.store.create_product(
            game="Free Fire",
            level="50",
            name="Single photo account",
            price_cents=3400,
            photo_file_ids=["single-photo"],
            account_email="one@example.com",
            account_password="one-password",
            created_by=1,
        )
        await self.store.create_product(
            game="Free Fire",
            level="67",
            name="Album account",
            price_cents=4200,
            photo_file_ids=["album-photo-1", "album-photo-2"],
            account_email="two@example.com",
            account_password="two-password",
            created_by=1,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_selecting_game_sends_all_levels_and_purchase_actions(self) -> None:
        class BotDouble:
            def __init__(self) -> None:
                self.photos = []
                self.albums = []
                self.messages = []

            async def send_photo(self, **kwargs):
                self.photos.append(kwargs)

            async def send_media_group(self, **kwargs):
                self.albums.append(kwargs)

            async def send_message(self, *args, **kwargs):
                self.messages.append((args, kwargs))

        bot = BotDouble()
        message = Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=123, type=ChatType.PRIVATE),
        )
        query = SimpleNamespace(
            message=message,
            data="game:1",
            bot=bot,
            answer=lambda *args, **kwargs: _completed(),
        )

        await show_levels(query, bot, self.store)

        self.assertEqual(len(bot.photos), 1)
        self.assertIn("Level:", bot.photos[0]["caption"])
        self.assertEqual(bot.photos[0]["reply_markup"].inline_keyboard[0][0].callback_data, "buy:1")
        self.assertEqual(len(bot.albums), 1)
        self.assertEqual(len(bot.albums[0]["media"]), 2)
        self.assertIn("Level:", bot.messages[0][0][1])
        self.assertEqual(bot.messages[0][1]["reply_markup"].inline_keyboard[0][0].callback_data, "buy:2")


class AdminProductKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_input_is_deleted_and_form_continues(self) -> None:
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=9876),
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text="RBX-SECRET-123",
            delete=AsyncMock(),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(update_data=AsyncMock(), set_state=AsyncMock())

        await receive_product_key(message, state, frozenset({9876}))

        state.update_data.assert_awaited_once_with(license_key="RBX-SECRET-123")
        message.delete.assert_awaited_once()
        state.set_state.assert_awaited_once_with(AdminProductForm.delivery)
        message.answer.assert_awaited_once()


class KeyCatalogCustomerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_shows_video_and_option_routes_to_payment_methods(self) -> None:
        class StoreDouble:
            async def key_catalog_item(self, item_id):
                return {
                    "id": item_id,
                    "name": "Roblox Premium",
                    "name_html": "",
                    "description": "Access for one day",
                    "description_html": "",
                    "media": [{"type": "video", "file_id": "promo-video-id"}],
                }

            async def key_catalog_options(self, item_id):
                return [{"id": 14, "label": "1 day", "label_html": "", "price_cents": 300, "stock_count": 2}]

            async def available_key_stock_product(self, option_id):
                return {
                    "id": 39,
                    "name": "Roblox Premium",
                    "label": "1 day",
                    "label_html": "",
                    "price_cents": 300,
                }

        class BotDouble:
            def __init__(self):
                self.send_video = AsyncMock()
                self.send_photo = AsyncMock()

        message = Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=123, type=ChatType.PRIVATE),
        )
        answer = AsyncMock()
        object.__setattr__(message, "answer", answer)
        query = SimpleNamespace(message=message, data="keyitem:7", answer=AsyncMock())
        bot = BotDouble()
        store = StoreDouble()

        await show_key_catalog_item(query, bot, store)

        bot.send_video.assert_awaited_once_with(123, "promo-video-id")
        listing_markup = answer.await_args.kwargs["reply_markup"]
        self.assertEqual(listing_markup.inline_keyboard[0][0].callback_data, "keyoption:14")
        self.assertIn("$3.00", listing_markup.inline_keyboard[0][0].text)

        option_message = Message(
            message_id=2,
            date=datetime.now(),
            chat=Chat(id=123, type=ChatType.PRIVATE),
        )
        option_answer = AsyncMock()
        object.__setattr__(option_message, "answer", option_answer)
        option_query = SimpleNamespace(message=option_message, data="keyoption:14", answer=AsyncMock())
        await choose_key_option(option_query, store)

        payment_markup = option_answer.await_args.kwargs["reply_markup"]
        self.assertEqual(payment_markup.inline_keyboard[0][0].callback_data, "pay:39:aba")
        self.assertEqual(payment_markup.inline_keyboard[0][1].callback_data, "pay:39:wallet")


class KeyCatalogAdminWizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_name_starts_media_step_and_stock_completion_saves_option(self) -> None:
        state_data = {}

        class StateDouble:
            def __init__(self):
                self.current_state = None

            async def update_data(self, **kwargs):
                state_data.update(kwargs)

            async def set_state(self, state):
                self.current_state = state

            async def get_data(self):
                return state_data

        state = StateDouble()
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=9876),
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text="Roblox Premium",
            html_text="Roblox Premium",
            answer=AsyncMock(),
        )
        await receive_key_catalog_name(message, state, frozenset({9876}))
        self.assertEqual(state.current_state, KeyCatalogForm.media)
        self.assertEqual(state_data["key_name"], "Roblox Premium")
        self.assertEqual(state_data["key_media"], [])

        state_data["key_current_option"] = {
            "label": "1 day",
            "label_html": "1 day",
            "price_cents": 300,
            "stock": [],
        }
        key_message = SimpleNamespace(
            from_user=SimpleNamespace(id=9876),
            chat=SimpleNamespace(type=ChatType.PRIVATE),
            text="RBX-KEY-1",
            delete=AsyncMock(),
            answer=AsyncMock(),
        )
        await receive_key_stock_text(key_message, state, frozenset({9876}))
        self.assertEqual(state_data["key_current_option"]["stock"], [{"license_key": "RBX-KEY-1"}])
        key_message.delete.assert_awaited_once()

        await finish_key_option_stock(key_message, state, frozenset({9876}))
        self.assertEqual(state.current_state, KeyCatalogForm.option_decision)
        self.assertEqual(state_data["key_options"][0]["label"], "1 day")
        self.assertEqual(len(state_data["key_options"][0]["stock"]), 1)


async def _completed():
    return None


if __name__ == "__main__":
    unittest.main()